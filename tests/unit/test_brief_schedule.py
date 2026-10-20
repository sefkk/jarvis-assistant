"""The automatic morning brief (scheduler/brief.py, `[brief.schedule]`): the schedule, the channel choice,
once per day, the skips, late delivery, the content, the config tool, the CLI and Telegram's /brief.
Time, the probes, the model and Telegram are all faked."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jarvis.config import load_config, update_local_config
from jarvis.exceptions import ConfigError, JarvisError
from jarvis.scheduler import brief as sb
from jarvis.scheduler.brief import Probes, choose_channels, decide, next_run, slot_for
from jarvis.scheduler.store import KIND_ALARM, KIND_TIMER, TimerStore
from jarvis.tools import brief as brief_mod
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.brief import SetBriefSchedule, build_brief
from jarvis.utils.calendar_types import CalendarEvent
from jarvis.utils.timeutil import local_tz

TZ = local_tz()
MON = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)  # a Monday
SAT = datetime(2026, 10, 24, 8, 0, tzinfo=TZ)  # a Saturday


def at(base: datetime, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return base.replace(hour=hour, minute=minute)


def active_probes(*, voice=True, paused=False, dnd=False, idle=30.0, locked=False) -> Probes:
    return Probes(voice_running=lambda: voice, paused=lambda: paused, dnd=lambda: dnd, idle_and_lock=lambda: (idle, locked))


class RecordingTelegram:
    def __init__(self, blocked: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.blocked = blocked

    def blocked_reason(self):
        return self.blocked

    def notify(self, title: str, message: str) -> None:
        self.sent.append((title, message))


class FakeAssistant:
    """Records the prompt; the reply is the fixed summary."""

    prompts: list[str] = []

    def __init__(self, reply: str = "Günaydın. Bugün 2 etkinlik var.", error: str | None = None) -> None:
        self.reply, self.error = reply, error

    def __call__(self, config, paths):
        return self

    def handle(self, text, sink, *, untrusted=None):
        FakeAssistant.prompts.append(text)
        sink.write_delta(self.reply)
        sink.finish()
        return SimpleNamespace(reply=self.reply, error=self.error)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh():
    FakeAssistant.prompts = []


@pytest.fixture
def cfg(config):
    config.brief.schedule.enabled = True
    return config.brief.schedule


def tick(config, paths, db, *, now, probes=None, telegram=None, assistant=None):
    return sb.tick(
        config,
        paths,
        db,
        None,
        now=now,
        probes=probes or active_probes(),
        telegram=telegram or RecordingTelegram(),
        assistant_factory=assistant or FakeAssistant(),
        threaded=False,
    )


def record_daily_brief_call(db, created_at_utc: datetime) -> None:
    db.start_session("s1", "m", "d")
    db.start_turn("t1", "s1", "günün özeti")
    db.conn.execute(
        "INSERT INTO tool_calls (turn_id, tool, args_json, risk, decision, duration_ms, error, is_delegate, created_at)"
        " VALUES ('t1', 'daily_brief', '{}', 'safe', 'run', 1, NULL, 0, ?)",
        (created_at_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),),
    )


# --- the schedule --------------------------------------------------------------------------------------------


def test_weekday_and_weekend_slots_and_off(cfg):
    assert slot_for(cfg, MON.date()) == at(MON, "09:00")
    assert slot_for(cfg, SAT.date()) == at(SAT, "10:30")
    cfg.weekend_time = ""
    assert slot_for(cfg, SAT.date()) is None and slot_for(cfg, MON.date()) == at(MON, "09:00")


def test_next_run_today_until_late_until_then_tomorrow_and_skips_handled_day(cfg):
    assert next_run(cfg, at(MON, "08:00")) == at(MON, "09:00")
    assert next_run(cfg, at(MON, "11:00")) == at(MON, "09:00")  # still deliverable (late)
    assert next_run(cfg, at(MON, "12:01")) == at(MON, "09:00") + timedelta(days=1)
    assert next_run(cfg, at(MON, "08:00"), handled_day=MON.date()) == at(MON, "09:00") + timedelta(days=1)
    cfg.weekend_time = "off"
    friday = at(MON, "10:00") + timedelta(days=4)
    assert next_run(cfg, friday, handled_day=friday.date()) == at(MON, "09:00") + timedelta(days=7)  # the weekend is skipped
    cfg.enabled = False
    assert next_run(cfg, MON) is None


def test_config_validation_normalizes_times_and_rejects_junk(paths):
    update_local_config(paths.config_file, "brief.schedule", {"weekday_time": "7:30", "weekend_time": "off", "late_until": "11:05"})
    sched = load_config(paths.config_file).brief.schedule
    assert (sched.weekday_time, sched.weekend_time, sched.late_until) == ("07:30", "", "11:05")
    with pytest.raises(ConfigError, match="weekday_time"):
        update_local_config(paths.config_file, "brief.schedule", {"weekday_time": "sabah"})
    assert load_config(paths.config_file).brief.schedule.weekday_time == "07:30"  # restored


# --- the decision ------------------------------------------------------------------------------------------


def test_decide_waits_before_the_slot_delivers_at_it_and_marks_late(cfg):
    assert decide(cfg, at(MON, "08:59"), state=None, asked=False)[0] == "wait"
    assert decide(cfg, at(MON, "09:00"), state=None, asked=False) == ("deliver", "on time")
    assert decide(cfg, at(MON, "09:01"), state=None, asked=False)[0] == "deliver"
    assert decide(cfg, at(MON, "10:30"), state=None, asked=False) == ("deliver_late", "late")
    assert decide(cfg, at(MON, "12:00"), state=None, asked=False)[0] == "deliver_late"
    assert decide(cfg, at(MON, "12:01"), state=None, asked=False)[0] == "skip"
    assert decide(cfg, at(MON, "10:00"), state=None, asked=True) == ("skip", "a brief was already asked for this morning")
    handled = sb.AutoState(day="2026-10-19", outcome="voice")
    assert decide(cfg, at(MON, "10:00"), state=handled, asked=False)[0] == "wait"
    cfg.enabled = False
    assert decide(cfg, at(MON, "10:00"), state=None, asked=False)[0] == "wait"


# --- the channel --------------------------------------------------------------------------------------------


def test_auto_channel_speaks_only_when_active_and_allowed(cfg):
    assert choose_channels(cfg, active_probes()) == (["voice"], "active at the Mac (30 s ago)")
    assert choose_channels(cfg, active_probes(idle=20 * 60))[0] == ["telegram"]
    assert choose_channels(cfg, active_probes(locked=True)) == (["telegram"], "the screen is locked")
    assert choose_channels(cfg, active_probes(dnd=True)) == (["telegram"], "Do Not Disturb is on")
    assert choose_channels(cfg, active_probes(paused=True)) == (["telegram"], "voice listening is paused")
    assert choose_channels(cfg, active_probes(voice=False)) == (["telegram"], "the voice agent is not running")
    assert choose_channels(cfg, active_probes(idle=None))[0] == ["telegram"]
    cfg.active_within_min = 30
    assert choose_channels(cfg, active_probes(idle=20 * 60))[0] == ["voice"]


def test_forced_channels_still_never_speak_while_dnd_or_paused(cfg):
    cfg.channel = "voice"
    assert choose_channels(cfg, active_probes(idle=3 * 3600)) == (["voice"], "channel = voice")  # idle does not matter
    assert choose_channels(cfg, active_probes(dnd=True))[0] == ["telegram"]
    assert choose_channels(cfg, active_probes(paused=True))[0] == ["telegram"]
    cfg.channel = "both"
    assert choose_channels(cfg, active_probes())[0] == ["voice", "telegram"]
    assert choose_channels(cfg, active_probes(dnd=True))[0] == ["telegram"]
    cfg.channel = "telegram"
    assert choose_channels(cfg, active_probes()) == (["telegram"], "channel = telegram")


def test_dnd_file_parsing(tmp_path):
    path = tmp_path / "Assertions.json"
    path.write_text('{"data": [{"storeAssertionRecords": [{"assertionDetails": {}}]}]}')
    assert sb.dnd_active(path) is True
    path.write_text('{"data": [{"storeAssertionRecords": []}]}')
    assert sb.dnd_active(path) is False
    path.write_text("not json")
    assert sb.dnd_active(path) is None
    assert sb.dnd_active(tmp_path / "missing.json") is None


# --- the tick: once per day, skips, late, fallback -----------------------------------------------------------


def test_tick_delivers_once_per_day_by_voice_when_active(config, paths, db, cfg):
    assert tick(config, paths, db, now=at(MON, "08:50")) is None
    assert not paths.voice_announce_file.exists()
    telegram = RecordingTelegram()
    assert tick(config, paths, db, now=at(MON, "09:00"), telegram=telegram) == "voice"
    from jarvis.voice import announce

    ann = announce.take(paths.voice_announce_file)
    assert ann is not None and ann.kind == "turn" and ann.source == "brief" and "at most six short sentences" in ann.text
    assert "(late" not in ann.text and telegram.sent == [] and FakeAssistant.prompts == []
    state = sb.load_state(db)
    assert state is not None and state.day == "2026-10-19" and state.outcome == "voice"
    assert tick(config, paths, db, now=at(MON, "09:00"), telegram=telegram) is None  # same tick again: nothing
    assert tick(config, paths, db, now=at(MON, "11:00"), telegram=telegram) is None  # later that day: nothing
    assert not paths.voice_announce_file.exists() and telegram.sent == []
    assert tick(config, paths, db, now=at(MON, "09:00") + timedelta(days=1), telegram=telegram) == "voice"  # the next day


def test_tick_sends_telegram_when_idle_with_the_compact_prompt(config, paths, db, cfg):
    telegram = RecordingTelegram()
    assistant = FakeAssistant(reply="• 10:00 Standup (Google)\n• 18 °C, güneşli")
    assert (
        tick(config, paths, db, now=at(MON, "09:00"), probes=active_probes(idle=3600), telegram=telegram, assistant=assistant) == "telegram"
    )
    assert telegram.sent == [("Günün özeti", "• 10:00 Standup (Google)\n• 18 °C, güneşli")]
    assert len(FakeAssistant.prompts) == 1 and "compact list" in FakeAssistant.prompts[0] and "Fenerbahçe" in FakeAssistant.prompts[0]
    assert "(tr)" in FakeAssistant.prompts[0] and "(late" not in FakeAssistant.prompts[0]
    assert not paths.voice_announce_file.exists()
    assert sb.load_state(db).outcome == "telegram"
    from jarvis import i18n

    i18n.set_language("en")
    english = RecordingTelegram()
    sb.send_telegram(config, paths, assistant_factory=FakeAssistant(), notifier=english)
    assert english.sent[0][0] == "Today's brief" == sb.TITLE


def test_tick_skips_when_a_brief_was_asked_this_morning(config, paths, db, cfg):
    record_daily_brief_call(db, at(MON, "07:45").astimezone(timezone.utc))
    telegram = RecordingTelegram()
    assert tick(config, paths, db, now=at(MON, "09:00"), telegram=telegram) == "skipped: a brief was already asked for this morning"
    assert telegram.sent == [] and not paths.voice_announce_file.exists()
    assert tick(config, paths, db, now=at(MON, "09:30"), telegram=telegram) is None  # the day is handled


def test_a_brief_asked_yesterday_does_not_count(config, paths, db, cfg):
    record_daily_brief_call(db, at(MON, "23:30").astimezone(timezone.utc) - timedelta(days=1))
    assert tick(config, paths, db, now=at(MON, "09:00")) == "voice"


def test_late_delivery_after_wake_and_skip_after_late_until(config, paths, db, cfg):
    telegram = RecordingTelegram()
    # the Mac slept through 09:00 and wakes at 10:40: delivered, marked late
    assert tick(config, paths, db, now=at(MON, "10:40"), probes=active_probes(idle=3600), telegram=telegram) == "telegram"
    assert "(late" in FakeAssistant.prompts[0] and "asleep" in FakeAssistant.prompts[0]
    assert telegram.sent and sb.load_state(db).outcome == "telegram"
    # the next day it wakes at 12:30: too late, skipped and remembered
    telegram.sent.clear()
    assert (
        tick(config, paths, db, now=at(MON, "12:30") + timedelta(days=1), telegram=telegram) == "skipped: missed: past late_until (12:00)"
    )
    assert telegram.sent == [] and tick(config, paths, db, now=at(MON, "13:00") + timedelta(days=1), telegram=telegram) is None


def test_weekend_uses_the_weekend_time_and_off_means_no_brief(config, paths, db, cfg):
    assert tick(config, paths, db, now=at(SAT, "09:30")) is None
    assert tick(config, paths, db, now=at(SAT, "10:30")) == "voice"
    cfg.weekend_time = ""
    assert tick(config, paths, db, now=at(SAT, "10:30") + timedelta(days=1)) is None
    assert sb.load_state(db).day == "2026-10-24"


def test_spoken_brief_not_taken_in_time_goes_to_telegram(config, paths, db, cfg):
    import os

    telegram = RecordingTelegram()
    assert tick(config, paths, db, now=at(MON, "09:00"), telegram=telegram) == "voice"
    written = at(MON, "09:00").timestamp()  # the request file's mtime, on the test's clock
    os.utime(paths.voice_announce_file, (written, written))
    assert tick(config, paths, db, now=at(MON, "09:02"), telegram=telegram) is None  # still waiting for the service
    assert tick(config, paths, db, now=at(MON, "09:05"), telegram=telegram) == "telegram"
    assert not paths.voice_announce_file.exists() and len(telegram.sent) == 1 and "(late" in FakeAssistant.prompts[0]
    assert sb.load_state(db).outcome == "telegram"


def test_failed_delivery_is_recorded_and_not_retried_that_day(config, paths, db, cfg, caplog):
    telegram = RecordingTelegram(blocked="the kill switch (/stop) is active")
    with caplog.at_level("ERROR", logger="jarvis.scheduler.brief"):
        outcome = tick(config, paths, db, now=at(MON, "09:00"), probes=active_probes(voice=False), telegram=telegram)
    assert outcome.startswith("failed:") and "kill switch" in outcome and telegram.sent == []
    assert tick(config, paths, db, now=at(MON, "09:10"), telegram=RecordingTelegram()) is None


def test_both_channels_and_a_partial_failure(config, paths, db, cfg):
    cfg.channel = "both"
    telegram = RecordingTelegram()
    assert tick(config, paths, db, now=at(MON, "09:00"), telegram=telegram) == "voice+telegram"
    assert paths.voice_announce_file.exists() and len(telegram.sent) == 1
    paths.voice_announce_file.unlink()
    assert tick(config, paths, db, now=at(MON, "09:00") + timedelta(days=1), telegram=RecordingTelegram(blocked="off")) == "voice"


def test_disabled_schedule_never_delivers_and_never_reads_tool_calls(config, paths, db, cfg, monkeypatch):
    cfg.enabled = False
    monkeypatch.setattr(sb, "asked_today", lambda db, now: (_ for _ in ()).throw(AssertionError("not read")))
    assert tick(config, paths, db, now=at(MON, "09:00")) is None and sb.load_state(db) is None


def test_tick_follows_the_config_the_scheduler_hands_it(config, paths, db, cfg):
    """The reload itself lives in the scheduler loop (test_config_watch.py); the brief job only has to use the
    config of the current tick, so a new time moves the next run without a restart."""
    assert tick(config, paths, db, now=at(MON, "07:35")) is None
    update_local_config(paths.config_file, "brief.schedule", {"weekday_time": "07:30"})
    reloaded = load_config(paths.config_file)
    assert sb.next_run(reloaded.brief.schedule, at(MON, "07:00")) == at(MON, "07:30")
    assert tick(reloaded, paths, db, now=at(MON, "07:36")) == "voice"


def test_threaded_delivery_runs_in_the_background(config, paths, db, cfg):
    import threading

    done = threading.Event()
    telegram = RecordingTelegram()

    class SlowAssistant(FakeAssistant):
        def handle(self, text, sink, *, untrusted=None):
            done.set()
            return super().handle(text, sink, untrusted=untrusted)

    outcome = sb.tick(
        config,
        paths,
        db,
        None,
        now=at(MON, "09:00"),
        probes=active_probes(idle=3600),
        telegram=telegram,
        assistant_factory=SlowAssistant(),
        threaded=True,
    )
    assert outcome == "delivering" and done.wait(5)
    for _ in range(50):
        if sb.load_state(db).outcome == "telegram":
            break
        threading.Event().wait(0.05)
    assert sb.load_state(db).outcome == "telegram" and telegram.sent


def test_summarize_keeps_only_the_text_after_the_last_tool_call(config, paths):
    class Narrating(FakeAssistant):
        def handle(self, text, sink, *, untrusted=None):
            sink.write_delta("Sabah özetini hazırlıyorum.")
            sink.segment_break()
            sink.notice("• daily_brief")
            sink.write_delta("Maça bakıyorum.")
            sink.segment_break()  # web_search runs on the server inside the same response...
            sink.write_delta("Günaydın Kerem. ")
            sink.write_delta("Bugün takvim boş.")
            sink.notice("• web_search (server)")  # ...and its notice only comes after the whole response
            sink.finish()
            return SimpleNamespace(reply="Günaydın Kerem. Bugün takvim boş.", error=None)

    assert sb.summarize(config, paths, "x", assistant_factory=Narrating()) == "Günaydın Kerem. Bugün takvim boş."
    sink = sb.QuietSink()
    sink.write_delta("only commentary")
    sink.segment_break()
    assert sink.final_text() == "only commentary"  # nothing after the last tool: keep what there is
    assert sink.confirm(None) is False


def test_summarize_raises_on_an_empty_or_failed_turn(config, paths):
    with pytest.raises(JarvisError, match="no text"):
        sb.summarize(config, paths, "x", assistant_factory=FakeAssistant(reply="  "))
    with pytest.raises(JarvisError, match="boom"):
        sb.summarize(config, paths, "x", assistant_factory=FakeAssistant(error="boom"))


# --- the content (tools/brief.py) --------------------------------------------------------------------------


class FakeTool:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, []

    def parse_args(self, raw):
        return raw

    def run(self, args):
        self.calls.append(args)
        return SimpleNamespace(content=self.text)


class FakeRegistry:
    def __init__(self, **tools) -> None:
        self.tools = tools

    def get(self, name):
        return self.tools.get(name)


def event(title: str, day: datetime, hour: int, *, source: str = "apple", calendar: str = "Futbol") -> CalendarEvent:
    start = day.replace(hour=hour, minute=0)
    return CalendarEvent(source=source, calendar=calendar, title=title, start=start, end=start + timedelta(hours=2))


def test_brief_content_has_the_filtered_calendar_current_location_weather_and_todays_alarms(config, paths, db, monkeypatch):
    """The agenda comes through `merge_calendars` (the Futbol filter applies there: other teams' matches are
    hidden, a note says so); the weather tool is called without a location, so it uses the Current Location
    with the home fallback; alarms and timers of the day come from the scheduler table; yesterday's match is flagged."""
    import jarvis.football.client as fc

    monkeypatch.setattr(fc.FootballClient, "_request", lambda self, path, params: (_ for _ in ()).throw(AssertionError("no API")))
    today = MON
    yesterday = MON - timedelta(days=1)

    def merged(cfg, start, end, include_filtered=False):
        assert include_filtered is False
        if start.date() == today.date():
            return [event("Fenerbahçe - Beşiktaş", today, 20), event("Standup", today, 10, source="google", calendar="Work")], {
                "Futbol filter": "2 events hidden (Galatasaray - Rizespor, Trabzonspor - Kasımpaşa)"
            }
        return [
            event("Dayılara gidiş - Fenerbahçe", yesterday, 12, source="google", calendar="Kişisel"),
            event("Göztepe - Fenerbahçe", yesterday, 19),
        ], {}

    monkeypatch.setattr(brief_mod, "merge_calendars", merged)
    store = TimerStore(db)
    store.add("Kalk", today.replace(hour=7, minute=0), KIND_ALARM)
    store.add("çay", today.replace(hour=16, minute=30), KIND_TIMER)
    store.add("yarınki", today.replace(hour=7, minute=0) + timedelta(days=1), KIND_ALARM)
    weather = FakeTool("Current Location (Kadıköy): now 18 °C, sunny")
    reminders = FakeTool("- Fatura öde (18:00)")
    runtime = Runtime(
        client=None, db=db, usage_tracker=None, registry=FakeRegistry(get_weather=weather, list_reminders=reminders), budget=None
    )
    text = build_brief(config, paths, runtime, today.date().isoformat()).as_text()
    assert "Standup" in text and "Fenerbahçe - Beşiktaş" in text and "Galatasaray" not in text.split("== Notes ==")[0]
    assert "- Futbol filter calendar: 2 events hidden" in text
    assert (
        len(weather.calls) == 1 and "location" not in weather.calls[0]
    )  # no location: the tool's default is the Current Location, then home
    assert "== Weather ==\nCurrent Location (Kadıköy)" in text
    assert "== Reminders due ==\n- Fatura öde (18:00)" in text
    assert "== Alarms and timers ==\nalarm 07:00: Kalk\ntimer 16:30: çay" in text and "yarınki" not in text
    assert "Match day (from the apple calendar): at 20:00: Fenerbahçe - Beşiktaş" in text
    assert "Yesterday's match (from the apple calendar): Göztepe - Fenerbahçe. Look up the result with web_search." in text


def test_yesterdays_match_needs_the_football_calendar_or_a_fixture_title(config):
    from jarvis.tools.brief import football_calendars, match_of_the_day, yesterdays_match

    assert football_calendars(config) == {"futbol"}
    coffee = event("Fenerbahçe'de kahve", MON, 15, source="google", calendar="Kişisel")
    trip = event("Dayılara gidiş - Fenerbahçe", MON, 12, source="google", calendar="Kişisel")
    match = event("Fenerbahçe - Beşiktaş", MON, 20)
    assert yesterdays_match("Fenerbahçe", [coffee, trip], {"futbol"}) is None
    assert yesterdays_match("Fenerbahçe", [trip, match], {"futbol"}) is match
    assert yesterdays_match("Fenerbahçe", [coffee, match], set()) is match  # no football calendar configured: fixture-shaped titles
    assert yesterdays_match("Fenerbahçe", [coffee], set()) is None
    # today's detection still counts any mention, but the football calendar's event wins
    assert match_of_the_day("Fenerbahçe", [trip, match], None, prefer_calendars={"futbol"}) == (match, None)
    assert match_of_the_day("Fenerbahçe", [trip, match], None) == (trip, None)


def test_brief_without_a_db_or_yesterday_match_has_no_timer_lines(config, paths, monkeypatch):
    monkeypatch.setattr(brief_mod, "merge_calendars", lambda cfg, start, end: ([], {}))
    text = build_brief(config, paths, None, "2026-10-19").as_text()
    assert "== Alarms and timers ==\nNone." in text and "Yesterday's match" not in text


# --- the tool, the CLI and Telegram ----------------------------------------------------------------------------


def test_set_brief_schedule_tool_shows_and_changes_the_schedule(config, paths, db):
    assert SetBriefSchedule.risk == RiskLevel.SAFE and SetBriefSchedule.delegate_ok is False
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None)
    tool = SetBriefSchedule(ctx)
    shown = tool.run(tool.parse_args({})).content
    assert shown.startswith("Morning brief schedule: on; weekdays 09:00, weekends 10:30; channel auto")
    changed = tool.run(tool.parse_args({"weekday_time": "7:30", "weekend_time": "off"})).content
    assert changed.startswith("Morning brief schedule updated") and "weekdays 07:30, weekends off" in changed
    sched = load_config(paths.config_file).brief.schedule
    assert (sched.weekday_time, sched.weekend_time) == ("07:30", "")
    tool.run(tool.parse_args({"enabled": False, "channel": "telegram", "late_until": "11:00"}))
    sched = load_config(paths.config_file).brief.schedule
    assert (sched.enabled, sched.channel, sched.late_until) == (False, "telegram", "11:00")
    text = paths.local_config_file.read_text(encoding="utf-8")
    assert text.count("[brief.schedule]") == 1 and 'weekday_time = "07:30"' in text and 'weekend_time = "off"' in text
    from jarvis.exceptions import ToolValidationError

    with pytest.raises(ToolValidationError, match="HH:MM"):
        tool.run(tool.parse_args({"weekday_time": "sabah"}))


def test_update_local_config_keeps_other_sections_and_comments(paths):
    paths.local_config_file.write_text(
        "[presence]\nhome_gateway_macs = []  # mine\n\n[brief.schedule]\nenabled = false  # off for now\n", encoding="utf-8"
    )
    update_local_config(paths.config_file, "brief.schedule", {"enabled": True, "weekday_time": "08:00"})
    text = paths.local_config_file.read_text(encoding="utf-8")
    assert "[presence]\nhome_gateway_macs = []  # mine\n" in text
    assert 'enabled = true  # off for now\nweekday_time = "08:00"\n' in text
    cfg = load_config(paths.config_file)
    assert cfg.brief.schedule.enabled and cfg.brief.schedule.weekday_time == "08:00"


def test_cli_brief_schedule_shows_and_toggles(config, paths, monkeypatch, capsys):
    from jarvis import main as main_mod
    from jarvis.scheduler import launchd

    monkeypatch.setenv("JARVIS_HOME", str(paths.home))
    monkeypatch.setattr(launchd, "status", lambda: SimpleNamespace(installed=True, running=True, detail="running"))
    assert main_mod.main(["brief", "schedule"]) == 0
    out = capsys.readouterr().out
    assert "sabah brifingi: on; weekdays 09:00, weekends 10:30; channel auto; late until 12:00; next " in out
    assert main_mod.main(["brief", "schedule", "off"]) == 0
    out = capsys.readouterr().out
    assert "enabled = False config.local.toml dosyasına yazıldı" in out and "sabah brifingi: off;" in out  # Turkish test config
    assert load_config(paths.config_file).brief.schedule.enabled is False
    assert main_mod.main(["brief", "schedule", "on"]) == 0 and load_config(paths.config_file).brief.schedule.enabled is True
    monkeypatch.setattr(launchd, "status", lambda: SimpleNamespace(installed=False, running=False, detail="not installed"))
    assert main_mod.main(["brief", "schedule"]) == 1 and "zamanlayıcı ajanı çalışmıyor" in capsys.readouterr().out


def test_cli_send_voice_and_telegram(config, paths, monkeypatch, capsys):
    from jarvis import main as main_mod
    from jarvis.presence import hooks
    from jarvis.voice import announce
    from jarvis.voice import pause as vpause

    monkeypatch.setenv("JARVIS_HOME", str(paths.home))
    monkeypatch.setattr(hooks, "voice_agent_running", lambda: True)
    monkeypatch.setattr(sb, "dnd_active", lambda path=None: False)
    vpause.pause(paths.voice_pause_file, by="cli")
    assert main_mod.main(["brief", "--send", "voice"]) == 1 and "voice listening is paused" in capsys.readouterr().out
    vpause.resume(paths.voice_pause_file)
    assert main_mod.main(["brief", "--send", "voice"]) == 0
    assert announce.take(paths.voice_announce_file).source == "brief"
    sent = []
    monkeypatch.setattr(sb, "send_telegram", lambda config, paths, **kw: sent.append(kw) or "• özet")
    assert main_mod.main(["brief", "--send", "telegram"]) == 0 and "• özet" in capsys.readouterr().out and sent == [{}]


def test_telegram_brief_command_runs_the_on_demand_prompt(config, paths, logs):
    from tests.unit.test_telegram_bot import FakeAssistant as BotAssistant
    from tests.unit.test_telegram_bot import make_bot, message_update

    assistant = BotAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.append([message_update("/brief")])
    bot.run()
    assert len(assistant.calls) == 1 and "daily brief" in assistant.calls[0][0] and "compact list" in assistant.calls[0][0]
    assert any("echo:" in text for text in api.sent())
    from jarvis.interfaces.telegram_bot import help_text

    assert "/brief" in help_text(config)
    assert '"reason": "brief_command"' in (logs / "audit.jsonl").read_text()


def test_brief_prompts_greet_for_the_time_end_with_the_last_item_and_cover_the_mail(config):
    """The 2026-10-19 brief: "Günaydın" at 22:27 and a closing quip nobody asked for; mail is now part of it."""
    for channel in ("voice", "telegram"):
        prompt = sb.prompt_for(channel, config)
        assert "fits the local time" in prompt and "good morning only in the morning" in prompt
        assert "no closing remark, joke or advice" in prompt
        assert "how many emails came in today with the important ones named" in prompt
        assert "or at home when the brief says so" in prompt
    assert "no closing remark" in sb.ON_DEMAND_PROMPT and "emails" in sb.ON_DEMAND_PROMPT
