"""Apple Calendar tools via EventKit: apple_list_events, apple_create_event,
apple_delete_event. Also `fetch_events` for the merged agenda and `health_checks` for the doctor.

Calendars named in `config.calendar.apple_calendar_exclude` are skipped on read. Calendars that
do not allow modifications (subscribed calendars, Birthdays, Holidays) are read-only: their events
are marked `read_only=True` and writes to them are refused with the list of writable calendars.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis.exceptions import PermissionMissingError, ToolError, ToolExecutionError, ToolValidationError
from jarvis.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from jarvis.utils import eventkit
from jarvis.utils.calendar_filters import apply_filters
from jarvis.utils.calendar_types import CalendarEvent, format_events
from jarvis.utils.timeutil import format_local, parse_date_range, parse_datetime

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check
    from jarvis.utils.eventkit import CalendarRecord, EventKitStore

log = logging.getLogger("jarvis.tools.apple_calendar")

SOURCE = "apple"
MAX_RANGE_DAYS = 366


# --- functions for the integrator (merged agenda, doctor) -------------------------------------


def _excluded_titles(config: "Config") -> set[str]:
    return {name.strip().casefold() for name in config.calendar.apple_calendar_exclude if name.strip()}


def readable_calendars(config: "Config", store: "EventKitStore") -> list["CalendarRecord"]:
    """Event calendars minus the excluded ones. Requires access (prompts once if never asked)."""
    store.ensure_access("events")
    excluded = _excluded_titles(config)
    calendars = [c for c in store.calendars("events") if c.title.strip().casefold() not in excluded]
    skipped = [c.title for c in store.calendars("events") if c.title.strip().casefold() in excluded]
    if skipped:
        log.debug("apple calendar: skipping excluded calendars %s", skipped)
    return calendars


def fetch_events(config: "Config", start: datetime, end: datetime, store: "EventKitStore | None" = None) -> list[CalendarEvent]:
    """Apple events in [start, end) as CalendarEvents, honoring the exclusion list.

    Raises PermissionMissingError (with instructions) or ToolExecutionError; never crashes.
    """
    store = store or eventkit.get_store()
    calendars = readable_calendars(config, store)
    if not calendars:
        return []
    read_only = {c.identifier for c in calendars if not c.allows_modifications}
    records = store.events(start, end, [c.identifier for c in calendars])
    return [
        CalendarEvent(
            source=SOURCE,
            calendar=r.calendar,
            title=r.title,
            start=r.start,
            end=r.end,
            all_day=r.all_day,
            location=r.location,
            notes=r.notes,
            id=r.identifier,
            read_only=r.calendar_id in read_only,
        )
        for r in records
    ]


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Calendar authorization status for `jarvis doctor`. Never prompts, never raises."""
    return [status_check("Apple Calendar access", "events")]


def status_check(name: str, entity: eventkit.Entity) -> "Check":
    """One Check for an EventKit entity's authorization status (status only, no prompt)."""
    from jarvis.observability.health import Check  # noqa: PLC0415 (avoid import cycles at load time)

    try:
        status = eventkit.EventKitStore.authorization_status(entity)
    except Exception as exc:  # EventKit missing or broken: report, do not raise
        return Check(name, False, f"cannot query EventKit: {type(exc).__name__}: {exc}")
    label = eventkit._ENTITY_LABEL[entity]
    if status == "authorized":
        return Check(name, True, "full access granted")
    if status == "not_determined":
        return Check(name, False, f"not granted yet: macOS will ask the first time Jarvis uses {label}")
    return Check(name, False, eventkit.permission_instructions(entity, status))


# --- shared helpers ------------------------------------------------------------------------------


class _AppleCalendarTool(Tool):
    """Base for the three tools: a shared (injectable) EventKitStore."""

    taints = True
    taint_source = "calendar"

    def __init__(self, ctx: ToolContext | None = None, store: "EventKitStore | None" = None) -> None:
        super().__init__(ctx)
        self._store = store

    @property
    def store(self) -> "EventKitStore":
        return self._store or eventkit.get_store()

    def _guarded(self, what: str, fn, *args, **kwargs):
        """Run a store call; EventKit's own errors pass through, anything unexpected becomes a ToolExecutionError."""
        try:
            return fn(*args, **kwargs)
        except (ToolError, PermissionMissingError):
            raise
        except Exception as exc:
            log.exception("apple calendar: unexpected failure while %s", what)
            raise ToolExecutionError(f"Apple Calendar failed while {what}: {type(exc).__name__}: {exc}") from exc


def _parse_range(text: str) -> tuple[datetime, datetime]:
    try:
        return parse_date_range(text)
    except ValueError as exc:  # timeutil lets impossible dates (2026-13-45) through as ValueError
        raise ToolValidationError(f"invalid date range {text!r}: {exc}") from None


def _parse_dt(text: str) -> datetime:
    try:
        return parse_datetime(text)
    except ValueError as exc:
        raise ToolValidationError(f"invalid date/time {text!r}: {exc}") from None


def _find_calendar(calendars: list["CalendarRecord"], name: str) -> "CalendarRecord | None":
    wanted = name.strip().casefold()
    for cal in calendars:
        if cal.title.strip().casefold() == wanted:
            return cal
    return None


def _names(calendars: list["CalendarRecord"]) -> str:
    return ", ".join(sorted(c.title for c in calendars)) or "(none)"


# --- apple_list_events ----------------------------------------------------------------------------


class AppleListEventsInput(BaseModel):
    date_range: str = Field(
        default="today",
        description=(
            "Which days: 'today', 'tomorrow', 'week', 'next week', 'weekend', a date 'YYYY-MM-DD', or a range 'YYYY-MM-DD..YYYY-MM-DD'."
        ),
    )
    calendar: str | None = Field(default=None, description="Only this Apple calendar (by name). Default: all.")
    include_filtered: bool = Field(
        default=False,
        description="Also show events a calendar filter hides. Only when the user explicitly asks about them.",
    )


class AppleListEvents(_AppleCalendarTool):
    name = "apple_list_events"
    description = (
        "List events from the Apple Calendar app (iCloud, local and subscribed calendars) for a date range. "
        "Use this for the Mac's own calendars; Google Calendar has its own tool."
    )
    input_model = AppleListEventsInput
    risk = RiskLevel.SAFE
    delegate_ok = True

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, AppleListEventsInput)
        start, end = _parse_range(args.date_range)
        if end - start > timedelta(days=MAX_RANGE_DAYS):
            raise ToolValidationError(f"date_range covers more than {MAX_RANGE_DAYS} days; narrow it down")
        ctx = self.require_ctx()
        events = self._guarded("reading events", fetch_events, ctx.config, start, end, self.store)
        if args.calendar is not None:
            wanted = args.calendar.strip().casefold()
            available = self._guarded("listing calendars", readable_calendars, ctx.config, self.store)
            if _find_calendar(available, args.calendar) is None:
                raise ToolValidationError(f"no Apple calendar named {args.calendar!r}; available: {_names(available)}")
            events = [e for e in events if e.calendar.strip().casefold() == wanted]
        filter_note = ""
        if not args.include_filtered:
            outcome = apply_filters(ctx.config, events)
            events = outcome.kept
            filter_note = ("\n" + outcome.note()) if outcome.hidden else ""
        header = f"Apple Calendar, {format_local(start)} to {format_local(end)}:"
        body = format_events(events, empty="No events.")
        read_only = sum(1 for e in events if e.read_only)
        note = f"\n({read_only} event(s) are in read-only calendars.)" if read_only else ""
        return ToolResult(f"{header}\n{body}{note}{filter_note}")


# --- apple_create_event ---------------------------------------------------------------------------


class AppleCreateEventInput(BaseModel):
    title: str = Field(description="Event title.")
    start: str = Field(description="Start as 'YYYY-MM-DD HH:MM' (local time), or 'YYYY-MM-DD' for an all-day event.")
    end: str | None = Field(
        default=None,
        description="End as 'YYYY-MM-DD HH:MM'. Default: one hour after start (or the same day for all-day events).",
    )
    all_day: bool = Field(default=False, description="True for an all-day event (also implied by a bare start date).")
    calendar: str | None = Field(default=None, description="Apple calendar name. Default: the Calendar app's default.")
    location: str | None = Field(default=None, description="Optional location.")
    notes: str | None = Field(default=None, description="Optional notes.")


class AppleCreateEvent(_AppleCalendarTool):
    name = "apple_create_event"
    description = "Create an event in the Apple Calendar app. Asks for confirmation. Subscribed and other read-only calendars are refused."
    input_model = AppleCreateEventInput
    risk = RiskLevel.CONFIRM

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, AppleCreateEventInput)
        title = args.title.strip()
        if not title:
            raise ToolValidationError("title is empty")
        all_day = args.all_day or _is_bare_date(args.start)
        start = _parse_dt(args.start)
        if args.end is None:
            end = start + timedelta(days=1) if all_day else start + timedelta(hours=1)
        else:
            end = _parse_dt(args.end)
            if all_day and _is_bare_date(args.end):
                end = end + timedelta(days=1)  # a bare end date means through the end of that day
        if end <= start:
            raise ToolValidationError("end must be after start")

        store = self.store
        self._guarded("checking access", store.ensure_access, "events")
        calendars = self._guarded("listing calendars", store.calendars, "events")
        writable = [c for c in calendars if c.allows_modifications]
        if args.calendar is None:
            calendar = self._guarded("reading the default calendar", store.default_calendar, "events")
            if calendar is None:
                raise ToolExecutionError(
                    f"Apple Calendar has no default calendar for new events; pass calendar=... (writable: {_names(writable)})"
                )
        else:
            calendar = _find_calendar(calendars, args.calendar)
            if calendar is None:
                raise ToolValidationError(f"no Apple calendar named {args.calendar!r}; writable calendars: {_names(writable)}")
        if not calendar.allows_modifications:
            why = "a subscribed" if calendar.is_subscribed else "a read-only"
            raise ToolExecutionError(
                f"calendar {calendar.title!r} is {why} calendar and cannot be changed; writable calendars: {_names(writable)}"
            )

        record = self._guarded(
            "creating the event",
            store.create_event,
            title,
            start,
            end,
            calendar_id=calendar.identifier,
            location=(args.location or "").strip() or None,
            notes=(args.notes or "").strip() or None,
            all_day=all_day,
        )
        when = (
            f"{format_local(record.start)[:10]} all day"
            if record.all_day
            else f"{format_local(record.start)} to {format_local(record.end)}"
        )
        return ToolResult(f"Created Apple Calendar event {record.title!r} in {record.calendar!r}, {when} [id {record.identifier}].")


def _is_bare_date(text: str) -> bool:
    text = text.strip()
    return len(text) == 10 and text[4] == "-" and text[7] == "-" and text.replace("-", "").isdigit()


# --- apple_delete_event ---------------------------------------------------------------------------


class AppleDeleteEventInput(BaseModel):
    event_id: str = Field(description="The event id shown by apple_list_events (its calendarItemIdentifier).")


class AppleDeleteEvent(_AppleCalendarTool):
    name = "apple_delete_event"
    description = "Delete one event from the Apple Calendar app by id. Asks for confirmation."
    input_model = AppleDeleteEventInput
    risk = RiskLevel.CONFIRM

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, AppleDeleteEventInput)
        event_id = args.event_id.strip()
        if not event_id:
            raise ToolValidationError("event_id is empty")
        store = self.store
        self._guarded("checking access", store.ensure_access, "events")
        record = self._guarded("deleting the event", store.delete_event, event_id)
        when = format_local(record.start)[:10] if record.all_day else format_local(record.start)
        return ToolResult(f"Deleted Apple Calendar event {record.title!r} ({when}, {record.calendar!r}).")
