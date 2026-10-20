from datetime import datetime, timedelta, timezone

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError
from jarvis.tools import calendar_merged
from jarvis.tools.base import ToolContext
from jarvis.tools.calendar_merged import Agenda, merge_calendars
from jarvis.utils.calendar_types import CalendarEvent

TZ = timezone(timedelta(hours=3))


def ev(source, title, hour, all_day=False):
    start = datetime(2026, 10, 20, hour, 0, tzinfo=TZ)
    return CalendarEvent(
        source=source,
        calendar=f"{source}-cal",
        title=title,
        start=start,
        end=start + timedelta(hours=1),
        all_day=all_day,
        id=f"{source}-{hour}",
    )


def patch_fetchers(monkeypatch, google=None, apple=None):
    def google_fetch(config, start, end):
        if isinstance(google, Exception):
            raise google
        return google or []

    def apple_fetch(config, start, end):
        if isinstance(apple, Exception):
            raise apple
        return apple or []

    monkeypatch.setattr(calendar_merged, "_fetchers", lambda: {"google": google_fetch, "apple": apple_fetch})


def test_merge_sorts_by_time_and_labels_source(config, paths, monkeypatch):
    patch_fetchers(
        monkeypatch, google=[ev("google", "Standup", 10)], apple=[ev("apple", "Dentist", 9), ev("apple", "Gym", 7, all_day=True)]
    )
    tool = Agenda(ToolContext.build(config, paths))
    out = tool.run(tool.parse_args({"date_range": "2026-10-20"}))
    lines = out.content.splitlines()
    assert lines[0].startswith("Agenda 2026-10-20 00:00 to 2026-10-21 00:00")
    assert "Gym" in lines[1] and "all day" in lines[1] and "(apple: apple-cal)" in lines[1]
    assert "Dentist" in lines[2] and "Standup" in lines[3] and "(google: google-cal)" in lines[3]
    assert not any(line.startswith("Note:") for line in lines)
    assert tool.taints and tool.taint_source == "calendar" and tool.risk.value == "SAFE"


def test_google_failure_falls_back_to_apple(config, paths, monkeypatch):
    patch_fetchers(monkeypatch, google=PermissionMissingError("run `jarvis google login`"), apple=[ev("apple", "Dentist", 9)])
    tool = Agenda(ToolContext.build(config, paths))
    out = tool.run(tool.parse_args({"date_range": "today"}))
    assert "Dentist" in out.content and "Note: google calendar unavailable: run `jarvis google login`" in out.content


def test_unexpected_crash_in_one_backend_is_contained(config, paths, monkeypatch):
    patch_fetchers(monkeypatch, google=[ev("google", "Standup", 10)], apple=RuntimeError("objc boom"))
    events, failures = merge_calendars(config, datetime(2026, 10, 20, tzinfo=TZ), datetime(2026, 10, 21, tzinfo=TZ))
    assert [e.title for e in events] == ["Standup"] and "RuntimeError: objc boom" in failures["apple"]


def test_both_failing_is_an_error(config, paths, monkeypatch):
    patch_fetchers(monkeypatch, google=ToolExecutionError("offline"), apple=PermissionMissingError("denied"))
    tool = Agenda(ToolContext.build(config, paths))
    with pytest.raises(ToolExecutionError, match="no calendar could be read"):
        tool.run(tool.parse_args({"date_range": "week"}))


def test_empty_agenda(config, paths, monkeypatch):
    patch_fetchers(monkeypatch)
    tool = Agenda(ToolContext.build(config, paths))
    assert "No events." in tool.run(tool.parse_args({})).content
