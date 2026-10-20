"""One-shot announcements for the voice service (`presence.on_arrive_greet`).

Another process (the scheduler, where the presence engine runs) cannot talk through the voice
service's speakers directly, so it leaves a small JSON file under JARVIS_HOME
(`Paths.voice_announce_file`) and the service, which polls it like the pause file, picks it up:
{"kind": "say" | "turn", "text": "...", "source": "presence", "created_at": iso}.

- `say`: the text is spoken as is (no model call).
- `turn`: the text is a prompt the service hands to the Assistant as a voice turn, so the
  reply (a greeting with a short brief) comes from the model in the user's language.

The service takes the file only while idle and not paused; a stale request (older than
MAX_AGE_S) is dropped, so a greeting cannot play an hour late.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("jarvis.voice.announce")

KINDS = ("say", "turn")
MAX_AGE_S = 300.0
MAX_TEXT_CHARS = 2000


@dataclass
class Announcement:
    kind: str
    text: str
    source: str
    created_at: str

    def age_s(self, now: datetime | None = None) -> float:
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return float("inf")
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - created).total_seconds()


def request(path: Path, *, kind: str, text: str, source: str) -> None:
    """Write (or replace) the pending announcement atomically. One at a time: a newer request
    replaces an older one that was not spoken yet."""
    if kind not in KINDS:
        raise ValueError(f"unknown announcement kind {kind!r}")
    text = " ".join((text or "").split())[:MAX_TEXT_CHARS]
    if not text:
        raise ValueError("announcement text is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": kind, "text": text, "source": source[:40], "created_at": datetime.now(timezone.utc).isoformat()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    log.info("voice announcement requested by %s (%s, %d chars)", source, kind, len(text))


def take(path: Path, now: datetime | None = None) -> Announcement | None:
    """Read and remove the pending announcement; None when there is none or it is stale/unreadable."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("voice announcement file %s is unreadable (%s); dropped", path, exc)
        raw = None
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if not isinstance(raw, dict):
        return None
    kind, text = raw.get("kind"), raw.get("text")
    if kind not in KINDS or not isinstance(text, str) or not text.strip():
        log.warning("voice announcement file %s is malformed; dropped", path)
        return None
    ann = Announcement(
        kind=kind, text=text[:MAX_TEXT_CHARS], source=str(raw.get("source") or "?")[:40], created_at=str(raw.get("created_at") or "")
    )
    if ann.age_s(now) > MAX_AGE_S:
        log.info("voice announcement from %s is %.0f s old; dropped", ann.source, ann.age_s(now))
        return None
    return ann


def pending(path: Path) -> bool:
    return path.is_file()
