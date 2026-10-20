"""Thin adapter around EventKit (pyobjc) for Apple Calendar and Reminders.

Everything Objective-C stays in this file. The tools only see plain dataclasses and
Python datetimes. `EventKit` and `Foundation` are imported lazily inside functions so
that tool discovery and the unit tests never touch the frameworks or trigger a
macOS permission prompt.

Access model (macOS 14+): Calendar and Reminders each need "Full Access"; the app that
gets the permission is the terminal (or whatever process) that runs Jarvis.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from jarvis.exceptions import PermissionMissingError, ToolExecutionError
from jarvis.utils.timeutil import local_tz

log = logging.getLogger("jarvis.eventkit")

Entity = Literal["events", "reminders"]

ACCESS_TIMEOUT_SECONDS = 30.0
FETCH_TIMEOUT_SECONDS = 20.0

# EKAuthorizationStatus values (EventKit.h). FullAccess shares the value 3 with the
# pre-macOS-14 "Authorized"; WriteOnly (4) exists only for events.
_STATUS_NAMES = {
    0: "not_determined",
    1: "restricted",
    2: "denied",
    3: "authorized",
    4: "write_only",
}

_CALENDAR_TYPES = {0: "local", 1: "caldav", 2: "exchange", 3: "subscription", 4: "birthday"}

_ENTITY_LABEL = {"events": "Calendars", "reminders": "Reminders"}


# --- plain records ----------------------------------------------------------------------


@dataclass
class CalendarRecord:
    title: str
    identifier: str
    allows_modifications: bool
    is_subscribed: bool
    type: str  # local, caldav, exchange, subscription, birthday, unknown


@dataclass
class EventRecord:
    identifier: str
    title: str
    calendar: str
    calendar_id: str
    start: datetime  # aware, local zone
    end: datetime  # aware, local zone; exclusive for all-day events (next midnight)
    all_day: bool = False
    location: str = ""
    notes: str = ""


@dataclass
class ReminderRecord:
    identifier: str
    title: str
    list_name: str
    list_id: str
    due: datetime | None = None  # aware, local zone
    due_all_day: bool = False  # the due date has no time of day
    completed: bool = False
    notes: str = ""
    priority: int = 0  # EventKit: 0 none, 1 high ... 9 low
    completion: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# --- instructions for the user --------------------------------------------------------------


def permission_instructions(entity: Entity, status: str = "denied") -> str:
    label = _ENTITY_LABEL[entity]
    what = {
        "denied": f"Access to {label} was denied.",
        "restricted": f"Access to {label} is restricted on this Mac (parental controls or a device profile).",
        "write_only": f"Jarvis only has write-only access to {label}; reading needs Full Access.",
        "timeout": f"The {label} permission prompt was not answered in time.",
        "not_determined": f"Jarvis has not been granted access to {label} yet.",
    }.get(status, f"Jarvis cannot access {label} (status: {status}).")
    return (
        f"{what} To fix it: open System Settings > Privacy & Security > {label}, find the terminal "
        "app that runs Jarvis (for example Terminal, iTerm or Ghostty) and switch it on; on macOS 14 "
        'or newer choose "Full Access". If the app is not listed, run Jarvis once more so macOS asks, '
        "or click the + button to add it. Then restart Jarvis."
    )


# --- the adapter --------------------------------------------------------------------------


class EventKitStore:
    """Owns one EKEventStore. Create it once; the store is built lazily on first use."""

    def __init__(self) -> None:
        self._store: Any = None
        self._granted: set[str] = set()
        self._lock = threading.Lock()

    # --- framework access ---------------------------------------------------------------------

    @property
    def store(self) -> Any:
        if self._store is None:
            try:
                import EventKit  # noqa: PLC0415 (lazy on purpose)
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ToolExecutionError("EventKit is not available: install pyobjc-framework-EventKit (uv sync)") from exc
            self._store = EventKit.EKEventStore.alloc().init()
        return self._store

    @staticmethod
    def _entity_type(entity: Entity) -> int:
        import EventKit  # noqa: PLC0415

        if entity == "events":
            return EventKit.EKEntityTypeEvent
        if entity == "reminders":
            return EventKit.EKEntityTypeReminder
        raise ValueError(f"unknown entity {entity!r}")

    # --- permissions ----------------------------------------------------------------------

    @staticmethod
    def authorization_status(entity: Entity) -> str:
        """Current status without prompting. One of the _STATUS_NAMES values or 'unknown'."""
        import EventKit  # noqa: PLC0415

        raw = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKitStore._entity_type(entity))
        return _STATUS_NAMES.get(int(raw), f"unknown({raw})")

    def ensure_access(self, entity: Entity) -> None:
        """Make sure Jarvis may read and write `entity`; prompt once if macOS has not asked yet."""
        if entity in self._granted:
            return
        with self._lock:
            if entity in self._granted:
                return
            status = self.authorization_status(entity)
            if status == "not_determined":
                status = self._request_access(entity)
            if status != "authorized":
                raise PermissionMissingError(permission_instructions(entity, status))
            self._granted.add(entity)

    def _request_access(self, entity: Entity) -> str:
        done = threading.Event()
        outcome: dict[str, Any] = {"granted": False, "error": None}

        def completion(granted: bool, error: Any) -> None:
            outcome["granted"] = bool(granted)
            outcome["error"] = error
            done.set()

        store = self.store
        try:
            if entity == "events" and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
                store.requestFullAccessToEventsWithCompletion_(completion)
            elif entity == "reminders" and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
                store.requestFullAccessToRemindersWithCompletion_(completion)
            else:  # macOS 13 and older
                store.requestAccessToEntityType_completion_(self._entity_type(entity), completion)
        except Exception as exc:
            raise ToolExecutionError(f"could not request {_ENTITY_LABEL[entity]} access: {exc}") from exc
        if not done.wait(ACCESS_TIMEOUT_SECONDS):
            return "timeout"
        if outcome["error"] is not None:
            log.warning("EventKit access request for %s reported: %s", entity, outcome["error"])
        if outcome["granted"]:
            return "authorized"
        # Re-read the real status so the message says denied/restricted/write-only correctly.
        status = self.authorization_status(entity)
        return status if status != "not_determined" else "denied"

    # --- calendars ---------------------------------------------------------------------------

    def calendars(self, entity: Entity) -> list[CalendarRecord]:
        try:
            raw = self.store.calendarsForEntityType_(self._entity_type(entity)) or []
            return [self._calendar_record(c) for c in raw]
        except Exception as exc:
            raise ToolExecutionError(f"could not list {_ENTITY_LABEL[entity]}: {exc}") from exc

    def default_calendar(self, entity: Entity) -> CalendarRecord | None:
        try:
            if entity == "events":
                cal = self.store.defaultCalendarForNewEvents()
            else:
                cal = self.store.defaultCalendarForNewReminders()
        except Exception as exc:
            raise ToolExecutionError(f"could not read the default {_ENTITY_LABEL[entity]} list: {exc}") from exc
        return self._calendar_record(cal) if cal is not None else None

    @staticmethod
    def _calendar_record(cal: Any) -> CalendarRecord:
        try:
            subscribed = bool(cal.isSubscribed())
        except Exception:  # older SDKs expose it only as a property
            subscribed = bool(getattr(cal, "subscribed", lambda: False)())
        return CalendarRecord(
            title=str(cal.title() or ""),
            identifier=str(cal.calendarIdentifier() or ""),
            allows_modifications=bool(cal.allowsContentModifications()),
            is_subscribed=subscribed,
            type=_CALENDAR_TYPES.get(int(cal.type()), "unknown"),
        )

    def _calendar_objects(self, entity: Entity, identifiers: list[str] | None) -> Any:
        """EKCalendar objects for the given identifiers, or None meaning 'all calendars'."""
        if identifiers is None:
            return None
        wanted = set(identifiers)
        raw = self.store.calendarsForEntityType_(self._entity_type(entity)) or []
        return [c for c in raw if str(c.calendarIdentifier()) in wanted]

    # --- events ----------------------------------------------------------------------------------

    def events(self, start: datetime, end: datetime, calendars: list[str] | None = None) -> list[EventRecord]:
        """Events overlapping [start, end). `calendars` are calendar identifiers; None means all."""
        if calendars is not None and not calendars:
            return []
        try:
            cal_objects = self._calendar_objects("events", calendars)
            predicate = self.store.predicateForEventsWithStartDate_endDate_calendars_(_to_nsdate(start), _to_nsdate(end), cal_objects)
            raw = self.store.eventsMatchingPredicate_(predicate) or []
            return [self._event_record(e) for e in raw]
        except Exception as exc:
            raise ToolExecutionError(f"could not read Apple Calendar events: {exc}") from exc

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        *,
        calendar_id: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        all_day: bool = False,
    ) -> EventRecord:
        import EventKit  # noqa: PLC0415

        try:
            store = self.store
            if calendar_id is None:
                calendar = store.defaultCalendarForNewEvents()
            else:
                calendar = store.calendarWithIdentifier_(calendar_id)
            if calendar is None:
                raise ToolExecutionError("the target calendar was not found in Apple Calendar")
            event = EventKit.EKEvent.eventWithEventStore_(store)
            event.setTitle_(title)
            event.setCalendar_(calendar)
            event.setAllDay_(bool(all_day))
            if all_day:
                first = _midnight(start)
                last = _midnight(end - timedelta(seconds=1))
                event.setStartDate_(_to_nsdate(first))
                event.setEndDate_(_to_nsdate(last + timedelta(hours=23, minutes=59, seconds=59)))
            else:
                event.setStartDate_(_to_nsdate(start))
                event.setEndDate_(_to_nsdate(end))
            if location:
                event.setLocation_(location)
            if notes:
                event.setNotes_(notes)
            ok, error = store.saveEvent_span_error_(event, EventKit.EKSpanThisEvent, None)
            if not ok:
                raise ToolExecutionError(f"Apple Calendar refused to save the event: {_describe(error)}")
            return self._event_record(event)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not create the Apple Calendar event: {exc}") from exc

    def delete_event(self, identifier: str) -> EventRecord:
        import EventKit  # noqa: PLC0415

        try:
            item = self.store.calendarItemWithIdentifier_(identifier)
            if item is None or not item.isKindOfClass_(EventKit.EKEvent):
                raise ToolExecutionError(f"no Apple Calendar event with id {identifier!r}")
            record = self._event_record(item)
            ok, error = self.store.removeEvent_span_error_(item, EventKit.EKSpanThisEvent, None)
            if not ok:
                raise ToolExecutionError(f"Apple Calendar refused to delete the event: {_describe(error)}")
            return record
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not delete the Apple Calendar event: {exc}") from exc

    @staticmethod
    def _event_record(event: Any) -> EventRecord:
        cal = event.calendar()
        all_day = bool(event.isAllDay())
        start = _from_nsdate(event.startDate())
        end = _from_nsdate(event.endDate())
        if all_day:
            # EventKit stores all-day events as 00:00:00 .. 23:59:59; expose an exclusive end.
            start = _midnight(start)
            end_local = end.astimezone(local_tz())
            end = _midnight(end_local)
            if end_local.time() != datetime.min.time():  # 23:59:59 of the last day -> next midnight
                end += timedelta(days=1)
            if end <= start:
                end = start + timedelta(days=1)
        return EventRecord(
            identifier=str(event.calendarItemIdentifier() or ""),
            title=str(event.title() or "(no title)"),
            calendar=str(cal.title() or "") if cal is not None else "",
            calendar_id=str(cal.calendarIdentifier() or "") if cal is not None else "",
            start=start,
            end=end,
            all_day=all_day,
            location=str(event.location() or ""),
            notes=str(event.notes() or ""),
        )

    # --- reminders --------------------------------------------------------------------------------

    def reminders(self, calendars: list[str] | None = None, include_completed: bool = False) -> list[ReminderRecord]:
        """Reminders in the given lists (identifiers; None = all). Async fetch, waited on."""
        if calendars is not None and not calendars:
            return []
        try:
            store = self.store
            cal_objects = self._calendar_objects("reminders", calendars)
            if include_completed:
                predicate = store.predicateForRemindersInCalendars_(cal_objects)
            else:
                predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, cal_objects)
            done = threading.Event()
            box: dict[str, Any] = {"items": None}

            def completion(items: Any) -> None:
                box["items"] = items
                done.set()

            store.fetchRemindersMatchingPredicate_completion_(predicate, completion)
            if not done.wait(FETCH_TIMEOUT_SECONDS):
                raise ToolExecutionError("Apple Reminders did not answer in time")
            return [self._reminder_record(r) for r in (box["items"] or [])]
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not read Apple Reminders: {exc}") from exc

    def create_reminder(
        self,
        title: str,
        *,
        due: datetime | None = None,
        due_all_day: bool = False,
        calendar_id: str | None = None,
        notes: str | None = None,
    ) -> ReminderRecord:
        import EventKit  # noqa: PLC0415

        try:
            store = self.store
            if calendar_id is None:
                calendar = store.defaultCalendarForNewReminders()
            else:
                calendar = store.calendarWithIdentifier_(calendar_id)
            if calendar is None:
                raise ToolExecutionError("the target list was not found in Apple Reminders")
            reminder = EventKit.EKReminder.reminderWithEventStore_(store)
            reminder.setTitle_(title)
            reminder.setCalendar_(calendar)
            if notes:
                reminder.setNotes_(notes)
            if due is not None:
                reminder.setDueDateComponents_(_to_components(due, with_time=not due_all_day))
                if not due_all_day:
                    # The Reminders app adds an alarm when a due time is set; match that.
                    # TODO(setup): drop this if you prefer silent due times.
                    reminder.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(_to_nsdate(due)))
            ok, error = store.saveReminder_commit_error_(reminder, True, None)
            if not ok:
                raise ToolExecutionError(f"Apple Reminders refused to save the reminder: {_describe(error)}")
            return self._reminder_record(reminder)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not create the reminder: {exc}") from exc

    def ensure_reminder_list(self, title: str) -> CalendarRecord:
        """The Reminders list named `title` (case-insensitive), created in the default reminders source
        (iCloud, so it syncs to the phone) when missing. Used by the alarm phone backup."""
        import EventKit  # noqa: PLC0415

        wanted = " ".join(title.casefold().split())
        for cal in self.calendars("reminders"):
            if " ".join(cal.title.casefold().split()) == wanted and cal.allows_modifications:
                return cal
        try:
            store = self.store
            calendar = EventKit.EKCalendar.calendarForEntityType_eventStore_(EventKit.EKEntityTypeReminder, store)
            calendar.setTitle_(title)
            default = store.defaultCalendarForNewReminders()
            source = default.source() if default is not None else None
            if source is None:
                sources = [s for s in (store.sources() or []) if int(s.sourceType()) in (1, 2)]  # local or CalDAV
                source = sources[0] if sources else None
            if source is None:
                raise ToolExecutionError("Apple Reminders has no account to create a list in")
            calendar.setSource_(source)
            ok, error = store.saveCalendar_commit_error_(calendar, True, None)
            if not ok:
                raise ToolExecutionError(f"Apple Reminders refused to create the list {title!r}: {_describe(error)}")
            log.info("reminders list %r created", title)
            return self._calendar_record(calendar)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not create the Reminders list {title!r}: {exc}") from exc

    def delete_reminder(self, identifier: str) -> bool:
        """Remove a reminder for good. False when it no longer exists."""
        import EventKit  # noqa: PLC0415

        try:
            item = self.store.calendarItemWithIdentifier_(identifier)
            if item is None or not item.isKindOfClass_(EventKit.EKReminder):
                return False
            ok, error = self.store.removeReminder_commit_error_(item, True, None)
            if not ok:
                raise ToolExecutionError(f"Apple Reminders refused to delete the reminder: {_describe(error)}")
            return True
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not delete the reminder: {exc}") from exc

    def complete_reminder(self, identifier: str) -> ReminderRecord:
        import EventKit  # noqa: PLC0415

        try:
            item = self.store.calendarItemWithIdentifier_(identifier)
            if item is None or not item.isKindOfClass_(EventKit.EKReminder):
                raise ToolExecutionError(f"no reminder with id {identifier!r}")
            item.setCompleted_(True)
            ok, error = self.store.saveReminder_commit_error_(item, True, None)
            if not ok:
                raise ToolExecutionError(f"Apple Reminders refused to complete the reminder: {_describe(error)}")
            return self._reminder_record(item)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"could not complete the reminder: {exc}") from exc

    @staticmethod
    def _reminder_record(reminder: Any) -> ReminderRecord:
        cal = reminder.calendar()
        due, all_day = _from_components(reminder.dueDateComponents())
        completion = reminder.completionDate()
        return ReminderRecord(
            identifier=str(reminder.calendarItemIdentifier() or ""),
            title=str(reminder.title() or "(no title)"),
            list_name=str(cal.title() or "") if cal is not None else "",
            list_id=str(cal.calendarIdentifier() or "") if cal is not None else "",
            due=due,
            due_all_day=all_day,
            completed=bool(reminder.isCompleted()),
            notes=str(reminder.notes() or ""),
            priority=int(reminder.priority() or 0),
            completion=_from_nsdate(completion) if completion is not None else None,
        )


# --- module-level shared store ---------------------------------------------------------------

_STORE: EventKitStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> EventKitStore:
    """One EKEventStore per process; the tools and the merged agenda share it."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = EventKitStore()
        return _STORE


# --- date conversion -----------------------------------------------------------------------------


def _to_nsdate(dt: datetime) -> Any:
    import Foundation  # noqa: PLC0415

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _from_nsdate(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value.timeIntervalSince1970()), tz=local_tz())


def _midnight(dt: datetime) -> datetime:
    local = dt.astimezone(local_tz())
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _to_components(dt: datetime, *, with_time: bool) -> Any:
    import Foundation  # noqa: PLC0415

    local = dt.astimezone(local_tz())
    comps = Foundation.NSDateComponents.alloc().init()
    comps.setYear_(local.year)
    comps.setMonth_(local.month)
    comps.setDay_(local.day)
    if with_time:
        comps.setHour_(local.hour)
        comps.setMinute_(local.minute)
        comps.setSecond_(0)
    return comps


def _from_components(comps: Any) -> tuple[datetime | None, bool]:
    """NSDateComponents -> (aware local datetime, is_all_day). (None, False) when unset."""
    if comps is None:
        return None, False
    import Foundation  # noqa: PLC0415

    undefined = Foundation.NSDateComponentUndefined
    year, month, day = int(comps.year()), int(comps.month()), int(comps.day())
    if undefined in (year, month, day):
        return None, False
    hour, minute = int(comps.hour()), int(comps.minute())
    all_day = hour == undefined or minute == undefined
    try:
        if all_day:
            return datetime(year, month, day, tzinfo=local_tz()), True
        second = int(comps.second())
        return datetime(year, month, day, hour, minute, 0 if second == undefined else second, tzinfo=local_tz()), False
    except ValueError:
        return None, False


def _describe(error: Any) -> str:
    if error is None:
        return "unknown error"
    try:
        return str(error.localizedDescription())
    except Exception:
        return str(error)
