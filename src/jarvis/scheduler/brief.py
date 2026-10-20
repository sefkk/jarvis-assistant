"""The automatic morning brief (`[brief.schedule]`): a periodic job of the scheduler agent, no new service.

Every tick (`tick`, ~10 s) decides from the config, the clock and one `kv` row whether today's brief is
due, then delivers it once:

- the slot is `weekday_time` on Monday-Friday and `weekend_time` on Saturday-Sunday ("" = no brief);
- once a day: the `kv` row `brief.auto` holds the day and the outcome, written *before* the delivery
  starts, so a second tick (or a second scheduler) cannot send it twice;
- if the user already asked for a brief that morning (a `daily_brief` tool call since local midnight,
  in the `tool_calls` table) the automatic one is skipped;
- a brief missed while the Mac slept is delivered on wake until `late_until` (marked late), then skipped;
- the channel: "auto" speaks it when the user is at the Mac (active within `active_within_min`, screen not
  locked, voice agent running, not paused, no Do Not Disturb) and sends it to Telegram otherwise. Speaking
  is never attempted while Do Not Disturb is on or voice is paused, whatever the config says.

Spoken: an announcement request (voice/announce.py, kind `turn`) the voice service hands to its Assistant,
so the model builds the short spoken version (`VOICE_PROMPT`). If the service has not taken it within
`VOICE_FALLBACK_AFTER_S` (busy, or it went away), the next tick reclaims it and sends Telegram instead.
Telegram: this process builds an Assistant once (the same model, persona and rules as `jarvis brief`),
asks for the compact list version (`TELEGRAM_PROMPT`) and sends the reply through the Telegram notifier.
The delivery runs in a thread so the timers keep ticking; tests run it inline.

`jarvis brief schedule`, `jarvis brief --send telegram|voice` and the `set_brief_schedule` tool reuse the
same functions. The scheduler loop hands every tick the current config (scheduler/config_watch.py re-reads
config.toml and config.local.toml when their modification time changed), so a schedule change by hand or
through the tool takes effect on the next tick, without `jarvis services reinstall`.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from jarvis.config import parse_hhmm
from jarvis.exceptions import JarvisError
from jarvis.i18n import t
from jarvis.io.base import ConfirmRequest, OutputSink, compose_reply
from jarvis.utils.timeutil import format_local, local_tz, now_local
from jarvis.voice import announce
from jarvis.voice import pause as vpause

if TYPE_CHECKING:
    from jarvis.config import BriefScheduleConfig, Config, Paths
    from jarvis.observability.db import Database

log = logging.getLogger("jarvis.scheduler.brief")

KV_KEY = "brief.auto"
SOURCE = "brief"
LATE_AFTER_S = 120.0  # delivered later than this after the slot: "late" (the Mac was asleep or the agent was down)
VOICE_FALLBACK_AFTER_S = 240.0  # below announce.MAX_AGE_S: reclaim an unspoken request before the service drops it
DND_ASSERTIONS_FILE = Path.home() / "Library" / "DoNotDisturb" / "DB" / "Assertions.json"

_CONTENT = (
    "today's events from both calendars (say which calendar), the weather where the user is now (or at home when "
    "the brief says so), whether {team} plays today (time, opponent, competition) and yesterday's result if there "
    "was a match, the reminders due today, the alarms and timers set for today, and how many emails came in today "
    "with the important ones named"
)
_GREETING = "a greeting by name that fits the local time on the brief's first line (good morning only in the morning)"
_NO_CLOSING = "End with the last item: no closing remark, joke or advice."
VOICE_PROMPT = (
    "System event: the scheduled morning brief{late}. Call daily_brief once for today and speak it in the user's "
    "language ({language}) as at most six short sentences: " + _GREETING + ", then " + _CONTENT + ". No lists, "
    "no headings; leave out what is empty. " + _NO_CLOSING + " If the football section says the match was not "
    "found, do one web_search."
)
TELEGRAM_PROMPT = (
    "System event: the scheduled morning brief{late}, sent to Telegram as a message. Call daily_brief once for today "
    "and write it in the user's language ({language}) as a compact list: one line with " + _GREETING + ", then one "
    "short line per item for "
    + _CONTENT
    + ". Leave out what is empty; a failed part gets half a line. "
    + _NO_CLOSING
    + " If the football section says the match was not found, do one web_search."
)
ON_DEMAND_PROMPT = (
    "Give me today's daily brief (call daily_brief once) as a compact list for a Telegram message in the user's "
    "language ({language}): one short line per item for " + _CONTENT + ". Leave out what is empty. " + _NO_CLOSING
)
LATE_TEXT = " (late: the Mac was asleep at the scheduled time, say so in half a sentence)"


def __getattr__(name: str) -> str:
    """`TITLE`, the Telegram message's title, read as an attribute: the current main language's."""
    if name == "TITLE":
        return t("brief.title")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- the schedule ---------------------------------------------------------------------------------------------


def slot_for(cfg: "BriefScheduleConfig", day: date) -> datetime | None:
    """When the brief is due on `day` (local), or None when no brief is scheduled on that weekday."""
    hhmm = parse_hhmm(cfg.weekend_time if day.weekday() >= 5 else cfg.weekday_time)
    if hhmm is None:
        return None
    return datetime.combine(day, time(*hhmm), tzinfo=local_tz())


def late_until_for(cfg: "BriefScheduleConfig", day: date) -> datetime:
    hour, minute = parse_hhmm(cfg.late_until) or (12, 0)
    return datetime.combine(day, time(hour, minute), tzinfo=local_tz())


def next_run(cfg: "BriefScheduleConfig", now: datetime, *, handled_day: date | None = None) -> datetime | None:
    """The next moment a brief will go out: today's slot while it is still deliverable (not handled yet and
    before `late_until`), else the first slot after now within a week."""
    if not cfg.enabled:
        return None
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        slot = slot_for(cfg, day)
        if slot is None or day == handled_day:
            continue
        if slot >= now or (offset == 0 and now <= late_until_for(cfg, day)):
            return slot
    return None


# --- state: one kv row ----------------------------------------------------------------------------------------


@dataclass
class AutoState:
    day: str  # YYYY-MM-DD (local)
    outcome: str  # "delivering" | "voice" | "telegram" | "voice+telegram" | "skipped: ..." | "failed: ..."
    at: str = ""

    @property
    def day_date(self) -> date | None:
        try:
            return date.fromisoformat(self.day)
        except ValueError:
            return None


def load_state(db: "Database") -> AutoState | None:
    raw = db.kv_get(KV_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return AutoState(day=str(data["day"]), outcome=str(data.get("outcome", "")), at=str(data.get("at", "")))
    except (ValueError, KeyError, TypeError):
        log.warning("brief state row is unreadable; ignored")
        return None


def save_state(db: "Database", day: date, outcome: str, now: datetime | None = None) -> AutoState:
    state = AutoState(day=day.isoformat(), outcome=outcome, at=(now or now_local()).isoformat(timespec="seconds"))
    db.kv_set(KV_KEY, json.dumps(state.__dict__, ensure_ascii=False))
    return state


def asked_today(db: "Database", now: datetime) -> bool:
    """Did the user ask for a brief since local midnight (a `daily_brief` call from any interface)?"""
    midnight = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo or local_tz())
    since = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    row = db.conn.execute("SELECT COUNT(*) AS n FROM tool_calls WHERE tool = 'daily_brief' AND created_at >= ?", (since,)).fetchone()
    return int(row["n"]) > 0


# --- the decision ---------------------------------------------------------------------------------------------


def decide(cfg: "BriefScheduleConfig", now: datetime, *, state: AutoState | None, asked: bool) -> tuple[str, str]:
    """(action, reason): "wait" (nothing to do this tick), "deliver", "deliver_late" or "skip" (mark the day
    as handled without a brief)."""
    if not cfg.enabled:
        return "wait", "brief.schedule.enabled is false"
    day = now.date()
    if state is not None and state.day == day.isoformat():
        return "wait", f"already handled today ({state.outcome})"
    slot = slot_for(cfg, day)
    if slot is None:
        return "wait", "no brief on this weekday"
    if now < slot:
        return "wait", f"due at {format_local(slot, with_date=False)}"
    if asked:
        return "skip", "a brief was already asked for this morning"
    if now > late_until_for(cfg, day):
        return "skip", f"missed: past late_until ({cfg.late_until})"
    late = (now - slot).total_seconds() > LATE_AFTER_S
    return ("deliver_late" if late else "deliver"), ("late" if late else "on time")


# --- the channel ----------------------------------------------------------------------------------------------


def dnd_active(path: Path = DND_ASSERTIONS_FILE) -> bool | None:
    """Is a Focus / Do Not Disturb mode on? Read from macOS's own assertions file; None when it cannot be
    read (no Full Disk Access for the scheduler's python, older macOS), which callers treat as "unknown"."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("data") or []
        return any(entry.get("storeAssertionRecords") for entry in entries if isinstance(entry, dict))
    except (OSError, ValueError, AttributeError):
        return None


def _idle_and_lock() -> tuple[float | None, bool | None]:
    from jarvis.tools import power  # noqa: PLC0415 - the same probes as mac_status, imported lazily

    idle = locked = None
    try:
        idle = power.parse_idle_s(power._output(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    except Exception as exc:
        log.debug("idle probe failed: %s", exc)
    try:
        locked = power.parse_console_locked(power._output(["ioreg", "-n", "Root", "-d", "1"]))
    except Exception as exc:
        log.debug("lock probe failed: %s", exc)
    return idle, locked


def _voice_agent_running() -> bool:
    from jarvis.presence.hooks import voice_agent_running  # noqa: PLC0415

    return voice_agent_running()


@dataclass
class Probes:
    """What the channel choice looks at; tests replace the callables."""

    voice_running: Callable[[], bool] = field(default=_voice_agent_running)
    paused: Callable[[], bool] = field(default=lambda: False)
    dnd: Callable[[], bool | None] = field(default=dnd_active)
    idle_and_lock: Callable[[], tuple[float | None, bool | None]] = field(default=_idle_and_lock)

    @classmethod
    def for_paths(cls, paths: "Paths") -> "Probes":
        return cls(paused=lambda: vpause.load_state(paths.voice_pause_file).paused)


def voice_blocked(probes: Probes) -> str | None:
    """Why the brief cannot be spoken right now, or None when it can."""
    if not probes.voice_running():
        return "the voice agent is not running"
    if probes.paused():
        return "voice listening is paused"
    if probes.dnd():
        return "Do Not Disturb is on"
    return None


def choose_channels(cfg: "BriefScheduleConfig", probes: Probes) -> tuple[list[str], str]:
    """The channels to deliver through and the reason, from `brief.schedule.channel` and the probes."""
    if cfg.channel == "telegram":
        return ["telegram"], "channel = telegram"
    blocked = voice_blocked(probes)
    if cfg.channel == "voice":
        return (["voice"], "channel = voice") if blocked is None else (["telegram"], blocked)
    if cfg.channel == "both":
        return (["voice", "telegram"], "channel = both") if blocked is None else (["telegram"], blocked)
    if blocked is not None:
        return ["telegram"], blocked
    idle, locked = probes.idle_and_lock()
    if locked:
        return ["telegram"], "the screen is locked"
    if idle is None:
        return ["telegram"], "idle time unknown"
    if idle > cfg.active_within_min * 60:
        return ["telegram"], f"idle for {int(idle // 60)} min (more than {cfg.active_within_min})"
    return ["voice"], f"active at the Mac ({int(idle)} s ago)"


# --- delivery ---------------------------------------------------------------------------------------------------


class QuietSink(OutputSink):
    """An output sink for a turn nobody watches: the reply is collected, confirmations are declined.
    The model also talks between its tool calls ("Preparing your morning brief."); the Brain calls
    `segment_break` before every tool block (server tools such as web search included), so the text is
    kept in segments and `final_text()` drops the running commentary (io.base.compose_reply)."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.notices: list[str] = []
        self.segments: list[str] = [""]

    def write_delta(self, text: str) -> None:
        self.parts.append(text)
        self.segments[-1] += text

    def segment_break(self) -> None:
        if self.segments[-1]:
            self.segments.append("")

    def finish(self) -> None:
        pass

    def confirm(self, request: ConfirmRequest) -> bool:  # noqa: ARG002 - nobody is there to approve
        return False

    def notice(self, text: str) -> None:
        self.notices.append(text)

    def debug(self, text: str) -> None:
        pass

    def final_text(self) -> str:
        return compose_reply(self.segments)


def _default_assistant(config: "Config", paths: "Paths") -> Any:
    from jarvis.assistant import Assistant  # noqa: PLC0415 - heavy; only when a brief actually goes out

    return Assistant(config, paths, channel="scheduler")  # the dashboard's channel filter tells these turns apart


def summarize(config: "Config", paths: "Paths", prompt: str, *, assistant_factory: Callable[..., Any] | None = None) -> str:
    """One model turn that builds the brief (the same Assistant as `jarvis brief`); returns the reply text."""
    assistant = (assistant_factory or _default_assistant)(config, paths)
    sink = QuietSink()
    try:
        result = assistant.handle(prompt, sink)
    finally:
        assistant.close()
    if getattr(result, "error", None):
        raise JarvisError(f"the brief turn failed: {result.error}")
    text = sink.final_text() or (result.reply or "").strip()
    if not text:
        raise JarvisError("the brief turn produced no text")
    return text


def prompt_for(channel: str, config: "Config", *, late: bool = False) -> str:
    template = VOICE_PROMPT if channel == "voice" else TELEGRAM_PROMPT
    return template.format(late=LATE_TEXT if late else "", language=config.general.language, team=config.football.team)


def request_voice(config: "Config", paths: "Paths", *, late: bool = False) -> None:
    """Leave the spoken-brief request for the voice service (it speaks it when idle, within five minutes)."""
    announce.request(paths.voice_announce_file, kind="turn", source=SOURCE, text=prompt_for("voice", config, late=late))


def send_telegram(
    config: "Config",
    paths: "Paths",
    *,
    late: bool = False,
    assistant_factory: Callable[..., Any] | None = None,
    notifier: Any | None = None,
) -> str:
    """Build the compact brief with the model and send it to the allowed chat. Returns the text sent."""
    if notifier is None:
        from jarvis.interfaces.telegram_notify import TelegramNotifier  # noqa: PLC0415

        notifier = TelegramNotifier(config, state_file=paths.telegram_state_file)
    blocked = getattr(notifier, "blocked_reason", lambda: None)()
    if blocked:
        raise JarvisError(f"not sent to Telegram: {blocked}")
    text = summarize(config, paths, prompt_for("telegram", config, late=late), assistant_factory=assistant_factory)
    notifier.notify(t("brief.title"), text)
    return text


def deliver(
    config: "Config",
    paths: "Paths",
    channels: list[str],
    *,
    late: bool = False,
    assistant_factory: Callable[..., Any] | None = None,
    notifier: Any | None = None,
) -> str:
    """Deliver through every channel; the outcome names what worked ("voice", "telegram", "voice+telegram")
    or raises when nothing did."""
    done: list[str] = []
    errors: list[str] = []
    if "voice" in channels:
        try:
            request_voice(config, paths, late=late)
            done.append("voice")
        except Exception as exc:
            errors.append(f"voice: {exc}")
    if "telegram" in channels:
        try:
            send_telegram(config, paths, late=late, assistant_factory=assistant_factory, notifier=notifier)
            done.append("telegram")
        except Exception as exc:
            errors.append(f"telegram: {exc}")
    if not done:
        raise JarvisError("; ".join(errors) or "no channel")
    for error in errors:
        log.warning("brief partly delivered: %s", error)
    return "+".join(done)


# --- the periodic job -------------------------------------------------------------------------------------------


_DELIVERY_LOCK = threading.Lock()


def _finish(paths: "Paths", day: date, outcome: str, now: datetime | None) -> None:
    from jarvis.observability.db import Database  # noqa: PLC0415 - own connection: this may run in a thread

    db = Database(paths.db_file)
    try:
        save_state(db, day, outcome, now)
    finally:
        db.close()


def _run_delivery(
    config: "Config",
    paths: "Paths",
    channels: list[str],
    day: date,
    *,
    late: bool,
    assistant_factory: Callable[..., Any] | None,
    notifier: Any | None,
    now: datetime | None,
) -> str:
    try:
        outcome = deliver(config, paths, channels, late=late, assistant_factory=assistant_factory, notifier=notifier)
        log.info("morning brief delivered: %s%s", outcome, " (late)" if late else "")
    except Exception as exc:
        outcome = f"failed: {type(exc).__name__}: {exc}"
        log.error("morning brief failed: %s", exc)
    _finish(paths, day, outcome, now)
    return outcome


def tick(
    config: "Config",
    paths: "Paths",
    db: "Database",
    notifier: Any = None,
    *,
    now: datetime | None = None,
    probes: Probes | None = None,
    assistant_factory: Callable[..., Any] | None = None,
    telegram: Any | None = None,
    threaded: bool = True,
) -> str | None:
    """One pass: deliver today's brief when it is due, reclaim an unspoken voice request, otherwise return at
    once. Returns the outcome written this tick (tests), None when nothing happened. `notifier` is the
    scheduler's timer notifier and is not used: the brief has its own channels."""
    del notifier
    cfg = config.brief.schedule
    now = now or now_local()
    state = load_state(db)
    probes = probes or Probes.for_paths(paths)

    # A spoken brief the voice service did not pick up (busy, paused meanwhile, gone): Telegram instead.
    if (
        state is not None
        and state.day == now.date().isoformat()
        and state.outcome == "voice"
        and announce.pending(paths.voice_announce_file)
    ):
        try:
            age = now.timestamp() - paths.voice_announce_file.stat().st_mtime
        except OSError:
            age = 0.0
        if age > VOICE_FALLBACK_AFTER_S:
            try:
                paths.voice_announce_file.unlink()
            except OSError:
                pass
            log.warning("spoken brief not taken by the voice service after %.0f s; sending it to Telegram", age)
            save_state(db, now.date(), "delivering", now)
            return _start(
                config,
                paths,
                ["telegram"],
                now.date(),
                late=True,
                assistant_factory=assistant_factory,
                notifier=telegram,
                now=now,
                threaded=threaded,
            )
        return None

    action, reason = decide(cfg, now, state=state, asked=asked_today(db, now) if cfg.enabled else False)
    if action == "wait":
        return None
    if action == "skip":
        log.info("morning brief skipped: %s", reason)
        return save_state(db, now.date(), f"skipped: {reason}", now).outcome
    channels, why = choose_channels(cfg, probes)
    late = action == "deliver_late"
    log.info("morning brief due (%s): %s because %s", reason, "+".join(channels), why)
    save_state(db, now.date(), "delivering", now)  # the once-per-day guard, before anything slow
    return _start(
        config, paths, channels, now.date(), late=late, assistant_factory=assistant_factory, notifier=telegram, now=now, threaded=threaded
    )


def _start(config, paths, channels, day, *, late, assistant_factory, notifier, now, threaded) -> str:
    if not threaded:
        return _run_delivery(config, paths, channels, day, late=late, assistant_factory=assistant_factory, notifier=notifier, now=now)
    if not _DELIVERY_LOCK.acquire(blocking=False):
        return "delivering"

    def run() -> None:
        try:
            _run_delivery(config, paths, channels, day, late=late, assistant_factory=assistant_factory, notifier=notifier, now=now)
        finally:
            _DELIVERY_LOCK.release()

    threading.Thread(target=run, name="jarvis-brief", daemon=True).start()
    return "delivering"


def job(config: "Config", paths: "Paths", db: "Database", notifier: Any) -> None:
    """The scheduler's periodic job (scheduler/service.py PERIODIC_JOBS)."""
    tick(config, paths, db, notifier)


# --- `jarvis brief schedule` and the tool ----------------------------------------------------------------------


def describe(cfg: "BriefScheduleConfig", now: datetime, state: AutoState | None = None) -> str:
    """One line: `on; weekdays 09:00, weekends 10:30; channel auto; late until 12:00; next Mon 2026-10-19 09:00`."""
    parts = [
        "on" if cfg.enabled else "off",
        f"weekdays {cfg.weekday_time or 'off'}, weekends {cfg.weekend_time or 'off'}",
        f"channel {cfg.channel}",
        f"late until {cfg.late_until}",
    ]
    handled = state.day_date if state is not None and state.day == now.date().isoformat() else None
    nxt = next_run(cfg, now, handled_day=handled)
    parts.append("next " + (nxt.strftime("%a %Y-%m-%d %H:%M") if nxt else "none"))
    if state is not None:
        parts.append(f"last {state.day}: {state.outcome}")
    return "; ".join(parts)


__all__ = [
    "AutoState",
    "Probes",
    "QuietSink",
    "ON_DEMAND_PROMPT",
    "TELEGRAM_PROMPT",
    "VOICE_PROMPT",
    "TITLE",
    "asked_today",
    "choose_channels",
    "decide",
    "deliver",
    "describe",
    "dnd_active",
    "job",
    "load_state",
    "next_run",
    "prompt_for",
    "request_voice",
    "save_state",
    "send_telegram",
    "slot_for",
    "summarize",
    "tick",
    "voice_blocked",
]
