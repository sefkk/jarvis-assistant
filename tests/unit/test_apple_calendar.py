"""Apple Calendar tools with a fake EventKitStore: no EventKit, no permission prompt."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.tools import apple_calendar
from jarvis.tools.apple_calendar import AppleCreateEvent, AppleDeleteEvent, AppleListEvents, fetch_events, health_checks
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.utils import eventkit
from jarvis.utils.eventkit import CalendarRecord, EventRecord, permission_instructions
from jarvis.utils.timeutil import local_tz

TZ = local_tz()


def cal(title: str, ident: str, *, writable: bool = True, subscribed: bool = False) -> CalendarRecord:
    return CalendarRecord(
        title=title,
        identifier=ident,
        allows_modifications=writable,
        is_subscribed=subscribed,
        type="subscription" if subscribed else "caldav",
    )


def ev(ident: str, title: str, calendar: CalendarRecord, start: datetime, hours: float = 1, all_day: bool = False) -> EventRecord:
    end = start + (timedelta(days=1) if all_day else timedelta(hours=hours))
    return EventRecord(ident, title, calendar.title, calendar.identifier, start, end, all_day=all_day)


class FakeStore:
    """Same surface as EventKitStore, records every call."""

    def __init__(
        self,
        calendars: list[CalendarRecord],
        events: list[EventRecord] | None = None,
        default: CalendarRecord | None = None,
        denied: str | None = None,
    ) -> None:
        self._calendars = calendars
        self._events = events or []
        self._default = default
        self._denied = denied
        self.calls: list[tuple] = []

    def ensure_access(self, entity: str) -> None:
        self.calls.append(("ensure_access", entity))
        if self._denied:
            raise PermissionMissingError(permission_instructions(entity, self._denied))

    def calendars(self, entity: str) -> list[CalendarRecord]:
        self.calls.append(("calendars", entity))
        return list(self._calendars)

    def default_calendar(self, entity: str) -> CalendarRecord | None:
        return self._default

    def events(self, start, end, calendars=None) -> list[EventRecord]:
        self.calls.append(("events", start, end, calendars))
        chosen = set(calendars) if calendars is not None else None
        return [e for e in self._events if (chosen is None or e.calendar_id in chosen) and e.start < end and e.end > start]

    def create_event(self, title, start, end, *, calendar_id=None, location=None, notes=None, all_day=False) -> EventRecord:
        self.calls.append(("create_event", title, start, end, calendar_id, location, notes, all_day))
        calendar = next(c for c in self._calendars if c.identifier == calendar_id)
        return EventRecord("new-1", title, calendar.title, calendar_id, start, end, all_day, location or "", notes or "")

    def delete_event(self, identifier) -> EventRecord:
        self.calls.append(("delete_event", identifier))
        for e in self._events:
            if e.identifier == identifier:
                return e
        raise ToolExecutionError(f"no Apple Calendar event with id {identifier!r}")


WORK = cal("Work", "cal-work")
HOME = cal("Home", "cal-home")
HOLIDAYS = cal("Holidays", "cal-holidays", writable=False, subscribed=True)
SYNCED = cal("Google Mirror", "cal-google")


@pytest.fixture
def ctx(config, paths) -> ToolContext:
    return ToolContext.build(config, paths)


def today_at(hour: int) -> datetime:
    now = datetime.now(TZ)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def store() -> FakeStore:
    events = [
        ev("e1", "Standup", WORK, today_at(9)),
        ev("e2", "Dentist", HOME, today_at(14), hours=0.5),
        ev("e3", "Republic Day", HOLIDAYS, today_at(0), all_day=True),
        ev("e4", "Synced thing", SYNCED, today_at(11)),
        ev("e5", "Next week", WORK, today_at(9) + timedelta(days=8)),
    ]
    return FakeStore([WORK, HOME, HOLIDAYS, SYNCED], events, default=WORK)


# --- metadata --------------------------------------------------------------------------


def test_tool_metadata():
    assert AppleListEvents.name == "apple_list_events" and AppleListEvents.risk is RiskLevel.SAFE
    assert AppleCreateEvent.name == "apple_create_event" and AppleCreateEvent.risk is RiskLevel.CONFIRM
    assert AppleDeleteEvent.name == "apple_delete_event" and AppleDeleteEvent.risk is RiskLevel.CONFIRM
    for tool in (AppleListEvents, AppleCreateEvent, AppleDeleteEvent):
        assert tool.taints and tool.taint_source == "calendar"
        schema = tool().api_schema()
        assert schema["name"] == tool.name and "title" not in schema["input_schema"]
    assert AppleCreateEvent().api_schema()["input_schema"]["required"] == ["title", "start"]


def test_module_import_does_not_touch_eventkit():
    """Importing the tools (as the registry does) must not load EventKit or build an EKEventStore."""
    import subprocess
    import sys

    code = (
        "import sys, jarvis.tools.apple_calendar, jarvis.tools.apple_reminders, jarvis.utils.eventkit as ek;"
        "print('EventKit' in sys.modules, 'Foundation' in sys.modules, ek._STORE)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False", "None"]


# --- fetch_events (integrator entry point) ------------------------------------------------


def test_fetch_events_honors_exclusion_and_marks_read_only(config, store):
    config.calendar.apple_calendar_exclude = ["google mirror"]  # case-insensitive match
    start, end = today_at(0), today_at(0) + timedelta(days=1)
    events = fetch_events(config, start, end, store=store)
    titles = {e.title for e in events}
    assert titles == {"Standup", "Dentist", "Republic Day"}
    assert all(e.source == "apple" for e in events)
    by_title = {e.title: e for e in events}
    assert by_title["Republic Day"].read_only and by_title["Republic Day"].all_day
    assert not by_title["Standup"].read_only
    assert by_title["Standup"].id == "e1" and by_title["Standup"].calendar == "Work"
    # the store was asked only for the non-excluded calendars
    assert store.calls[0] == ("ensure_access", "events")
    assert set(store.calls[-1][3]) == {"cal-work", "cal-home", "cal-holidays"}


def test_fetch_events_all_calendars_excluded_returns_empty(config, store):
    config.calendar.apple_calendar_exclude = ["Work", "Home", "Holidays", "Google Mirror"]
    assert fetch_events(config, today_at(0), today_at(23), store=store) == []
    assert not any(c[0] == "events" for c in store.calls)


def test_fetch_events_uses_module_store_by_default(config, store, monkeypatch):
    monkeypatch.setattr(eventkit, "get_store", lambda: store)
    events = fetch_events(config, today_at(0), today_at(23))
    assert len(events) == 4


def test_fetch_events_permission_denied(config):
    store = FakeStore([WORK], denied="denied")
    with pytest.raises(PermissionMissingError, match="Privacy & Security > Calendars"):
        fetch_events(config, today_at(0), today_at(23), store=store)


# --- apple_list_events -------------------------------------------------------------------


def test_list_events_today(ctx, store):
    tool = AppleListEvents(ctx, store=store)
    result = tool.run(tool.parse_args({}))
    assert "Standup" in result.content and "Dentist" in result.content and "Synced thing" in result.content
    assert "Next week" not in result.content
    assert "all day: Republic Day" in result.content
    assert "[id e1]" in result.content
    assert "1 event(s) are in read-only calendars" in result.content
    assert "<untrusted" not in result.content  # the brain wraps, not the tool


def test_list_events_week_and_calendar_filter(ctx, store):
    tool = AppleListEvents(ctx, store=store)
    result = tool.run(tool.parse_args({"date_range": "week", "calendar": "work"}))
    assert "Standup" in result.content and "Dentist" not in result.content
    result = tool.run(tool.parse_args({"date_range": "next week", "calendar": "Work"}))
    assert "Next week" in result.content or "No events." in result.content  # depends on today's weekday


def test_list_events_unknown_calendar(ctx, store):
    tool = AppleListEvents(ctx, store=store)
    with pytest.raises(ToolValidationError, match="Holidays"):
        tool.run(tool.parse_args({"calendar": "Nope"}))


def test_list_events_honors_exclusion(ctx, store):
    ctx.config.calendar.apple_calendar_exclude = ["Google Mirror"]
    tool = AppleListEvents(ctx, store=store)
    result = tool.run(tool.parse_args({}))
    assert "Synced thing" not in result.content and "Standup" in result.content
    with pytest.raises(ToolValidationError):  # an excluded calendar cannot be selected either
        tool.run(tool.parse_args({"calendar": "Google Mirror"}))


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-10-20..2026-10-10", "whenever", "2026-10-10..nope"])
def test_list_events_bad_range_is_validation_error(ctx, store, bad):
    tool = AppleListEvents(ctx, store=store)
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"date_range": bad}))
    assert store.calls == []


def test_list_events_range_too_long(ctx, store):
    tool = AppleListEvents(ctx, store=store)
    with pytest.raises(ToolValidationError, match="366"):
        tool.run(tool.parse_args({"date_range": "2020-01-01..2026-01-01"}))


def test_list_events_permission_denied(ctx):
    tool = AppleListEvents(ctx, store=FakeStore([WORK], denied="restricted"))
    with pytest.raises(PermissionMissingError) as info:
        tool.run(tool.parse_args({}))
    msg = str(info.value)
    assert "restricted" in msg and "System Settings > Privacy & Security > Calendars" in msg and "Full Access" in msg


def test_list_events_unexpected_store_failure_is_tool_error(ctx, store):
    def boom(*a, **k):
        raise RuntimeError("objc went away")

    store.events = boom
    tool = AppleListEvents(ctx, store=store)
    with pytest.raises(ToolExecutionError, match="objc went away"):
        tool.run(tool.parse_args({}))


# --- apple_create_event --------------------------------------------------------------------


def test_create_timed_event_default_calendar(ctx, store):
    tool = AppleCreateEvent(ctx, store=store)
    result = tool.run(tool.parse_args({"title": " Lunch ", "start": "2026-10-20 12:30", "location": "Cafe"}))
    call = [c for c in store.calls if c[0] == "create_event"][0]
    _, title, start, end, calendar_id, location, notes, all_day = call
    assert title == "Lunch" and calendar_id == "cal-work" and location == "Cafe" and notes is None
    assert start == datetime(2026, 10, 20, 12, 30, tzinfo=TZ)
    assert end == start + timedelta(hours=1)
    assert all_day is False
    assert "Created Apple Calendar event 'Lunch' in 'Work'" in result.content and "[id new-1]" in result.content
    assert "2026-10-20 12:30 to 2026-10-20 13:30" in result.content


def test_create_event_explicit_end_and_calendar(ctx, store):
    tool = AppleCreateEvent(ctx, store=store)
    tool.run(
        tool.parse_args({"title": "Movie", "start": "2026-10-20 20:00", "end": "2026-10-20 22:15", "calendar": "home", "notes": "Row F"})
    )
    call = [c for c in store.calls if c[0] == "create_event"][0]
    assert call[3] == datetime(2026, 10, 20, 22, 15, tzinfo=TZ) and call[4] == "cal-home" and call[6] == "Row F"


def test_create_all_day_event(ctx, store):
    tool = AppleCreateEvent(ctx, store=store)
    result = tool.run(tool.parse_args({"title": "Offsite", "start": "2026-11-02"}))
    call = [c for c in store.calls if c[0] == "create_event"][0]
    assert call[2] == datetime(2026, 11, 2, tzinfo=TZ) and call[3] == datetime(2026, 11, 3, tzinfo=TZ)
    assert call[7] is True
    assert "2026-11-02 all day" in result.content

    tool.run(tool.parse_args({"title": "Trip", "start": "2026-11-02", "end": "2026-11-04", "all_day": True}))
    call = [c for c in store.calls if c[0] == "create_event"][1]
    assert call[3] == datetime(2026, 11, 5, tzinfo=TZ)  # bare end date means through that whole day


@pytest.mark.parametrize(
    "args",
    [
        {"title": "X", "start": "tomorrow"},
        {"title": "X", "start": "2026-10-20 10:00", "end": "2026-10-20 09:00"},
        {"title": "   ", "start": "2026-10-20 10:00"},
    ],
)
def test_create_event_bad_arguments(ctx, store, args):
    tool = AppleCreateEvent(ctx, store=store)
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args(args))
    assert not any(c[0] == "create_event" for c in store.calls)


def test_create_event_refuses_read_only_calendar(ctx, store):
    tool = AppleCreateEvent(ctx, store=store)
    with pytest.raises(ToolExecutionError) as info:
        tool.run(tool.parse_args({"title": "Hack", "start": "2026-10-20 10:00", "calendar": "Holidays"}))
    msg = str(info.value)
    assert "subscribed" in msg and "Work" in msg and "Home" in msg and "Holidays" not in msg.split("writable calendars:")[1]
    assert not any(c[0] == "create_event" for c in store.calls)


def test_create_event_refuses_read_only_default(ctx):
    store = FakeStore([WORK, HOLIDAYS], default=HOLIDAYS)
    tool = AppleCreateEvent(ctx, store=store)
    with pytest.raises(ToolExecutionError, match="writable calendars: Work"):
        tool.run(tool.parse_args({"title": "Hack", "start": "2026-10-20 10:00"}))


def test_create_event_unknown_calendar_lists_writable(ctx, store):
    tool = AppleCreateEvent(ctx, store=store)
    with pytest.raises(ToolValidationError, match="writable calendars: Google Mirror, Home, Work"):
        tool.run(tool.parse_args({"title": "X", "start": "2026-10-20 10:00", "calendar": "Nope"}))


def test_create_event_no_default_calendar(ctx):
    tool = AppleCreateEvent(ctx, store=FakeStore([WORK], default=None))
    with pytest.raises(ToolExecutionError, match="no default calendar"):
        tool.run(tool.parse_args({"title": "X", "start": "2026-10-20 10:00"}))


def test_create_event_permission_denied(ctx):
    tool = AppleCreateEvent(ctx, store=FakeStore([WORK], denied="denied"))
    with pytest.raises(PermissionMissingError, match="Calendars"):
        tool.run(tool.parse_args({"title": "X", "start": "2026-10-20 10:00"}))


# --- apple_delete_event --------------------------------------------------------------------


def test_delete_event(ctx, store):
    tool = AppleDeleteEvent(ctx, store=store)
    result = tool.run(tool.parse_args({"event_id": " e2 "}))
    assert ("ensure_access", "events") in store.calls and ("delete_event", "e2") in store.calls
    assert "Deleted Apple Calendar event 'Dentist'" in result.content and "'Home'" in result.content


def test_delete_event_unknown_id(ctx, store):
    tool = AppleDeleteEvent(ctx, store=store)
    with pytest.raises(ToolExecutionError, match="no Apple Calendar event with id 'zzz'"):
        tool.run(tool.parse_args({"event_id": "zzz"}))
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"event_id": "  "}))


def test_delete_event_permission_denied(ctx):
    tool = AppleDeleteEvent(ctx, store=FakeStore([WORK], denied="denied"))
    with pytest.raises(PermissionMissingError):
        tool.run(tool.parse_args({"event_id": "e1"}))


# --- health checks -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, ok, needle",
    [
        ("authorized", True, "full access"),
        ("not_determined", False, "not granted yet"),
        ("denied", False, "Privacy & Security > Calendars"),
        ("restricted", False, "restricted"),
        ("write_only", False, "Full Access"),
    ],
)
def test_health_checks_report_status_without_prompting(config, paths, monkeypatch, status, ok, needle):
    monkeypatch.setattr(eventkit.EventKitStore, "authorization_status", staticmethod(lambda entity: status))
    checks = health_checks(config, paths)
    assert len(checks) == 1 and checks[0].name == "Apple Calendar access"
    assert checks[0].ok is ok and needle in checks[0].detail and not checks[0].fatal


def test_health_checks_never_raise(config, paths, monkeypatch):
    def broken(entity):
        raise ImportError("No module named EventKit")

    monkeypatch.setattr(eventkit.EventKitStore, "authorization_status", staticmethod(broken))
    checks = apple_calendar.health_checks(config, paths)
    assert checks[0].ok is False and "EventKit" in checks[0].detail
