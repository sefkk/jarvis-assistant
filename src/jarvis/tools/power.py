"""Mac power controls: `mac_status`, `lock_screen`, `sleep_mac`, `schedule_sleep`,
`cancel_scheduled_sleep`. Built for the moment the Mac was left on and the user is out with the phone.

Every command is argv-only and runs as the user, never with sudo (verified on macOS 26, Darwin 25):

- `pmset -g batt`              battery percentage, charging state, time remaining, power source
- `pmset -g`                   what is preventing sleep right now (caffeinate, powerd, ...)
- `pmset -g log`               power-source changes, to say how long the Mac has been on battery
- `ioreg -c IOHIDSystem -d 4`  HIDIdleTime: nanoseconds since the last key press or mouse move
- `ioreg -n Root -d 1`         IOConsoleLocked: whether the screen is locked
- `sysctl -n kern.boottime`    uptime
- `pmset sleepnow`             immediate sleep; no root needed (unlike `pmset schedule`, which is why
                               a scheduled sleep goes through Jarvis's own launchd scheduler instead)
- `pmset displaysleepnow`      the lock fallback: display off, which locks when "require password
                               after sleep" is on

Locking calls `SACLockScreenImmediate` from the private login framework, the call behind Alfred's,
Hammerspoon's and Keyboard Maestro's "lock screen" actions: immediate, no permission, no sudo
(`CGSession -suspend` no longer exists on current macOS, and the Control-Command-Q keystroke needs
Accessibility access). The display-sleep command is the fallback when the framework is missing.

An immediate sleep runs on a countdown in this process (a daemon thread), so the reply reaches the
user before the Mac goes down and "cancel" / "stop" ("iptal" / "dur") inside the delay cancels it. A scheduled sleep is
two rows in the timers table (kind `sleep_notice` one minute before, kind `sleep` at the time), fired
by `jarvis scheduler run`, which notifies the Mac and Telegram and then runs `pmset sleepnow` itself.
Before any sleep the logs are flushed and the SQLite connection is closed (it reopens on first use
after the wake).
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field, model_validator

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.i18n import t, t_all
from jarvis.observability import logging_setup
from jarvis.observability.db import Database
from jarvis.scheduler.store import KIND_SLEEP, KIND_SLEEP_NOTICE, TimerRow, TimerStore
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolResult
from jarvis.tools.timers import _TimerTool, resolve_at
from jarvis.utils.text import fold, fold_words
from jarvis.utils.timeutil import format_local, local_tz, now_local

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.power")

LOGIN_FRAMEWORK = "/System/Library/PrivateFrameworks/login.framework/login"
LOCK_SYMBOL = "SACLockScreenImmediate"
COMMAND_TIMEOUT_S = 10.0
LOG_TIMEOUT_S = 20.0  # `pmset -g log` is a few MB; only read when the Mac is on battery
LOCK_VERIFY_S = 2.0

DEFAULT_DELAY_S = 10
MIN_DELAY_S = 3
MAX_DELAY_S = 120
NOTICE_LEAD_S = 60  # the "1 minute" notice before a scheduled sleep
MIN_SCHEDULE_LEAD_S = 90  # the notice must fit before the sleep
CANCEL_GRACE_S = 30  # "cancel" still cancels a scheduled sleep this long after its time (the scheduler may be late)

# What the user sees comes from locales/<language>/power.toml; the sleep warning is the sentence the owner
# asked for, word for word. Other modules read these names as attributes (`power.WARNING_TEXT`); each access
# returns the text in the current main language, so a template here is unformatted.
_TEXTS = {
    "WARNING_TEXT": "power.sleep.warning",
    "CANCEL_HINT": "power.sleep.cancel_hint",
    "CANCELLED_TEXT": "power.sleep.cancelled",
    "SCHEDULED_CANCELLED_TEXT": "power.sleep.scheduled_cancelled",
    "NOTICE_TEXT": "power.sleep.notice",
    "SLEEP_NOW_TEXT": "power.sleep.now",
    "SLEEP_SKIPPED_TEXT": "power.sleep.skipped",
    "SLEEP_LABEL": "power.sleep.label",
    "NOTICE_LABEL": "power.sleep.notice_label",
    "SCHEDULER_WARNING": "timers.scheduler_missing",
}


def __getattr__(name: str) -> str:
    key = _TEXTS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return t(key)


# Whole-message cancel words (any channel), in every language at once. Matched exactly after trimming
# punctuation, so "iptal" or "cancel" cancels while "iptal etme" or an unrelated sentence goes to the model.
CANCEL_WORDS = frozenset(fold_words(t_all("power.words.cancel")))

_BATT_LINE_RE = re.compile(r"(\d{1,3})%;\s*(.*?)(?:\s+present:.*)?$", re.M)
_REMAINING_RE = re.compile(r"(\d{1,2}:\d{2}) remaining")
_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
_LOCKED_RE = re.compile(r'"IOConsoleLocked"\s*=\s*(Yes|No)')
_BOOT_RE = re.compile(r"sec\s*=\s*(\d+)")
_BLOCKERS_RE = re.compile(r"sleep prevented by ([^)\n]+)")
_LOG_SOURCE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}).*Using (AC|BATT|Batt)\b", re.M)


# --- running commands -------------------------------------------------------------------------------


def run_argv(argv: list[str], *, timeout: float = COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Every external command of this module goes through here: a list, never a shell, never sudo.
    Tests replace it."""
    if not argv or argv[0] == "sudo":
        raise ToolExecutionError("power tools never run sudo")
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolExecutionError(f"could not run {argv[0]}: {exc}") from exc


def _output(argv: list[str], *, timeout: float = COMMAND_TIMEOUT_S) -> str:
    proc = run_argv(argv, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise ToolExecutionError(f"{' '.join(argv[:2])} failed: {detail}")
    return proc.stdout


def _now() -> datetime:  # module-level so tests can freeze the clock
    return now_local()


# --- status ------------------------------------------------------------------------------------------


@dataclass
class MacStatus:
    power_source: str = "unknown"  # "AC" | "Battery" | "unknown"
    battery_percent: int | None = None
    charging: bool | None = None
    battery_state: str = ""  # charging / discharging / charged / not charging / finishing charge
    battery_remaining: str | None = None  # "1:32": to full when charging, to empty when discharging
    on_battery_since: datetime | None = None
    idle_s: float | None = None
    screen_locked: bool | None = None
    uptime_s: float | None = None
    sleep_blockers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_batt(text: str) -> dict[str, Any]:
    """`pmset -g batt`: power source, percent, state, time remaining. Missing pieces are None."""
    out: dict[str, Any] = {
        "power_source": "unknown",
        "battery_percent": None,
        "charging": None,
        "battery_state": "",
        "battery_remaining": None,
    }
    source = _SOURCE_RE.search(text)
    if source:
        out["power_source"] = "AC" if "AC" in source.group(1) else "Battery"
    line = _BATT_LINE_RE.search(text)
    if line:
        out["battery_percent"] = max(0, min(100, int(line.group(1))))
        rest = line.group(2).strip().lower()  # "charging; 1:32 remaining" / "AC attached; not charging" / "charged"
        parts = [p.strip() for p in rest.split(";") if p.strip()]
        if "not charging" in parts:
            state = "not charging"
        elif any(p.startswith("finishing") for p in parts):
            state = "finishing charge"
        else:
            state = parts[0] if parts else ""
        out["battery_state"] = state
        out["charging"] = state in ("charging", "finishing charge")
        remaining = _REMAINING_RE.search(rest)
        out["battery_remaining"] = remaining.group(1) if remaining else None
    return out


def parse_idle_s(text: str) -> float | None:
    match = _IDLE_RE.search(text)
    return int(match.group(1)) / 1_000_000_000 if match else None


def parse_console_locked(text: str) -> bool | None:
    match = _LOCKED_RE.search(text)
    return match.group(1) == "Yes" if match else None


def parse_uptime_s(text: str, now: datetime) -> float | None:
    match = _BOOT_RE.search(text)
    if not match:
        return None
    boot = datetime.fromtimestamp(int(match.group(1)), tz=local_tz())
    return max(0.0, (now - boot).total_seconds())


def parse_sleep_blockers(text: str) -> list[str]:
    match = _BLOCKERS_RE.search(text)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def parse_on_battery_since(log_text: str) -> datetime | None:
    """The moment the Mac last switched to battery: walk the power-source mentions of `pmset -g log`
    backwards while they say battery; the first one after the last AC mention is the switch."""
    entries = _LOG_SOURCE_RE.findall(log_text)
    since: datetime | None = None
    for stamp, source in reversed(entries):
        if source == "AC":
            break
        try:
            since = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
    return since


def collect_status(*, now: datetime | None = None, with_battery_log: bool = True) -> MacStatus:
    """Read everything; a failing command becomes a line in `errors`, never an exception."""
    now = now or _now()
    status = MacStatus()
    try:
        for key, value in parse_batt(_output(["pmset", "-g", "batt"])).items():
            setattr(status, key, value)
    except ToolExecutionError as exc:
        status.errors.append(f"battery: {exc}")
    try:
        status.idle_s = parse_idle_s(_output(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    except ToolExecutionError as exc:
        status.errors.append(f"idle: {exc}")
    try:
        status.screen_locked = parse_console_locked(_output(["ioreg", "-n", "Root", "-d", "1"]))
    except ToolExecutionError as exc:
        status.errors.append(f"lock: {exc}")
    try:
        status.uptime_s = parse_uptime_s(_output(["sysctl", "-n", "kern.boottime"]), now)
    except ToolExecutionError as exc:
        status.errors.append(f"uptime: {exc}")
    try:
        status.sleep_blockers = parse_sleep_blockers(_output(["pmset", "-g"]))
    except ToolExecutionError as exc:
        status.errors.append(f"sleep settings: {exc}")
    if with_battery_log and status.power_source == "Battery":
        try:
            status.on_battery_since = parse_on_battery_since(_output(["pmset", "-g", "log"], timeout=LOG_TIMEOUT_S))
        except ToolExecutionError as exc:
            status.errors.append(f"battery log: {exc}")
    return status


def _duration(seconds: float, language: str | None = None) -> str:
    seconds = int(round(seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return t("power.duration.days", language, days=days, hours=hours)
    if hours:
        return t("power.duration.hours", language, hours=hours, minutes=minutes)
    if minutes:
        return t("power.duration.minutes", language, minutes=minutes, seconds=secs)
    return t("power.duration.seconds", language, seconds=secs)


# `pmset -g batt` battery states -> keys of power.status.state
_BATTERY_STATES = {
    "charging": "charging",
    "discharging": "discharging",
    "charged": "charged",
    "not charging": "not_charging",
    "ac attached": "not_charging",
    "finishing charge": "finishing",
}


def format_status(status: MacStatus, now: datetime | None = None, language: str | None = None) -> str:
    """Short lines in the owner's language (the same text goes to the model and to a /status reply)."""
    now = now or _now()
    lang = language
    lines = []
    if status.power_source == "AC":
        power = t("power.status.ac", lang)
    elif status.power_source == "Battery":
        power = t("power.status.battery_power", lang)
    else:
        power = t("power.status.unknown", lang)
    battery = ""
    if status.battery_percent is not None:
        key = _BATTERY_STATES.get(status.battery_state)
        state = t(f"power.status.state.{key}", lang) if key else status.battery_state
        battery = t("power.status.battery", lang, percent=status.battery_percent)
        if state:
            battery += t("power.status.battery_state", lang, state=state)
        if status.battery_remaining:
            battery += t("power.status.until_full" if status.charging else "power.status.until_empty", lang, time=status.battery_remaining)
    lines.append(t("power.status.power", lang, power=power, battery=battery))
    if status.power_source == "Battery":
        if status.on_battery_since is not None:
            lines.append(
                t(
                    "power.status.on_battery",
                    lang,
                    duration=_duration((now - status.on_battery_since).total_seconds(), lang),
                    since=format_local(status.on_battery_since, with_date=False),
                )
            )
        else:
            lines.append(t("power.status.on_battery_unknown", lang))
    if status.idle_s is not None:
        lines.append(t("power.status.idle", lang, duration=_duration(status.idle_s, lang)))
    if status.screen_locked is not None:
        lines.append(t("power.status.locked" if status.screen_locked else "power.status.unlocked", lang))
    if status.uptime_s is not None:
        lines.append(t("power.status.uptime", lang, duration=_duration(status.uptime_s, lang)))
    if status.sleep_blockers:
        lines.append(t("power.status.blockers", lang, blockers=", ".join(status.sleep_blockers)))
    if status.errors:
        lines.append(t("power.status.errors", lang, errors="; ".join(status.errors)))
    return "\n".join(lines)


# --- lock ---------------------------------------------------------------------------------------------


def lock_available() -> bool:
    try:
        return hasattr(ctypes.CDLL(LOGIN_FRAMEWORK), LOCK_SYMBOL)
    except OSError:
        return False


def _sac_lock() -> None:
    """`SACLockScreenImmediate()` from the login framework. Tests replace it."""
    lib = ctypes.CDLL(LOGIN_FRAMEWORK)
    fn = getattr(lib, LOCK_SYMBOL)
    fn.restype = ctypes.c_int
    fn()


def screen_locked() -> bool | None:
    try:
        return parse_console_locked(_output(["ioreg", "-n", "Root", "-d", "1"]))
    except ToolExecutionError:
        return None


def lock_screen_now(*, verify_s: float | None = None, sleep: Callable[[float], None] = time.sleep) -> str:
    """Lock the screen and return how: 'login framework' (verified or not) or 'display sleep'."""
    try:
        _sac_lock()
    except Exception as exc:  # framework missing or the symbol changed: fall back to the display
        log.warning("login framework lock failed (%s); falling back to pmset displaysleepnow", exc)
        _output(["pmset", "displaysleepnow"])
        return "display sleep"
    deadline = time.monotonic() + (LOCK_VERIFY_S if verify_s is None else verify_s)
    while True:
        if screen_locked():
            return "login framework"
        if time.monotonic() >= deadline:
            return "login framework (unverified)"
        sleep(0.2)


# --- sleep: now, with a countdown ---------------------------------------------------------------------


def sleep_now() -> None:
    """`pmset sleepnow`: immediate system sleep, no root needed."""
    _output(["pmset", "sleepnow"], timeout=15)


def prepare_for_sleep(db: Database | None = None) -> None:
    """Everything that should be on disk before the Mac goes down: the SQLite connection is closed
    (it reopens on first use after the wake) and the log files are flushed."""
    if db is not None:
        try:
            db.close()
        except Exception:  # a closing problem must not keep the Mac awake
            log.exception("could not close the database before sleep")
    log.info("sleeping now")
    logging_setup.flush()


@dataclass
class PendingSleep:
    delay_s: float
    started_at: float
    channel: str | None = None
    timer: Any = None
    cancelled: bool = False
    fired: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def remaining_s(self, now: float | None = None) -> float:
        return max(0.0, self.started_at + self.delay_s - (time.monotonic() if now is None else now))


def _make_timer(delay_s: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    return timer


_lock = threading.Lock()
_pending: PendingSleep | None = None


def pending_countdown() -> PendingSleep | None:
    with _lock:
        return _pending


def start_countdown(
    delay_s: float,
    *,
    before_sleep: Callable[[], None] | None = None,
    sleep_fn: Callable[[], None] | None = None,
    channel: str | None = None,
) -> PendingSleep:
    """Sleep the Mac after `delay_s` unless `cancel_countdown()` runs first. A second call replaces
    the first countdown."""
    global _pending
    cancel_countdown()
    pending = PendingSleep(delay_s=delay_s, started_at=time.monotonic(), channel=channel)

    def fire() -> None:
        global _pending
        with _lock:
            if pending.cancelled:
                return
            pending.fired = True
            if _pending is pending:
                _pending = None
        try:
            if before_sleep is not None:
                before_sleep()
            (sleep_fn or sleep_now)()
            logging_setup.audit({"tool": "sleep_mac", "decision": "slept", "delay_s": delay_s, "channel": channel})
        except Exception as exc:
            log.error("sleep failed: %s", exc)
            logging_setup.audit({"tool": "sleep_mac", "decision": "sleep_failed", "error": str(exc)[:200]})
        finally:
            pending.done.set()

    with _lock:
        pending.timer = _make_timer(delay_s, fire)
        _pending = pending
        pending.timer.start()
    logging_setup.audit({"tool": "sleep_mac", "decision": "countdown", "delay_s": delay_s, "channel": channel})
    return pending


def cancel_countdown() -> PendingSleep | None:
    """Stop the pending countdown; returns it, or None when nothing was pending (or it already fired)."""
    global _pending
    with _lock:
        pending, _pending = _pending, None
        if pending is None or pending.fired:
            return None
        pending.cancelled = True
        if pending.timer is not None:
            pending.timer.cancel()
        pending.done.set()
    logging_setup.audit({"tool": "sleep_mac", "decision": "cancelled", "remaining_s": round(pending.remaining_s(), 1)})
    return pending


# --- sleep: scheduled through the timers table ----------------------------------------------------------


def scheduled_sleep(store: TimerStore) -> TimerRow | None:
    for row in store.pending():
        if row.kind == KIND_SLEEP:
            return row
    return None


def cancel_scheduled(store: TimerStore) -> int:
    """Cancel the pending sleep and its notice. Returns how many rows were cancelled."""
    return store.cancel_by_kind(KIND_SLEEP, KIND_SLEEP_NOTICE)


def schedule(store: TimerStore, fire_at: datetime, *, now: datetime | None = None) -> tuple[int, int | None]:
    """One scheduled sleep at a time: an earlier one is replaced. Returns (sleep id, notice id)."""
    now = now or _now()
    if fire_at - now < timedelta(seconds=MIN_SCHEDULE_LEAD_S):
        raise ToolValidationError(
            f"a scheduled sleep must be at least {MIN_SCHEDULE_LEAD_S // 60 + 1} minutes ahead; use sleep_mac for now"
        )
    cancel_scheduled(store)
    sleep_id = store.add(t("power.sleep.label"), fire_at, KIND_SLEEP)
    notice_at = fire_at - timedelta(seconds=NOTICE_LEAD_S)
    notice_id = store.add(t("power.sleep.notice", at=format_local(fire_at, with_date=False)), notice_at, KIND_SLEEP_NOTICE)
    log.info("sleep scheduled for %s (row %s, notice %s)", fire_at.isoformat(timespec="seconds"), sleep_id, notice_id)
    logging_setup.audit({"tool": "schedule_sleep", "decision": "scheduled", "at": fire_at.isoformat(timespec="seconds")})
    return sleep_id, notice_id


def perform_scheduled_sleep(db: Database | None = None) -> None:
    """What the scheduler runs when a `sleep` row fires (the notification went out just before)."""
    prepare_for_sleep(db)
    sleep_now()
    logging_setup.audit({"tool": "schedule_sleep", "decision": "slept"})


# --- "cancel" / "stop" ("iptal" / "dur") from any channel ---------------------------------------------------


def is_cancel_word(text: str) -> bool:
    cleaned = fold(text.strip()).strip(" .!?,;:")
    return cleaned in CANCEL_WORDS


def cancel_on_request(
    text: str, *, paths: "Paths | None" = None, db: Database | None = None, now: datetime | None = None, language: str | None = None
) -> str | None:
    """If `text` is a cancel word and a sleep is about to happen, cancel it and return what to tell
    the user (in `language`, default the main language); otherwise None (the message goes to the model
    as usual). Covers the in-process countdown and a scheduled sleep whose one-minute notice is out (or
    is due within the grace period)."""
    if not is_cancel_word(text):
        return None
    if cancel_countdown() is not None:
        return t("power.sleep.cancelled", language)
    if db is None and paths is None:
        return None
    own = db is None
    db = db or Database(paths.db_file)  # type: ignore[union-attr]
    try:
        store = TimerStore(db)
        row = scheduled_sleep(store)
        if row is None:
            return None
        now = now or _now()
        if row.fire_at - now > timedelta(seconds=NOTICE_LEAD_S + CANCEL_GRACE_S):
            return None  # far away: the user did not mean the sleep
        cancel_scheduled(store)
    finally:
        if own:
            db.close()
    logging_setup.audit({"tool": "schedule_sleep", "decision": "cancelled_by_word", "at": row.fire_at.isoformat(timespec="seconds")})
    return t("power.sleep.scheduled_cancelled", language, at=format_local(row.fire_at, with_date=False))


# --- the tools -------------------------------------------------------------------------------------------


def _runtime(tool: Tool) -> Any:
    return tool.ctx.runtime if tool.ctx is not None else None


class MacStatusTool(Tool):
    name = "mac_status"
    description = (
        "Report the Mac's state: power source, battery percent and charging, time on battery, how long ago "
        "the keyboard or mouse was last used (idle), whether the screen is locked, uptime, and what prevents "
        "sleep. Use for 'is the computer on', 'how is the battery', 'did I leave the Mac on'. Reads only; no sudo."
    )
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = True

    def run(self, args: BaseModel) -> ToolResult:
        now = _now()
        status = collect_status(now=now)
        return ToolResult(format_status(status, now))


class LockScreenTool(Tool):
    name = "lock_screen"
    description = (
        "Lock the Mac's screen immediately. The Mac keeps running, so Jarvis and Telegram keep working. Use for 'lock the screen'. No sudo."
    )
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = False

    def run(self, args: BaseModel) -> ToolResult:
        if screen_locked():
            return ToolResult(t("power.lock.already"))
        how = lock_screen_now()
        logging_setup.audit({"tool": "lock_screen", "decision": "locked", "how": how})
        if how == "display sleep":
            return ToolResult(t("power.lock.display_sleep"))
        if how.endswith("(unverified)"):
            return ToolResult(t("power.lock.unverified"))
        return ToolResult(t("power.lock.locked"))


class SleepMacInput(BaseModel):
    delay_s: int = Field(
        default=DEFAULT_DELAY_S,
        ge=MIN_DELAY_S,
        le=MAX_DELAY_S,
        description=f"Seconds to wait before sleeping so the reply is delivered first (default {DEFAULT_DELAY_S}).",
    )


class SleepMacTool(Tool):
    name = "sleep_mac"
    description = (
        "Put the Mac to sleep after a short countdown (`pmset sleepnow`, no sudo). The user can cancel by "
        "saying 'cancel' or 'stop' ('iptal' / 'dur') inside the delay. The result is the exact warning the user must "
        "get: repeat it word for word in your reply. After the Mac sleeps it cannot be woken from Telegram. Use for "
        "'put the computer to sleep', 'sleep the Mac'; for a later time use schedule_sleep."
    )
    input_model = SleepMacInput
    risk = RiskLevel.CONFIRM
    taints = False
    delegate_ok = False

    def confirm_preview(self, args: BaseModel) -> str | None:
        assert isinstance(args, SleepMacInput)
        return t("power.sleep.confirm_now", seconds=args.delay_s)

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SleepMacInput)
        runtime = _runtime(self)
        db = getattr(runtime, "db", None)
        channel = getattr(runtime, "channel", None)
        start_countdown(args.delay_s, before_sleep=lambda: prepare_for_sleep(db), channel=channel)
        warning = t("power.sleep.warning", seconds=args.delay_s)
        text = f"{warning} {t('power.sleep.cancel_hint')}"
        sink = getattr(runtime, "sink", None)
        if channel == "telegram" and sink is not None:
            # The phone must have the warning before the Mac goes down, whatever the model's reply takes.
            try:
                sink.notice(warning)
                text += (
                    "\n(System: this warning was already sent to the phone as a notice; answer with one short "
                    "sentence that confirms it and names the cancel words, without repeating it word for word.)"
                )
            except Exception:  # the result still carries the warning
                log.debug("could not send the sleep warning as a notice", exc_info=True)
        return ToolResult(text)


class ScheduleSleepInput(BaseModel):
    in_minutes: float | None = Field(
        default=None,
        ge=2,
        le=1440,
        description="Minutes from now (2 to 1440). Give exactly one of in_minutes or at.",
    )
    at: str | None = Field(
        default=None,
        description=(
            "Clock time instead: 'HH:MM' (today, or tomorrow if already past), 'YYYY-MM-DD HH:MM' or ISO 8601. "
            "Resolve relative words yourself first. Give exactly one of in_minutes or at."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "ScheduleSleepInput":
        if (self.in_minutes is None) == (self.at is None):
            raise ValueError("give exactly one of in_minutes or at")
        return self


class ScheduleSleepTool(_TimerTool):
    name = "schedule_sleep"
    description = (
        "Put the Mac to sleep at a later time through the background scheduler (no sudo). One minute before, "
        "the user gets a notification on the Mac and on Telegram and can cancel with 'cancel' ('iptal'). Only one "
        "scheduled sleep exists at a time (a new one replaces it). Use for 'sleep the Mac at 23:00', 'put the computer "
        "to sleep in an hour'."
    )
    input_model = ScheduleSleepInput
    risk = RiskLevel.CONFIRM
    delegate_ok = False

    def confirm_preview(self, args: BaseModel) -> str | None:
        assert isinstance(args, ScheduleSleepInput)
        now = _now()
        fire_at = self._fire_at(args, now)
        return t("power.sleep.confirm_scheduled", at=format_local(fire_at))

    @staticmethod
    def _fire_at(args: ScheduleSleepInput, now: datetime) -> datetime:
        if args.in_minutes is not None:
            return now + timedelta(minutes=args.in_minutes)
        return resolve_at(args.at or "", now)

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ScheduleSleepInput)
        now = _now()
        fire_at = self._fire_at(args, now)
        previous = scheduled_sleep(self.store)
        schedule(self.store, fire_at, now=now)
        text = (
            f"The Mac sleeps at {format_local(fire_at)} (in {_duration((fire_at - now).total_seconds(), 'en')}). "
            f"At {format_local(fire_at - timedelta(seconds=NOTICE_LEAD_S), with_date=False)} a notice comes on the Mac "
            f'and on Telegram; saying "cancel" ("iptal") keeps it awake.'
        )
        if previous is not None:
            text += f" (The earlier plan, {format_local(previous.fire_at)}, was cancelled.)"
        return ToolResult(text + self.scheduler_warning())


class CancelScheduledSleepTool(_TimerTool):
    name = "cancel_scheduled_sleep"
    description = (
        "Cancel the scheduled sleep (and a running sleep countdown, if any). Use for 'cancel the sleep', 'don't let the computer sleep'."
    )
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    delegate_ok = False  # the delegate never touches the Mac's power state

    def run(self, args: BaseModel) -> ToolResult:
        countdown = cancel_countdown()
        row = scheduled_sleep(self.store)
        cancelled = cancel_scheduled(self.store)
        if countdown is None and cancelled == 0:
            return ToolResult("No sleep is scheduled or pending.")
        parts = []
        if countdown is not None:
            parts.append("the pending sleep countdown was cancelled")
        if row is not None and cancelled:
            parts.append(f"the sleep scheduled for {format_local(row.fire_at)} was cancelled")
        logging_setup.audit(
            {"tool": "cancel_scheduled_sleep", "decision": "cancelled", "rows": cancelled, "countdown": countdown is not None}
        )
        return ToolResult("Done: " + "; ".join(parts) + ". The Mac stays on.")


# --- jarvis doctor -----------------------------------------------------------------------------------------


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """`pmset` works without sudo, the lock call exists, and whether a sleep is scheduled. Never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    try:
        proc = run_argv(["pmset", "-g", "batt"])
        parsed = parse_batt(proc.stdout)
        if proc.returncode == 0 and parsed["power_source"] != "unknown":
            detail = f"pmset works without sudo ({parsed['power_source']}"
            if parsed["battery_percent"] is not None:
                detail += f", battery {parsed['battery_percent']}%"
            checks.append(Check("Mac power (pmset)", True, detail + ")"))
        else:
            checks.append(
                Check("Mac power (pmset)", False, f"pmset -g batt failed: {(proc.stderr or proc.stdout).strip()[:120] or proc.returncode}")
            )
    except Exception as exc:
        checks.append(Check("Mac power (pmset)", False, f"pmset unavailable: {type(exc).__name__}: {exc}"))
    checks.append(
        Check(
            "Mac lock (login framework)",
            True,
            "SACLockScreenImmediate available"
            if lock_available()
            else "framework missing: lock_screen falls back to pmset displaysleepnow",
            warn=not lock_available(),
        )
    )
    try:
        db = Database(paths.db_file)
        try:
            row = scheduled_sleep(TimerStore(db))
        finally:
            db.close()
        checks.append(Check("Scheduled sleep", True, f"Mac sleeps at {format_local(row.fire_at)}" if row else "none scheduled"))
    except Exception as exc:
        checks.append(Check("Scheduled sleep", False, f"cannot read timers: {type(exc).__name__}: {exc}"))
    return checks


__all__ = [
    "CANCEL_WORDS",
    "NOTICE_TEXT",
    "SLEEP_NOW_TEXT",
    "SLEEP_SKIPPED_TEXT",
    "WARNING_TEXT",
    "SCHEDULER_WARNING",
    "MacStatus",
    "collect_status",
    "format_status",
    "lock_screen_now",
    "sleep_now",
    "prepare_for_sleep",
    "start_countdown",
    "cancel_countdown",
    "pending_countdown",
    "schedule",
    "cancel_scheduled",
    "scheduled_sleep",
    "perform_scheduled_sleep",
    "cancel_on_request",
    "is_cancel_word",
    "health_checks",
]
