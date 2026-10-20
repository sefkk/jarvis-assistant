"""Kill switch state for the Telegram bot: `/stop` from the phone, re-enable needs
both `/start` from the phone AND `jarvis telegram enable` on the Mac.

The state lives in `Paths.telegram_state_file` as JSON:
{"stopped": bool, "reenable_requested": bool, "stopped_at", "reenable_requested_at", "enabled_at"}.
Writes are atomic (temp file + os.replace). A missing file means "not stopped"; an unreadable
file is treated as stopped (fail closed) until the owner runs `jarvis telegram enable`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from jarvis.i18n import t
from jarvis.utils.timeutil import now_iso

log = logging.getLogger("jarvis.telegram.state")


@dataclass
class TelegramState:
    stopped: bool = False
    reenable_requested: bool = False
    stopped_at: str | None = None
    reenable_requested_at: str | None = None
    enabled_at: str | None = None


def load_state(path: Path) -> TelegramState:
    if not path.is_file():
        return TelegramState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state is not an object")
        return TelegramState(
            stopped=bool(raw.get("stopped", False)),
            reenable_requested=bool(raw.get("reenable_requested", False)),
            stopped_at=raw.get("stopped_at"),
            reenable_requested_at=raw.get("reenable_requested_at"),
            enabled_at=raw.get("enabled_at"),
        )
    except (OSError, ValueError) as exc:
        log.error("telegram state file %s is unreadable (%s); treating the bot as stopped", path, exc)
        return TelegramState(stopped=True, stopped_at=now_iso())


def save_state(path: Path, state: TelegramState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_stopped(path: Path) -> bool:
    return load_state(path).stopped


def request_stop(path: Path) -> TelegramState:
    """`/stop` from the allowed user: the bot ignores everything until re-enabled on both sides."""
    state = load_state(path)
    state.stopped = True
    state.reenable_requested = False
    state.stopped_at = now_iso()
    save_state(path, state)
    log.warning("telegram kill switch engaged")
    return state


def request_reenable(path: Path) -> TelegramState:
    """`/start` while stopped: records the phone side of the two-step re-enable."""
    state = load_state(path)
    if not state.stopped:
        return state
    state.reenable_requested = True
    state.reenable_requested_at = now_iso()
    save_state(path, state)
    log.warning("telegram re-enable requested from the phone; waiting for `jarvis telegram enable` on the Mac")
    return state


def enable_from_mac(path: Path) -> str:
    """`jarvis telegram enable`: clears the kill switch only when `/start` was sent first.
    Returns a one-line message for the CLI."""
    state = load_state(path)
    if not state.stopped:
        return t("telegram.cli.enable_not_stopped")
    if not state.reenable_requested:
        return t("telegram.cli.enable_needs_start")
    state.stopped = False
    state.reenable_requested = False
    state.enabled_at = now_iso()
    save_state(path, state)
    log.warning("telegram bot re-enabled from the Mac")
    return t("telegram.cli.enabled")
