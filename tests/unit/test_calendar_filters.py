"""`[[calendar.filters]]`: keyword matching (accents, case, short keywords as whole words), only the
configured calendar is filtered, the agenda, the brief, the list tools and the match-day detection
see only the kept events, `include_filtered` brings the rest back, and the doctor/CLI report counts."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis import i18n
from jarvis.config import CalendarConfig, CalendarFilter, load_config
from jarvis.exceptions import ConfigError
from jarvis.tools import brief as brief_mod
from jarvis.tools import calendar_merged
from jarvis.tools.apple_calendar import AppleListEvents
from jarvis.tools.base import Runtime, ToolContext
from jarvis.tools.brief import build_brief
from jarvis.tools.calendar_merged import Agenda, filter_report, health_checks, merge_calendars
from jarvis.utils import eventkit
from jarvis.utils.calendar_filters import (
    apply_filters,
    describe,
    fold_text,
    keyword_in,
    looks_like_fixture,
)
from jarvis.utils.calendar_types import CalendarEvent
from jarvis.utils.eventkit import CalendarRecord, EventRecord
from jarvis.utils.timeutil import local_tz, now_local

TZ = local_tz()
KEYWORDS = ["Fenerbahçe", "Fenerbahce", "Fener", "FB"]


def flt(calendar: str = "Futbol", keywords: list[str] | None = None, **kw) -> CalendarFilter:
    return CalendarFilter(calendar=calendar, mode="include_only", keywords=keywords or KEYWORDS, **kw)


def ev(title: str, calendar: str = "Futbol", day: str = "2026-10-20", hour: int = 19, source: str = "apple") -> CalendarEvent:
    start = datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=TZ)
    return CalendarEvent(
        source=source, calendar=calendar, title=title, start=start, end=start + timedelta(hours=2), id=f"{calendar}-{title}"
    )


@pytest.fixture
def filtered_config(config):
    config.calendar.filters = [flt()]
    return config


# --- matching -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Fenerbahçe", "fenerbahce"),
        ("KASIMPAŞA", "kasimpasa"),
        ("Çorum FK - Fenerbahçe", "corum fk - fenerbahce"),
        ("İSTANBUL ığüşöç", "istanbul igusoc"),
        ("  two   spaces ", "two spaces"),
        ("", ""),
    ],
)
def test_fold_text_is_turkish_and_case_insensitive(text, expected):
    assert fold_text(text) == expected


@pytest.mark.parametrize(
    "title, keyword, hit",
    [
        ("Galatasaray - Fenerbahçe", "Fenerbahçe", True),
        ("Galatasaray - FENERBAHÇE", "fenerbahce", True),
        ("galatasaray - fenerbahce", "Fenerbahçe", True),
        ("Fenerbahçe - Göztepe", "Fener", True),  # a long keyword matches inside a word
        ("Fener - GS", "Fenerbahçe", False),  # but not the other way round
        ("FB vs Beşiktaş", "FB", True),
        ("fb - gs", "FB", True),
        ("Beşiktaş - FB", "FB", True),
        ("FBB - Göztepe", "FB", False),  # short keywords only as a whole word
        ("Kasımpaşa - Samsunspor", "FB", False),  # no "fb" hidden in a word either way
        ("Çorum FK - Trabzonspor", "FB", False),
        ("Çorum FK - Fenerbahçe", "FB", False),
        ("Alanyaspor - Kocaelispor", "Fener", False),
        ("", "Fener", False),
        ("Fener", "", False),
    ],
)
def test_keyword_in(title, keyword, hit):
    assert keyword_in(title, keyword) is hit


@pytest.mark.parametrize(
    "title, fixture",
    [
        ("Galatasaray - Fenerbahçe", True),
        ("Fenerbahçe – Göztepe", True),
        ("FB vs Beşiktaş", True),
        ("FB vs. Beşiktaş", True),
        ("Arsenal v Chelsea", True),
        ("Flight: TK 0001 from IST to AMS", False),
        ("Fenerbahçe maçı", False),
        ("Standup", False),
        ("", False),
        ("Diş hekimi - iptal", True),  # shaped like a fixture; a non-match title in the football calendar is the user's call
    ],
)
def test_looks_like_fixture(title, fixture):
    assert looks_like_fixture(title) is fixture


# --- apply_filters -------------------------------------------------------------------------------------


def test_only_the_configured_calendar_is_filtered(filtered_config):
    events = [
        ev("Kasımpaşa - Samsunspor"),
        ev("Galatasaray - Fenerbahçe"),
        ev("FB - Beşiktaş"),
        ev("fenerbahce - Göztepe"),
        ev("Trabzonspor - Beşiktaş", calendar="Formula 1"),  # another calendar: untouched even if fixture-shaped
        ev("Diş hekimi", calendar="Kişisel"),
        ev("Standup", calendar="Work", source="google"),
    ]
    outcome = apply_filters(filtered_config, events)
    assert [e.title for e in outcome.kept] == [
        "Galatasaray - Fenerbahçe",
        "FB - Beşiktaş",
        "fenerbahce - Göztepe",
        "Trabzonspor - Beşiktaş",
        "Diş hekimi",
        "Standup",
    ]
    assert [e.title for e in outcome.hidden] == ["Kasımpaşa - Samsunspor"] and outcome.hidden_count == 1
    assert "1 event(s) in Futbol hidden by the calendar filter" in outcome.note() and "include_filtered=true" in outcome.note()


def test_calendar_name_matches_case_and_accent_insensitively(config):
    config.calendar.filters = [flt(calendar="FUTBOL")]
    outcome = apply_filters(config, [ev("Kasımpaşa - Samsunspor", calendar="futbol"), ev("Fener - GS", calendar="Futbol")])
    assert [e.title for e in outcome.hidden] == ["Kasımpaşa - Samsunspor"] and [e.title for e in outcome.kept] == ["Fener - GS"]


def test_non_fixture_titles_in_the_football_calendar_stay_visible_by_default(config):
    flight = ev("Flight: TK 0001 from IST to AMS")
    note = ev("Kombine yenileme")
    config.calendar.filters = [flt()]
    assert [e.title for e in apply_filters(config, [flight, note, ev("Beşiktaş - Göztepe")]).kept] == [flight.title, note.title]
    config.calendar.filters = [flt(only_matches=False)]
    outcome = apply_filters(config, [flight, note, ev("Beşiktaş - Göztepe"), ev("Fener - Göztepe")])
    assert [e.title for e in outcome.kept] == ["Fener - Göztepe"] and outcome.hidden_count == 3


def test_no_filters_keeps_everything(config):
    assert config.calendar.filters == [flt()], "the shipped config.toml filters the Futbol calendar"
    config.calendar.filters = []
    events = [ev("Kasımpaşa - Samsunspor"), ev("Standup", calendar="Work")]
    outcome = apply_filters(config, events)
    assert outcome.kept == events and outcome.hidden == [] and outcome.note() == ""


def test_describe():
    assert describe(flt()) == "Futbol: include_only [Fenerbahçe, Fenerbahce, Fener, FB] (fixture-shaped titles only)"
    assert describe(flt(only_matches=False)).endswith("[Fenerbahçe, Fenerbahce, Fener, FB]")


# --- config ------------------------------------------------------------------------------------------


def test_config_validation(paths):
    text = paths.config_file.read_text()
    assert 'calendar = "Futbol"' in text and 'mode = "include_only"' in text
    assert load_config(paths.config_file).calendar.filters[0].keywords == KEYWORDS

    paths.config_file.write_text(text.replace('mode = "include_only"', 'mode = "exclude"'))
    with pytest.raises(ConfigError, match="calendar.filters.0.mode"):
        load_config(paths.config_file)

    paths.config_file.write_text(text.replace('keywords = ["Fenerbahçe", "Fenerbahce", "Fener", "FB"]', 'keywords = [" ", ""]'))
    with pytest.raises(ConfigError, match="at least one non-empty keyword"):
        load_config(paths.config_file)

    paths.config_file.write_text(
        text.replace(
            'keywords = ["Fenerbahçe", "Fenerbahce", "Fener", "FB"]',
            'keywords = ["Fenerbahçe", "Fenerbahce", "Fener", "FB"]\nonly_matches = false',
        )
    )
    assert load_config(paths.config_file).calendar.filters[0].only_matches is False

    with pytest.raises(ValueError, match="listed twice"):
        CalendarConfig(filters=[flt(), flt(calendar="futbol")])
    with pytest.raises(ValueError):
        CalendarFilter(calendar="Futbol", keywords=["Fener"], extra=True)  # type: ignore[call-arg]


# --- agenda, list tools, brief --------------------------------------------------------------------------


def patch_fetchers(monkeypatch, google=None, apple=None):
    monkeypatch.setattr(
        calendar_merged,
        "_fetchers",
        lambda: {"google": lambda config, start, end: list(google or []), "apple": lambda config, start, end: list(apple or [])},
    )


FOOTBALL_DAY = [
    ev("Kasımpaşa - Samsunspor", hour=13),
    ev("Beşiktaş - Göztepe", hour=16),
    ev("Galatasaray - Fenerbahçe", hour=21),
    ev("Flight: TK 0001 from IST to AMS", hour=11),
]
OTHER = [ev("Standup", calendar="Work", hour=9, source="google")]


def test_agenda_shows_only_the_teams_matches_and_says_what_is_hidden(filtered_config, paths, monkeypatch):
    patch_fetchers(monkeypatch, google=OTHER, apple=FOOTBALL_DAY)
    tool = Agenda(ToolContext.build(filtered_config, paths))
    out = tool.run(tool.parse_args({"date_range": "2026-10-20"})).content
    assert "Standup" in out and "Galatasaray - Fenerbahçe" in out and "Flight: TK 0001" in out
    assert "Kasımpaşa" not in out and "Beşiktaş - Göztepe" not in out
    assert out.rstrip().endswith(
        "(2 event(s) in Futbol hidden by the calendar filter; pass include_filtered=true only if the user asks about them.)"
    )


def test_agenda_include_filtered_returns_the_other_matches(filtered_config, paths, monkeypatch):
    patch_fetchers(monkeypatch, google=OTHER, apple=FOOTBALL_DAY)
    tool = Agenda(ToolContext.build(filtered_config, paths))
    out = tool.run(tool.parse_args({"date_range": "2026-10-20", "include_filtered": True})).content
    assert "Kasımpaşa - Samsunspor" in out and "Beşiktaş - Göztepe" in out and "Galatasaray - Fenerbahçe" in out
    assert "hidden by the calendar filter" not in out
    field = tool.input_model.model_fields["include_filtered"]
    assert field.default is False and "explicitly asks" in (field.description or "")


def test_merge_calendars_is_filtered_by_default(filtered_config, monkeypatch):
    patch_fetchers(monkeypatch, apple=FOOTBALL_DAY)
    day = datetime(2026, 10, 20, tzinfo=TZ)
    events, _ = merge_calendars(filtered_config, day, day + timedelta(days=1))
    assert [e.title for e in events] == ["Flight: TK 0001 from IST to AMS", "Galatasaray - Fenerbahçe"]
    events, _ = merge_calendars(filtered_config, day, day + timedelta(days=1), include_filtered=True)
    assert len(events) == 4


def test_brief_match_day_only_counts_the_teams_match(filtered_config, paths, db, monkeypatch):
    """Without the filter the brief's detection would already pick Fenerbahçe (it scans for the team),
    but the agenda section listed every match of the day. Now it lists only Fenerbahçe's."""
    patch_fetchers(monkeypatch, google=OTHER, apple=FOOTBALL_DAY)
    monkeypatch.setattr(brief_mod, "merge_calendars", calendar_merged.merge_calendars)
    data = build_brief(filtered_config, paths, Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None), "2026-10-20")
    assert [e.title for e in data.agenda] == ["Standup", "Flight: TK 0001 from IST to AMS", "Galatasaray - Fenerbahçe"]
    assert data.match_event is not None and data.match_event.title == "Galatasaray - Fenerbahçe"
    text = data.as_text()
    assert (
        "Kasımpaşa" not in text
        and "Beşiktaş" not in text
        and "Match day (from the apple calendar): at 21:00: Galatasaray - Fenerbahçe" in text
    )


def test_brief_without_the_teams_match_sees_no_match_day(filtered_config, paths, db, monkeypatch):
    patch_fetchers(monkeypatch, apple=[ev("Kasımpaşa - Samsunspor", hour=13), ev("Beşiktaş - Göztepe", hour=16)])
    monkeypatch.setattr(brief_mod, "merge_calendars", calendar_merged.merge_calendars)
    data = build_brief(filtered_config, paths, Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None), "2026-10-20")
    assert data.agenda == [] and data.match_event is None and "Not found in the calendar" in data.as_text()


def test_apple_list_events_honors_the_filter(filtered_config, paths):
    from tests.unit.test_apple_calendar import FakeStore

    futbol = CalendarRecord("Futbol", "cal-f", True, False, "caldav")
    work = CalendarRecord("Work", "cal-w", True, False, "caldav")
    today = now_local().replace(hour=12, minute=0, second=0, microsecond=0)
    records = [
        EventRecord("1", "Kasımpaşa - Samsunspor", "Futbol", "cal-f", today, today + timedelta(hours=2)),
        EventRecord("2", "Fenerbahçe - Göztepe", "Futbol", "cal-f", today + timedelta(hours=3), today + timedelta(hours=5)),
        EventRecord("3", "Standup", "Work", "cal-w", today, today + timedelta(hours=1)),
    ]
    tool = AppleListEvents(ToolContext.build(filtered_config, paths), store=FakeStore([futbol, work], records))
    out = tool.run(tool.parse_args({})).content
    assert "Fenerbahçe - Göztepe" in out and "Standup" in out and "Kasımpaşa" not in out and "1 event(s) in Futbol hidden" in out
    out = tool.run(tool.parse_args({"include_filtered": True})).content
    assert "Kasımpaşa - Samsunspor" in out and "hidden" not in out
    out = tool.run(tool.parse_args({"calendar": "Futbol"})).content
    assert "Fenerbahçe - Göztepe" in out and "Kasımpaşa" not in out and "Standup" not in out


def test_google_list_events_honors_the_filter(config, paths, monkeypatch):
    from jarvis.tools import google_calendar as gc
    from jarvis.utils import google_auth
    from tests.unit.test_google_calendar import FakeService

    config.calendar.filters = [flt(calendar="Kerem's calendar")]
    service = FakeService(
        {
            "events.list": {
                "summary": "Kerem's calendar",
                "accessRole": "owner",
                "items": [
                    {
                        "id": "a",
                        "summary": "Kasımpaşa - Samsunspor",
                        "start": {"dateTime": "2026-10-20T13:00:00+03:00"},
                        "end": {"dateTime": "2026-10-20T15:00:00+03:00"},
                    },
                    {
                        "id": "b",
                        "summary": "FB - Göztepe",
                        "start": {"dateTime": "2026-10-20T19:00:00+03:00"},
                        "end": {"dateTime": "2026-10-20T21:00:00+03:00"},
                    },
                ],
            }
        }
    )
    monkeypatch.setattr(google_auth, "build_service", lambda api, version: service)
    tool = gc.GoogleListEvents(ToolContext.build(config, paths))
    out = tool.run(tool.parse_args({"date_range": "2026-10-20"})).content
    assert (
        out.startswith("1 Google Calendar event(s)")
        and "FB - Göztepe" in out
        and "Kasımpaşa" not in out
        and "1 event(s) in Kerem's calendar hidden" in out
    )
    out = tool.run(tool.parse_args({"date_range": "2026-10-20", "include_filtered": True})).content
    assert out.startswith("2 Google Calendar event(s)") and "Kasımpaşa - Samsunspor" in out


# --- doctor / `jarvis calendar filters` ------------------------------------------------------------------


def test_filter_report_counts_the_coming_week(filtered_config, monkeypatch):
    today = now_local().replace(hour=20, minute=0, second=0, microsecond=0)

    def day(n: int, title: str, calendar: str = "Futbol") -> CalendarEvent:
        start = today + timedelta(days=n)
        return CalendarEvent(source="apple", calendar=calendar, title=title, start=start, end=start + timedelta(hours=2))

    apple = [
        day(0, "Kasımpaşa - Samsunspor"),
        day(2, "Galatasaray - Fenerbahçe"),
        day(3, "Beşiktaş - Göztepe"),
        day(4, "Flight: TK 0001 from IST to AMS"),
        day(5, "Standup", "Work"),
        day(9, "Trabzonspor - Göztepe"),
    ]
    seen: dict[str, tuple] = {}

    def apple_fetch(config, start, end):
        seen["range"] = (start, end)
        return [e for e in apple if start <= e.start < end]

    monkeypatch.setattr(calendar_merged, "_fetchers", lambda: {"google": lambda c, s, e: [], "apple": apple_fetch})
    monkeypatch.setattr(calendar_merged, "_apple_not_ready", lambda: "")
    report = filter_report(filtered_config)
    assert report.days == 7 and (seen["range"][1] - seen["range"][0]) == timedelta(days=7)
    assert [e.title for e in report.kept["Futbol"]] == ["Galatasaray - Fenerbahçe", "Flight: TK 0001 from IST to AMS"]
    assert [e.title for e in report.hidden["Futbol"]] == ["Kasımpaşa - Samsunspor", "Beşiktaş - Göztepe"] and report.hidden_total == 2
    assert report.summary(report.filters[0]) == (
        "Futbol: include_only [Fenerbahçe, Fenerbahce, Fener, FB] (fixture-shaped titles only); next 7 days: 2 shown, 2 hidden"
    )
    checks = health_checks(filtered_config, None)
    assert [c.name for c in checks] == ["Calendar filter: Futbol"] and checks[0].ok and "2 hidden" in checks[0].detail


def test_filter_report_never_prompts_for_apple_access(filtered_config, monkeypatch):
    def apple_fetch(config, start, end):
        raise AssertionError("the doctor must not read Apple Calendar before access is granted (it would prompt)")

    monkeypatch.setattr(calendar_merged, "_fetchers", lambda: {"google": lambda c, s, e: [], "apple": apple_fetch})
    monkeypatch.setattr(eventkit.EventKitStore, "authorization_status", staticmethod(lambda entity: "not_determined"))
    report = filter_report(filtered_config)
    assert report.skipped == {"apple": "not read, Calendar access is not_determined"} and report.hidden_total == 0
    assert "apple: not read, Calendar access is not_determined" in report.summary(report.filters[0])
    assert health_checks(filtered_config, None)[0].ok  # a skipped count is information, not a failure


def test_filter_report_without_filters_and_doctor_line(config):
    config.calendar.filters = []
    assert filter_report(config).filters == []
    checks = health_checks(config, None)
    assert checks[0].name == "Calendar filters" and checks[0].ok and "none configured" in checks[0].detail


def test_calendar_filters_cli(filtered_config, paths, monkeypatch, capsys):
    from rich.console import Console

    from jarvis.main import build_parser, cmd_calendar

    today = now_local().replace(hour=20, minute=0, second=0, microsecond=0)
    apple = [
        CalendarEvent(source="apple", calendar="Futbol", title=t, start=today + timedelta(days=i), end=today + timedelta(days=i, hours=2))
        for i, t in enumerate(["Kasımpaşa - Samsunspor", "Fenerbahçe - Göztepe"])
    ]
    monkeypatch.setattr(calendar_merged, "_fetchers", lambda: {"google": lambda c, s, e: [], "apple": lambda c, s, e: apple})
    args = build_parser().parse_args(["calendar", "filters", "--days", "3"])
    assert cmd_calendar(args, filtered_config, Console(width=200)) == 0
    out = capsys.readouterr().out
    assert "Futbol: include_only [Fenerbahçe, Fenerbahce, Fener, FB]" in out and "önümüzdeki 3 gün: 1 gösteriliyor, 1 gizli" in out
    assert "gösterilen: " in out and "Fenerbahçe - Göztepe" in out and "gizlenen: " in out and "Kasımpaşa - Samsunspor" in out
    i18n.set_language("en")
    assert cmd_calendar(args, filtered_config, Console(width=200)) == 0
    out = capsys.readouterr().out
    assert "next 3 days: 1 shown, 1 hidden" in out and "    shown: " in out and "    hidden: " in out
    filtered_config.calendar.filters = []
    assert cmd_calendar(args, filtered_config, Console(width=200)) == 0
    assert "No calendar filters configured" in capsys.readouterr().out
