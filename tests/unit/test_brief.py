"""Daily brief assembly: agenda, weather, reminders and the match of the day (calendar, then
reminders, then a web-search hint; never the football API), each part guarded."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.tools import brief as brief_mod
from jarvis.tools.base import RiskLevel, Runtime, ToolContext, ToolResult
from jarvis.tools.brief import WEB_SEARCH_HINT, BriefData, DailyBrief, build_brief, match_of_the_day, mentions_team
from jarvis.utils.calendar_types import CalendarEvent
from jarvis.utils.timeutil import local_tz


class FakeTool:
    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.text, self.error, self.calls = text, error, []

    def parse_args(self, raw):
        return raw

    def run(self, args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return ToolResult(self.text or "")


class FakeRegistry:
    def __init__(self, **tools) -> None:
        self.tools = tools

    def get(self, name):
        return self.tools.get(name)


def event(title: str, day: str, hour: int, source: str = "google", location: str = "", all_day: bool = False) -> CalendarEvent:
    start = datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=local_tz())
    return CalendarEvent(
        source=source, calendar="Work", title=title, start=start, end=start + timedelta(hours=1), location=location, all_day=all_day
    )


@pytest.fixture(autouse=True)
def _calendars(monkeypatch):
    """Calendars are faked per test; the football API must never be touched by the brief."""
    import jarvis.football.client as fc

    def no_network(self, path, params):
        raise AssertionError("the brief must not call the football API")

    monkeypatch.setattr(fc.FootballClient, "_request", no_network)
    monkeypatch.setattr(brief_mod, "merge_calendars", lambda config, start, end: ([], {}))


def runtime_with(db, **tools) -> Runtime:
    return Runtime(client=None, db=db, usage_tracker=None, registry=FakeRegistry(**tools), budget=None)


# --- match-day detection (calendar, then reminders, then web search) ---------------------------


@pytest.mark.parametrize(
    "text, hit",
    [
        ("Fenerbahçe - Galatasaray", True),
        ("FENERBAHCE vs Besiktas", True),
        ("Fener maçı", True),
        ("Standup", False),
        ("FB - GS", False),
        ("Fen", False),
        ("Diş hekimi", False),
        ("", False),
    ],
)
def test_mentions_team(text, hit):
    assert mentions_team(text, "Fenerbahçe") is hit


def test_detection_order_calendar_before_reminders_before_web():
    match = event("Fenerbahçe - Trabzonspor", "2026-10-18", 20, "apple", location="Ülker Stadyumu")
    other = event("Standup", "2026-10-18", 9)
    reminders = "1 reminder(s) due:\n[ ] Fener maçı için bilet al (due 2026-10-18)"
    assert match_of_the_day("Fenerbahçe", [match, other], reminders) == (match, None)
    assert match_of_the_day("Fenerbahçe", [other], reminders) == (None, "[ ] Fener maçı için bilet al (due 2026-10-18)")
    assert match_of_the_day("Fenerbahçe", [other], "No reminders.") == (None, None)
    assert match_of_the_day("Fenerbahçe", [], None) == (None, None)
    # the earliest matching event wins, and the notes count too
    late = event("Fener", "2026-10-18", 21, "apple")
    noted = event("Akşam", "2026-10-18", 19)
    noted.notes = "Fenerbahçe maçı"
    assert match_of_the_day("Fenerbahçe", [late, noted], None) == (noted, None)


# --- assembly ---------------------------------------------------------------------------------------------


def test_brief_on_a_match_day_from_the_calendar(config, paths, db, monkeypatch):
    monkeypatch.setattr(
        brief_mod,
        "merge_calendars",
        lambda config, start, end: (
            [event("Standup", "2026-10-18", 9), event("Fenerbahçe - Galatasaray", "2026-10-18", 19, "apple", location="Ülker Stadyumu")],
            {},
        ),
    )
    weather = FakeTool("Istanbul: now 18°C, clear")
    reminders = FakeTool("1 reminder(s) due 2026-10-18:\n[ ] Buy tickets (due 2026-10-18) (list: Personal)")
    data = build_brief(config, paths, runtime_with(db, get_weather=weather, list_reminders=reminders), "2026-10-18")
    assert data.day.isoformat() == "2026-10-18" and data.failures == {}
    assert [e.title for e in data.agenda] == ["Standup", "Fenerbahçe - Galatasaray"]
    assert data.match_event is not None and data.match_event.title == "Fenerbahçe - Galatasaray" and data.match_reminder is None
    assert reminders.calls == [{"due_range": "2026-10-18"}] and weather.calls and weather.calls[0]["days"] >= 1
    text = data.as_text()
    assert re.match(r"Daily brief for Sun 2026-10-18 \(as of \d\d:\d\d local time\)\n\n== Agenda ==\n", text)
    assert "Standup" in text and "(apple: Work)" in text
    assert "== Weather ==\nIstanbul: now 18°C, clear" in text
    assert "== Reminders due ==\n1 reminder(s) due 2026-10-18:" in text
    assert "== Fenerbahçe ==\nMatch day (from the apple calendar): at 19:00: Fenerbahçe - Galatasaray @ Ülker Stadyumu\n" in text
    assert "web_search if the user wants it" in text and "Not found" not in text and "== Notes ==" not in text


def test_brief_match_from_a_reminder_when_the_calendar_has_none(config, paths, db):
    reminders = FakeTool("1 reminder(s) due 2026-10-18:\n[ ] Fener maçı 20:00 (due 2026-10-18) (list: Personal)")
    data = build_brief(config, paths, runtime_with(db, get_weather=FakeTool("sunny"), list_reminders=reminders), "2026-10-18")
    assert data.match_event is None and data.match_reminder == "[ ] Fener maçı 20:00 (due 2026-10-18) (list: Personal)"
    assert "== Fenerbahçe ==\nA reminder due today mentions Fenerbahçe: [ ] Fener maçı 20:00" in data.as_text()


def test_brief_without_a_match_tells_the_model_to_web_search_and_never_calls_the_api(config, paths, db):
    data = build_brief(
        config, paths, runtime_with(db, get_weather=FakeTool("sunny"), list_reminders=FakeTool("No reminders.")), "2026-03-03"
    )
    assert not data.match_found
    text = data.as_text()
    assert "== Fenerbahçe ==\n" + WEB_SEARCH_HINT.format(team="Fenerbahçe", day_word="on 2026-03-03") in text
    assert "web_search" in text and "Do not use the football.* tools" in text
    assert "== Agenda ==\nNo events." in text and "football" not in data.failures


def test_brief_today_says_today(config, paths, db):
    from jarvis.utils.timeutil import now_local

    data = build_brief(config, paths, runtime_with(db), "today")
    assert data.day == now_local().date() and "plays today" in data.as_text()


def test_brief_first_line_has_the_local_time_for_the_greeting(config, paths, db, monkeypatch):
    """The 2026-10-19 brief said "Günaydın" at 22:27: the model gets the time on the first line."""
    fixed = datetime(2026, 10, 19, 22, 27, tzinfo=local_tz())
    monkeypatch.setattr(brief_mod, "now_local", lambda: fixed)
    text = build_brief(config, paths, runtime_with(db), "2026-10-19").as_text()
    assert text.splitlines()[0] == "Daily brief for Mon 2026-10-19 (as of 22:27 local time)"


class LocatedWeather(FakeTool):
    """A weather tool with a context, so the brief can tell where the turn comes from."""

    def __init__(self, ctx, text="sunny"):
        super().__init__(text)
        self.ctx = ctx


@pytest.mark.parametrize("channel, expected", [("telegram", {"location": "home"}), ("scheduler", {"location": "home"}), ("cli", {})])
def test_remote_brief_weather_is_for_home_instead_of_asking_for_a_location(config, paths, db, channel, expected):
    """The 2026-10-19 Telegram brief: the weather section told the model to ask for a location, so the
    message explained "Konumun telefondan gelmediği için İstanbul'a baktım". A pushed brief has nobody to ask."""
    paths.profile_file.write_text("# Profile\n\n- Name: Test User\n- Location: Exampletown, Testland\n", encoding="utf-8")
    runtime = runtime_with(db, list_reminders=FakeTool("none"))
    runtime.channel = channel
    ctx = ToolContext.build(config, paths)
    ctx.runtime = runtime
    weather = LocatedWeather(ctx)
    runtime.registry.tools["get_weather"] = weather
    build_brief(config, paths, runtime, "2026-10-18")
    assert len(weather.calls) == 1 and {k: v for k, v in weather.calls[0].items() if k != "days"} == expected


def test_remote_brief_weather_without_a_home_location_keeps_the_tool_default(config, paths, db):
    paths.profile_file.write_text("# Profile\n\n- Name: Test User\n", encoding="utf-8")
    runtime = runtime_with(db)
    runtime.channel = "telegram"
    ctx = ToolContext.build(config, paths)
    ctx.runtime = runtime
    weather = LocatedWeather(ctx)
    runtime.registry.tools["get_weather"] = weather
    build_brief(config, paths, runtime, "2026-10-18")
    assert "location" not in weather.calls[0]


def test_brief_lists_todays_mail_only_today_and_only_when_gmail_is_allowed(config, paths, db, monkeypatch):
    from jarvis.tools import gmail
    from jarvis.utils.timeutil import now_local

    seen = []

    def fake_inbox(since):
        seen.append(since)
        return [
            gmail.InboxMessage(since + timedelta(hours=9), "Electric Co", "Your October bill is due", ["INBOX", "IMPORTANT"]),
            gmail.InboxMessage(since + timedelta(hours=8), "Shop", "50% off everything", ["INBOX", "CATEGORY_PROMOTIONS"]),
        ], False

    monkeypatch.setattr(gmail, "inbox_since", fake_inbox)
    allowed = runtime_with(db, gmail_search_threads=FakeTool())
    data = build_brief(config, paths, allowed, "today")
    assert seen and seen[0].hour == 0 and seen[0].minute == 0 and seen[0].date() == now_local().date()
    text = data.as_text()
    assert "== Mail today ==\n2 message(s) in the inbox today (00:00-" in text
    assert "| from Electric Co | Your October bill is due [important]" in text
    assert "50% off" not in text and "+ 1 promotion/social message(s), not listed" in text

    seen.clear()
    assert build_brief(config, paths, allowed, "tomorrow").mail is None and seen == []  # tomorrow has no inbox yet
    assert build_brief(config, paths, runtime_with(db), "today").mail is None and seen == []  # no Gmail in this turn
    config.brief.include_mail = False
    assert build_brief(config, paths, allowed, "today").mail is None and seen == []


def test_brief_mail_failure_is_a_note(config, paths, db, monkeypatch):
    from jarvis.exceptions import JarvisError
    from jarvis.tools import gmail

    def broken(since):
        raise JarvisError("Google token expired")

    monkeypatch.setattr(gmail, "inbox_since", broken)
    data = build_brief(config, paths, runtime_with(db, gmail_search_threads=FakeTool()), "today")
    assert data.mail is None and "- mail: Google token expired" in data.as_text()


def test_failing_calendar_backend_becomes_a_note(config, paths, db, monkeypatch):
    monkeypatch.setattr(
        brief_mod, "merge_calendars", lambda config, start, end: ([event("Standup", "2026-03-03", 9)], {"google": "token expired"})
    )
    text = build_brief(
        config, paths, runtime_with(db, get_weather=FakeTool("sunny"), list_reminders=FakeTool("none")), "2026-03-03"
    ).as_text()
    assert "Standup" in text and "== Notes ==\n- google calendar: token expired" in text

    def boom(config, start, end):
        raise ToolExecutionError("no calendar could be read: google: x; apple: y")

    monkeypatch.setattr(brief_mod, "merge_calendars", boom)
    data = build_brief(config, paths, runtime_with(db, get_weather=FakeTool("sunny"), list_reminders=FakeTool("none")), "2026-03-03")
    assert data.agenda == [] and data.failures["calendar"] == "no calendar could be read: google: x; apple: y"
    assert "== Agenda ==\nNo events." in data.as_text() and "- calendar: no calendar could be read" in data.as_text()


def test_no_runtime_skips_weather_and_reminders_with_notes(config, paths):
    data = build_brief(config, paths, None, "2026-10-18")
    assert data.failures == {"weather": "skipped (no tool runtime)", "reminders": "skipped (no tool runtime)"}
    text = data.as_text()
    assert "== Weather ==\n(not available)" in text and "== Reminders due ==\n(not available)" in text
    assert "Not found in the calendar or the reminders" in text
    assert "- weather: skipped (no tool runtime)" in text


def test_failing_weather_and_reminder_tools_do_not_kill_the_brief(config, paths, db):
    rt = runtime_with(
        db,
        get_weather=FakeTool(error=ToolValidationError("no location given")),
        list_reminders=FakeTool(error=RuntimeError("EventKit down")),
    )
    data = build_brief(config, paths, rt, "2026-03-03")
    assert data.weather is None and data.reminders is None
    assert data.failures["weather"] == "no location given" and data.failures["reminders"] == "RuntimeError: EventKit down"


# --- travel (brief.include_travel) ------------------------------------------------------------


def test_travel_is_off_by_default_and_added_for_the_first_located_event(config, paths, db, monkeypatch):
    events = [
        event("Standup", "2026-10-18", 9),
        event("Diş hekimi", "2026-10-18", 14, "apple", location="Kadıköy"),
        event("Dinner", "2026-10-18", 20, location="Beşiktaş"),
    ]
    monkeypatch.setattr(brief_mod, "merge_calendars", lambda config, start, end: (events, {}))
    travel = FakeTool("transit: 35 min (12.1 km); leave by 13:15")
    data = build_brief(config, paths, runtime_with(db, travel_time=travel), "2026-10-18")
    assert data.travel is None and travel.calls == [] and "== Travel ==" not in data.as_text()

    config.brief.include_travel = True
    data = build_brief(config, paths, runtime_with(db, travel_time=travel), "2026-10-18")
    assert travel.calls == [{"destination": "Kadıköy", "mode": "transit", "arrive_by": "2026-10-18 14:00"}]
    assert data.travel == "To Diş hekimi (Kadıköy):\ntransit: 35 min (12.1 km); leave by 13:15"
    assert "== Agenda ==" in data.as_text() and "== Travel ==\nTo Diş hekimi (Kadıköy):" in data.as_text()

    # no maps tool (no key): a note, not a failure; no located event: nothing at all
    data = build_brief(config, paths, runtime_with(db), "2026-10-18")
    assert data.travel is None and "no maps tool" in data.failures["travel"]
    monkeypatch.setattr(brief_mod, "merge_calendars", lambda config, start, end: ([event("Standup", "2026-10-18", 9)], {}))
    data = build_brief(config, paths, runtime_with(db, travel_time=travel), "2026-10-18")
    assert data.travel is None and "travel" not in data.failures
    failing = FakeTool(error=ToolExecutionError("maps quota"))
    monkeypatch.setattr(brief_mod, "merge_calendars", lambda config, start, end: (events, {}))
    data = build_brief(config, paths, runtime_with(db, travel_time=failing), "2026-10-18")
    assert data.failures["travel"] == "maps quota" and "== Agenda ==" in data.as_text()


def test_date_validation():
    with pytest.raises(ToolValidationError):
        brief_mod._parse_day("week")
    with pytest.raises(ToolValidationError):
        brief_mod._parse_day("2026-13-45")


def test_brief_data_text_when_everything_is_missing():
    data = BriefData(day=datetime(2026, 1, 5).date(), team="Fenerbahçe", failures={"calendar": "down"})
    text = data.as_text()
    assert (
        text.startswith("Daily brief for Mon 2026-01-05")
        and "== Fenerbahçe ==\nNot found in the calendar" in text
        and "- calendar: down" in text
    )


# --- the tool -----------------------------------------------------------------------------------------------


def test_daily_brief_tool_metadata_and_run(config, paths, db):
    assert DailyBrief.risk == RiskLevel.SAFE and DailyBrief.taints is True and DailyBrief.taint_source == "brief"
    assert DailyBrief.delegate_ok is True
    ctx = ToolContext.build(config, paths)
    ctx.runtime = runtime_with(db, get_weather=FakeTool("sunny"), list_reminders=FakeTool("No reminders."))
    tool = DailyBrief(ctx)
    result = tool.run(tool.parse_args({"date": "2026-10-18"}))
    assert not result.is_error and "web_search" in result.content and "== Weather ==\nsunny" in result.content
    assert tool.parse_args({}).date == "today"
    assert "TODO(setup): scheduled automatic brief" not in Path(brief_mod.__file__).read_text(
        encoding="utf-8"
    )  # implemented: scheduler/brief.py
    assert "== Alarms and timers ==\nNone." in result.content
