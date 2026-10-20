"""The local location state: `data/location_state.json` under the Jarvis home, written atomically,
read by every process (CLI, voice agent, Telegram bot). It holds the exact coordinates that never
go into profile.md:

- `mac`: where the Mac is (Shortcut / CoreLocationCLI / IP), used for local turns (cli, voice).
- `telegram`: the last location the user shared in the Telegram chat, used for remote turns only.
- `last_error` / `last_attempt_at`: why the last refresh failed, so the CLI can say it.

With `location.keep_history = true` every new Mac location is also appended to
`data/location_history.jsonl`; off by default (no movement trail on disk).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.location.model import Location
from jarvis.utils.fileio import atomic_write_text

log = logging.getLogger("jarvis.location")

STATE_VERSION = 1


@dataclass
class LocationState:
    mac: Location | None = None
    telegram: Location | None = None
    last_error: str | None = None
    last_attempt_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "mac": self.mac.to_dict() if self.mac else None,
            "telegram": self.telegram.to_dict() if self.telegram else None,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
        }


def load_state(path: Path) -> LocationState:
    """Never raises: a missing, unreadable or malformed file is an empty state."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LocationState()
    except (OSError, ValueError) as exc:
        log.warning("location state unreadable (%s); starting empty", type(exc).__name__)
        return LocationState()
    if not isinstance(data, dict):
        return LocationState()
    error = data.get("last_error")
    attempt = data.get("last_attempt_at")
    return LocationState(
        mac=Location.from_dict(data.get("mac")),
        telegram=Location.from_dict(data.get("telegram")),
        last_error=str(error)[:300] if isinstance(error, str) else None,
        last_attempt_at=str(attempt)[:40] if isinstance(attempt, str) else None,
    )


def save_state(path: Path, state: LocationState) -> None:
    atomic_write_text(Path(path), json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n")


def append_history(path: Path, location: Location) -> None:
    """One JSON line per refresh (only when `location.keep_history` is on)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(location.to_dict(), ensure_ascii=False) + "\n")
