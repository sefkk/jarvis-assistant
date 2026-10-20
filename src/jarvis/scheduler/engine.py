"""Pure scheduler logic: the pomodoro state machine and `fire_due`.

No I/O of its own beyond the store and the notifier it is handed, so tests drive it with a
fake clock and a recording notifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from jarvis.i18n import t
from jarvis.scheduler.notify import Notifier
from jarvis.scheduler.store import KIND_ALARM, KIND_POMODORO, KIND_SLEEP, KIND_SLEEP_NOTICE, TimerRow, TimerStore
from jarvis.utils.timeutil import format_local, local_tz, next_alarm_occurrence, now_local

log = logging.getLogger("jarvis.scheduler")

NOTIFICATION_TITLE = "Jarvis"
# Notification strings are what the owner sees on screen, so they come from locales/<language>/timers.toml.
# Other modules read these names as attributes; each access returns the text in the current main language.
_TEXTS = {
    "LATE_SUFFIX": "timers.late",
    "TEXT_BREAK": "timers.pomodoro.break",
    "TEXT_LONG_BREAK": "timers.pomodoro.long_break",
    "TEXT_FOCUS": "timers.pomodoro.focus",
    "TEXT_DONE": "timers.pomodoro.done",
}


def __getattr__(name: str) -> str:
    key = _TEXTS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return t(key)


Phase = Literal["work", "break", "long_break"]


class PomodoroState(BaseModel):
    """The phase a pending pomodoro row is *currently in*. When the row fires, that phase has
    ended: `notification_text()` announces what comes next and `next_phase()` builds it."""

    work_min: int = Field(ge=1)
    break_min: int = Field(ge=1)
    long_break_min: int = Field(ge=1)
    cycles: int = Field(ge=1)
    phase: Phase = "work"
    cycle: int = Field(default=1, ge=1)
    label: str | None = None

    @classmethod
    def start(cls, *, work_min: int, break_min: int, long_break_min: int, cycles: int, label: str | None = None) -> "PomodoroState":
        return cls(work_min=work_min, break_min=break_min, long_break_min=long_break_min, cycles=cycles, phase="work", cycle=1, label=label)

    @classmethod
    def from_json(cls, text: str | None) -> "PomodoroState | None":
        if not text:
            return None
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            log.error("corrupt pomodoro state ignored: %s", exc)
            return None

    def duration_min(self) -> int:
        """How long the current phase lasts."""
        if self.phase == "work":
            return self.work_min
        if self.phase == "break":
            return self.break_min
        return self.long_break_min

    def is_last_work(self) -> bool:
        return self.phase == "work" and self.cycle >= self.cycles

    def next_phase(self) -> "PomodoroState | None":
        """The phase that begins when this one ends, or None when the pomodoro is complete."""
        if self.phase == "work":
            return self.model_copy(update={"phase": "long_break" if self.is_last_work() else "break"})
        if self.phase == "break":
            return self.model_copy(update={"phase": "work", "cycle": self.cycle + 1})
        return None  # long break over: done

    def notification_text(self) -> str:
        """What to tell the user when this phase ends."""
        if self.phase == "work":
            return t("timers.pomodoro.long_break" if self.is_last_work() else "timers.pomodoro.break")
        if self.phase == "break":
            return t("timers.pomodoro.focus", cycle=self.cycle + 1, cycles=self.cycles)
        return t("timers.pomodoro.done")

    def describe(self) -> str:
        """Short human-readable position, e.g. `work 2/4` (used by list_timers)."""
        phase = {"work": "work", "break": "break", "long_break": "long break"}[self.phase]
        return f"{phase} {self.cycle}/{self.cycles}"

    def plan(self) -> str:
        return f"{self.cycles} x {self.work_min} min work with {self.break_min} min breaks, then a {self.long_break_min} min long break"


@dataclass
class FiredTimer:
    id: int
    kind: str
    label: str
    fire_at: datetime
    message: str
    late: bool
    notified: bool
    error: str | None = None
    next_id: int | None = None  # the pomodoro phase, or the next occurrence of a repeating alarm, scheduled after this one
    slept: bool = False  # a `sleep` row that actually ran `pmset sleepnow`
    rang: bool = False  # an `alarm` row that started ringing
    missed: bool = False  # an `alarm` row that was late (the Mac was asleep): notified as missed, not rung


def message_for(timer: TimerRow, state: PomodoroState | None) -> str:
    if timer.kind == KIND_POMODORO and state is not None:
        text = state.notification_text()
        return f"{text} ({state.label})" if state.label else text
    return timer.label


def _default_sleep_action(store: TimerStore) -> None:
    from jarvis.tools import power  # noqa: PLC0415 - the scheduler must start even if the power module breaks

    power.perform_scheduled_sleep(store.db)


def fire_due(
    store: TimerStore,
    notifier: Notifier,
    now: datetime | None = None,
    late_after_s: float = 120,
    *,
    sleep_notifier: Notifier | None = None,
    sleep_action: Callable[[], None] | None = None,
    alarm_handler: Any | None = None,
) -> list[FiredTimer]:
    """Fire every due pending timer once. Safe to call from several processes: a timer is
    marked fired (UPDATE ... WHERE status='pending') *before* it is notified, and whoever
    loses that race skips it. A notifier failure is logged; the timer stays fired.

    Mac power rows (tools/power.py) go through `sleep_notifier` (Mac + Telegram): a `sleep_notice`
    is a plain notification; a `sleep` notifies and then runs `sleep_action` (`pmset sleepnow`),
    except when it is late, so a scheduler that was down does not put the Mac to sleep at a random
    moment the next morning.

    Alarm rows (scheduler/alarm.py) schedule their next occurrence first (a repeat), then go to
    `alarm_handler.handle(timer, state, result, store)`: ringing on time, a "missed" notification when
    late. Without a handler (tests, `run_once` in a script) they notify like a timer."""
    now = now or now_local()
    if now.tzinfo is None:
        now = now.replace(tzinfo=local_tz())
    fired: list[FiredTimer] = []
    for timer in store.due(now):
        if not store.mark_fired(timer.id, now):
            log.debug("timer %s already handled elsewhere", timer.id)
            continue
        late = (now - timer.fire_at).total_seconds() > late_after_s
        state = PomodoroState.from_json(timer.pomodoro_state_json) if timer.kind == KIND_POMODORO else None
        message = message_for(timer, state)
        if late:
            message += t("timers.late")
        result = FiredTimer(timer.id, timer.kind, timer.label, timer.fire_at, message, late, notified=False)

        if timer.kind in (KIND_SLEEP, KIND_SLEEP_NOTICE):
            fired.append(_fire_power_row(store, timer, result, sleep_notifier or notifier, sleep_action))
            continue
        if timer.kind == KIND_ALARM:
            fired.append(_fire_alarm_row(store, timer, result, now, notifier, alarm_handler))
            continue

        # Schedule the next pomodoro phase before notifying, so a broken channel cannot stall
        # the pomodoro. Phases are measured from the moment this one actually fired: after a
        # long sleep the whole sequence must not cascade through at once.
        if state is not None:
            nxt = state.next_phase()
            if nxt is not None:
                result.next_id = store.add(timer.label, now + timedelta(minutes=nxt.duration_min()), KIND_POMODORO, nxt)

        try:
            notifier.notify(NOTIFICATION_TITLE, message)
            result.notified = True
            log.info("timer %s fired: %s%s", timer.id, message, " (late)" if late else "")
        except Exception as exc:  # the timer is already fired; never let one failure stop the rest
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("timer %s fired but notification failed: %s", timer.id, result.error)
        fired.append(result)
    return fired


def _fire_power_row(
    store: TimerStore, timer: TimerRow, result: FiredTimer, notifier: Notifier, sleep_action: Callable[[], None] | None
) -> FiredTimer:
    if timer.kind == KIND_SLEEP:
        if result.late:
            result.message = t("power.sleep.skipped", at=format_local(timer.fire_at, with_date=False))
            log.warning("scheduled sleep %s was due at %s and is skipped (late)", timer.id, timer.fire_at.isoformat(timespec="seconds"))
        else:
            result.message = t("power.sleep.now")
    try:
        notifier.notify(NOTIFICATION_TITLE, result.message)
        result.notified = True
    except Exception as exc:  # the sleep still happens: the user asked for it and the notice went out a minute ago
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("timer %s fired but notification failed: %s", timer.id, result.error)
    if timer.kind == KIND_SLEEP and not result.late:
        try:
            (sleep_action or (lambda: _default_sleep_action(store)))()
            result.slept = True
            log.info("scheduled sleep %s executed", timer.id)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("scheduled sleep %s failed: %s", timer.id, result.error)
    else:
        log.info("timer %s fired: %s", timer.id, result.message)
    return result


def _fire_alarm_row(
    store: TimerStore, timer: TimerRow, result: FiredTimer, now: datetime, notifier: Notifier, handler: Any | None
) -> FiredTimer:
    from jarvis.scheduler.alarm import AlarmState  # noqa: PLC0415

    state = AlarmState.from_json(timer.pomodoro_state_json)
    if state is not None and state.repeat:
        # The next occurrence first, measured from now (after a long sleep the series goes on from today),
        # so nothing that happens while ringing can stop the series.
        try:
            next_at = next_alarm_occurrence(state.hour, state.minute, state.repeat, after=max(now, timer.fire_at))
            result.next_id = store.add(timer.label, next_at, KIND_ALARM, state.for_next_occurrence())
            log.info("alarm %s repeats: next occurrence %s (row %s)", timer.id, next_at.isoformat(timespec="seconds"), result.next_id)
        except Exception as exc:
            log.error("could not schedule the next occurrence of alarm %s: %s", timer.id, exc)
    result.message = t("alarm.notify.ring", label=timer.label, at=format_local(timer.fire_at, with_date=False)) + (
        t("timers.late") if result.late else ""
    )
    if handler is None or state is None:
        try:
            notifier.notify(NOTIFICATION_TITLE, result.message)
            result.notified = True
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("alarm %s fired but notification failed: %s", timer.id, result.error)
        return result
    try:
        handler.handle(timer, state, result, store)
    except Exception as exc:  # the row is fired; the next tick must go on
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("alarm %s handler failed", timer.id)
    return result
