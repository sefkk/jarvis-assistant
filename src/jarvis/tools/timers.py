"""Timers and pomodoro: set_timer, list_timers, cancel_timer, start_pomodoro,
stop_pomodoro, plus `health_checks` for the doctor.

The tools only write rows; the launchd scheduler (`jarvis scheduler run`) fires them, so a
timer still works after the CLI exits. When the scheduler is not installed the timer is saved
anyway and the result and the sink warn the user.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.observability.db import Database
from jarvis.scheduler import launchd
from jarvis.scheduler.engine import PomodoroState
from jarvis.scheduler.store import KIND_POMODORO, KIND_TIMER, TimerRow, TimerStore
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolContext, ToolResult
from jarvis.utils.timeutil import format_local, now_local, parse_datetime

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.timers")

MIN_MINUTES = 0.1
MAX_MINUTES = 1440.0  # 24 hours; anything longer is a reminder, not a timer
_CLOCK_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def __getattr__(name: str) -> str:
    """`SCHEDULER_WARNING`, read as an attribute by other modules: the text in the current main language."""
    if name == "SCHEDULER_WARNING":
        return t("timers.scheduler_missing")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Scheduler agent state and pending timers for `jarvis doctor`. Never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    st = launchd.status()
    if not st.installed:
        checks.append(
            Check("Timer scheduler (launchd)", True, "not installed: timers and alarms need it (`jarvis scheduler install`)", warn=True)
        )
    elif not st.running:
        checks.append(Check("Timer scheduler (launchd)", False, f"{st.detail}; try `jarvis scheduler install` again"))
    else:
        checks.append(Check("Timer scheduler (launchd)", True, st.detail))
    try:
        db = Database(paths.db_file)
        try:
            store = TimerStore(db)
            pending = store.pending()
        finally:
            db.close()
        if pending:
            nxt = pending[0]
            detail = f"{len(pending)} pending, next {format_local(nxt.fire_at)} ({nxt.label})"
        else:
            detail = "none pending"
        checks.append(Check("Timers", True, detail))
    except Exception as exc:
        checks.append(Check("Timers", False, f"cannot read timers: {type(exc).__name__}: {exc}"))
    return checks


# --- shared ---------------------------------------------------------------------------------------


def _now() -> datetime:  # module-level so tests can freeze the clock
    return now_local()


def resolve_at(text: str, now: datetime) -> datetime:
    """'18:00' -> today at 18:00, or tomorrow if that moment has passed; otherwise
    'YYYY-MM-DD HH:MM' or ISO 8601 through parse_datetime. Must be in the future."""
    text = text.strip()
    match = _CLOCK_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ToolValidationError(f"invalid clock time {text!r}")
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when
    when = parse_datetime(text, now=now)
    if when <= now:
        raise ToolValidationError(f"{format_local(when)} is in the past; give a future time")
    if when - now > timedelta(days=366):
        raise ToolValidationError("timers can be at most a year ahead")
    return when


def _in_words(delta: timedelta) -> str:
    seconds = int(round(delta.total_seconds()))
    if seconds < 60:
        return f"in {seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}min")
    return "in " + " ".join(parts)


def describe_row(row: TimerRow, now: datetime) -> str:
    when = f"{format_local(row.fire_at)} ({_in_words(row.fire_at - now)})"
    if row.kind == KIND_POMODORO:
        state = PomodoroState.from_json(row.pomodoro_state_json)
        phase = state.describe() if state else "?"
        return f"#{row.id}  {when}  pomodoro {phase}: {row.label}"
    return f"#{row.id}  {when}  {row.label}"


class _TimerTool(Tool):
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = None

    def __init__(self, ctx: ToolContext | None = None, db: Database | None = None) -> None:
        super().__init__(ctx)
        self._db = db
        self._own_db: Database | None = None

    @property
    def db(self) -> Database:
        if self._db is not None:
            return self._db
        ctx = self.require_ctx()
        if ctx.runtime is not None and ctx.runtime.db is not None:
            return ctx.runtime.db
        if self._own_db is None:  # no Assistant around (evals, scripts): open the project db
            self._own_db = Database(ctx.paths.db_file)
        return self._own_db

    @property
    def store(self) -> TimerStore:
        return TimerStore(self.db)

    def scheduler_warning(self) -> str:
        """Empty when the launchd agent is installed; otherwise the warning, also sent to the sink."""
        if launchd.status().installed:
            return ""
        warning = t("timers.scheduler_missing")
        runtime = self.ctx.runtime if self.ctx else None
        sink = getattr(runtime, "sink", None)
        if sink is not None:
            try:
                sink.notice(warning)
            except Exception:  # a sink problem must not fail the tool
                log.debug("could not send the scheduler notice to the sink", exc_info=True)
        log.warning("timer saved but the scheduler agent is not installed")
        return "\n" + warning


# --- set_timer ------------------------------------------------------------------------------------


class SetTimerInput(BaseModel):
    minutes: float | None = Field(
        default=None,
        ge=MIN_MINUTES,
        le=MAX_MINUTES,
        description="Duration from now in minutes (0.1 to 1440). Give exactly one of minutes or at.",
    )
    at: str | None = Field(
        default=None,
        description=(
            "Absolute time instead of a duration: 'HH:MM' (today, or tomorrow if already past), "
            "'YYYY-MM-DD HH:MM', or ISO 8601. Resolve relative words yourself first: 'tomorrow 09:00' becomes "
            "tomorrow's date as 'YYYY-MM-DD 09:00'. Give exactly one of minutes or at."
        ),
    )
    label: str = Field(
        min_length=1, max_length=200, description="What to remind, shown in the notification (e.g. 'Laundry'), in the user's language."
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "SetTimerInput":
        if (self.minutes is None) == (self.at is None):
            raise ValueError("give exactly one of minutes or at")
        return self


class SetTimer(_TimerTool):
    name = "set_timer"
    description = (
        "Set a one-shot timer that shows a macOS notification: either in N minutes or at a clock time. "
        "Fires through the background scheduler even when Jarvis is closed. Use create_reminder for "
        "to-dos further than a day away."
    )
    input_model = SetTimerInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SetTimerInput)
        now = _now()
        label = args.label.strip()
        if args.minutes is not None:
            fire_at = now + timedelta(minutes=args.minutes)
        else:
            fire_at = resolve_at(args.at or "", now)
        timer_id = self.store.add(label, fire_at, KIND_TIMER)
        log.info("timer %s set for %s: %s", timer_id, fire_at.isoformat(timespec="seconds"), label)
        text = f"Timer #{timer_id} set for {format_local(fire_at)} ({_in_words(fire_at - now)}): {label}"
        return ToolResult(text + self.scheduler_warning())


# --- list_timers ----------------------------------------------------------------------------------


class ListTimers(_TimerTool):
    name = "list_timers"
    description = "List pending timers and the running pomodoro phase with their fire times."
    input_model = EmptyInput

    def run(self, args: BaseModel) -> ToolResult:
        now = _now()
        rows = self.store.pending()
        if not rows:
            return ToolResult("No pending timers.")
        lines = [f"{len(rows)} pending timer(s):"] + [describe_row(r, now) for r in rows]
        return ToolResult("\n".join(lines))


# --- cancel_timer ---------------------------------------------------------------------------------


class CancelTimerInput(BaseModel):
    id: int | None = Field(default=None, ge=1, description="Timer id from list_timers or set_timer.")
    label: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Cancel every pending timer with this label (case-insensitive; partial match if nothing matches exactly).",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "CancelTimerInput":
        if (self.id is None) == (self.label is None):
            raise ValueError("give exactly one of id or label")
        return self


class CancelTimer(_TimerTool):
    name = "cancel_timer"
    description = "Cancel a pending timer by id or by label. Use stop_pomodoro for the pomodoro."
    input_model = CancelTimerInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CancelTimerInput)
        store = self.store
        if args.id is not None:
            row = store.get(args.id)
            if row is None:
                raise ToolValidationError(f"no timer with id {args.id}")
            if not store.cancel(args.id):
                return ToolResult(f"Timer #{args.id} ({row.label}) is already {row.status}.")
            log.info("timer %s cancelled", args.id)
            return ToolResult(f"Cancelled timer #{args.id} ({row.label}, was due {format_local(row.fire_at)}).")
        label = (args.label or "").strip()
        count = store.cancel_by_label(label)
        if count == 0:  # fall back to a substring match on the pending labels
            needle = label.lower()
            matches = [r for r in store.pending() if needle in r.label.lower()]
            for r in matches:
                store.cancel(r.id)
            count = len(matches)
        if count == 0:
            return ToolResult(f"No pending timer labelled {label!r}.")
        log.info("%d timer(s) cancelled by label %r", count, label)
        return ToolResult(f"Cancelled {count} timer(s) labelled {label!r}.")


# --- pomodoro -------------------------------------------------------------------------------------


class StartPomodoroInput(BaseModel):
    work_min: int | None = Field(default=None, ge=1, le=180, description="Focus length in minutes (default from config, 25).")
    break_min: int | None = Field(default=None, ge=1, le=60, description="Short break in minutes (default 5).")
    long_break_min: int | None = Field(default=None, ge=1, le=120, description="Long break after the last cycle (default 15).")
    cycles: int | None = Field(default=None, ge=1, le=12, description="Number of focus cycles (default 4).")
    label: str | None = Field(default=None, max_length=200, description="Optional name of the task being worked on.")


class StartPomodoro(_TimerTool):
    name = "start_pomodoro"
    description = (
        "Start a pomodoro: alternating focus and break notifications ('Break time', 'Focus time, 2/4') "
        "until the cycles are done. Refuses if one is already running."
    )
    input_model = StartPomodoroInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, StartPomodoroInput)
        ctx = self.require_ctx()
        defaults = ctx.config.timers
        store = self.store
        active = store.active_pomodoro()
        if active is not None:
            state = PomodoroState.from_json(active.pomodoro_state_json)
            where = state.describe() if state else "unknown phase"
            raise ToolExecutionError(
                f"a pomodoro is already running ({where}, next change at {format_local(active.fire_at)}); call stop_pomodoro first"
            )
        label = (args.label or "").strip() or None
        state = PomodoroState.start(
            work_min=args.work_min or defaults.pomodoro_work_min,
            break_min=args.break_min or defaults.pomodoro_break_min,
            long_break_min=args.long_break_min or defaults.pomodoro_long_break_min,
            cycles=args.cycles or defaults.pomodoro_cycles,
            label=label,
        )
        now = _now()
        first_end = now + timedelta(minutes=state.work_min)
        timer_id = store.add(label or "Pomodoro", first_end, KIND_POMODORO, state)
        log.info("pomodoro %s started: %s", timer_id, state.plan())
        text = (
            f"Pomodoro started{f' for {label!r}' if label else ''}: {state.plan()}. "
            f"Focus 1/{state.cycles} runs until {format_local(first_end, with_date=False)}; "
            f"you get a notification at every phase change."
        )
        return ToolResult(text + self.scheduler_warning())


class StopPomodoro(_TimerTool):
    name = "stop_pomodoro"
    description = "Stop the running pomodoro (cancels its pending phase)."
    input_model = EmptyInput

    def run(self, args: BaseModel) -> ToolResult:
        active = self.store.active_pomodoro()
        count = self.store.cancel_pomodoro()
        if count == 0:
            return ToolResult("No pomodoro is running.")
        state = PomodoroState.from_json(active.pomodoro_state_json) if active else None
        where = f" (was in {state.describe()})" if state else ""
        log.info("pomodoro stopped%s", where)
        return ToolResult(f"Pomodoro stopped{where}.")
