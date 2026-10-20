"""Apple Reminders tools with a fake EventKitStore: no EventKit, no permission prompt."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.tools.apple_reminders import CompleteReminder, CreateReminder, ListReminders, health_checks
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.utils import eventkit
from jarvis.utils.eventkit import CalendarRecord, ReminderRecord, permission_instructions
from jarvis.utils.timeutil import local_tz

TZ = local_tz()


def rlist(title: str, ident: str, *, writable: bool = True) -> CalendarRecord:
    return CalendarRecord(title=title, identifier=ident, allows_modifications=writable, is_subscribed=False, type="caldav")


def rem(
    ident: str,
    title: str,
    lst: CalendarRecord,
    due: datetime | None = None,
    *,
    all_day: bool = False,
    completed: bool = False,
    priority: int = 0,
) -> ReminderRecord:
    return ReminderRecord(ident, title, lst.title, lst.identifier, due=due, due_all_day=all_day, completed=completed, priority=priority)


class FakeStore:
    def __init__(
        self,
        lists: list[CalendarRecord],
        reminders: list[ReminderRecord] | None = None,
        default: CalendarRecord | None = None,
        denied: str | None = None,
    ) -> None:
        self._lists = lists
        self._reminders = reminders or []
        self._default = default
        self._denied = denied
        self.calls: list[tuple] = []

    def ensure_access(self, entity: str) -> None:
        self.calls.append(("ensure_access", entity))
        if self._denied:
            raise PermissionMissingError(permission_instructions(entity, self._denied))

    def calendars(self, entity: str) -> list[CalendarRecord]:
        self.calls.append(("calendars", entity))
        return list(self._lists)

    def default_calendar(self, entity: str) -> CalendarRecord | None:
        return self._default

    def reminders(self, calendars=None, include_completed=False) -> list[ReminderRecord]:
        self.calls.append(("reminders", calendars, include_completed))
        chosen = set(calendars) if calendars is not None else None
        return [r for r in self._reminders if (chosen is None or r.list_id in chosen) and (include_completed or not r.completed)]

    def create_reminder(self, title, *, due=None, due_all_day=False, calendar_id=None, notes=None) -> ReminderRecord:
        self.calls.append(("create_reminder", title, due, due_all_day, calendar_id, notes))
        lst = next(c for c in self._lists if c.identifier == calendar_id)
        return ReminderRecord("new-1", title, lst.title, calendar_id, due=due, due_all_day=due_all_day, notes=notes or "")

    def complete_reminder(self, identifier) -> ReminderRecord:
        self.calls.append(("complete_reminder", identifier))
        for r in self._reminders:
            if r.identifier == identifier:
                r.completed = True
                return r
        raise ToolExecutionError(f"no reminder with id {identifier!r}")


TODO = rlist("Reminders", "list-default")
WORK = rlist("Work", "list-work")
SHARED = rlist("Family (shared)", "list-shared", writable=False)


def today_at(hour: int) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def ctx(config, paths) -> ToolContext:
    return ToolContext.build(config, paths)


@pytest.fixture
def store() -> FakeStore:
    items = [
        rem("r1", "Buy milk", TODO, today_at(18)),
        rem("r2", "Pay rent", TODO, today_at(0), all_day=True, priority=1),
        rem("r3", "Call Ali", WORK, today_at(10) + timedelta(days=3)),
        rem("r4", "Someday", WORK),
        rem("r5", "Done thing", TODO, today_at(8), completed=True),
    ]
    return FakeStore([TODO, WORK, SHARED], items, default=TODO)


# --- metadata --------------------------------------------------------------------------


def test_tool_metadata():
    assert ListReminders.name == "list_reminders" and ListReminders.risk is RiskLevel.SAFE
    assert CreateReminder.name == "create_reminder" and CreateReminder.risk is RiskLevel.CONFIRM
    assert CompleteReminder.name == "complete_reminder" and CompleteReminder.risk is RiskLevel.CONFIRM
    for tool in (ListReminders, CreateReminder, CompleteReminder):
        assert tool.taints and tool.taint_source == "reminders"
        assert tool().api_schema()["name"] == tool.name


# --- list_reminders ----------------------------------------------------------------------


def test_list_all_open_reminders(ctx, store):
    tool = ListReminders(ctx, store=store)
    result = tool.run(tool.parse_args({}))
    lines = result.content.splitlines()
    assert lines[0] == "4 reminder(s):"
    assert "Done thing" not in result.content
    # dated first (earliest first), undated last; all-day due shows only the date; priority flagged
    assert lines[1].startswith("[ ] Pay rent !") and "(due " + today_at(0).strftime("%Y-%m-%d") + ")" in lines[1]
    assert lines[2].startswith("[ ] Buy milk (due ") and today_at(18).strftime("%Y-%m-%d %H:%M") in lines[2]
    assert lines[3].startswith("[ ] Call Ali")
    assert lines[4].startswith("[ ] Someday (list: Work) [id r4]")
    assert store.calls[0] == ("ensure_access", "reminders")
    assert ("reminders", None, False) in store.calls


def test_list_reminders_by_list_and_completed(ctx, store):
    tool = ListReminders(ctx, store=store)
    result = tool.run(tool.parse_args({"list_name": "work"}))
    assert "Call Ali" in result.content and "Buy milk" not in result.content
    assert ("reminders", ["list-work"], False) in store.calls
    result = tool.run(tool.parse_args({"include_completed": True}))
    assert "[x] Done thing" in result.content and result.content.startswith("5 reminder(s)")


def test_list_reminders_due_range(ctx, store):
    tool = ListReminders(ctx, store=store)
    result = tool.run(tool.parse_args({"due_range": "today"}))
    assert "Buy milk" in result.content and "Pay rent" in result.content
    assert "Call Ali" not in result.content and "Someday" not in result.content
    day = (today_at(0) + timedelta(days=3)).strftime("%Y-%m-%d")
    result = tool.run(tool.parse_args({"due_range": day, "list_name": "Work"}))
    assert "Call Ali" in result.content and "Someday" not in result.content
    result = tool.run(tool.parse_args({"due_range": "2000-01-01"}))
    assert result.content == "No reminders due 2000-01-01."


def test_list_reminders_bad_range_or_list(ctx, store):
    tool = ListReminders(ctx, store=store)
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"due_range": "2026-99-99"}))
    assert store.calls == []  # validated before touching the store
    with pytest.raises(ToolValidationError, match="available: Family \\(shared\\), Reminders, Work"):
        tool.run(tool.parse_args({"list_name": "Groceries"}))


def test_list_reminders_permission_denied(ctx):
    tool = ListReminders(ctx, store=FakeStore([TODO], denied="denied"))
    with pytest.raises(PermissionMissingError) as info:
        tool.run(tool.parse_args({}))
    assert "System Settings > Privacy & Security > Reminders" in str(info.value) and "Full Access" in str(info.value)


def test_list_reminders_unexpected_failure_is_tool_error(ctx, store):
    def boom(*a, **k):
        raise OSError("xpc connection interrupted")

    store.reminders = boom
    tool = ListReminders(ctx, store=store)
    with pytest.raises(ToolExecutionError, match="xpc connection interrupted"):
        tool.run(tool.parse_args({}))


# --- create_reminder -----------------------------------------------------------------------


def test_create_reminder_default_list_no_due(ctx, store):
    tool = CreateReminder(ctx, store=store)
    result = tool.run(tool.parse_args({"title": " Water plants "}))
    assert ("create_reminder", "Water plants", None, False, "list-default", None) in store.calls
    assert result.content == "Created reminder 'Water plants' in 'Reminders' [id new-1]."


def test_create_reminder_timed_due_and_notes(ctx, store):
    tool = CreateReminder(ctx, store=store)
    result = tool.run(tool.parse_args({"title": "Call bank", "due": "2026-10-21 09:30", "list_name": "WORK", "notes": "ask about fee"}))
    call = [c for c in store.calls if c[0] == "create_reminder"][0]
    assert call[2] == datetime(2026, 10, 21, 9, 30, tzinfo=TZ) and call[3] is False
    assert call[4] == "list-work" and call[5] == "ask about fee"
    assert "(due 2026-10-21 09:30)" in result.content and "'Work'" in result.content


def test_create_reminder_all_day_due(ctx, store):
    tool = CreateReminder(ctx, store=store)
    result = tool.run(tool.parse_args({"title": "Taxes", "due": "2026-10-31"}))
    call = [c for c in store.calls if c[0] == "create_reminder"][0]
    assert call[2] == datetime(2026, 10, 31, tzinfo=TZ) and call[3] is True
    assert "(due 2026-10-31)" in result.content and "00:00" not in result.content


@pytest.mark.parametrize(
    "args",
    [
        {"title": "X", "due": "next tuesday"},
        {"title": "", "due": "2026-10-31"},
        {"title": "X", "list_name": "Nope"},
    ],
)
def test_create_reminder_bad_arguments(ctx, store, args):
    tool = CreateReminder(ctx, store=store)
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args(args))
    assert not any(c[0] == "create_reminder" for c in store.calls)


def test_create_reminder_refuses_read_only_list(ctx, store):
    tool = CreateReminder(ctx, store=store)
    with pytest.raises(ToolExecutionError, match="read-only; writable lists: Reminders, Work"):
        tool.run(tool.parse_args({"title": "X", "list_name": "Family (shared)"}))
    assert not any(c[0] == "create_reminder" for c in store.calls)


def test_create_reminder_no_default_list(ctx):
    tool = CreateReminder(ctx, store=FakeStore([WORK], default=None))
    with pytest.raises(ToolExecutionError, match="no default list"):
        tool.run(tool.parse_args({"title": "X"}))


def test_create_reminder_permission_denied(ctx):
    tool = CreateReminder(ctx, store=FakeStore([TODO], denied="restricted"))
    with pytest.raises(PermissionMissingError, match="restricted"):
        tool.run(tool.parse_args({"title": "X"}))


# --- complete_reminder ---------------------------------------------------------------------


def test_complete_reminder(ctx, store):
    tool = CompleteReminder(ctx, store=store)
    result = tool.run(tool.parse_args({"reminder_id": " r1 "}))
    assert ("ensure_access", "reminders") in store.calls and ("complete_reminder", "r1") in store.calls
    assert result.content == "Completed reminder 'Buy milk' (list: Reminders)."


def test_complete_reminder_unknown_or_empty_id(ctx, store):
    tool = CompleteReminder(ctx, store=store)
    with pytest.raises(ToolExecutionError, match="no reminder with id 'zzz'"):
        tool.run(tool.parse_args({"reminder_id": "zzz"}))
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"reminder_id": ""}))


def test_complete_reminder_permission_denied(ctx):
    tool = CompleteReminder(ctx, store=FakeStore([TODO], denied="denied"))
    with pytest.raises(PermissionMissingError):
        tool.run(tool.parse_args({"reminder_id": "r1"}))


# --- health checks -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, ok, needle",
    [
        ("authorized", True, "full access"),
        ("not_determined", False, "not granted yet"),
        ("denied", False, "Privacy & Security > Reminders"),
    ],
)
def test_health_checks_status_only(config, paths, monkeypatch, status, ok, needle):
    monkeypatch.setattr(eventkit.EventKitStore, "authorization_status", staticmethod(lambda entity: status))
    checks = health_checks(config, paths)
    assert len(checks) == 1 and checks[0].name == "Apple Reminders access"
    assert checks[0].ok is ok and needle in checks[0].detail


def test_health_checks_never_raise(config, paths, monkeypatch):
    def broken(entity):
        raise RuntimeError("objc exploded")

    monkeypatch.setattr(eventkit.EventKitStore, "authorization_status", staticmethod(broken))
    checks = health_checks(config, paths)
    assert checks[0].ok is False and "objc exploded" in checks[0].detail
