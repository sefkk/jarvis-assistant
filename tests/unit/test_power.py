"""Mac power tools (tools/power.py) with every command faked: nothing sleeps, locks or reads pmset here.
Covers the status parsing, the lock (framework and fallback), the sleep countdown with its warning and
"iptal", the scheduled sleep with the one-minute notice, and that no command ever starts with sudo."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jarvis import i18n
from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.observability import logging_setup
from jarvis.observability.health import INTEGRATION_MODULES
from jarvis.scheduler import launchd
from jarvis.scheduler.engine import NOTIFICATION_TITLE, fire_due
from jarvis.scheduler.launchd import SchedulerStatus
from jarvis.scheduler.store import KIND_SLEEP, KIND_SLEEP_NOTICE, TimerStore
from jarvis.tools import power
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.power import (
    CancelScheduledSleepTool,
    LockScreenTool,
    MacStatus,
    MacStatusTool,
    ScheduleSleepTool,
    SleepMacTool,
    collect_status,
    format_status,
    health_checks,
    parse_batt,
    parse_on_battery_since,
)
from jarvis.utils.timeutil import local_tz
from tests.conftest import RecordingSink
from tests.unit.test_scheduler import RecordingNotifier

TZ = local_tz()
NOW = datetime(2026, 10, 18, 22, 0, tzinfo=TZ)
BOOT_SEC = int((NOW - timedelta(days=33, hours=15)).timestamp())

BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=37879907)\t27%; charging; 1:32 remaining present: true\n"
BATT_BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=37879907)\t64%; discharging; 4:10 remaining present: true\n"
BATT_FULL = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"
BATT_NOT_CHARGING = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true\n"
BATT_NO_ESTIMATE = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t55%; charging; (no estimate) present: true\n"
IDLE = '  | |     "HIDIdleTime" = 133734762333\n'
LOCKED_NO = '      "IOConsoleLocked" = No\n'
LOCKED_YES = '      "IOConsoleLocked" = Yes\n'
BOOT = f"{{ sec = {BOOT_SEC}, usec = 979747 }} Mon Sep 14 21:59:05 2026\n"
PMSET_G = (
    "System-wide power settings:\nCurrently in use:\n standby              1\n"
    " sleep                1 (sleep prevented by caffeinate, dasd, powerd)\n displaysleep         20\n"
)
PMSET_G_FREE = "System-wide power settings:\nCurrently in use:\n sleep                1\n displaysleep         20\n"
PMSET_LOG = (
    "2026-10-18 09:00:00 +0300 Wake                \tWake from Normal Sleep Using AC (Charge:90%)\n"
    "2026-10-18 12:00:00 +0300 Assertions          \tSummary- [System: DeclUser] Using AC(Charge: 95)\n"
    "2026-10-18 19:46:00 +0300 Assertions          \tSummary- [System: DeclUser] Using Batt(Charge: 88)\n"
    "2026-10-18 20:30:00 +0300 Wake                \tWake from Deep Idle Using BATT (Charge:70%)\n"
    "2026-10-18 21:50:00 +0300 Assertions          \tSummary- [System: DeclUser] Using Batt(Charge: 65)\n"
)

CMD_BATT = ("pmset", "-g", "batt")
CMD_IDLE = ("ioreg", "-c", "IOHIDSystem", "-d", "4")
CMD_LOCKED = ("ioreg", "-n", "Root", "-d", "1")
CMD_BOOT = ("sysctl", "-n", "kern.boottime")
CMD_PMSET_G = ("pmset", "-g")
CMD_LOG = ("pmset", "-g", "log")
CMD_SLEEP = ("pmset", "sleepnow")
CMD_DISPLAY = ("pmset", "displaysleepnow")


class FakeCommands:
    """`power.run_argv` replacement: canned stdout per argv, a recorded call list, optional failures."""

    def __init__(self, outputs: dict[tuple, str], *, fail: set[tuple] | None = None) -> None:
        self.outputs = dict(outputs)
        self.fail = set(fail or ())
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=10.0):
        self.calls.append(list(argv))
        assert argv[0] != "sudo"
        key = tuple(argv)
        if key in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        if key not in self.outputs:
            raise AssertionError(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0, self.outputs[key], "")

    def of(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


def outputs(batt=BATT_AC, locked=LOCKED_NO, pmset_g=PMSET_G, log=PMSET_LOG) -> dict[tuple, str]:
    return {
        CMD_BATT: batt,
        CMD_IDLE: IDLE,
        CMD_LOCKED: locked,
        CMD_BOOT: BOOT,
        CMD_PMSET_G: pmset_g,
        CMD_LOG: log,
        CMD_SLEEP: "Sleeping now...\n",
        CMD_DISPLAY: "",
    }


class FakeTimer:
    def __init__(self, delay: float, fn) -> None:
        self.delay, self.fn = delay, fn
        self.started = self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fn()


@pytest.fixture
def commands(monkeypatch) -> FakeCommands:
    fake = FakeCommands(outputs())
    monkeypatch.setattr(power, "run_argv", fake)
    return fake


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(power, "_now", lambda: NOW)
    return NOW


@pytest.fixture
def fake_timers(monkeypatch):
    made: list[FakeTimer] = []

    def make(delay, fn):
        timer = FakeTimer(delay, fn)
        made.append(timer)
        return timer

    monkeypatch.setattr(power, "_make_timer", make)
    yield made
    power.cancel_countdown()


@pytest.fixture
def scheduler_installed(monkeypatch):
    monkeypatch.setattr(launchd, "status", lambda: SchedulerStatus(installed=True, running=True, pid=1, detail="running"))


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


def make_ctx(config, paths, db, sink, channel="cli") -> ToolContext:
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None, sink=sink, channel=channel)
    return ctx


def run(tool_cls, ctx, **args):
    tool = tool_cls(ctx)
    return tool.run(tool.parse_args(args))


def audit_lines(logs: Path) -> list[dict]:
    import json

    path = logs / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


# --- parsing ----------------------------------------------------------------------------------------


def test_parse_batt_variants():
    assert parse_batt(BATT_AC) == {
        "power_source": "AC",
        "battery_percent": 27,
        "charging": True,
        "battery_state": "charging",
        "battery_remaining": "1:32",
    }
    battery = parse_batt(BATT_BATTERY)
    assert battery["power_source"] == "Battery" and battery["charging"] is False and battery["battery_remaining"] == "4:10"
    assert battery["battery_state"] == "discharging"
    full = parse_batt(BATT_FULL)
    assert full["battery_percent"] == 100 and full["battery_state"] == "charged" and full["charging"] is False
    nc = parse_batt(BATT_NOT_CHARGING)
    assert nc["battery_state"] == "not charging" and nc["charging"] is False and nc["battery_remaining"] is None
    est = parse_batt(BATT_NO_ESTIMATE)
    assert est["charging"] is True and est["battery_remaining"] is None
    assert parse_batt("garbage") == {
        "power_source": "unknown",
        "battery_percent": None,
        "charging": None,
        "battery_state": "",
        "battery_remaining": None,
    }


def test_parse_on_battery_since_finds_the_switch_after_the_last_ac_entry():
    since = parse_on_battery_since(PMSET_LOG)
    assert since == datetime(2026, 10, 18, 19, 46, tzinfo=since.tzinfo)
    assert parse_on_battery_since("nothing here") is None
    # all on battery, no AC line: the earliest battery entry
    only_batt = "\n".join(line for line in PMSET_LOG.splitlines() if "Using AC" not in line)
    assert parse_on_battery_since(only_batt).strftime("%H:%M") == "19:46"


def test_collect_status_on_ac_skips_the_power_log(commands, frozen):
    status = collect_status(now=NOW)
    assert status.power_source == "AC" and status.battery_percent == 27 and status.charging is True
    assert status.idle_s == pytest.approx(133.73, abs=0.01)
    assert status.screen_locked is False
    assert status.uptime_s == pytest.approx(timedelta(days=33, hours=15).total_seconds(), abs=1)
    assert status.sleep_blockers == ["caffeinate", "dasd", "powerd"]
    assert status.errors == [] and status.on_battery_since is None
    assert commands.of(*CMD_LOG) == []  # the multi-megabyte log is read only on battery
    text = format_status(status, NOW, "tr")
    assert "Güç: adaptöre takılı, pil %27 (şarj oluyor), dolmasına 1:32" in text
    assert "Son kullanım: 2 dk 14 sn önce" in text
    assert "Ekran: kilitli değil" in text
    assert "Açık kalma süresi: 33 gün 15 sa" in text
    assert "Uykuyu engelleyen: caffeinate, dasd, powerd" in text
    assert "Pilde geçen süre" not in text
    english = format_status(status, NOW, "en")
    assert english.startswith("Power: plugged in, battery 27% (charging), 1:32 until full\nLast used: 2 min 14 s ago (keyboard/mouse)")
    assert "Screen: not locked" in english and "Up for: 33 d 15 h" in english and "Preventing sleep: caffeinate" in english


def test_collect_status_on_battery_says_how_long(monkeypatch, frozen):
    fake = FakeCommands(outputs(batt=BATT_BATTERY, locked=LOCKED_YES, pmset_g=PMSET_G_FREE))
    monkeypatch.setattr(power, "run_argv", fake)
    status = collect_status(now=NOW)
    assert status.power_source == "Battery" and status.on_battery_since.strftime("%H:%M") == "19:46"
    text = format_status(status, NOW, "tr")
    assert "Güç: pilde, pil %64 (boşalıyor), bitmesine 4:10" in text
    assert "Pilde geçen süre: 2 sa 14 dk (19:46'ten beri)" in text
    assert "Ekran: kilitli" in text and "Uykuyu engelleyen" not in text
    english = format_status(status, NOW, "en")
    assert "Power: on battery, battery 64% (discharging), 4:10 left" in english
    assert "On battery for: 2 h 14 min (since 19:46)" in english and "Screen: locked" in english


def test_collect_status_reports_failures_instead_of_raising(monkeypatch, frozen):
    fake = FakeCommands(outputs(), fail={CMD_IDLE, CMD_BOOT})
    monkeypatch.setattr(power, "run_argv", fake)
    status = collect_status(now=NOW)
    assert status.idle_s is None and status.uptime_s is None and status.battery_percent == 27
    assert len(status.errors) == 2 and "idle" in status.errors[0]
    assert "Okunamayan:" in format_status(status, NOW, "tr") and "Could not read:" in format_status(status, NOW, "en")


def test_mac_status_tool(commands, frozen, config, paths, db, sink):
    tool = MacStatusTool(make_ctx(config, paths, db, sink))
    assert tool.risk is RiskLevel.SAFE and tool.taints is False and tool.delegate_ok is True
    result = tool.run(tool.parse_args({}))
    assert result.content.startswith("Güç: adaptöre takılı") and not result.is_error


# --- lock ------------------------------------------------------------------------------------------


def test_lock_screen_uses_the_login_framework_and_verifies(monkeypatch, config, paths, db, sink, logs):
    fake = FakeCommands(outputs())
    monkeypatch.setattr(power, "run_argv", fake)
    locked = []

    def sac():
        locked.append(True)
        fake.outputs[CMD_LOCKED] = LOCKED_YES  # the console reports the lock right after the call

    monkeypatch.setattr(power, "_sac_lock", sac)
    tool = LockScreenTool(make_ctx(config, paths, db, sink))
    assert tool.risk is RiskLevel.SAFE and tool.delegate_ok is False
    assert tool.run(tool.parse_args({})).content == "Ekran kilitlendi."
    assert locked == [True] and fake.of(*CMD_DISPLAY) == []
    assert any(a.get("tool") == "lock_screen" and a.get("how") == "login framework" for a in audit_lines(logs))
    # already locked: nothing is called
    locked.clear()
    assert tool.run(tool.parse_args({})).content == "Ekran zaten kilitli."
    assert locked == []


def test_lock_screen_falls_back_to_display_sleep(monkeypatch, config, paths, db, sink):
    fake = FakeCommands(outputs())
    monkeypatch.setattr(power, "run_argv", fake)

    def broken():
        raise OSError("dlopen failed")

    monkeypatch.setattr(power, "_sac_lock", broken)
    result = run(LockScreenTool, make_ctx(config, paths, db, sink))
    assert "ekran uykusu" in result.content
    assert fake.of(*CMD_DISPLAY) == [list(CMD_DISPLAY)]


def test_lock_screen_unverified_when_the_console_never_reports_it(monkeypatch, config, paths, db, sink):
    fake = FakeCommands(outputs())
    monkeypatch.setattr(power, "run_argv", fake)
    monkeypatch.setattr(power, "_sac_lock", lambda: None)
    monkeypatch.setattr(power, "LOCK_VERIFY_S", 0.0)
    result = run(LockScreenTool, make_ctx(config, paths, db, sink))
    assert "doğrulanamadı" in result.content
    i18n.set_language("en")
    assert (
        run(LockScreenTool, make_ctx(config, paths, db, sink)).content == "The lock command was sent; the lock state could not be verified."
    )


# --- sleep now, with a countdown ------------------------------------------------------------------


def test_sleep_mac_metadata_and_bounds():
    tool = SleepMacTool()
    assert tool.risk is RiskLevel.CONFIRM and tool.delegate_ok is False
    assert tool.parse_args({}).delay_s == 10
    assert "pmset sleepnow" in tool.confirm_preview(tool.parse_args({"delay_s": 30}))
    with pytest.raises(ToolValidationError):
        tool.parse_args({"delay_s": 2})
    with pytest.raises(ToolValidationError):
        tool.parse_args({"delay_s": 121})


def test_sleep_mac_warns_first_then_sleeps_after_the_delay(commands, fake_timers, config, paths, db, sink, logs):
    ctx = make_ctx(config, paths, db, sink, channel="telegram")
    result = run(SleepMacTool, ctx)
    warning = power.WARNING_TEXT.format(seconds=10)
    assert result.content.startswith(warning + " " + power.CANCEL_HINT)
    assert sink.notices == [warning]  # the phone has the warning before the model even answers
    assert "already sent to the phone" in result.content
    timer = fake_timers[-1]
    assert timer.delay == 10 and timer.started and not timer.cancelled
    assert commands.of(*CMD_SLEEP) == []  # nothing slept yet
    assert power.pending_countdown() is not None
    timer.fire()
    assert commands.of(*CMD_SLEEP) == [list(CMD_SLEEP)]
    assert db.closed  # the connection was closed before the sleep...
    assert db.kv_get("missing") is None and not db.closed  # ...and reopens on the next use after the wake
    assert power.pending_countdown() is None
    decisions = [a.get("decision") for a in audit_lines(logs) if a.get("tool") == "sleep_mac"]
    assert decisions == ["countdown", "slept"]


def test_sleep_mac_outside_telegram_sends_no_notice(commands, fake_timers, config, paths, db, sink):
    result = run(SleepMacTool, make_ctx(config, paths, db, sink, channel="voice"), delay_s=20)
    assert sink.notices == [] and "System:" not in result.content
    assert result.content.startswith(power.WARNING_TEXT.format(seconds=20))
    assert fake_timers[-1].delay == 20


def test_sleep_mac_cancelled_by_iptal_within_the_delay(commands, fake_timers, config, paths, db, sink, logs):
    run(SleepMacTool, make_ctx(config, paths, db, sink))
    timer = fake_timers[-1]
    assert power.cancel_on_request("merhaba", paths=paths) is None  # not a cancel word: goes to the model
    assert power.cancel_on_request("İptal!", paths=paths) == power.CANCELLED_TEXT
    assert timer.cancelled and power.pending_countdown() is None
    timer.fire()  # a late fire of the cancelled timer does nothing
    assert commands.of(*CMD_SLEEP) == []
    assert power.cancel_on_request("iptal", paths=paths) is None  # nothing pending any more
    decisions = [a.get("decision") for a in audit_lines(logs) if a.get("tool") == "sleep_mac"]
    assert decisions == ["countdown", "cancelled"]


@pytest.mark.parametrize(
    "word", ["dur", "Dur.", "vazgeç", "iptal et", "stop", "uyutma", "CANCEL", "Cancel!", "don't sleep", "IPTAL", "Hayır"]
)
def test_cancel_words(commands, fake_timers, config, paths, db, sink, word):
    run(SleepMacTool, make_ctx(config, paths, db, sink))
    assert power.cancel_on_request(word, paths=paths) == power.CANCELLED_TEXT


def test_sleep_mac_in_english(commands, fake_timers, config, paths, db, sink):
    i18n.set_language("en")
    tool = SleepMacTool(make_ctx(config, paths, db, sink, channel="telegram"))
    assert tool.confirm_preview(tool.parse_args({"delay_s": 30})) == 'pmset sleepnow (in 30 seconds; "cancel" calls it off)'
    result = tool.run(tool.parse_args({}))
    assert result.content.startswith(
        'Putting the Mac to sleep in 10 seconds. Once it is asleep you can\'t wake me from Telegram. Say "cancel" or "stop" to call it off.'
    )
    assert power.cancel_on_request("iptal", paths=paths) == "OK, sleep cancelled; the Mac stays on."
    run(SleepMacTool, make_ctx(config, paths, db, sink))
    assert power.cancel_on_request("cancel", paths=paths, language="tr") == "Tamam, uyutmayı iptal ettim; Mac açık kalıyor."


def test_a_second_sleep_mac_replaces_the_first_countdown(commands, fake_timers, config, paths, db, sink):
    ctx = make_ctx(config, paths, db, sink)
    run(SleepMacTool, ctx, delay_s=60)
    run(SleepMacTool, ctx, delay_s=5)
    assert fake_timers[0].cancelled and fake_timers[1].started and not fake_timers[1].cancelled
    assert power.pending_countdown().delay_s == 5


def test_sleep_failure_is_logged_not_raised(monkeypatch, fake_timers, config, paths, db, sink, logs):
    fake = FakeCommands(outputs(), fail={CMD_SLEEP})
    monkeypatch.setattr(power, "run_argv", fake)
    run(SleepMacTool, make_ctx(config, paths, db, sink))
    fake_timers[-1].fire()
    assert any(a.get("decision") == "sleep_failed" for a in audit_lines(logs))


def test_prepare_for_sleep_closes_the_db_and_flushes_logs(monkeypatch, db):
    flushed = []
    monkeypatch.setattr(logging_setup, "flush", lambda: flushed.append(True))
    power.prepare_for_sleep(db)
    assert db.closed and flushed == [True]
    power.prepare_for_sleep(None)  # no db: only the flush
    assert flushed == [True, True]


# --- scheduled sleep -------------------------------------------------------------------------------


def test_schedule_sleep_writes_the_sleep_and_the_notice(frozen, scheduler_installed, config, paths, db, sink):
    tool = ScheduleSleepTool(make_ctx(config, paths, db, sink))
    assert tool.risk is RiskLevel.CONFIRM
    assert "22:30" in tool.confirm_preview(tool.parse_args({"in_minutes": 30}))
    assert tool.confirm_preview(tool.parse_args({"in_minutes": 30})).startswith("Mac 2026-10-18 22:30 uyuyacak")
    result = tool.run(tool.parse_args({"in_minutes": 30}))  # for the model: English
    assert "sleeps at 2026-10-18 22:30 (in 30 min 0 s)" in result.content and "22:29" in result.content
    rows = TimerStore(db).pending()
    assert [(r.kind, r.fire_at) for r in rows] == [
        (KIND_SLEEP_NOTICE, NOW + timedelta(minutes=29)),
        (KIND_SLEEP, NOW + timedelta(minutes=30)),
    ]
    assert "1 dakika" in rows[0].label and "22:30" in rows[0].label
    # a second schedule replaces the first (one at a time)
    result = tool.run(tool.parse_args({"at": "23:00"}))
    assert "The earlier plan" in result.content
    rows = TimerStore(db).pending()
    assert [r.fire_at for r in rows] == [NOW.replace(hour=22, minute=59), NOW.replace(hour=23, minute=0)]
    with pytest.raises(ToolValidationError):
        tool.parse_args({})
    with pytest.raises(ToolValidationError):
        tool.parse_args({"in_minutes": 1})
    with pytest.raises(ToolValidationError, match="minutes ahead"):
        tool.run(tool.parse_args({"at": (NOW + timedelta(seconds=70)).strftime("%Y-%m-%d %H:%M")}))


def test_schedule_sleep_warns_without_the_scheduler(frozen, monkeypatch, config, paths, db, sink):
    monkeypatch.setattr(launchd, "status", lambda: SchedulerStatus(installed=False, running=False, detail="not installed"))
    result = run(ScheduleSleepTool, make_ctx(config, paths, db, sink), in_minutes=10)
    assert power.SCHEDULER_WARNING in result.content and sink.notices == [power.SCHEDULER_WARNING]
    assert power.SCHEDULER_WARNING.startswith("Uyarı: arka plan zamanlayıcısı kurulu değil")


def test_scheduler_fires_the_notice_then_sleeps(frozen, scheduler_installed, config, paths, db, sink):
    run(ScheduleSleepTool, make_ctx(config, paths, db, sink), in_minutes=30)
    store = TimerStore(db)
    plain, both = RecordingNotifier(), RecordingNotifier()
    slept = []
    # a minute before: the notice goes through the Mac + Telegram notifier, nothing sleeps
    fired = fire_due(store, plain, now=NOW + timedelta(minutes=29), sleep_notifier=both, sleep_action=lambda: slept.append(True))
    assert [f.kind for f in fired] == [KIND_SLEEP_NOTICE] and fired[0].notified and not fired[0].slept
    assert both.sent == [(NOTIFICATION_TITLE, power.NOTICE_TEXT.format(at="22:30"))] and plain.sent == []
    assert slept == []
    # at the time: notify, then sleep
    fired = fire_due(store, plain, now=NOW + timedelta(minutes=30), sleep_notifier=both, sleep_action=lambda: slept.append(True))
    assert [f.kind for f in fired] == [KIND_SLEEP] and fired[0].slept and fired[0].message == power.SLEEP_NOW_TEXT
    assert both.sent[-1] == (NOTIFICATION_TITLE, power.SLEEP_NOW_TEXT) and slept == [True]
    assert store.pending() == []


def test_scheduler_skips_a_late_sleep(frozen, scheduler_installed, config, paths, db, sink):
    run(ScheduleSleepTool, make_ctx(config, paths, db, sink), in_minutes=30)
    store = TimerStore(db)
    both = RecordingNotifier()
    slept = []
    fired = fire_due(
        store,
        RecordingNotifier(),
        now=NOW + timedelta(minutes=40),
        late_after_s=120,
        sleep_notifier=both,
        sleep_action=lambda: slept.append(True),
    )
    kinds = {f.kind: f for f in fired}
    assert kinds[KIND_SLEEP].late and not kinds[KIND_SLEEP].slept and slept == []
    assert kinds[KIND_SLEEP].message == power.SLEEP_SKIPPED_TEXT.format(at="22:30")
    assert (NOTIFICATION_TITLE, power.SLEEP_SKIPPED_TEXT.format(at="22:30")) in both.sent


def test_scheduler_sleep_action_default_runs_pmset(commands, frozen, scheduler_installed, config, paths, db, sink, monkeypatch):
    flushed = []
    monkeypatch.setattr(logging_setup, "flush", lambda: flushed.append(True))
    run(ScheduleSleepTool, make_ctx(config, paths, db, sink), in_minutes=30)
    store = TimerStore(db)
    fired = fire_due(store, RecordingNotifier(), now=NOW + timedelta(minutes=30), sleep_notifier=RecordingNotifier())
    assert [f.kind for f in fired] == [KIND_SLEEP_NOTICE, KIND_SLEEP]  # both due in the same tick: notice first
    assert fired[1].slept and commands.of(*CMD_SLEEP) == [list(CMD_SLEEP)] and flushed == [True]
    assert db.kv_get("x") is None  # the store's connection was closed and reopened


def test_cancel_scheduled_sleep_tool(frozen, scheduler_installed, fake_timers, commands, config, paths, db, sink, logs):
    ctx = make_ctx(config, paths, db, sink)
    tool = CancelScheduledSleepTool(ctx)
    assert tool.risk is RiskLevel.SAFE
    assert tool.run(tool.parse_args({})).content == "No sleep is scheduled or pending."
    run(ScheduleSleepTool, ctx, in_minutes=30)
    run(SleepMacTool, ctx)
    result = tool.run(tool.parse_args({}))
    assert (
        "the pending sleep countdown was cancelled" in result.content
        and "sleep scheduled for 2026-10-18 22:30 was cancelled" in result.content
    )
    assert TimerStore(db).pending() == [] and fake_timers[-1].cancelled


def test_iptal_cancels_a_scheduled_sleep_only_once_the_notice_is_out(frozen, scheduler_installed, config, paths, db, sink, logs):
    run(ScheduleSleepTool, make_ctx(config, paths, db, sink), in_minutes=30)
    # 20 minutes early: "iptal" is about something else
    assert power.cancel_on_request("iptal", db=db, now=NOW + timedelta(minutes=10)) is None
    assert len(TimerStore(db).pending()) == 2
    # the notice is out (fire_at within 90 s): cancelled, both rows
    assert power.cancel_on_request("dur", db=db, now=NOW + timedelta(minutes=29, seconds=10)) == power.SCHEDULED_CANCELLED_TEXT.format(
        at="22:30"
    )
    assert TimerStore(db).pending() == []
    # with `paths`, a connection of its own is opened and closed
    run(ScheduleSleepTool, make_ctx(config, paths, db, sink), in_minutes=5)
    assert power.cancel_on_request("iptal", paths=paths, now=NOW + timedelta(minutes=4, seconds=30)) is not None
    assert TimerStore(db).pending() == []
    assert any(a.get("decision") == "cancelled_by_word" for a in audit_lines(logs))


# --- no sudo, doctor, registry ---------------------------------------------------------------------


def test_no_sudo_anywhere():
    source = Path(power.__file__).read_text(encoding="utf-8")
    assert '["sudo"' not in source and "'sudo'" not in source  # no argv literal starts with sudo
    assert "shell=True" not in source and "os.system" not in source
    with pytest.raises(ToolExecutionError, match="sudo"):
        power.run_argv(["sudo", "pmset", "sleepnow"])


def test_run_argv_never_uses_a_shell(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    power.run_argv(["pmset", "-g", "batt"])
    assert calls[0][0] == ["pmset", "-g", "batt"] and not calls[0][1].get("shell")


def test_health_checks(commands, config, paths, monkeypatch):
    monkeypatch.setattr(power, "lock_available", lambda: True)
    checks = {c.name: c for c in health_checks(config, paths)}
    assert checks["Mac power (pmset)"].ok and "without sudo" in checks["Mac power (pmset)"].detail
    assert checks["Mac lock (login framework)"].ok
    assert checks["Scheduled sleep"].ok and checks["Scheduled sleep"].detail == "none scheduled"
    assert "jarvis.tools.power" in INTEGRATION_MODULES


def test_health_checks_report_a_broken_pmset(monkeypatch, config, paths):
    monkeypatch.setattr(power, "run_argv", FakeCommands(outputs(), fail={CMD_BATT}))
    monkeypatch.setattr(power, "lock_available", lambda: False)
    checks = {c.name: c for c in health_checks(config, paths)}
    assert not checks["Mac power (pmset)"].ok
    assert checks["Mac lock (login framework)"].warn and "displaysleepnow" in checks["Mac lock (login framework)"].detail


def test_format_status_handles_an_empty_status():
    assert format_status(MacStatus(), NOW, "tr") == "Güç: bilinmiyor"
    assert format_status(MacStatus(), NOW) == "Power: unknown"  # the main language (English outside the `config` fixture)
