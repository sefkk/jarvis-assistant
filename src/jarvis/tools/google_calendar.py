"""Google Calendar tools: google_list_events, google_create_event, google_update_event,
google_delete_event. OAuth scope: calendar.events only.

Event titles, descriptions and attendees come from outside, so every tool taints the turn.
`fetch_events` is the module-level reader the merged agenda uses; it raises ToolExecutionError
or PermissionMissingError so the caller can fall back to Apple Calendar.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolValidationError
from jarvis.security.guards import validate_email, validate_header_value
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.utils import google_auth
from jarvis.utils.calendar_filters import apply_filters
from jarvis.utils.calendar_types import CalendarEvent, format_events
from jarvis.utils.timeutil import format_local, local_tz, parse_date_range, parse_datetime

if TYPE_CHECKING:
    from jarvis.config import Config

PAGE_SIZE = 250
MAX_EVENTS = 1000
TITLE_MAX_CHARS = 500
TEXT_MAX_CHARS = 8000
MAX_ATTENDEES = 50
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-@.]{1,1024}$")
_CALENDAR_ID_RE = re.compile(r"^[A-Za-z0-9_\-@.#%+]{1,256}$")


# --- shared helpers ------------------------------------------------------------------------------


def _service() -> Any:
    return google_auth.build_service("calendar", "v3")


def _check_event_id(event_id: str) -> str:
    event_id = (event_id or "").strip()
    if not _EVENT_ID_RE.match(event_id):
        raise ToolValidationError("event_id is empty or malformed; use the id shown by google_list_events")
    return event_id


def _check_calendar_id(calendar_id: str) -> str:
    calendar_id = (calendar_id or "").strip() or "primary"
    if not _CALENDAR_ID_RE.match(calendar_id):
        raise ToolValidationError(f"invalid calendar_id {calendar_id!r}")
    return calendar_id


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())


def _parse_google_time(node: dict[str, Any] | None) -> tuple[datetime, bool] | None:
    """Google's start/end object: {"dateTime": RFC3339[, "timeZone": IANA]} or {"date": YYYY-MM-DD}."""
    if not isinstance(node, dict):
        return None
    if node.get("dateTime"):
        try:
            dt = datetime.fromisoformat(str(node["dateTime"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            tz: Any = local_tz()
            if node.get("timeZone"):
                try:
                    tz = ZoneInfo(str(node["timeZone"]))
                except Exception:
                    pass
            dt = dt.replace(tzinfo=tz)
        return dt, False
    if node.get("date"):
        try:
            d = date.fromisoformat(str(node["date"]))
        except ValueError:
            return None
        return datetime(d.year, d.month, d.day, tzinfo=local_tz()), True
    return None


def _to_calendar_event(item: dict[str, Any], calendar_name: str, read_only: bool) -> CalendarEvent | None:
    if item.get("status") == "cancelled":
        return None
    start = _parse_google_time(item.get("start"))
    end = _parse_google_time(item.get("end"))
    if start is None:
        return None
    if end is None:
        end = (start[0] + (timedelta(days=1) if start[1] else timedelta(hours=1)), start[1])
    attendees = [str(a.get("email")) for a in item.get("attendees") or [] if isinstance(a, dict) and a.get("email") and not a.get("self")]
    return CalendarEvent(
        source="google",
        calendar=calendar_name,
        title=str(item.get("summary") or "(no title)")[:TITLE_MAX_CHARS],
        start=start[0],
        end=end[0],
        all_day=start[1],
        location=str(item.get("location") or "")[:TITLE_MAX_CHARS],
        notes=str(item.get("description") or "")[:TEXT_MAX_CHARS],
        id=str(item.get("id") or ""),
        read_only=read_only,
        attendees=attendees,
    )


def fetch_events(config: "Config | None", start: datetime, end: datetime, calendar_id: str = "primary") -> list[CalendarEvent]:
    """Events between start and end (end exclusive), expanded recurrences, sorted by start time.

    Raises PermissionMissingError (not logged in) or ToolExecutionError (API/network) so the merged
    agenda can fall back to Apple results and say that Google failed.
    """
    calendar_id = _check_calendar_id(calendar_id)
    start, end = _aware(start), _aware(end)
    if end <= start:
        raise ToolValidationError("the end of the range must be after the start")
    service = _service()
    events: list[CalendarEvent] = []
    calendar_name = calendar_id
    read_only = False
    page_token: str | None = None
    while True:
        request = service.events().list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=PAGE_SIZE,
            pageToken=page_token,
        )
        response = google_auth.execute(request, "listing Google Calendar events")
        if not isinstance(response, dict):
            break
        calendar_name = str(response.get("summary") or calendar_name)
        read_only = response.get("accessRole") in ("reader", "freeBusyReader")
        for item in response.get("items") or []:
            event = _to_calendar_event(item, calendar_name, read_only)
            if event is not None:
                events.append(event)
        page_token = response.get("nextPageToken")
        if not page_token or len(events) >= MAX_EVENTS:
            break
    events.sort(key=CalendarEvent.sort_key)
    return events


def _time_body(dt: datetime, all_day: bool) -> dict[str, str]:
    if all_day:
        return {"date": dt.date().isoformat()}
    return {"dateTime": _aware(dt).isoformat()}


def _parse_bounds(start_text: str, end_text: str) -> tuple[datetime, datetime, bool]:
    """Two bare dates mean an all-day event (Google's end date is exclusive, so a one-day
    event has end = start + 1 day). Anything else is a timed event in the local zone."""
    all_day = bool(_DATE_ONLY_RE.match(start_text.strip()) and _DATE_ONLY_RE.match(end_text.strip()))
    start = parse_datetime(start_text)
    end = parse_datetime(end_text)
    if all_day:
        if end < start:
            raise ToolValidationError("end must not be before start")
        end = end + timedelta(days=1)
    elif end <= start:
        raise ToolValidationError("end must be after start")
    return start, end, all_day


def _describe(start: datetime, end: datetime, all_day: bool) -> str:
    if all_day:
        last = end - timedelta(days=1)
        if last.date() == start.date():
            return f"{start.strftime('%a %Y-%m-%d')} (all day)"
        return f"{start.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')} (all day)"
    return f"{format_local(start)} to {format_local(end)}"


# --- tools --------------------------------------------------------------------------------------


class ListEventsInput(BaseModel):
    date_range: str = Field(
        default="today",
        description=(
            "today, tomorrow, week, next week, weekend, YYYY-MM-DD, YYYY-MM-DD..YYYY-MM-DD, or 'YYYY-MM-DD HH:MM..YYYY-MM-DD HH:MM'."
        ),
    )
    calendar_id: str = Field(default="primary", description="Google calendar id. Default: the primary calendar.")
    include_filtered: bool = Field(
        default=False,
        description="Also show events a calendar filter hides. Only when the user explicitly asks about them.",
    )


class GoogleListEvents(Tool):
    name = "google_list_events"
    description = (
        "List events from the user's Google Calendar for a date range. Each line ends with the event id "
        "needed by google_update_event and google_delete_event."
    )
    input_model = ListEventsInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "calendar"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ListEventsInput)
        ctx = self.require_ctx()
        start, end = parse_date_range(args.date_range)
        events = fetch_events(ctx.config, start, end, args.calendar_id)
        note = ""
        if not args.include_filtered:
            outcome = apply_filters(ctx.config, events)
            events = outcome.kept
            note = ("\n" + outcome.note()) if outcome.hidden else ""
        span = f"{format_local(start)} to {format_local(end)}"
        if not events:
            return ToolResult(f"No Google Calendar events between {span}.{note}")
        header = f"{len(events)} Google Calendar event(s) between {span}:"
        return ToolResult(header + "\n" + format_events(events) + note)


class CreateEventInput(BaseModel):
    title: str = Field(description="Event title.")
    start: str = Field(description="Start: 'YYYY-MM-DD HH:MM' (local time) or 'YYYY-MM-DD' for an all-day event.")
    end: str = Field(description="End: 'YYYY-MM-DD HH:MM', or the last day 'YYYY-MM-DD' for an all-day event.")
    description: str | None = Field(default=None, description="Optional notes.")
    location: str | None = Field(default=None, description="Optional location.")
    attendees: list[str] | None = Field(default=None, description="Optional list of attendee email addresses.")
    calendar_id: str = Field(default="primary", description="Google calendar id. Default: the primary calendar.")
    send_invitations: bool = Field(default=False, description="Email invitations to the attendees. Default false: nobody is emailed.")


class GoogleCreateEvent(Tool):
    name = "google_create_event"
    description = "Create an event in the user's Google Calendar. Asks for confirmation."
    input_model = CreateEventInput
    risk = RiskLevel.CONFIRM
    taints = True
    taint_source = "calendar"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CreateEventInput)
        self.require_ctx()
        title = validate_header_value("title", args.title).strip()
        if not title:
            raise ToolValidationError("title is empty")
        if len(title) > TITLE_MAX_CHARS:
            raise ToolValidationError(f"title longer than {TITLE_MAX_CHARS} characters")
        calendar_id = _check_calendar_id(args.calendar_id)
        start, end, all_day = _parse_bounds(args.start, args.end)
        attendees = [validate_email(a) for a in (args.attendees or [])]
        if len(attendees) > MAX_ATTENDEES:
            raise ToolValidationError(f"more than {MAX_ATTENDEES} attendees")
        body: dict[str, Any] = {
            "summary": title,
            "start": _time_body(start, all_day),
            "end": _time_body(end, all_day),
        }
        if args.description:
            body["description"] = args.description[:TEXT_MAX_CHARS]
        if args.location:
            body["location"] = validate_header_value("location", args.location)[:TITLE_MAX_CHARS]
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        request = (
            _service()
            .events()
            .insert(
                calendarId=calendar_id,
                body=body,
                sendUpdates="all" if (attendees and args.send_invitations) else "none",
            )
        )
        created = google_auth.execute(request, "creating the Google Calendar event")
        created = created if isinstance(created, dict) else {}
        event_id = str(created.get("id") or "")
        note = ""
        if attendees:
            note = f" Invitations {'sent' if args.send_invitations else 'not sent'} to {len(attendees)} attendee(s)."
        ident = f" [id {event_id}]" if event_id else ""
        return ToolResult(f"Created Google Calendar event '{title}' {_describe(start, end, all_day)}.{note}{ident}")


class UpdateEventInput(BaseModel):
    event_id: str = Field(description="The event id from google_list_events.")
    title: str | None = Field(default=None, description="New title.")
    start: str | None = Field(default=None, description="New start ('YYYY-MM-DD HH:MM' or 'YYYY-MM-DD').")
    end: str | None = Field(default=None, description="New end ('YYYY-MM-DD HH:MM' or 'YYYY-MM-DD').")
    description: str | None = Field(default=None, description="New notes.")
    location: str | None = Field(default=None, description="New location.")
    calendar_id: str = Field(default="primary", description="Google calendar id. Default: the primary calendar.")


class GoogleUpdateEvent(Tool):
    name = "google_update_event"
    description = "Change the title, time, notes or location of an existing Google Calendar event. Only the given fields change. Asks for confirmation."
    input_model = UpdateEventInput
    risk = RiskLevel.CONFIRM
    taints = True
    taint_source = "calendar"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, UpdateEventInput)
        self.require_ctx()
        event_id = _check_event_id(args.event_id)
        calendar_id = _check_calendar_id(args.calendar_id)
        body: dict[str, Any] = {}
        changed: list[str] = []
        if args.title is not None:
            title = validate_header_value("title", args.title).strip()
            if not title:
                raise ToolValidationError("title is empty")
            body["summary"] = title[:TITLE_MAX_CHARS]
            changed.append("title")
        if args.start is not None and args.end is not None:
            start, end, all_day = _parse_bounds(args.start, args.end)
            body["start"] = _time_body(start, all_day)
            body["end"] = _time_body(end, all_day)
            changed.append("time")
        elif args.start is not None or args.end is not None:
            # One side only: patch it as a timed value. Google rejects an end before the start.
            which = "start" if args.start is not None else "end"
            text = args.start if args.start is not None else args.end
            assert text is not None
            all_day = bool(_DATE_ONLY_RE.match(text.strip()))
            dt = parse_datetime(text)
            if all_day and which == "end":
                dt = dt + timedelta(days=1)
            body[which] = _time_body(dt, all_day)
            changed.append(which)
        if args.description is not None:
            body["description"] = args.description[:TEXT_MAX_CHARS]
            changed.append("description")
        if args.location is not None:
            body["location"] = validate_header_value("location", args.location)[:TITLE_MAX_CHARS]
            changed.append("location")
        if not body:
            raise ToolValidationError("nothing to update: give at least one of title, start, end, description, location")
        request = _service().events().patch(calendarId=calendar_id, eventId=event_id, body=body)
        updated = google_auth.execute(request, "updating the Google Calendar event")
        updated = updated if isinstance(updated, dict) else {}
        title = str(updated.get("summary") or body.get("summary") or event_id)
        return ToolResult(f"Updated {', '.join(changed)} of Google Calendar event '{title}' [id {event_id}].")


class DeleteEventInput(BaseModel):
    event_id: str = Field(description="The event id from google_list_events.")
    calendar_id: str = Field(default="primary", description="Google calendar id. Default: the primary calendar.")


class GoogleDeleteEvent(Tool):
    name = "google_delete_event"
    description = "Delete an event from the user's Google Calendar by id. Asks for confirmation."
    input_model = DeleteEventInput
    risk = RiskLevel.CONFIRM
    taints = True
    taint_source = "calendar"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, DeleteEventInput)
        self.require_ctx()
        event_id = _check_event_id(args.event_id)
        calendar_id = _check_calendar_id(args.calendar_id)
        request = _service().events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates="none")
        google_auth.execute(request, "deleting the Google Calendar event")
        return ToolResult(f"Deleted Google Calendar event [id {event_id}].")
