"""Pause state for the voice service: `jarvis voice pause [--for 60m]`, the `pause_listening` tool
("sesi kapat", "stop listening", any channel), Telegram `/mute`, and the ways back: `jarvis voice resume`,
`/unmute`, the hotkey's "Should I start listening again?" dialog, or the pause duration running out.

Paused means the service releases the microphone (the audio stream is closed, so the orange mic
indicator disappears) but keeps running with the models loaded. The state is one JSON file under
JARVIS_HOME (`Paths.voice_pause_file`), so it survives restarts and reboots:
{"paused_at": iso, "until": iso | null, "by": "cli" | "voice" | "telegram" | "tool"}.
A missing file means "listening"; a file whose `until` has passed means "auto-resume is due".
The service polls the file once a second and owns every transition (mic, notification, state).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.i18n import resolve, t
from jarvis.utils.timeutil import now_utc
from jarvis.voice.confirm import normalize_phrase

log = logging.getLogger("jarvis.voice.pause")

MAX_PAUSE_MINUTES = 24 * 60
SOURCES = {
    "cli": "jarvis voice pause",
    "voice": "spoken phrase",
    "telegram": "/mute",
    "tool": "pause_listening",
    "presence": "the Mac left the home network (presence.on_leave_pause_voice)",
    "dashboard": "the dashboard",
}

# Notifications and spoken feedback (the service sends these on every transition it sees): `voice.pause.*`.
PAUSED_TITLE = "Jarvis"

# "60m", "1h30m", "1 saat", "30 dk", "1,5 saat": a number, optional space, a unit not followed by a letter.
_DURATION_RE = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(saatliğine|saatlik|saat|sa|h|hours?|hrs?|dakikalığına|dakikalık|dakika|dk|m|minutes?|mins?)(?![^\W\d_])",
    re.IGNORECASE,
)
_HOUR_UNITS = {"saatliğine", "saatlik", "saat", "sa", "h", "hour", "hours", "hr", "hrs"}


@dataclass
class PauseState:
    paused: bool = False
    paused_at: str | None = None
    until: str | None = None
    by: str | None = None
    expired: bool = False  # the file is there but `until` has passed: auto-resume is due

    @property
    def until_dt(self) -> datetime | None:
        return _parse_iso(self.until) if self.until else None

    def remaining_minutes(self, now: datetime | None = None) -> int | None:
        until = self.until_dt
        if until is None:
            return None
        left = (until - (now or now_utc())).total_seconds() / 60.0
        return max(0, int(round(left)))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --- durations -----------------------------------------------------------------------------------


def parse_duration(text: str | None) -> int | None:
    """'60m', '1h', '1h30m', '90', '1 saat', '30 dk', '1,5 saat' -> minutes; None when unparsable."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) or None
    total = 0.0
    matched_any = False
    rest = text
    for m in _DURATION_RE.finditer(text):
        matched_any = True
        value = float(m.group(1).replace(",", "."))
        total += value * (60 if m.group(2).lower() in _HOUR_UNITS else 1)
        rest = rest.replace(m.group(0), " ", 1)
    if not matched_any or rest.strip():
        return None
    minutes = int(round(total))
    return minutes if minutes > 0 else None


def strip_duration(text: str) -> tuple[str, int | None]:
    """Remove a duration expression from a sentence: '1 saat sesi kapat' -> ('sesi kapat', 60)."""
    minutes = 0.0
    found = False
    rest = text
    for m in _DURATION_RE.finditer(text):
        found = True
        minutes += float(m.group(1).replace(",", ".")) * (60 if m.group(2).lower() in _HOUR_UNITS else 1)
        rest = rest.replace(m.group(0), " ", 1)
    value = int(round(minutes)) if found else None
    return rest, (value if value else None)


def duration_label(minutes: int | None, language: str | None = None) -> str:
    """'60 min', '90 min', '2 h 30 min' ('60 dk', '2 sa 30 dk' in Turkish) for notifications, `status` and
    replies: minutes up to two hours. `language` defaults to the main language."""
    if not minutes:
        return ""
    minutes = int(minutes)
    if minutes < 120:
        return t("voice.pause.minutes_short", language, minutes=minutes)
    hours, mins = divmod(minutes, 60)
    if mins:
        return t("voice.pause.hours_minutes_short", language, hours=hours, minutes=mins)
    return t("voice.pause.hours_short", language, hours=hours)


def spoken_label(minutes: int, language: str | None = None) -> str:
    """'1 hour 30 minutes' / '1 saat 30 dakika' (text-to-speech reads abbreviations badly)."""
    hours, mins = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append(t("voice.pause.hour_one" if hours == 1 else "voice.pause.hours", language, count=hours))
    if mins or not parts:
        parts.append(t("voice.pause.minute_one" if mins == 1 else "voice.pause.minutes", language, count=mins))
    return " ".join(parts)


def spoken_label_tr(minutes: int) -> str:
    """'1 saat', '30 dakika', '1 saat 30 dakika'."""
    return spoken_label(minutes, "tr")


def match_pause_phrase(text: str, phrases: list[str]) -> tuple[bool, int | None]:
    """Exact match against the configured pause phrases, with an optional duration anywhere in the
    sentence ('1 saat sesi kapat', 'sesi kapat 30 dakika', 'stop listening 30 minutes'). Nothing fuzzy
    beyond that."""
    wanted = {normalize_phrase(p) for p in phrases if normalize_phrase(p)}
    if not wanted:
        return False, None
    rest, minutes = strip_duration(text)
    if normalize_phrase(rest) in wanted:
        return True, minutes
    return False, None


# --- the state file -----------------------------------------------------------------------------------


def load_state(path: Path, now: datetime | None = None) -> PauseState:
    if not path.is_file():
        return PauseState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("pause state is not an object")
    except (OSError, ValueError) as exc:
        # The file exists, so the user asked for silence: stay paused until `jarvis voice resume`.
        log.warning("voice pause file %s is unreadable (%s); treating listening as paused", path, exc)
        return PauseState(paused=True)
    state = PauseState(paused=True, paused_at=raw.get("paused_at"), until=raw.get("until"), by=raw.get("by"))
    until = state.until_dt
    if until is not None and until <= (now or now_utc()):
        state.paused, state.expired = False, True
    return state


def save_state(path: Path, state: PauseState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"paused_at": state.paused_at, "until": state.until, "by": state.by}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_paused(path: Path, now: datetime | None = None) -> bool:
    return load_state(path, now).paused


def pause(path: Path, *, by: str, minutes: int | None = None, now: datetime | None = None) -> PauseState:
    """Pause, or re-pause with a new duration (None = until resumed). The original `paused_at` is
    kept when already paused; `by` records the latest request."""
    if minutes is not None and not 1 <= int(minutes) <= MAX_PAUSE_MINUTES:
        raise ValueError(f"pause duration must be between 1 and {MAX_PAUSE_MINUTES} minutes")
    now = now or now_utc()
    current = load_state(path, now)
    state = PauseState(
        paused=True,
        paused_at=current.paused_at if current.paused and current.paused_at else _iso(now),
        until=_iso(now + timedelta(minutes=int(minutes))) if minutes else None,
        by=by,
    )
    save_state(path, state)
    log.info("voice listening paused by %s%s", by, f" for {minutes} min" if minutes else "")
    return state


def resume(path: Path) -> bool:
    """Remove the pause. Returns False when listening was not paused (no file)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    log.info("voice listening resumed")
    return True


# --- texts -----------------------------------------------------------------------------------------------


def _local(iso: str | None) -> str:
    dt = _parse_iso(iso) if iso else None
    return dt.astimezone().strftime("%H:%M") if dt else "?"


def paused_notification(state: PauseState, language: str | None = None) -> str:
    label = duration_label(state.remaining_minutes(), language)
    return t("voice.pause.notification_for", language, label=label) if label else t("voice.pause.notification", language)


def resumed_notification(language: str | None = None) -> str:
    return t("voice.pause.resumed", language)


def resume_question(language: str | None = None) -> str:
    """The hotkey's dialog while paused."""
    return t("voice.pause.resume_question", language)


def resume_options(language: str | None = None) -> list[tuple[str, str]]:
    """The dialog's buttons as (key, label); the service acts on the key, so any language parses."""
    return [("no", t("voice.pause.resume_no", language)), ("yes", t("voice.pause.resume_yes", language))]


def spoken_pause_ack(language: str | None, minutes: int | None) -> str:
    lang = resolve(language)
    if minutes:
        return t("voice.pause.ack_for", lang, label=spoken_label(minutes, lang))
    return t("voice.pause.ack", lang)


def describe(state: PauseState, now: datetime | None = None) -> str:
    """For `jarvis voice status` and `jarvis doctor`."""
    if state.expired:
        return "paused, but the pause time is over: the service resumes on its next check (or run `jarvis voice resume`)"
    if not state.paused:
        return "listening"
    who = SOURCES.get(state.by or "", state.by or "unknown")
    text = f"paused since {_local(state.paused_at)}"
    if state.until:
        text += f" until {_local(state.until)} ({duration_label(state.remaining_minutes(now))} left, auto-resume)"
    else:
        text += " until resumed"
    return f"{text} (by {who}); back on with `jarvis voice resume`, /unmute, or hold the hotkey and answer the dialog"
