"""google_calendar tools against a fake googleapiclient service (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.tools import google_calendar as gc
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.utils import google_auth
from jarvis.utils.timeutil import local_tz

# --- fake service ---------------------------------------------------------------------------


class FakeService:
    """`service.events().list(**kw)` -> request. A call without kwargs is a sub-resource, a call
    with kwargs is recorded as ("events.list", kw) and returns the canned response (or raises it).
    A list response is popped in order; a callable gets the kwargs."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def __getattr__(self, name):
        return _Resource(self, name)


class _Resource:
    def __init__(self, service: FakeService, path: str) -> None:
        self.service = service
        self.path = path

    def __call__(self, **kwargs):
        if not kwargs:
            return self
        self.service.calls.append((self.path, kwargs))
        outcome = self.service.responses.get(self.path, {})
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if callable(outcome):
            outcome = outcome(kwargs)
        return _Request(outcome)

    def __getattr__(self, name):
        return _Resource(self.service, f"{self.path}.{name}")


class _Request:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def execute(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def http_error(status: int, message: str):
    import httplib2
    from googleapiclient.errors import HttpError

    return HttpError(
        httplib2.Response({"status": status, "reason": "x"}),
        json.dumps({"error": {"message": message}}).encode(),
        uri="https://www.googleapis.com/calendar/v3/x",
    )


@pytest.fixture
def ctx(config, paths) -> ToolContext:
    return ToolContext.build(config, paths)


@pytest.fixture
def service(monkeypatch) -> FakeService:
    fake = FakeService()
    monkeypatch.setattr(google_auth, "build_service", lambda api, version: fake)
    return fake


def run(tool_cls, ctx, **args):
    tool = tool_cls(ctx)
    return tool.run(tool.parse_args(args))


TZ = local_tz()
LISTING = {
    "summary": "Kerem's calendar",
    "accessRole": "owner",
    "items": [
        {
            "id": "ev_timed",
            "summary": "Standup",
            "location": "Zoom",
            "start": {"dateTime": "2026-10-20T10:00:00+03:00"},
            "end": {"dateTime": "2026-10-20T10:30:00+03:00"},
            "attendees": [{"email": "a@example.com"}, {"email": "me@example.com", "self": True}],
        },
        {
            "id": "ev_allday",
            "summary": "Holiday",
            "start": {"date": "2026-10-20"},
            "end": {"date": "2026-10-21"},
        },
        {"id": "ev_gone", "summary": "Cancelled", "status": "cancelled", "start": {"date": "2026-10-20"}},
        {
            "id": "ev_naive",
            "summary": "Naive",
            "start": {"dateTime": "2026-10-20T15:00:00", "timeZone": "Europe/Istanbul"},
            "end": {"dateTime": "2026-10-20T16:00:00", "timeZone": "Europe/Istanbul"},
        },
    ],
}


# --- metadata ---------------------------------------------------------------------------------


def test_tool_metadata():
    assert [t.name for t in (gc.GoogleListEvents, gc.GoogleCreateEvent, gc.GoogleUpdateEvent, gc.GoogleDeleteEvent)] == [
        "google_list_events",
        "google_create_event",
        "google_update_event",
        "google_delete_event",
    ]
    assert gc.GoogleListEvents.risk == RiskLevel.SAFE
    for t in (gc.GoogleCreateEvent, gc.GoogleUpdateEvent, gc.GoogleDeleteEvent):
        assert t.risk == RiskLevel.CONFIRM
    for t in (gc.GoogleListEvents, gc.GoogleCreateEvent, gc.GoogleUpdateEvent, gc.GoogleDeleteEvent):
        assert t.taints and t.taint_source == "calendar"


# --- fetch / list -----------------------------------------------------------------------------


def test_fetch_events_parameters_and_parsing(service, config):
    service.responses["events.list"] = LISTING
    start = datetime(2026, 10, 20, tzinfo=TZ)
    end = start + timedelta(days=1)
    events = gc.fetch_events(config, start, end)

    path, kw = service.calls[0]
    assert path == "events.list"
    assert kw["calendarId"] == "primary" and kw["singleEvents"] is True and kw["orderBy"] == "startTime"
    assert kw["timeMin"] == start.isoformat() and kw["timeMax"] == end.isoformat()
    assert "+" in kw["timeMin"] or "-" in kw["timeMin"][10:]  # RFC3339 with offset

    by_id = {e.id: e for e in events}
    assert set(by_id) == {"ev_timed", "ev_allday", "ev_naive"}  # cancelled skipped
    timed = by_id["ev_timed"]
    assert timed.source == "google" and timed.calendar == "Kerem's calendar"
    assert not timed.all_day and timed.title == "Standup" and timed.location == "Zoom"
    assert timed.start == datetime.fromisoformat("2026-10-20T10:00:00+03:00")
    assert timed.attendees == ["a@example.com"]
    allday = by_id["ev_allday"]
    assert allday.all_day and allday.start == datetime(2026, 10, 20, tzinfo=TZ)
    assert allday.end == datetime(2026, 10, 21, tzinfo=TZ)
    naive = by_id["ev_naive"]
    assert naive.start.utcoffset() == timedelta(hours=3)
    assert events == sorted(events, key=lambda e: e.sort_key())


def test_fetch_events_paginates(service, config):
    service.responses["events.list"] = [
        {
            "summary": "Cal",
            "items": [{"id": "a", "summary": "A", "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-21"}}],
            "nextPageToken": "p2",
        },
        {"summary": "Cal", "items": [{"id": "b", "summary": "B", "start": {"date": "2026-10-21"}, "end": {"date": "2026-10-22"}}]},
    ]
    start = datetime(2026, 10, 20, tzinfo=TZ)
    events = gc.fetch_events(config, start, start + timedelta(days=7))
    assert [e.id for e in events] == ["a", "b"]
    assert service.calls[1][1]["pageToken"] == "p2"


def test_fetch_events_propagates_auth_and_api_failures(monkeypatch, service, config):
    start = datetime(2026, 10, 20, tzinfo=TZ)
    service.responses["events.list"] = http_error(500, "Backend Error")
    with pytest.raises(ToolExecutionError, match="500"):
        gc.fetch_events(config, start, start + timedelta(days=1))

    def no_login(api, version):
        raise PermissionMissingError("run `jarvis google login`")

    monkeypatch.setattr(google_auth, "build_service", no_login)
    with pytest.raises(PermissionMissingError):
        gc.fetch_events(config, start, start + timedelta(days=1))


def test_list_events_tool_output(service, ctx):
    service.responses["events.list"] = LISTING
    result = run(gc.GoogleListEvents, ctx, date_range="2026-10-20")
    assert "3 Google Calendar event(s)" in result.content
    assert "Standup @ Zoom (google: Kerem's calendar) [id ev_timed]" in result.content
    assert "all day: Holiday" in result.content and "[id ev_allday]" in result.content
    assert "Cancelled" not in result.content
    assert "<untrusted" not in result.content  # the brain wraps, not the tool


def test_list_events_empty_and_bad_range(service, ctx):
    service.responses["events.list"] = {"summary": "Cal", "items": []}
    assert run(gc.GoogleListEvents, ctx, date_range="tomorrow").content.startswith("No Google Calendar events")
    with pytest.raises(ToolValidationError):
        run(gc.GoogleListEvents, ctx, date_range="someday")
    with pytest.raises(ToolValidationError):
        run(gc.GoogleListEvents, ctx, date_range="today", calendar_id="bad id/with spaces")


# --- create -----------------------------------------------------------------------------------


def test_create_event_payload_with_local_offset_and_attendees(service, ctx):
    service.responses["events.insert"] = {"id": "new1", "summary": "Lunch"}
    result = run(
        gc.GoogleCreateEvent,
        ctx,
        title="Lunch",
        start="2026-10-20 12:00",
        end="2026-10-20 13:00",
        attendees=[" a@example.com ", "b@example.org"],
        location="Cafe",
        description="bring laptop",
    )
    path, kw = service.calls[0]
    assert path == "events.insert" and kw["calendarId"] == "primary" and kw["sendUpdates"] == "none"
    body = kw["body"]
    assert body["summary"] == "Lunch" and body["location"] == "Cafe" and body["description"] == "bring laptop"
    assert body["start"] == {"dateTime": datetime(2026, 10, 20, 12, 0, tzinfo=TZ).isoformat()}
    assert body["end"] == {"dateTime": datetime(2026, 10, 20, 13, 0, tzinfo=TZ).isoformat()}
    assert body["start"]["dateTime"][-6] in "+-" or body["start"]["dateTime"].endswith("+00:00")
    assert body["attendees"] == [{"email": "a@example.com"}, {"email": "b@example.org"}]
    assert "Created Google Calendar event 'Lunch'" in result.content and "[id new1]" in result.content
    assert "not sent" in result.content


def test_create_event_send_invitations_flag(service, ctx):
    service.responses["events.insert"] = {"id": "x"}
    run(
        gc.GoogleCreateEvent,
        ctx,
        title="T",
        start="2026-10-20 12:00",
        end="2026-10-20 13:00",
        attendees=["a@example.com"],
        send_invitations=True,
    )
    assert service.calls[0][1]["sendUpdates"] == "all"


def test_create_all_day_event(service, ctx):
    service.responses["events.insert"] = {"id": "x"}
    result = run(gc.GoogleCreateEvent, ctx, title="Trip", start="2026-10-20", end="2026-10-21")
    body = service.calls[0][1]["body"]
    assert body["start"] == {"date": "2026-10-20"} and body["end"] == {"date": "2026-10-22"}  # exclusive end
    assert "all day" in result.content


def test_create_event_rejects_bad_input_before_any_call(service, ctx):
    with pytest.raises(ToolValidationError, match="invalid email"):
        run(gc.GoogleCreateEvent, ctx, title="T", start="2026-10-20 12:00", end="2026-10-20 13:00", attendees=["not-an-email"])
    with pytest.raises(ToolValidationError, match="line break"):
        run(
            gc.GoogleCreateEvent,
            ctx,
            title="T",
            start="2026-10-20 12:00",
            end="2026-10-20 13:00",
            attendees=["a@example.com\r\nbcc: x@y.com"],
        )
    with pytest.raises(ToolValidationError, match="header injection"):
        run(gc.GoogleCreateEvent, ctx, title="Evil\r\nX", start="2026-10-20 12:00", end="2026-10-20 13:00")
    with pytest.raises(ToolValidationError, match="end must be after start"):
        run(gc.GoogleCreateEvent, ctx, title="T", start="2026-10-20 12:00", end="2026-10-20 11:00")
    with pytest.raises(ToolValidationError, match="cannot parse"):
        run(gc.GoogleCreateEvent, ctx, title="T", start="next tuesday", end="2026-10-20 11:00")
    with pytest.raises(ToolValidationError, match="empty"):
        run(gc.GoogleCreateEvent, ctx, title="   ", start="2026-10-20 12:00", end="2026-10-20 13:00")
    assert service.calls == []


# --- update / delete -------------------------------------------------------------------------


def test_update_event_patches_only_given_fields(service, ctx):
    service.responses["events.patch"] = {"id": "ev1", "summary": "Standup (moved)"}
    result = run(gc.GoogleUpdateEvent, ctx, event_id="ev1", title="Standup (moved)", location="Room 2")
    path, kw = service.calls[0]
    assert path == "events.patch" and kw["calendarId"] == "primary" and kw["eventId"] == "ev1"
    assert kw["body"] == {"summary": "Standup (moved)", "location": "Room 2"}
    assert "Updated title, location" in result.content and "[id ev1]" in result.content

    service.calls.clear()
    run(gc.GoogleUpdateEvent, ctx, event_id="ev1", start="2026-10-20 14:00", end="2026-10-20 15:00")
    body = service.calls[0][1]["body"]
    assert set(body) == {"start", "end"}
    assert body["start"] == {"dateTime": datetime(2026, 10, 20, 14, 0, tzinfo=TZ).isoformat()}


def test_update_event_requires_a_change_and_valid_id(service, ctx):
    with pytest.raises(ToolValidationError, match="nothing to update"):
        run(gc.GoogleUpdateEvent, ctx, event_id="ev1")
    with pytest.raises(ToolValidationError, match="event_id"):
        run(gc.GoogleUpdateEvent, ctx, event_id="  ", title="x")
    with pytest.raises(ToolValidationError, match="header injection"):
        run(gc.GoogleUpdateEvent, ctx, event_id="ev1", title="a\nb")
    assert service.calls == []


def test_delete_event(service, ctx):
    service.responses["events.delete"] = ""
    result = run(gc.GoogleDeleteEvent, ctx, event_id="ev_timed")
    assert service.calls == [("events.delete", {"calendarId": "primary", "eventId": "ev_timed", "sendUpdates": "none"})]
    assert result.content == "Deleted Google Calendar event [id ev_timed]."


def test_http_errors_become_tool_errors(service, ctx):
    service.responses["events.delete"] = http_error(404, "Not Found")
    with pytest.raises(ToolExecutionError, match="not found"):
        run(gc.GoogleDeleteEvent, ctx, event_id="nope")
    service.responses["events.patch"] = http_error(401, "Invalid Credentials")
    with pytest.raises(PermissionMissingError, match="jarvis google login"):
        run(gc.GoogleUpdateEvent, ctx, event_id="ev1", title="x")
    service.responses["events.insert"] = ConnectionRefusedError()
    with pytest.raises(ToolExecutionError, match="network error"):
        run(gc.GoogleCreateEvent, ctx, title="T", start="2026-10-20 12:00", end="2026-10-20 13:00")
