"""agenda: reads Google and Apple calendars together, merged by time, each event labeled.

Fallback: if one side fails (no token, no permission, network), the other side's
events are returned and the failure is stated.

Per-calendar filters (`[[calendar.filters]]`, utils/calendar_filters.py) are applied here, so the
agenda, the daily brief and the match-day detection all see the same filtered list. The hidden
events come back with `include_filtered=true`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, Field

from jarvis.exceptions import JarvisError, ToolExecutionError
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.utils.calendar_filters import FilterOutcome, apply_filters, describe, filter_for
from jarvis.utils.calendar_types import CalendarEvent, format_events
from jarvis.utils.timeutil import format_local, now_local, parse_date_range

if TYPE_CHECKING:
    from jarvis.config import CalendarFilter, Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.calendar")
REPORT_DAYS = 7  # `jarvis doctor` / `jarvis calendar filters` count what is hidden in the coming week

Fetcher = Callable[..., list[CalendarEvent]]


def _fetchers() -> dict[str, Fetcher]:
    """Imported lazily so a broken integration cannot take the other one down."""
    from jarvis.tools.apple_calendar import fetch_events as apple_fetch
    from jarvis.tools.google_calendar import fetch_events as google_fetch

    return {"google": google_fetch, "apple": apple_fetch}


class AgendaInput(BaseModel):
    date_range: str = Field(
        default="today",
        description="today, tomorrow, week, next week, weekend, YYYY-MM-DD, or YYYY-MM-DD..YYYY-MM-DD",
    )
    include_filtered: bool = Field(
        default=False,
        description=(
            "Also show the events a calendar filter hides (other teams' matches in the football calendar). "
            "Only when the user explicitly asks about those, e.g. another team's match."
        ),
    )


class Agenda(Tool):
    name = "agenda"
    description = (
        "List events from both the Google and the Apple calendar for a date range, merged by time, "
        "each labeled with its source. Use this for 'what do I have' questions."
    )
    input_model = AgendaInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "calendar"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, AgendaInput)
        config = self.require_ctx().config
        start, end = parse_date_range(args.date_range)
        events, failures, filtered = merge_calendars_detailed(config, start, end, include_filtered=args.include_filtered)
        header = f"Agenda {format_local(start)} to {format_local(end)}:"
        body = format_events(events, empty="No events.")
        notes = [f"Note: {name} calendar unavailable: {reason}" for name, reason in failures.items()]
        if not args.include_filtered and filtered.hidden:
            notes.append(filtered.note())
        return ToolResult("\n".join([header, body, *notes]))


def merge_calendars(
    config, start: datetime, end: datetime, *, include_filtered: bool = False
) -> tuple[list[CalendarEvent], dict[str, str]]:
    """Events from every source that works (calendar filters applied unless `include_filtered`),
    plus a reason for every source that failed."""
    events, failures, _ = merge_calendars_detailed(config, start, end, include_filtered=include_filtered)
    return events, failures


def merge_calendars_detailed(
    config, start: datetime, end: datetime, *, include_filtered: bool = False
) -> tuple[list[CalendarEvent], dict[str, str], FilterOutcome]:
    """Like `merge_calendars`, and also what the calendar filters kept and hid. With
    `include_filtered` every event is returned and the outcome's `hidden` list is empty."""
    try:
        fetchers = _fetchers()
    except Exception as exc:  # an import error in one module
        raise ToolExecutionError(f"calendar integrations unavailable: {exc}") from exc
    events, failures = _collect(config, start, end, fetchers)
    if len(failures) == len(fetchers):
        raise ToolExecutionError("no calendar could be read: " + "; ".join(f"{k}: {v}" for k, v in failures.items()))
    outcome = apply_filters(config, events)
    if include_filtered:
        return events, failures, FilterOutcome(kept=events)
    if outcome.hidden:
        log.debug("calendar filters hid %d event(s): %s", outcome.hidden_count, [e.title for e in outcome.hidden])
    return outcome.kept, failures, outcome


def _collect(config, start: datetime, end: datetime, fetchers: dict[str, Fetcher]) -> tuple[list[CalendarEvent], dict[str, str]]:
    """Every source's events, sorted, plus a reason per source that failed. Never raises."""
    events: list[CalendarEvent] = []
    failures: dict[str, str] = {}
    for name, fetch in fetchers.items():
        try:
            events.extend(fetch(config, start, end))
        except JarvisError as exc:
            failures[name] = str(exc)
            log.warning("%s calendar failed: %s", name, exc)
        except Exception as exc:  # never let one backend crash the agenda
            failures[name] = f"{type(exc).__name__}: {exc}"
            log.exception("%s calendar crashed", name)
    events.sort(key=CalendarEvent.sort_key)
    return events, failures


# --- `jarvis doctor` / `jarvis calendar filters` ---------------------------------------------------


@dataclass
class FilterReport:
    """What the configured filters do to the coming `days` days: per filter, the events it kept and
    hid in its calendar. `failures` are sources that could not be read, `skipped` sources that were
    not tried (Apple access not granted yet: reading would pop the macOS prompt)."""

    days: int
    filters: list["CalendarFilter"]
    kept: dict[str, list[CalendarEvent]] = field(default_factory=dict)  # filter calendar -> shown
    hidden: dict[str, list[CalendarEvent]] = field(default_factory=dict)  # filter calendar -> hidden
    failures: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def hidden_total(self) -> int:
        return sum(len(v) for v in self.hidden.values())

    def summary(self, flt: "CalendarFilter") -> str:
        kept, hidden = len(self.kept.get(flt.calendar, [])), len(self.hidden.get(flt.calendar, []))
        text = f"{describe(flt)}; next {self.days} days: {kept} shown, {hidden} hidden"
        problems = {**self.skipped, **self.failures}
        if problems:
            text += " (" + "; ".join(f"{k}: {v}" for k, v in problems.items()) + ")"
        return text


def filter_report(config: "Config", *, days: int = REPORT_DAYS, allow_prompt: bool = False) -> FilterReport:
    """Count what every filter hides from now on for `days` days. Never raises and, unless
    `allow_prompt`, never triggers the macOS Calendar permission prompt."""
    report = FilterReport(days=days, filters=list(config.calendar.filters))
    if not report.filters:
        return report
    try:
        fetchers = dict(_fetchers())
    except Exception as exc:
        report.failures["calendar"] = f"integrations unavailable: {exc}"
        return report
    if "apple" in fetchers and not allow_prompt:
        reason = _apple_not_ready()
        if reason:
            fetchers.pop("apple")
            report.skipped["apple"] = reason
    start = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    events, report.failures = _collect(config, start, start + timedelta(days=days), fetchers)
    for flt in report.filters:
        report.kept[flt.calendar], report.hidden[flt.calendar] = [], []
    outcome = apply_filters(config, events)
    for event in outcome.kept:
        flt = filter_for(config, event.calendar)
        if flt is not None:
            report.kept[flt.calendar].append(event)
    for event in outcome.hidden:
        flt = filter_for(config, event.calendar)
        if flt is not None:
            report.hidden[flt.calendar].append(event)
    return report


def _apple_not_ready() -> str:
    """Why the Apple calendar is not read without prompting: '' when full access is already granted."""
    try:
        from jarvis.utils.eventkit import EventKitStore

        status = EventKitStore.authorization_status("events")
    except Exception as exc:
        return f"EventKit unavailable ({type(exc).__name__})"
    return "" if status == "authorized" else f"not read, Calendar access is {status}"


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """`jarvis doctor`: the active calendar filters and how much each hides this week."""
    from jarvis.observability.health import Check  # noqa: PLC0415 (avoid import cycles at load time)

    if not config.calendar.filters:
        return [Check("Calendar filters", True, "none configured ([[calendar.filters]] in config.toml)")]
    report = filter_report(config)
    return [Check(f"Calendar filter: {flt.calendar}", True, report.summary(flt)) for flt in report.filters]
