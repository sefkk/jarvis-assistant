"""The Audit page: `logs/audit.jsonl` as a filterable, read-only table. The file is parsed once
and cached until its size or modification time changes; the channel of an entry comes from its session."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.dashboard.logs import decision_class, decision_label
from jarvis.dashboard.ui_text import UI, channel_label

if TYPE_CHECKING:
    import sqlite3

PAGE_SIZE = 100
DETAIL_KEYS_HIDDEN = {"ts", "session_id", "turn_id", "tool", "decision", "tainted", "risk"}
DECISION_GROUPS = {
    UI.DECISION_RAN: ("ran",),
    UI.DECISION_CONFIRMED: ("confirmed",),
    UI.DECISION_DENIED: ("denied",),
    UI.DECISION_BLOCKED: ("blocked",),
    UI.DECISION_ERROR: ("error", "validation_error"),
}
TOOL_CHANNELS = {"voice": "voice", "telegram": "telegram", "alarm": "scheduler", "sleep_mac": None, "location": None}


@dataclass
class Entry:
    ts: str
    session_id: str | None
    turn_id: str | None
    tool: str
    decision: str
    tainted: bool
    risk: str | None
    details: dict[str, Any]
    channel: str | None = None

    @property
    def decision_label(self) -> str:
        return decision_label(self.decision)

    @property
    def decision_class(self) -> str:
        return decision_class(self.decision)

    @property
    def channel_label(self) -> str:
        return channel_label(self.channel) if self.channel else "–"

    @property
    def details_text(self) -> str:
        """Compact, one line; the template escapes it."""
        if not self.details:
            return ""
        text = json.dumps(self.details, ensure_ascii=False, default=str)
        return text if len(text) <= 240 else text[:239] + "…"

    @property
    def details_full(self) -> str:
        return json.dumps(self.details, ensure_ascii=False, indent=2, default=str) if self.details else ""


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._entries: list[Entry] = []
        self._lock = threading.Lock()

    def entries(self) -> list[Entry]:
        """Every entry, newest first; re-read only when the file changed."""
        try:
            stat = self.path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if stamp == self._stamp and (stamp is not None or self._entries == []):
                return self._entries
            self._entries = self._parse() if stamp is not None else []
            self._stamp = stamp
            return self._entries

    def _parse(self) -> list[Entry]:
        out: list[Entry] = []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return out
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            details = {k: v for k, v in data.items() if k not in DETAIL_KEYS_HIDDEN}
            out.append(
                Entry(
                    ts=str(data.get("ts") or ""),
                    session_id=data.get("session_id"),
                    turn_id=data.get("turn_id"),
                    tool=str(data.get("tool") or data.get("msg") or "?"),
                    decision=str(data.get("decision") or ""),
                    tainted=bool(data.get("tainted")),
                    risk=data.get("risk"),
                    details=details,
                )
            )
        out.reverse()
        return out

    def for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        return [
            {"ts": e.ts, "tool": e.tool, "decision": e.decision, "tainted": e.tainted, "risk": e.risk, **e.details}
            for e in reversed(self.entries())
            if e.turn_id == turn_id
        ]

    def tools(self) -> list[str]:
        return sorted({e.tool for e in self.entries()})


def session_channels(conn: "sqlite3.Connection | None") -> dict[str, str]:
    if conn is None:
        return {}
    try:
        return {r["id"]: (r["channel"] or "cli") for r in conn.execute("SELECT id, channel FROM sessions")}
    except Exception:
        return {}


def _in_range(ts: str, start_iso: str, end_iso: str) -> bool:
    return start_iso <= ts < end_iso


def filter_entries(
    entries: list[Entry],
    *,
    start_iso: str,
    end_iso: str,
    tool: str | None,
    decision: str | None,
    tainted: str | None,
    channel: str | None,
    channels: dict[str, str],
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    wanted_decisions = DECISION_GROUPS.get(decision or "")
    other = decision == UI.DECISION_OTHER
    known = {d for group in DECISION_GROUPS.values() for d in group}
    selected: list[Entry] = []
    for e in entries:
        if not _in_range(e.ts, start_iso, end_iso):
            continue
        if tool and e.tool != tool:
            continue
        if wanted_decisions and e.decision not in wanted_decisions:
            continue
        if other and e.decision in known:
            continue
        if tainted == "yes" and not e.tainted:
            continue
        if tainted == "no" and e.tainted:
            continue
        e.channel = channels.get(e.session_id or "") or TOOL_CHANNELS.get(e.tool)
        if channel and e.channel != channel:
            continue
        selected.append(e)
    total = len(selected)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    rows = selected[(page - 1) * page_size : page * page_size]
    return {"rows": rows, "total": total, "page": page, "pages": pages}


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


__all__ = ["AuditLog", "Entry", "filter_entries", "session_channels", "DECISION_GROUPS", "PAGE_SIZE", "parse_ts", "timezone"]
