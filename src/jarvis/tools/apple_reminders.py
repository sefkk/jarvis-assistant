"""Apple Reminders tools via EventKit: list_reminders, create_reminder,
complete_reminder. Also `health_checks` for the doctor.

Reminder lists are EventKit calendars of the reminder entity type. Lists that do not allow
modifications are refused for writes with the list of writable ones.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis.exceptions import PermissionMissingError, ToolError, ToolExecutionError, ToolValidationError
from jarvis.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from jarvis.utils import eventkit
from jarvis.utils.timeutil import format_local, parse_date_range, parse_datetime

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check
    from jarvis.utils.eventkit import CalendarRecord, EventKitStore, ReminderRecord

log = logging.getLogger("jarvis.tools.apple_reminders")

MAX_LISTED = 200


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Reminders authorization status for `jarvis doctor`. Never prompts, never raises."""
    from jarvis.tools.apple_calendar import status_check  # noqa: PLC0415

    return [status_check("Apple Reminders access", "reminders")]


# --- shared helpers ------------------------------------------------------------------------------


class _AppleRemindersTool(Tool):
    taints = True
    taint_source = "reminders"

    def __init__(self, ctx: ToolContext | None = None, store: "EventKitStore | None" = None) -> None:
        super().__init__(ctx)
        self._store = store

    @property
    def store(self) -> "EventKitStore":
        return self._store or eventkit.get_store()

    def _guarded(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ToolError, PermissionMissingError):
            raise
        except Exception as exc:
            log.exception("apple reminders: unexpected failure while %s", what)
            raise ToolExecutionError(f"Apple Reminders failed while {what}: {type(exc).__name__}: {exc}") from exc

    def _lists(self) -> list["CalendarRecord"]:
        self._guarded("checking access", self.store.ensure_access, "reminders")
        return self._guarded("listing reminder lists", self.store.calendars, "reminders")


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


def _find_list(lists: list["CalendarRecord"], name: str) -> "CalendarRecord | None":
    wanted = name.strip().casefold()
    for item in lists:
        if item.title.strip().casefold() == wanted:
            return item
    return None


def _names(lists: list["CalendarRecord"]) -> str:
    return ", ".join(sorted(c.title for c in lists)) or "(none)"


def _due_text(r: "ReminderRecord") -> str:
    if r.due is None:
        return ""
    return f" (due {format_local(r.due)[:10] if r.due_all_day else format_local(r.due)})"


def format_reminder(r: "ReminderRecord") -> str:
    box = "[x]" if r.completed else "[ ]"
    prio = " !" if 0 < r.priority <= 4 else ""
    ident = f" [id {r.identifier}]" if r.identifier else ""
    return f"{box} {r.title}{prio}{_due_text(r)} (list: {r.list_name}){ident}"


# --- list_reminders --------------------------------------------------------------------------------


class ListRemindersInput(BaseModel):
    list_name: str | None = Field(default=None, description="Only this Reminders list (by name). Default: all lists.")
    include_completed: bool = Field(default=False, description="Also show completed reminders.")
    due_range: str | None = Field(
        default=None,
        description=(
            "Only reminders due in this range: 'today', 'tomorrow', 'week', 'YYYY-MM-DD' or "
            "'YYYY-MM-DD..YYYY-MM-DD'. Reminders without a due date are left out when set."
        ),
    )


class ListReminders(_AppleRemindersTool):
    name = "list_reminders"
    description = "List reminders from the Apple Reminders app, optionally one list or one due-date range."
    input_model = ListRemindersInput
    risk = RiskLevel.SAFE
    delegate_ok = True

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ListRemindersInput)
        window: tuple[datetime, datetime] | None = None
        if args.due_range is not None and args.due_range.strip():
            window = _parse_range(args.due_range)
        lists = self._lists()
        selected: list[str] | None = None
        if args.list_name is not None:
            target = _find_list(lists, args.list_name)
            if target is None:
                raise ToolValidationError(f"no Reminders list named {args.list_name!r}; available: {_names(lists)}")
            selected = [target.identifier]
        records = self._guarded("reading reminders", self.store.reminders, selected, args.include_completed)
        if not args.include_completed:
            records = [r for r in records if not r.completed]
        if window is not None:
            start, end = window
            records = [r for r in records if r.due is not None and start <= r.due < end]
        records.sort(key=_reminder_sort_key)
        scope = f" in {args.list_name!r}" if args.list_name else ""
        when = f" due {args.due_range.strip()}" if window else ""
        if not records:
            return ToolResult(f"No reminders{scope}{when}.")
        shown = records[:MAX_LISTED]
        lines = [f"{len(records)} reminder(s){scope}{when}:"] + [format_reminder(r) for r in shown]
        if len(records) > len(shown):
            lines.append(f"[{len(records) - len(shown)} more not shown]")
        return ToolResult("\n".join(lines))


def _reminder_sort_key(r: "ReminderRecord") -> tuple[int, int, float, str]:
    return (1 if r.completed else 0, 0 if r.due else 1, r.due.timestamp() if r.due else 0.0, r.title.lower())


# --- create_reminder -------------------------------------------------------------------------------


class CreateReminderInput(BaseModel):
    title: str = Field(description="What to remind.")
    due: str | None = Field(
        default=None,
        description="Due date 'YYYY-MM-DD' (no time) or 'YYYY-MM-DD HH:MM' (local time). Default: no due date.",
    )
    list_name: str | None = Field(default=None, description="Reminders list name. Default: the app's default list.")
    notes: str | None = Field(default=None, description="Optional notes.")


class CreateReminder(_AppleRemindersTool):
    name = "create_reminder"
    description = "Create a reminder in the Apple Reminders app. Asks for confirmation."
    input_model = CreateReminderInput
    risk = RiskLevel.CONFIRM

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CreateReminderInput)
        title = args.title.strip()
        if not title:
            raise ToolValidationError("title is empty")
        due: datetime | None = None
        due_all_day = False
        if args.due is not None and args.due.strip():
            due = _parse_dt(args.due)
            due_all_day = _is_bare_date(args.due)
        lists = self._lists()
        writable = [c for c in lists if c.allows_modifications]
        if args.list_name is None:
            target = self._guarded("reading the default list", self.store.default_calendar, "reminders")
            if target is None:
                raise ToolExecutionError(f"Apple Reminders has no default list; pass list_name=... (writable: {_names(writable)})")
        else:
            target = _find_list(lists, args.list_name)
            if target is None:
                raise ToolValidationError(f"no Reminders list named {args.list_name!r}; writable lists: {_names(writable)}")
        if not target.allows_modifications:
            raise ToolExecutionError(f"Reminders list {target.title!r} is read-only; writable lists: {_names(writable)}")
        record = self._guarded(
            "creating the reminder",
            self.store.create_reminder,
            title,
            due=due,
            due_all_day=due_all_day,
            calendar_id=target.identifier,
            notes=(args.notes or "").strip() or None,
        )
        return ToolResult(f"Created reminder {record.title!r} in {record.list_name!r}{_due_text(record)} [id {record.identifier}].")


def _is_bare_date(text: str) -> bool:
    text = text.strip()
    return len(text) == 10 and text[4] == "-" and text[7] == "-" and text.replace("-", "").isdigit()


# --- complete_reminder -----------------------------------------------------------------------------


class CompleteReminderInput(BaseModel):
    reminder_id: str = Field(description="The reminder id shown by list_reminders.")


class CompleteReminder(_AppleRemindersTool):
    name = "complete_reminder"
    description = "Mark one reminder in the Apple Reminders app as completed, by id. Asks for confirmation."
    input_model = CompleteReminderInput
    risk = RiskLevel.CONFIRM

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CompleteReminderInput)
        reminder_id = args.reminder_id.strip()
        if not reminder_id:
            raise ToolValidationError("reminder_id is empty")
        self._guarded("checking access", self.store.ensure_access, "reminders")
        record = self._guarded("completing the reminder", self.store.complete_reminder, reminder_id)
        return ToolResult(f"Completed reminder {record.title!r} (list: {record.list_name}).")
