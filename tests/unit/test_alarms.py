"""Alarms (tools/alarms.py, scheduler/alarm.py) with every device faked: no afplay, no osascript, no
caffeinate, no EventKit, no Telegram. Covers the time parsing and repeats, the ringing loop and its
time limit, stop / snooze from the dialog, the command file (hotkey, voice, Telegram, tool), the volume
restore, that the ring is our own sound (Do Not Disturb cannot mute it), the keep-awake window, the lid /
battery warning, missed alarms, and the phone backup's creation and cleanup."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.observability.health import INTEGRATION_MODULES
from jarvis.scheduler import alarm as A
from jarvis.scheduler import engine, launchd, service
from jarvis.scheduler.alarm import (
    AlarmHandler,
    AlarmState,
    KeepAwake,
    PhoneBackup,
    Ringer,
    classify_alarm_word,
    request_snooze,
    request_stop,
    ringing,
)
from jarvis.scheduler.engine import fire_due
from jarvis.scheduler.launchd import SchedulerStatus
from jarvis.scheduler.store import KIND_ALARM, KIND_TIMER, TimerStore
from jarvis.tools import alarms as T
from jarvis.tools import power
from jarvis.tools.alarms import (
    BATTERY_WARNING,
    LID_WARNING,
    CancelAlarm,
    ListAlarms,
    SetAlarm,
    SnoozeAlarm,
    StopAlarm,
    cli_cancel,
    cli_list,
    health_checks,
)
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.utils.eventkit import CalendarRecord, ReminderRecord
from jarvis.utils.timeutil import (
    EVERY_DAY,
    WEEKDAYS,
    WEEKENDS,
    describe_repeat,
    local_tz,
    next_alarm_occurrence,
    parse_alarm_time,
    parse_repeat,
)
from tests.conftest import RecordingSink
from tests.unit.test_power import BATT_AC, BATT_BATTERY, FakeCommands, outputs
from tests.unit.test_scheduler import RecordingNotifier
from tests.unit.test_voice_cues import FakeProc

TZ = local_tz()
NOW = datetime(2026, 10, 18, 15, 30, tzinfo=TZ)  # a Sunday
CLAMSHELL = ("ioreg", "-r", "-k", "AppleClamshellState", "-d", "4")
LID_OPEN = '    "AppleClamshellState" = No\n'
LID_SHUT = '    "AppleClamshellState" = Yes\n'


# --- fixtures and fakes ----------------------------------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(T, "_now", lambda: NOW)
    return NOW


@pytest.fixture
def scheduler_installed(monkeypatch):
    monkeypatch.setattr(launchd, "status", lambda: SchedulerStatus(installed=True, running=True, pid=1, detail="running"))


@pytest.fixture
def mac(monkeypatch) -> FakeCommands:
    """pmset / ioreg answers for the lid and battery warning: AC power, lid open by default."""
    fake = FakeCommands({**outputs(), CLAMSHELL: LID_OPEN})
    monkeypatch.setattr(power, "run_argv", fake)
    return fake


class FakeEventKit:
    """The EventKitStore surface the phone backup uses: lists, reminders, authorization."""

    def __init__(self, status: str = "authorized") -> None:
        self.status = status
        self.lists: list[CalendarRecord] = [CalendarRecord("Reminders", "list-default", True, False, "caldav")]
        self.reminders: dict[str, ReminderRecord] = {}
        self.calls: list[tuple] = []
        self.n = 0

    def authorization_status(self, entity: str) -> str:
        return self.status

    def ensure_access(self, entity: str) -> None:
        self.calls.append(("ensure_access", entity))
        if self.status != "authorized":
            raise ToolExecutionError("no access")

    def calendars(self, entity: str) -> list[CalendarRecord]:
        return list(self.lists)

    def ensure_reminder_list(self, title: str) -> CalendarRecord:
        self.calls.append(("ensure_reminder_list", title))
        for cal in self.lists:
            if cal.title == title:
                return cal
        cal = CalendarRecord(title, f"list-{len(self.lists)}", True, False, "caldav")
        self.lists.append(cal)
        return cal

    def create_reminder(self, title, *, due=None, due_all_day=False, calendar_id=None, notes=None) -> ReminderRecord:
        self.n += 1
        ident = f"rem-{self.n}"
        lst = next(c for c in self.lists if c.identifier == calendar_id)
        rec = ReminderRecord(ident, title, lst.title, calendar_id, due=due, due_all_day=due_all_day, notes=notes or "")
        self.reminders[ident] = rec
        self.calls.append(("create_reminder", title, due, calendar_id))
        return rec

    def delete_reminder(self, identifier: str) -> bool:
        self.calls.append(("delete_reminder", identifier))
        return self.reminders.pop(identifier, None) is not None

    def complete_reminder(self, identifier: str) -> ReminderRecord:
        self.calls.append(("complete_reminder", identifier))
        return self.reminders[identifier]


@pytest.fixture
def eventkit() -> FakeEventKit:
    return FakeEventKit()


@pytest.fixture
def backup(config, eventkit) -> PhoneBackup:
    return PhoneBackup(config, store_factory=lambda: eventkit)


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


def make_ctx(config, paths, db, sink, channel="cli") -> ToolContext:
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None, sink=sink, channel=channel)
    return ctx


def run(tool_cls, ctx, backup=None, **args):
    tool = tool_cls(ctx, backup=backup)
    return tool.run(tool.parse_args(args))


def audit_lines(logs: Path) -> list[dict]:
    path = logs / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


class Procs:
    """Fake Popen for afplay, the dialog and caffeinate. afplay runs finish at once (so the ring loops);
    the dialog waits until the test answers it; caffeinate runs until killed."""

    def __init__(self) -> None:
        self.procs: list[FakeProc] = []

    def popen(self, argv, **kw):
        proc = FakeProc(list(argv))
        name = Path(argv[0]).name
        if name == "afplay":
            proc.finish()
        if name == "osascript":
            proc.out = None
            proc.communicate = lambda timeout=None: (proc.out, "")  # type: ignore[attr-defined]
        self.procs.append(proc)
        return proc

    def of(self, name: str) -> list[FakeProc]:
        return [p for p in self.procs if Path(p.argv[0]).name == name]

    @property
    def dialog(self) -> FakeProc | None:
        dialogs = self.of("osascript")
        return dialogs[-1] if dialogs else None

    def answer_dialog(self, text: str) -> None:
        assert self.dialog is not None
        self.dialog.out = text
        self.dialog.finish()


class FakeVolume:
    """`run_applescript` replacement for the volume scripts: a 25 %, unmuted Mac."""

    def __init__(self, fail_read: bool = False) -> None:
        self.volume, self.muted = 25, False
        self.sets: list[tuple[int, bool]] = []
        self.fail_read = fail_read

    def __call__(self, script, args=(), *, timeout=15.0):
        if "get volume settings" in script:
            if self.fail_read:
                raise ToolExecutionError("no audio device")
            return f"{self.volume},{'true' if self.muted else 'false'}"
        self.volume, self.muted = int(args[0]), args[1] == "true"
        self.sets.append((self.volume, self.muted))
        return "ok"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def make_ringer(paths, config, *, procs=None, volume=None, clock=None, db=None, backup=None, notifier=None, afplay=True):
    procs = procs or Procs()
    volume = volume or FakeVolume()
    clock = clock or Clock()
    which = lambda name: None if (name == "afplay" and not afplay) else f"/usr/bin/{name}"  # noqa: E731
    ringer = Ringer(
        paths,
        config,
        popen=procs.popen,
        which=which,
        clock=clock,
        sleep=clock.sleep,
        run_applescript=volume,
        fallback_sound=str(paths.sounds_dir / "fallback.aiff"),
        db_factory=(lambda: db) if db is not None else None,
        phone_backup=backup,
        notifier=notifier,
    )
    return ringer, procs, volume, clock


def alarm_row(store: TimerStore, label="Kalk", at=None, state=None):
    state = state or AlarmState(hour=7, minute=0)
    row_id = store.add(label, at or NOW, KIND_ALARM, state)
    return store.get(row_id)


class NoCloseDb:
    """The ringer closes the db it opens; the test's shared db must survive that."""

    def __init__(self, db) -> None:
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self) -> None:
        return None


# --- time parsing and repeats ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("7:30", (7, 30, None, [])),
        ("07.30", (7, 30, None, [])),
        ("saat 7", (7, 0, None, [])),
        ("7'de", (7, 0, None, [])),
        ("yarın sabah 7'de", (7, 0, date(2026, 10, 19), [])),
        ("yarin sabah 7 de", (7, 0, date(2026, 10, 19), [])),
        ("Yarın 7:30", (7, 30, date(2026, 10, 19), [])),
        ("bugün 16:00", (16, 0, date(2026, 10, 18), [])),
        ("öbür gün 8", (8, 0, date(2026, 10, 20), [])),
        ("akşam 9", (21, 0, None, [])),
        ("gece 11", (23, 0, None, [])),
        ("gece 1", (1, 0, None, [])),
        ("öğlen 12", (12, 0, None, [])),
        ("sabah 12", (0, 0, None, [])),
        ("hafta içi her gün 7:30", (7, 30, None, WEEKDAYS)),
        ("her gün 8", (8, 0, None, EVERY_DAY)),
        ("her sabah 6:45", (6, 45, None, EVERY_DAY)),
        ("hafta sonu 10:00", (10, 0, None, WEEKENDS)),
        ("cumartesi 10:00", (10, 0, date(2026, 10, 24), [])),  # one shot: next Saturday
        ("her cumartesi 10:00", (10, 0, None, [5])),
        ("pazartesi ve çarşamba 6:45", (6, 45, None, [0, 2])),
        ("carsamba 9", (9, 0, date(2026, 10, 21), [])),
        ("tomorrow 6am", (6, 0, date(2026, 10, 19), [])),
        ("weekdays 7:15", (7, 15, None, WEEKDAYS)),
        ("2026-10-20 06:00", (6, 0, date(2026, 10, 20), [])),
    ],
)
def test_parse_alarm_time(text, expected):
    assert parse_alarm_time(text, now=NOW) == expected


def test_parse_alarm_time_rejects_no_time_and_bad_clock():
    with pytest.raises(ToolValidationError):
        parse_alarm_time("yarın sabah", now=NOW)
    with pytest.raises(ToolValidationError):
        parse_alarm_time("25:70", now=NOW)


def test_parse_repeat_words_and_names():
    assert parse_repeat("daily") == EVERY_DAY and parse_repeat("her gün") == EVERY_DAY
    assert parse_repeat("hafta içi") == WEEKDAYS and parse_repeat("weekends") == WEEKENDS
    assert parse_repeat("mon,wed") == [0, 2] and parse_repeat("pazartesi ve çarşamba") == [0, 2]
    assert parse_repeat("cumartesileri") == [5] and parse_repeat("") == [] and parse_repeat("none") == []
    with pytest.raises(ToolValidationError):
        parse_repeat("sometimes")


def test_next_occurrence_today_tomorrow_repeat_and_explicit_day():
    assert next_alarm_occurrence(16, 0, [], after=NOW) == NOW.replace(hour=16, minute=0, second=0, microsecond=0)
    assert next_alarm_occurrence(7, 0, [], after=NOW) == datetime(2026, 10, 19, 7, 0, tzinfo=TZ)
    assert next_alarm_occurrence(7, 30, WEEKDAYS, after=NOW) == datetime(2026, 10, 19, 7, 30, tzinfo=TZ)  # Monday
    assert next_alarm_occurrence(10, 0, WEEKENDS, after=NOW) == datetime(2026, 10, 24, 10, 0, tzinfo=TZ)  # Saturday (Sunday 10:00 passed)
    assert next_alarm_occurrence(7, 0, [], after=NOW, day=date(2026, 10, 20)) == datetime(2026, 10, 20, 7, 0, tzinfo=TZ)
    with pytest.raises(ToolValidationError):
        next_alarm_occurrence(7, 0, [], after=NOW, day=date(2026, 10, 18))  # today 07:00 is past


def test_describe_repeat():
    assert describe_repeat([]) == "" and describe_repeat(EVERY_DAY, "tr") == "her gün"
    assert describe_repeat(WEEKDAYS, "tr") == "hafta içi" and describe_repeat(WEEKENDS, "en") == "weekends"
    assert describe_repeat([0, 2], "tr") == "her Pzt, Çar" and describe_repeat([0, 2], "en") == "every Mon, Wed"
    assert describe_repeat(EVERY_DAY) == "daily"  # the main language (English outside the `config` fixture)


def test_parse_alarm_time_and_repeat_in_english():
    assert parse_alarm_time("tomorrow morning 7", now=NOW) == (7, 0, date(2026, 10, 19), [])
    assert parse_alarm_time("every saturday 10:00", now=NOW) == (10, 0, None, [5])
    assert parse_alarm_time("FRIDAY 8", now=NOW) == (8, 0, date(2026, 10, 23), [])  # no dotless i from "FRIDAY"
    assert parse_alarm_time("Weekdays 7:30", now=NOW) == (7, 30, None, WEEKDAYS)
    assert parse_repeat("Monday and Wednesday") == [0, 2] and parse_repeat("CUMARTESİ") == [5]
    from jarvis.utils.timeutil import parse_date_range

    assert parse_date_range("YARIN", now=NOW) == parse_date_range("tomorrow", now=NOW) == parse_date_range("Yarın", now=NOW)


def test_alarm_state_roundtrip_and_corrupt():
    state = AlarmState(hour=7, minute=30, repeat=WEEKDAYS, reminder_id="r1", snoozed_from=3, snooze_count=1)
    again = AlarmState.from_json(state.model_dump_json())
    assert again == state and again.clock() == "07:30" and again.wants_reminder and again.wants_telegram
    assert AlarmState.from_json("{not json") is None and AlarmState.from_json(None) is None
    nxt = state.for_next_occurrence()
    assert nxt.reminder_id is None and nxt.snoozed_from is None and nxt.repeat == WEEKDAYS
    snooze = state.for_snooze(9)
    assert snooze.snoozed_from == 9 and snooze.snooze_count == 2 and snooze.repeat == []


def test_store_accepts_alarm_rows_after_migration(db):
    store = TimerStore(db)
    row_id = store.add("Kalk", NOW + timedelta(hours=1), KIND_ALARM, AlarmState(hour=16, minute=30))
    assert store.get(row_id).kind == KIND_ALARM
    store.add("plain", NOW + timedelta(hours=2), KIND_TIMER)
    assert [r.id for r in store.pending(KIND_ALARM)] == [row_id]
    assert db.schema_version() >= 9


@pytest.mark.parametrize(
    "text, action",
    [
        ("tamam", "stop"),
        ("Kapat!", "stop"),
        ("durdur", "stop"),
        ("tamam kapat", "stop"),
        ("kalktım", "stop"),
        ("stop", "stop"),
        ("ertele", "snooze"),
        ("biraz daha uyuyayım, ertele", "snooze"),
        ("snooze", "snooze"),
        ("on dakika ertele", "snooze"),
        ("yarın ne var", None),
        ("", None),
        ("tamam yarın görüşürüz", None),
        ("DISMISS", "stop"),
        ("OK, I'm up", None),
        ("I'm up!", "stop"),
        ("Turn it off.", "stop"),
        ("snooze it", "snooze"),
        ("Later", "snooze"),
        ("what's on today", None),
        ("ALARMI KAPAT", "stop"),
    ],
)
def test_classify_alarm_word(text, action):
    assert classify_alarm_word(text) == action


# --- set_alarm ---------------------------------------------------------------------------------------------------


def test_tool_metadata():
    assert SetAlarm.name == "set_alarm" and SetAlarm.risk is RiskLevel.SAFE and SetAlarm.delegate_ok is False
    assert CancelAlarm.risk is RiskLevel.SAFE and CancelAlarm.delegate_ok is False
    assert ListAlarms.delegate_ok is True and SnoozeAlarm.risk is RiskLevel.SAFE and StopAlarm.risk is RiskLevel.SAFE
    assert "jarvis.tools.alarms" in INTEGRATION_MODULES


def test_set_alarm_writes_the_row_and_the_phone_backup(frozen, scheduler_installed, mac, config, paths, db, sink, backup, eventkit, logs):
    ctx = make_ctx(config, paths, db, sink)
    result = run(SetAlarm, ctx, backup=backup, time="yarın sabah 7'de", label="Kalk")
    row = TimerStore(db).pending(KIND_ALARM)[0]
    assert row.fire_at == datetime(2026, 10, 19, 7, 0, tzinfo=TZ) and row.label == "Kalk"
    state = AlarmState.from_json(row.pomodoro_state_json)
    assert state.hour == 7 and state.repeat == [] and state.reminder_id == "rem-1" and state.phone_backup == "both"
    assert ("ensure_reminder_list", "Jarvis Alarm") in eventkit.calls
    created = next(c for c in eventkit.calls if c[0] == "create_reminder")
    assert created[1] == "⏰ Kalk" and created[2] == row.fire_at
    text = result.content  # for the model: English, whatever the main language
    assert text.startswith("Alarm #1 set: tomorrow 07:00 (in 15 h 30 min), 'Kalk'")
    assert "rings loudly on the Mac" in text and "'Durdur' and 'Ertele (9 dk)'" in text  # the buttons as the user sees them
    assert "an Apple Reminder in the list 'Jarvis Alarm' at 07:00" in text
    assert "it does not ring like an alarm" in text  # honest about the reminder
    assert "a Telegram message (the bot is not set up, so it will not be sent)" in text  # telegram not configured in the test config
    assert "A night or early-morning alarm" in text  # wake-up alarm: the model must mention the phone backup
    assert LID_WARNING not in text and "Warning" not in text
    assert any(a.get("tool") == "set_alarm" and a.get("decision") == "set" for a in audit_lines(logs))


def test_set_alarm_repeat_from_words_and_from_the_argument(frozen, scheduler_installed, mac, config, paths, db, sink, backup):
    ctx = make_ctx(config, paths, db, sink)
    result = run(SetAlarm, ctx, backup=backup, time="hafta içi her gün 7:30")
    row = TimerStore(db).pending(KIND_ALARM)[0]
    assert AlarmState.from_json(row.pomodoro_state_json).repeat == WEEKDAYS and row.label == "Alarm"
    assert "repeats weekdays" in result.content and row.fire_at.weekday() == 0
    result = run(SetAlarm, ctx, backup=backup, time="10:00", label="Spor", repeat="sat,sun")
    row = TimerStore(db).pending(KIND_ALARM)[1]
    assert AlarmState.from_json(row.pomodoro_state_json).repeat == WEEKENDS and row.fire_at == datetime(2026, 10, 24, 10, 0, tzinfo=TZ)


def test_set_alarm_warns_about_the_lid_and_the_battery(frozen, scheduler_installed, mac, config, paths, db, sink, backup):
    ctx = make_ctx(config, paths, db, sink)
    mac.outputs[CLAMSHELL] = LID_SHUT
    mac.outputs[("pmset", "-g", "batt")] = BATT_BATTERY.replace("64%", "12%")
    text = run(SetAlarm, ctx, backup=backup, time="7:00").content
    assert LID_WARNING in text and BATTERY_WARNING.format(percent=12) in text
    mac.outputs[CLAMSHELL] = LID_OPEN
    mac.outputs[("pmset", "-g", "batt")] = BATT_BATTERY  # 64 %: no warning
    text = run(SetAlarm, ctx, backup=backup, time="7:05").content
    assert "Warning" not in text
    mac.outputs[("pmset", "-g", "batt")] = BATT_AC.replace("27%", "5%")  # on AC: the percentage does not matter
    assert "Warning" not in run(SetAlarm, ctx, backup=backup, time="7:10").content


def test_set_alarm_survives_a_failing_phone_backup_and_says_so(frozen, scheduler_installed, mac, config, paths, db, sink):
    broken = FakeEventKit(status="denied")
    ctx = make_ctx(config, paths, db, sink)
    text = run(SetAlarm, ctx, backup=PhoneBackup(config, store_factory=lambda: broken), time="7:00").content
    assert TimerStore(db).pending(KIND_ALARM) and "The phone backup (Reminder) could not be created" in text
    assert "the Apple Reminder could not be created" in text


def test_set_alarm_phone_backup_off_rings_on_the_mac_only(frozen, scheduler_installed, mac, config, paths, db, sink, eventkit):
    config.alarm.phone_backup = "off"
    ctx = make_ctx(config, paths, db, sink)
    text = run(SetAlarm, ctx, backup=PhoneBackup(config, store_factory=lambda: eventkit), time="7:00").content
    assert "Phone backup off" in text and eventkit.calls == []


def test_set_alarm_warns_without_the_scheduler(frozen, monkeypatch, mac, config, paths, db, sink, backup):
    monkeypatch.setattr(launchd, "status", lambda: SchedulerStatus(installed=False, running=False, detail="not installed"))
    ctx = make_ctx(config, paths, db, sink)
    assert t("timers.scheduler_missing") in run(SetAlarm, ctx, backup=backup, time="7:00").content


def test_set_alarm_rejects_the_past_and_nonsense(frozen, scheduler_installed, mac, config, paths, db, sink, backup):
    ctx = make_ctx(config, paths, db, sink)
    with pytest.raises(ToolValidationError):
        run(SetAlarm, ctx, backup=backup, time="bugün 7:00")
    with pytest.raises(ToolValidationError):
        run(SetAlarm, ctx, backup=backup, time="sabah erken")


# --- list / cancel -----------------------------------------------------------------------------------------------


def test_list_and_cancel_by_id_label_and_time(frozen, scheduler_installed, mac, config, paths, db, sink, backup, eventkit):
    ctx = make_ctx(config, paths, db, sink)
    run(SetAlarm, ctx, backup=backup, time="7:00", label="Kalk", repeat="weekdays")
    run(SetAlarm, ctx, backup=backup, time="7:30", label="İlaç")
    run(SetAlarm, ctx, backup=backup, time="22:00", label="Uyku")
    listed = run(ListAlarms, ctx).content  # for the model: English
    assert (
        listed.startswith("3 alarm(s):")
        and "#1  2026-10-19 07:00 (tomorrow, in 15 h 30 min)  Kalk  [weekdays]  phone: reminder+telegram" in listed
    )
    assert "#3  2026-10-18 22:00 (today, in 6 h 30 min)  Uyku" in listed
    assert run(CancelAlarm, ctx, backup=backup, time="7:30").content.startswith("1 alarm iptal edildi: #2 2026-10-19 07:30 'İlaç'")
    assert ("delete_reminder", "rem-2") in eventkit.calls
    assert run(CancelAlarm, ctx, backup=backup, label="uyku").content.startswith("1 alarm iptal edildi: #3")
    assert run(CancelAlarm, ctx, backup=backup, id=1).content.startswith("1 alarm iptal edildi: #1")
    assert TimerStore(db).pending(KIND_ALARM) == [] and not eventkit.reminders
    assert run(CancelAlarm, ctx, backup=backup, id=1).content == "Alarm #1 (Kalk) zaten cancelled."
    assert run(CancelAlarm, ctx, backup=backup, time="7:30").content == "07:30 için kurulu alarm yok."
    assert run(ListAlarms, ctx).content == "No alarms set."
    with pytest.raises(ToolValidationError):
        run(CancelAlarm, ctx, backup=backup, id=99)
    with pytest.raises(ToolValidationError):
        run(CancelAlarm, ctx, backup=backup)


def test_cancel_all(frozen, scheduler_installed, mac, config, paths, db, sink, backup):
    ctx = make_ctx(config, paths, db, sink)
    run(SetAlarm, ctx, backup=backup, time="7:00")
    run(SetAlarm, ctx, backup=backup, time="8:00")
    assert run(CancelAlarm, ctx, backup=backup, label="hepsi").content.startswith("2 alarm iptal edildi")


def test_cli_list_and_cancel(frozen, scheduler_installed, mac, config, paths, db, sink, backup, monkeypatch):
    ctx = make_ctx(config, paths, db, sink)
    run(SetAlarm, ctx, backup=backup, time="7:00", label="Kalk")
    assert cli_list(paths, now=NOW).startswith(
        "1 kurulu alarm:\n#1  2026-10-19 07:00 (yarın, 15 sa 30 dk sonra)  Kalk  tel: hatırlatıcı+telegram"
    )
    monkeypatch.setattr(T.PhoneBackup, "remove", lambda self, rid: True)
    assert cli_cancel(config, paths, "1").startswith("1 alarm iptal edildi")
    assert cli_list(paths, now=NOW) == "kurulu alarm yok"


def test_cli_list_and_cancel_in_english(frozen, scheduler_installed, mac, config, paths, db, sink, backup, monkeypatch):
    from jarvis import i18n

    i18n.set_language("en")
    ctx = make_ctx(config, paths, db, sink)
    run(SetAlarm, ctx, backup=backup, time="7:00", label="Wake up", repeat="weekdays")
    assert cli_list(paths, now=NOW).startswith(
        "1 pending alarm(s):\n#1  2026-10-19 07:00 (tomorrow, in 15 h 30 min)  Wake up  [weekdays]  phone: reminder+telegram"
    )
    monkeypatch.setattr(T.PhoneBackup, "remove", lambda self, rid: True)
    assert cli_cancel(config, paths, "1").startswith("1 alarm(s) cancelled: #1")
    assert cli_cancel(config, paths, "all") == "No alarms set."
    assert cli_list(paths, now=NOW) == "no pending alarms"


# --- stop / snooze tools, the command file ---------------------------------------------------------------------------


def write_ring_file(paths, alarm_id=1, label="Kalk", *, pid=None, started=None, max_ring_s=600.0):
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    started = started or datetime.now(tz=TZ)
    paths.alarm_ring_file.write_text(
        json.dumps(
            {
                "alarm_id": alarm_id,
                "label": label,
                "fire_at": NOW.isoformat(),
                "started_at": started.isoformat(),
                "pid": os.getpid() if pid is None else pid,
                "max_ring_s": max_ring_s,
            }
        )
    )


def read_command(paths) -> dict | None:
    return json.loads(paths.alarm_command_file.read_text()) if paths.alarm_command_file.is_file() else None


def test_stop_and_snooze_tools_when_nothing_rings(config, paths, db, sink):
    ctx = make_ctx(config, paths, db, sink)
    assert run(StopAlarm, ctx).content == A.NOT_RINGING_TEXT
    assert run(SnoozeAlarm, ctx).content == A.NOT_RINGING_TEXT
    assert read_command(paths) is None


def test_stop_tool_writes_the_command_and_waits_for_the_ringer(config, paths, db, sink, monkeypatch, logs):
    ctx = make_ctx(config, paths, db, sink, channel="telegram")
    write_ring_file(paths)
    monkeypatch.setattr(A, "ACK_WAIT_S", 0.0)  # no ringer in this process: do not wait for the file to go
    assert run(StopAlarm, ctx).content == A.STOPPED_TEXT
    command = read_command(paths)
    assert command["action"] == "stop" and command["by"] == "tool/telegram" and command["alarm_id"] == 1
    assert any(a.get("decision") == "stop_requested" and a.get("by") == "tool/telegram" for a in audit_lines(logs))


def test_snooze_tool_default_and_explicit_minutes(frozen, config, paths, db, sink, monkeypatch):
    ctx = make_ctx(config, paths, db, sink)
    write_ring_file(paths)
    monkeypatch.setattr(A, "ACK_WAIT_S", 0.0)
    assert run(SnoozeAlarm, ctx).content == "Alarm ertelendi: 9 dakika sonra (15:39) yeniden çalar."
    assert read_command(paths)["minutes"] == 9
    assert run(SnoozeAlarm, ctx, minutes=15).content == "Alarm ertelendi: 15 dakika sonra (15:45) yeniden çalar."
    assert read_command(paths)["minutes"] == 15


def test_stop_tool_reports_a_ringer_that_does_not_answer(config, paths, db, sink, monkeypatch):
    ctx = make_ctx(config, paths, db, sink)
    write_ring_file(paths)
    monkeypatch.setattr(A, "ACK_WAIT_S", 0.05)
    with pytest.raises(ToolExecutionError):
        run(StopAlarm, ctx)


def test_stale_ring_files_are_cleaned_up(paths):
    write_ring_file(paths, pid=2_000_000_000)  # no such process
    assert ringing(paths) is None and not paths.alarm_ring_file.is_file()
    write_ring_file(paths, started=datetime.now(tz=TZ) - timedelta(minutes=30), max_ring_s=60)  # long over
    assert ringing(paths) is None
    write_ring_file(paths)
    info = ringing(paths)
    assert info is not None and info.alarm_id == 1 and info.label == "Kalk"
    paths.alarm_ring_file.write_text("{broken")
    assert ringing(paths) is None and not paths.alarm_ring_file.is_file()


def test_request_stop_is_a_no_op_without_a_ringing_alarm(paths):
    assert request_stop(paths, by="hotkey") is False and request_snooze(paths, by="voice") is False
    assert read_command(paths) is None
    with pytest.raises(ValueError):
        A.request_command(paths, "dance", by="x")


def test_take_command_drops_stale_requests(paths):
    write_ring_file(paths)
    assert request_stop(paths, by="cli") is True
    old = read_command(paths)
    old["created_at"] = (datetime.now(tz=TZ) - timedelta(minutes=10)).isoformat()
    paths.alarm_command_file.write_text(json.dumps(old))
    assert A.take_command(paths) is None and not paths.alarm_command_file.is_file()


# --- the ringer -------------------------------------------------------------------------------------------------------


@pytest.fixture
def sound(paths):
    paths.sounds_dir.mkdir(exist_ok=True)
    (paths.sounds_dir / "alarm.m4a").write_bytes(b"m4a")
    return paths.sounds_dir / "alarm.m4a"


def ring_until(ringer, row, state, clock, procs, *, stop_after_loops=None, answer=None, request=None, max_ring_s=None):
    """Drive one ring on the fake clock: after `stop_after_loops` afplay runs, apply `request` (a callable)
    or answer the dialog with `answer`; without either the ring hits its time limit."""
    counter = {"n": 0}
    original_popen = procs.popen

    def popen(argv, **kw):
        proc = original_popen(argv, **kw)
        if Path(argv[0]).name == "afplay":
            counter["n"] += 1
            if stop_after_loops is not None and counter["n"] == stop_after_loops:
                if request is not None:
                    request()
                elif answer is not None:
                    procs.answer_dialog(answer)
        return proc

    ringer._popen = popen
    return ringer.ring(row, state, max_ring_s=max_ring_s)


def test_ring_loops_the_sound_raises_and_restores_the_volume_and_stops_on_durdur(config, paths, db, sound, logs):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=3, answer=A.STOP_LABEL)
    assert result.outcome == "stopped" and result.by == "dialog" and result.loops == 3
    afplays = procs.of("afplay")
    assert len(afplays) == 3 and afplays[0].argv[1:] == ["-v", "1", str(sound)]  # our own sound: Do Not Disturb cannot mute it
    assert volume.sets == [(70, False), (25, False)] and result.volume_restored is True  # up to alarm.volume, back to 25 %
    dialog = procs.of("osascript")[0]
    assert "display dialog" in dialog.argv[2] and dialog.argv[4:] == ["⏰ Kalk\n15:30", "Durdur", "Ertele (9 dk)", "600"]
    assert not paths.alarm_ring_file.is_file() and not paths.alarm_command_file.is_file()
    assert [r.id for r in store.pending(KIND_ALARM)] == [row.id]  # a stop adds no snooze row
    decisions = [a.get("decision") for a in audit_lines(logs) if a.get("tool") == "alarm"]
    assert decisions == ["ringing", "stopped"]


def test_ring_writes_the_state_file_while_ringing(config, paths, db, sound):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    seen = {}

    def check():
        info = ringing(paths)
        seen["info"] = info
        procs.answer_dialog(A.STOP_LABEL)

    ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=1, request=check)
    assert seen["info"].alarm_id == row.id and seen["info"].label == "Kalk" and seen["info"].pid == os.getpid()


def test_ring_snoozed_from_the_dialog_schedules_the_snooze_row_and_moves_the_reminder(frozen, config, paths, db, sound, backup, eventkit):
    store = TimerStore(db)
    state = AlarmState(hour=7, minute=0, repeat=WEEKDAYS, reminder_id="rem-old")
    eventkit.reminders["rem-old"] = ReminderRecord("rem-old", "⏰ Kalk", "Jarvis Alarm", "list-1")
    row = alarm_row(store, state=state)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db), backup=backup)
    result = ring_until(ringer, row, state, clock, procs, stop_after_loops=2, answer="Ertele (9 dk)")
    assert result.outcome == "snoozed" and result.by == "dialog" and result.minutes == 9
    snooze = store.get(result.snooze_id)
    assert snooze.kind == KIND_ALARM and snooze.label == "Kalk"
    assert timedelta(minutes=8, seconds=50) < snooze.fire_at - datetime.now(TZ) <= timedelta(minutes=9)
    snooze_state = AlarmState.from_json(snooze.pomodoro_state_json)
    assert snooze_state.snoozed_from == row.id and snooze_state.snooze_count == 1 and snooze_state.repeat == []
    assert snooze_state.reminder_id == "rem-1" and "rem-old" not in eventkit.reminders
    assert ("delete_reminder", "rem-old") in eventkit.calls


def test_english_dialog_buttons_and_the_snooze_answer(frozen, config, paths, db, sound):
    from jarvis import i18n

    i18n.set_language("en")
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=1, answer="Snooze (9 min)")
    assert result.outcome == "snoozed" and result.by == "dialog" and result.minutes == 9
    assert procs.of("osascript")[0].argv[4:] == ["⏰ Kalk\n15:30", "Stop", "Snooze (9 min)", "600"]
    assert store.get(result.snooze_id).label == "Kalk"


def test_escape_in_the_dialog_snoozes(config, paths, db, sound):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=1, answer="cancel")
    assert result.outcome == "snoozed" and result.by == "dialog"


def test_ring_stops_by_itself_after_max_ring_min_and_tells_the_phone(config, paths, db, sound):
    config.alarm.max_ring_min = 2
    store = TimerStore(db)
    row = alarm_row(store)
    both = RecordingNotifier()
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db), notifier=both)
    result = ringer.ring(row, AlarmState(hour=7, minute=0))
    assert result.outcome == "timeout" and result.by == "timeout"
    assert clock.t - 1000.0 >= 120 and result.loops >= 100  # kept looping until the limit on the fake clock
    assert volume.sets[-1] == (25, False)
    assert both.sent == [(A.NOTIFICATION_TITLE, A.TIMEOUT_TEXT.format(label="Kalk", at="15:30", minutes=2))]
    assert procs.dialog is not None and procs.dialog.poll() is not None  # the dialog was closed too


def test_dialog_timeout_ends_the_ring(config, paths, db, sound):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=2, answer="timeout")
    assert result.outcome == "timeout"


@pytest.mark.parametrize("by", ["hotkey", "voice", "telegram", "cli", "tool/cli"])
def test_ring_stops_on_a_command_from_another_process(config, paths, db, sound, by):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(
        ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=2, request=lambda: request_stop(paths, by=by)
    )
    assert result.outcome == "stopped" and result.by == by and result.loops == 2
    assert volume.sets[-1] == (25, False) and not paths.alarm_command_file.is_file()


def test_ring_snoozes_on_a_command_with_minutes(frozen, config, paths, db, sound):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(
        ringer,
        row,
        AlarmState(hour=7, minute=0),
        clock,
        procs,
        stop_after_loops=1,
        request=lambda: request_snooze(paths, by="telegram", minutes=15),
    )
    assert result.outcome == "snoozed" and result.by == "telegram" and result.minutes == 15
    snooze = store.get(result.snooze_id)
    assert timedelta(minutes=14, seconds=50) < snooze.fire_at - datetime.now(TZ) <= timedelta(minutes=15)


def test_ring_ignores_a_stale_command_from_before(config, paths, db, sound):
    """A stop request written when nothing rang must not end the next ring at once."""
    store = TimerStore(db)
    row = alarm_row(store)
    write_ring_file(paths)
    request_stop(paths, by="cli")
    paths.alarm_ring_file.unlink()
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    result = ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=3, answer=A.STOP_LABEL)
    assert result.loops == 3


def test_ring_without_the_sound_file_uses_the_fallback_and_without_afplay_still_shows_the_dialog(config, paths, db):
    store = TimerStore(db)
    row = alarm_row(store)
    paths.sounds_dir.mkdir(exist_ok=True)
    (paths.sounds_dir / "fallback.aiff").write_bytes(b"aiff")
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=1, answer=A.STOP_LABEL)
    assert procs.of("afplay")[0].argv[-1].endswith("fallback.aiff")
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db), afplay=False)
    procs2 = procs

    def answer():
        procs2.answer_dialog(A.STOP_LABEL)

    # no afplay: the loop only watches the dialog and the command file
    ringer._popen = procs.popen
    clock_steps = {"n": 0}
    original_sleep = clock.sleep

    def sleep(s):
        clock_steps["n"] += 1
        if clock_steps["n"] == 3:
            answer()
        original_sleep(s)

    ringer._sleep = sleep
    result = ringer.ring(row, AlarmState(hour=7, minute=0))
    assert result.outcome == "stopped" and procs.of("afplay") == [] and volume.sets == [(70, False), (25, False)]


def test_volume_is_left_alone_when_it_cannot_be_read(config, paths, db, sound):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, volume=FakeVolume(fail_read=True), db=NoCloseDb(db))
    result = ring_until(ringer, row, AlarmState(hour=7, minute=0), clock, procs, stop_after_loops=1, answer=A.STOP_LABEL)
    assert result.volume_restored is None and volume.sets == [(70, False)]  # raised, nothing to restore to


def test_ringer_start_runs_one_alarm_at_a_time(config, paths, db, sound):
    store = TimerStore(db)
    row = alarm_row(store)
    ringer, procs, volume, clock = make_ringer(paths, config, db=NoCloseDb(db))
    import threading

    gate = threading.Event()
    original = ringer.ring

    def slow_ring(r, s, **kw):
        gate.wait(2.0)
        return original(r, s, **kw)

    ringer.ring = slow_ring  # type: ignore[method-assign]
    assert ringer.start(row, AlarmState(hour=7, minute=0)) is True
    assert ringer.busy and ringer.start(row, AlarmState(hour=7, minute=0)) is False
    procs.popen  # the dialog has not opened yet; answer it as soon as it appears
    write_ring_file(paths)  # so the stop request has something to address once the ring begins
    request_stop(paths, by="cli")
    gate.set()
    ringer.join(5.0)
    assert not ringer.busy and ringer.last is not None


def test_no_shell_anywhere():
    src = Path(A.__file__).read_text(encoding="utf-8") + Path(T.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src and "os.system" not in src and "sudo" not in src


# --- keep awake ------------------------------------------------------------------------------------------------------


def test_keep_awake_window(config, paths):
    procs = Procs()
    keep = KeepAwake(popen=procs.popen, which=lambda n: "/usr/bin/caffeinate")
    assert keep.update(None, hours=12, extra_min=12, now=NOW) is False and not keep.active
    far = NOW + timedelta(hours=20)
    assert keep.update(far, hours=12, extra_min=12, now=NOW) is False and procs.procs == []
    soon = NOW + timedelta(hours=5)
    assert keep.update(soon, hours=12, extra_min=12, now=NOW) is True and keep.active
    argv = procs.of("caffeinate")[0].argv
    assert argv[1:3] == ["-i", "-t"] and int(argv[3]) == 5 * 3600 + 12 * 60
    assert keep.update(soon, hours=12, extra_min=12, now=NOW) is True and keep.starts == 1  # same alarm: kept
    assert keep.update(soon + timedelta(hours=1), hours=12, extra_min=12, now=NOW) is True and keep.starts == 2  # moved: restarted
    assert procs.of("caffeinate")[0].poll() is not None  # the old one was killed
    assert keep.update(None, hours=12, extra_min=12, now=NOW) is False and not keep.active
    assert procs.of("caffeinate")[1].poll() is not None
    assert keep.update(soon, hours=0, extra_min=12, now=NOW) is False  # disabled
    assert "not running" in keep.describe()


def test_keep_awake_without_caffeinate(config):
    keep = KeepAwake(popen=Procs().popen, which=lambda n: None)
    assert keep.update(NOW + timedelta(hours=1), hours=12, extra_min=12, now=NOW) is False and not keep.available()


def test_keep_awake_job_reads_the_next_alarm(config, paths, db, monkeypatch):
    store = TimerStore(db)
    store.add("Kalk", datetime.now(TZ) + timedelta(hours=3), KIND_ALARM, AlarmState(hour=7, minute=0))
    procs = Procs()
    monkeypatch.setattr(A, "KEEP_AWAKE", KeepAwake(popen=procs.popen, which=lambda n: "/usr/bin/caffeinate"))
    A.keep_awake_job(config, paths, db, None)
    assert A.KEEP_AWAKE.active and procs.of("caffeinate")
    store.cancel_by_kind(KIND_ALARM)
    A.keep_awake_job(config, paths, db, None)
    assert not A.KEEP_AWAKE.active


# --- the engine: on time, late (missed), repeating ---------------------------------------------------------------------


class FakeRinger:
    def __init__(self, busy=False) -> None:
        self.started: list[tuple[int, AlarmState]] = []
        self._busy = busy

    def start(self, row, state, *, blocking=False):
        if self._busy:
            return False
        self.started.append((row.id, state))
        return True


def make_handler(config, paths, *, telegram=True, ringer=None, backup=None):
    mac = RecordingNotifier()
    tg = RecordingNotifier() if telegram else None
    ringer = ringer or FakeRinger()
    return AlarmHandler(config, paths, mac=mac, telegram=tg, ringer=ringer, phone_backup=backup), mac, tg, ringer


def test_alarm_on_time_rings_and_messages_the_phone(config, paths, db):
    store = TimerStore(db)
    state = AlarmState(hour=7, minute=0)
    store.add("Kalk", NOW, KIND_ALARM, state)
    handler, mac, tg, ringer = make_handler(config, paths)
    plain = RecordingNotifier()
    fired = fire_due(store, plain, now=NOW + timedelta(seconds=30), alarm_handler=handler)
    assert len(fired) == 1 and fired[0].rang and not fired[0].missed and fired[0].kind == KIND_ALARM
    assert ringer.started[0][0] == 1 and tg.sent == [(A.NOTIFICATION_TITLE, "⏰ Alarm: Kalk (15:30)")]
    assert mac.sent == [] and plain.sent == []  # the ring is the Mac's notification
    assert fired[0].next_id is None and store.pending(KIND_ALARM) == []


def test_alarm_late_is_missed_not_rung(config, paths, db):
    store = TimerStore(db)
    store.add("Kalk", NOW, KIND_ALARM, AlarmState(hour=7, minute=0))
    handler, mac, tg, ringer = make_handler(config, paths)
    fired = fire_due(store, RecordingNotifier(), now=NOW + timedelta(hours=3), late_after_s=120, alarm_handler=handler)
    assert fired[0].missed and not fired[0].rang and ringer.started == []
    text = A.MISSED_TEXT.format(label="Kalk", at="15:30")
    assert mac.sent == [(A.NOTIFICATION_TITLE, text)] and tg.sent == [(A.NOTIFICATION_TITLE, text)] and fired[0].message == text


def test_alarm_notifications_and_the_phone_reminder_in_english(config, paths, db, backup, eventkit):
    from jarvis import i18n

    i18n.set_language("en")
    store = TimerStore(db)
    store.add("Wake up", NOW, KIND_ALARM, AlarmState(hour=7, minute=0))
    handler, mac, tg, ringer = make_handler(config, paths)
    fire_due(store, RecordingNotifier(), now=NOW + timedelta(hours=3), late_after_s=120, alarm_handler=handler)
    assert mac.sent == [(A.NOTIFICATION_TITLE, "Missed alarm: Wake up (15:30). The Mac was asleep, so it did not ring.")]
    ident = backup.create("Wake up", NOW)
    assert eventkit.reminders[ident].title == "⏰ Wake up" and eventkit.reminders[ident].notes.startswith("Jarvis alarm (")
    assert A.STOPPED_TEXT == "Alarm stopped." and A.SNOOZED_TEXT.format(minutes=9, at="07:09").startswith("Alarm snoozed")


def test_repeating_alarm_schedules_the_next_occurrence_first_even_when_missed(config, paths, db, backup, eventkit):
    store = TimerStore(db)
    state = AlarmState(hour=7, minute=30, repeat=WEEKDAYS, reminder_id="rem-0")
    monday = datetime(2026, 10, 19, 7, 30, tzinfo=TZ)
    store.add("Kalk", monday, KIND_ALARM, state)
    handler, mac, tg, ringer = make_handler(config, paths, backup=backup)
    fired = fire_due(store, RecordingNotifier(), now=monday + timedelta(hours=2), late_after_s=120, alarm_handler=handler)
    assert fired[0].missed and fired[0].next_id is not None
    nxt = store.get(fired[0].next_id)
    assert nxt.fire_at == datetime(2026, 10, 20, 7, 30, tzinfo=TZ) and nxt.status == "pending"
    nxt_state = AlarmState.from_json(nxt.pomodoro_state_json)
    assert nxt_state.repeat == WEEKDAYS and nxt_state.reminder_id == "rem-1"  # the scheduler made the next reminder
    assert ("create_reminder", "⏰ Kalk", nxt.fire_at, "list-1") in eventkit.calls


def test_repeating_alarm_after_a_long_sleep_goes_on_from_today(config, paths, db):
    store = TimerStore(db)
    store.add("Kalk", datetime(2026, 10, 12, 7, 30, tzinfo=TZ), KIND_ALARM, AlarmState(hour=7, minute=30, repeat=EVERY_DAY))
    handler, *_ = make_handler(config, paths)
    fired = fire_due(store, RecordingNotifier(), now=NOW, alarm_handler=handler)  # a week later, Sunday 15:30
    assert store.get(fired[0].next_id).fire_at == datetime(2026, 10, 19, 7, 30, tzinfo=TZ)


def test_next_reminder_is_skipped_when_the_scheduler_has_no_reminders_access(config, paths, db, logs):
    store = TimerStore(db)
    store.add("Kalk", NOW, KIND_ALARM, AlarmState(hour=15, minute=30, repeat=EVERY_DAY))
    denied = FakeEventKit(status="denied")
    handler, *_ = make_handler(config, paths, backup=PhoneBackup(config, store_factory=lambda: denied))
    fired = fire_due(store, RecordingNotifier(), now=NOW + timedelta(seconds=5), alarm_handler=handler)
    assert AlarmState.from_json(store.get(fired[0].next_id).pomodoro_state_json).reminder_id is None
    assert not any(c[0] == "create_reminder" for c in denied.calls)
    assert any(a.get("decision") == "backup_skipped" for a in audit_lines(logs))


def test_second_alarm_while_one_rings_is_a_notification(config, paths, db):
    store = TimerStore(db)
    store.add("İlaç", NOW, KIND_ALARM, AlarmState(hour=15, minute=30))
    handler, mac, tg, ringer = make_handler(config, paths, ringer=FakeRinger(busy=True))
    fired = fire_due(store, RecordingNotifier(), now=NOW + timedelta(seconds=5), alarm_handler=handler)
    assert not fired[0].rang and mac.sent == [(A.NOTIFICATION_TITLE, "⏰ Alarm: İlaç (15:30)")]


def test_alarm_without_a_handler_notifies_like_a_timer(config, paths, db):
    store = TimerStore(db)
    store.add("Kalk", NOW, KIND_ALARM, AlarmState(hour=15, minute=30))
    plain = RecordingNotifier()
    fired = fire_due(store, plain, now=NOW + timedelta(seconds=5))
    assert fired[0].notified and plain.sent == [(engine.NOTIFICATION_TITLE, "⏰ Alarm: Kalk (15:30)")]


def test_alarm_without_telegram_backup_only_rings(config, paths, db):
    store = TimerStore(db)
    store.add("Kalk", NOW, KIND_ALARM, AlarmState(hour=15, minute=30, phone_backup="reminder"))
    handler, mac, tg, ringer = make_handler(config, paths)
    fire_due(store, RecordingNotifier(), now=NOW + timedelta(seconds=5), alarm_handler=handler)
    assert tg.sent == [] and ringer.started


def test_handler_failure_does_not_stop_the_tick(config, paths, db):
    store = TimerStore(db)
    store.add("Kalk", NOW, KIND_ALARM, AlarmState(hour=15, minute=30))
    store.add("plain", NOW, KIND_TIMER)

    class Broken:
        def handle(self, *a, **k):
            raise RuntimeError("no speakers")

    plain = RecordingNotifier()
    fired = fire_due(store, plain, now=NOW + timedelta(seconds=5), alarm_handler=Broken())
    assert fired[0].error == "RuntimeError: no speakers" and plain.sent == [(engine.NOTIFICATION_TITLE, "plain")]
    assert store.get(1).status == "fired"


def test_run_once_rings_to_the_end_in_the_foreground(config, paths, db, monkeypatch):
    TimerStore(db).add("Kalk", datetime.now(TZ) - timedelta(minutes=1), KIND_ALARM, AlarmState(hour=15, minute=30))
    handler, mac, tg, ringer = make_handler(config, paths)
    seen = {}

    def fake_handler(cfg, pth, *, blocking=False):
        seen["blocking"] = blocking
        return handler

    monkeypatch.setattr(service, "alarm_handler_for", fake_handler)
    monkeypatch.setattr(service, "notifier_for", lambda cfg: RecordingNotifier())
    assert service.run_once(config, paths) == 1 and seen["blocking"] is True and ringer.started


def test_scheduler_handler_is_built_from_the_config(config, paths, monkeypatch):
    monkeypatch.setattr(service, "alarm_notifiers_for", lambda cfg: (RecordingNotifier(), None))
    handler = service.alarm_handler_for(config, paths)
    assert isinstance(handler, AlarmHandler) and isinstance(handler.ringer, Ringer) and handler.telegram is None
    monkeypatch.setattr(service, "alarm_notifiers_for", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert service.alarm_handler_for(config, paths) is None


# --- the phone backup -------------------------------------------------------------------------------------------------


def test_phone_backup_creates_in_the_jarvis_list_and_removes(config, backup, eventkit, logs):
    ident = backup.create("Kalk", NOW)
    assert ident == "rem-1" and eventkit.lists[-1].title == "Jarvis Alarm"
    assert eventkit.reminders[ident].list_name == "Jarvis Alarm" and eventkit.reminders[ident].notes.startswith("Jarvis alarmı")
    assert backup.create("İlaç", NOW) == "rem-2" and eventkit.calls.count(("ensure_reminder_list", "Jarvis Alarm")) == 1
    assert backup.remove("rem-1") is True and "rem-1" not in eventkit.reminders
    assert backup.remove(None) is False and backup.remove("nope") is False  # tries delete, then complete; neither finds it
    assert ("delete_reminder", "nope") in eventkit.calls and ("complete_reminder", "nope") in eventkit.calls
    decisions = [a.get("decision") for a in audit_lines(logs) if a.get("tool") == "alarm"]
    assert decisions[:3] == ["backup_created", "backup_created", "backup_removed"]


def test_phone_backup_respects_the_mode_and_authorization(config, eventkit):
    config.alarm.phone_backup = "telegram"
    assert PhoneBackup(config, store_factory=lambda: eventkit).create("Kalk", NOW) is None
    config.alarm.phone_backup = "reminder"
    denied = FakeEventKit(status="not_determined")
    assert PhoneBackup(config, store_factory=lambda: denied).create("Kalk", NOW, require_authorized=True) is None
    assert denied.calls == []  # never prompts from the scheduler
    with pytest.raises(ToolExecutionError):
        PhoneBackup(config, store_factory=lambda: denied).create("Kalk", NOW)  # the tool process: the usual error
    assert PhoneBackup(config, store_factory=lambda: denied).remove("rem-1") is False


def test_phone_backup_describe_is_honest(config, backup):
    text = backup.describe(NOW, reminder_id="rem-1", telegram_ok=True)  # for the model: English
    assert "a normal reminder notification, it does not ring like an alarm" in text and "a Telegram message" in text
    assert "could not be created" in backup.describe(NOW, reminder_id=None, telegram_ok=True)
    config.alarm.phone_backup = "off"
    assert PhoneBackup(config).describe(NOW, reminder_id=None, telegram_ok=False).startswith("Phone backup off")


# --- doctor -------------------------------------------------------------------------------------------------------------


def test_health_checks(config, paths, db, monkeypatch, sound):
    monkeypatch.setattr(A.KeepAwake, "available", lambda self: True)
    monkeypatch.setattr("jarvis.utils.eventkit.EventKitStore.authorization_status", staticmethod(lambda entity: "authorized"))
    TimerStore(db).add("Kalk", NOW + timedelta(days=1), KIND_ALARM, AlarmState(hour=7, minute=0))
    checks = {c.name: c for c in health_checks(config, paths)}
    assert checks["Alarm sound"].ok and not checks["Alarm sound"].warn and checks["Alarm sound"].detail == str(sound)
    assert checks["Alarm keep-awake (caffeinate)"].ok
    assert (
        checks["Alarm phone backup"].detail.startswith("both: Reminders access granted") and checks["Alarm phone backup"].warn
    )  # telegram not set up
    assert checks["Alarms"].detail.startswith("1 pending, next 2026-10-19 15:30 (Kalk)")


def test_health_checks_warn_about_a_missing_sound_and_permission(config, paths, monkeypatch):
    monkeypatch.setattr(A.KeepAwake, "available", lambda self: False)
    monkeypatch.setattr("jarvis.utils.eventkit.EventKitStore.authorization_status", staticmethod(lambda entity: "denied"))
    config.telegram.enabled = True
    config.telegram.allowed_chat_id = 5
    checks = {c.name: c for c in health_checks(config, paths)}
    assert checks["Alarm sound"].warn and "missing" in checks["Alarm sound"].detail
    assert not checks["Alarm keep-awake (caffeinate)"].ok
    assert "Reminders access is denied" in checks["Alarm phone backup"].detail and checks["Alarm phone backup"].warn
    assert "Telegram message at the alarm time" in checks["Alarm phone backup"].detail
    assert checks["Alarms"].detail == "none pending"


def test_health_checks_show_a_ringing_alarm(config, paths):
    write_ring_file(paths, label="Kalk")
    checks = {c.name: c for c in health_checks(config, paths)}
    assert checks["Alarms"].detail.startswith("RINGING now: #1 Kalk")


def test_shipped_config_has_the_alarm_section_and_remote_tools(config):
    assert config.alarm.phone_backup == "both" and config.alarm.max_ring_min == 10 and config.alarm.snooze_min == 9
    assert config.alarm.keep_awake_hours == 12 and config.alarm.reminder_list == "Jarvis Alarm"
    assert "alarms.*" in config.telegram.remote_allowed_tools
