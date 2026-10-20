"""The Logs pages: the session list, full-text search over the turns (FTS5), one session's turns,
and the turn detail (what was said and heard, the reply, every tool call with its arguments and result, the
thinking, the timeline, the API calls, taint and confirmations). Read-only; every string of user or tool
origin is escaped by the templates, and the search snippets are built here with explicit escaping."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from markupsafe import Markup, escape

from jarvis.dashboard.analytics import Filters, stage_breakdown
from jarvis.dashboard.ui_text import UI, channel_label
from jarvis.observability import reports

if TYPE_CHECKING:
    import sqlite3

PAGE_SIZE = 50
RESULT_PREVIEW = 600  # characters of a tool result shown before "Show full result"
_MARK_START, _MARK_END = "\x02", "\x03"
DOTLESS_I = "\u0131"
_WORD_RE = re.compile(r"[\w'’]+", re.UNICODE)
DECISION_LABELS = {
    "ran": UI.DECISION_RAN,
    "confirmed": UI.DECISION_CONFIRMED,
    "denied": UI.DECISION_DENIED,
    "blocked": UI.DECISION_BLOCKED,
    "error": UI.DECISION_ERROR,
    "validation_error": UI.DECISION_ERROR,
}


def decision_label(decision: str | None) -> str:
    if not decision:
        return UI.DECISION_OTHER
    return DECISION_LABELS.get(decision, decision.replace("_", " ").capitalize())


def decision_class(decision: str | None) -> str:
    label = decision_label(decision)
    return {
        UI.DECISION_RAN: "ok",
        UI.DECISION_CONFIRMED: "ok",
        UI.DECISION_DENIED: "stopped",
        UI.DECISION_BLOCKED: "failed",
        UI.DECISION_ERROR: "failed",
    }.get(label, "muted")


# --- sessions ---------------------------------------------------------------------------------------------------


def _session_where(f: Filters) -> tuple[str, list[Any]]:
    sql = "s.started_at >= ? AND s.started_at < ?"
    args: list[Any] = [f.start_iso, f.end_iso]
    if f.channel:
        sql += " AND COALESCE(s.channel, 'cli') = ?"
        args.append(f.channel)
    return sql, args


def sessions(conn: "sqlite3.Connection", f: Filters, *, page: int = 1, page_size: int = PAGE_SIZE) -> dict[str, Any]:
    where, args = _session_where(f)
    total = int(conn.execute(f"SELECT COUNT(*) FROM sessions s WHERE {where}", args).fetchone()[0])
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    rows = conn.execute(
        f"SELECT s.id, s.started_at, s.ended_at, s.channel, s.model_main,"
        f" (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turns,"
        f" (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id AND t.error IS NOT NULL) AS errors,"
        f" (SELECT COALESCE(SUM(a.cost_usd), 0) FROM api_calls a JOIN turns t ON t.id = a.turn_id WHERE t.session_id = s.id) AS cost,"
        f" (SELECT t.user_input FROM turns t WHERE t.session_id = s.id ORDER BY t.started_at LIMIT 1) AS first_input"
        f" FROM sessions s WHERE {where} ORDER BY s.started_at DESC LIMIT ? OFFSET ?",
        [*args, page_size, (page - 1) * page_size],
    ).fetchall()
    items = [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "channel": r["channel"],
            "channel_label": channel_label(r["channel"]),
            "model": r["model_main"],
            "turns": int(r["turns"]),
            "errors": int(r["errors"]),
            "cost": float(r["cost"]),
            "first_input": r["first_input"] or "",
        }
        for r in rows
    ]
    return {"rows": items, "total": total, "page": page, "pages": pages}


# --- search -----------------------------------------------------------------------------------------------------


def fts_query(text: str) -> str | None:
    """A safe FTS5 query from free text: every word becomes a quoted prefix phrase, implicitly ANDed, so
    operators, colons and quotes in the input cannot change the query's meaning or raise a syntax error.
    The tokenizer folds the accented letters, but the dotless i (U+0131) is its own letter, so a word with
    either i is searched in both spellings (a query typed without the dotless i still finds the stored word)."""
    words = _WORD_RE.findall(text or "")
    if not words:
        return None
    parts = []
    for word in words[:12]:
        variants = _dotless_variants(word)
        quoted = ['"' + v.replace('"', '""') + '"*' for v in variants]
        parts.append(quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")")
    return " ".join(parts)


def _dotless_variants(word: str) -> list[str]:
    """The word plus its dotted/dotless i spellings (at most 8 variants: beyond three such letters only the full swaps)."""
    lowered = word.casefold().replace("i\u0307", "i")
    positions = [n for n, ch in enumerate(lowered) if ch in ("i", DOTLESS_I)]
    if not positions:
        return [word]
    out = [lowered]
    if len(positions) <= 3:
        for mask in range(1, 2 ** len(positions)):
            chars = list(lowered)
            for bit, pos in enumerate(positions):
                if mask >> bit & 1:
                    chars[pos] = DOTLESS_I if chars[pos] == "i" else "i"
            out.append("".join(chars))
    else:
        out.append(lowered.replace("i", "\0").replace(DOTLESS_I, "i").replace("\0", DOTLESS_I))
    seen: list[str] = []
    for v in out:
        if v not in seen:
            seen.append(v)
    return seen


def highlight(snippet: str) -> Markup:
    """Escape an FTS snippet, then turn its markers into <mark>."""
    text = str(escape(snippet))
    return Markup(text.replace(_MARK_START, "<mark>").replace(_MARK_END, "</mark>"))


def search(conn: "sqlite3.Connection", f: Filters, query: str, *, page: int = 1, page_size: int = PAGE_SIZE) -> dict[str, Any]:
    match = fts_query(query)
    if match is None:
        return {"rows": [], "total": 0, "page": 1, "pages": 1, "query": query}
    where, args = _session_where(f)
    base = (
        "FROM turns_fts x JOIN turns t ON t.id = x.turn_id LEFT JOIN sessions s ON s.id = t.session_id"
        " WHERE turns_fts MATCH ? AND t.started_at >= ? AND t.started_at < ?" + (" AND COALESCE(s.channel, 'cli') = ?" if f.channel else "")
    )
    params = [match, *args]
    try:
        total = int(conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        rows = conn.execute(
            f"SELECT t.id, t.session_id, t.started_at, t.total_ms, t.error, t.tainted, s.channel,"
            f" snippet(turns_fts, 1, ?, ?, '…', 16) AS input_snippet, snippet(turns_fts, 2, ?, ?, '…', 16) AS reply_snippet"
            f" {base} ORDER BY t.started_at DESC LIMIT ? OFFSET ?",
            [_MARK_START, _MARK_END, _MARK_START, _MARK_END, *params, page_size, (page - 1) * page_size],
        ).fetchall()
    except Exception:  # an FTS table missing on an old file, or a query FTS still dislikes: no results, no crash
        return {"rows": [], "total": 0, "page": 1, "pages": 1, "query": query}
    items = [
        {
            "turn_id": r["id"],
            "session_id": r["session_id"],
            "started_at": r["started_at"],
            "total_ms": r["total_ms"],
            "error": r["error"],
            "tainted": bool(r["tainted"]),
            "channel_label": channel_label(r["channel"]),
            "input": highlight(r["input_snippet"] or ""),
            "reply": highlight(r["reply_snippet"] or ""),
        }
        for r in rows
    ]
    return {"rows": items, "total": total, "page": page, "pages": pages, "query": query}


# --- one session ------------------------------------------------------------------------------------------------


def session_detail(conn: "sqlite3.Connection", session_id: str, *, page: int = 1, page_size: int = PAGE_SIZE) -> dict[str, Any] | None:
    s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if s is None:
        return None
    total = int(conn.execute("SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)).fetchone()[0])
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    rows = conn.execute(
        "SELECT t.id, t.started_at, t.user_input, t.final_reply, t.total_ms, t.tainted, t.error,"
        " COALESCE(u.cost_usd, (SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls a WHERE a.turn_id = t.id)) AS cost,"
        " COALESCE(u.tools_json, '[]') AS tools_json, COALESCE(u.denied, 0) AS denied, COALESCE(u.confirmations, 0) AS confirmations"
        " FROM turns t LEFT JOIN turn_summaries u ON u.turn_id = t.id WHERE t.session_id = ? ORDER BY t.started_at LIMIT ? OFFSET ?",
        (session_id, page_size, (page - 1) * page_size),
    ).fetchall()
    turns = []
    for r in rows:
        try:
            tools = json.loads(r["tools_json"])
        except ValueError:
            tools = []
        turns.append(
            {
                "id": r["id"],
                "started_at": r["started_at"],
                "user_input": r["user_input"] or "",
                "reply": r["final_reply"] or "",
                "total_ms": r["total_ms"],
                "tainted": bool(r["tainted"]),
                "error": r["error"],
                "cost": float(r["cost"] or 0.0),
                "tools": tools,
                "denied": int(r["denied"]),
                "confirmations": int(r["confirmations"]),
            }
        )
    cost = float(
        conn.execute(
            "SELECT COALESCE(SUM(a.cost_usd), 0) FROM api_calls a JOIN turns t ON t.id = a.turn_id WHERE t.session_id = ?", (session_id,)
        ).fetchone()[0]
    )
    return {
        "id": s["id"],
        "started_at": s["started_at"],
        "ended_at": s["ended_at"],
        "channel": s["channel"],
        "channel_label": channel_label(s["channel"]),
        "model_main": s["model_main"],
        "model_delegate": s["model_delegate"],
        "turns": turns,
        "total": total,
        "page": page,
        "pages": pages,
        "cost": cost,
        "errors": sum(1 for t in turns if t["error"]),
    }


class _ConnShim:
    """`reports.export_session` wants an object with `.conn`; the dashboard has a plain read-only connection."""

    def __init__(self, conn) -> None:
        self.conn = conn


def export_markdown(conn: "sqlite3.Connection", session_id: str) -> str | None:
    try:
        return reports.export_session(_ConnShim(conn), session_id)
    except ValueError:
        return None


# --- turn detail --------------------------------------------------------------------------------------------------


@dataclass
class ToolEvent:
    name: str
    args: str  # pretty JSON (secrets were masked when stored)
    result: str = ""
    is_error: bool = False
    images: int = 0
    decision: str | None = None
    duration_ms: float | None = None
    error: str | None = None
    risk: str | None = None
    is_delegate: bool = False

    @property
    def decision_label(self) -> str:
        return decision_label(self.decision)

    @property
    def decision_class(self) -> str:
        return decision_class(self.decision)

    @property
    def result_preview(self) -> str:
        return self.result if len(self.result) <= RESULT_PREVIEW else self.result[:RESULT_PREVIEW] + "…"

    @property
    def result_long(self) -> bool:
        return len(self.result) > RESULT_PREVIEW


@dataclass
class Event:
    kind: str  # user | untrusted | thinking | text | tool | server_tool | cut
    text: str = ""
    tool: ToolEvent | None = None


@dataclass
class TurnDetail:
    turn: dict[str, Any]
    events: list[Event] = field(default_factory=list)
    tool_calls: list[ToolEvent] = field(default_factory=list)
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    has_transcript: bool = True
    thinking_count: int = 0

    @property
    def confirmations(self) -> list[ToolEvent]:
        return [t for t in self.tool_calls if t.decision in ("confirmed", "denied")]

    @property
    def taint_sources(self) -> list[str]:
        out: list[str] = []
        for entry in self.audit:
            for source in entry.get("taint_sources") or []:
                if source not in out:
                    out.append(str(source))
        return out

    @property
    def tokens(self) -> dict[str, int]:
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        return {k: sum(int(c.get(k) or 0) for c in self.api_calls) for k in keys}

    @property
    def cost(self) -> float:
        return sum(float(c.get("cost_usd") or 0.0) for c in self.api_calls)


def _text_of_result(content: Any) -> tuple[str, int]:
    """The text of a tool_result block and how many images it carried."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    texts, images = [], 0
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "image" or "image" in str(block.get("type", "")):
                images += 1
            else:
                texts.append(json.dumps(block, ensure_ascii=False))
        else:
            texts.append(str(block))
    return "\n".join(texts), images


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def turn_detail(conn: "sqlite3.Connection", turn_id: str, audit_entries: list[dict[str, Any]] | None = None) -> TurnDetail | None:
    row = conn.execute(
        "SELECT t.*, s.channel, s.model_main FROM turns t LEFT JOIN sessions s ON s.id = t.session_id WHERE t.id = ?", (turn_id,)
    ).fetchone()
    if row is None:
        return None
    columns = row.keys()
    turn = {k: row[k] for k in columns}
    turn["channel_label"] = channel_label(turn.get("channel"))
    turn["raw_transcript"] = turn.get("raw_transcript") if "raw_transcript" in columns else None
    detail = TurnDetail(turn=turn, audit=list(audit_entries or []))

    calls = conn.execute("SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY id", (turn_id,)).fetchall()
    tool_rows = [
        ToolEvent(
            name=c["tool"],
            args=_pretty(_loads(c["args_json"])),
            decision=c["decision"],
            duration_ms=c["duration_ms"],
            error=c["error"],
            risk=c["risk"],
            is_delegate=bool(c["is_delegate"]),
        )
        for c in calls
    ]
    detail.tool_calls = tool_rows
    unmatched = list(tool_rows)

    messages = conn.execute("SELECT role, content_json FROM messages WHERE turn_id = ? ORDER BY id", (turn_id,)).fetchall()
    detail.has_transcript = bool(messages)
    pending: dict[str, ToolEvent] = {}  # tool_use id -> event waiting for its result
    for index, m in enumerate(messages):
        content = _loads(m["content_json"])
        if m["role"] == "user":
            if isinstance(content, str):
                detail.events.append(Event("user", content))
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    event = pending.pop(str(block.get("tool_use_id")), None)
                    text, images = _text_of_result(block.get("content"))
                    if event is None:
                        event = ToolEvent(name="?", args="{}")
                        detail.events.append(Event("tool", tool=event))
                    event.result, event.images, event.is_error = text, images, bool(block.get("is_error"))
                elif block.get("type") == "text":
                    text = str(block.get("text", ""))
                    kind = "untrusted" if text.lstrip().startswith("<untrusted") else "user"
                    if index == 0 and kind == "user" and not any(e.kind == "user" for e in detail.events):
                        detail.events.append(Event("user", text))
                    else:
                        detail.events.append(Event(kind, text))
            continue
        for block in content if isinstance(content, list) else [{"type": "text", "text": str(content)}]:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    detail.events.append(Event("text", str(block["text"])))
            elif kind == "thinking":
                detail.thinking_count += 1
                detail.events.append(Event("thinking", str(block.get("thinking") or "")))
            elif kind == "tool_use":
                name = str(block.get("name"))
                match = next((t for t in unmatched if t.name == name), None)
                if match is not None:
                    unmatched.remove(match)
                    match.args = _pretty(block.get("input", {}))
                    event = match
                else:
                    event = ToolEvent(name=name, args=_pretty(block.get("input", {})))
                pending[str(block.get("id"))] = event
                detail.events.append(Event("tool", tool=event))
            elif kind == "server_tool_use":
                detail.events.append(Event("server_tool", f"{block.get('name')} {json.dumps(block.get('input', {}), ensure_ascii=False)}"))
            elif kind in ("web_search_tool_result", "web_fetch_tool_result"):
                detail.events.append(Event("server_tool", str(kind)))
    if not messages:
        detail.events.append(Event("user", turn.get("user_input") or ""))
        for tool in tool_rows:
            detail.events.append(Event("tool", tool=tool))
        if turn.get("final_reply"):
            detail.events.append(Event("text", turn["final_reply"]))

    detail.api_calls = [dict(r) for r in conn.execute("SELECT * FROM api_calls WHERE turn_id = ? ORDER BY id", (turn_id,)).fetchall()]
    timings = conn.execute("SELECT stage, duration_ms FROM timings WHERE turn_id = ? ORDER BY id", (turn_id,)).fetchall()
    total = float(turn.get("total_ms") or 0.0) or max((float(t["duration_ms"]) for t in timings), default=0.0) or 1.0
    detail.timeline = [
        {
            "stage": t["stage"],
            "ms": float(t["duration_ms"]),
            "pct": round(min(100.0, float(t["duration_ms"]) / total * 100), 1),
            "cls": _stage_class(t["stage"]),
        }
        for t in timings
    ]
    return detail


def _stage_class(stage: str) -> str:
    if stage.startswith("tool:") or stage.startswith("delegate:tool:"):
        return "tools"
    if stage in ("api_call", "delegate:api_call", "compaction", "memory_pass"):
        return "api"
    if stage == "ttft":
        return "ttft"
    if stage in ("stt", "vad_end", "wake_detected"):
        return "stt"
    if stage in ("first_audio", "tts_total"):
        return "first_audio"
    return "api"


def _loads(text: str | None) -> Any:
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return text


__all__ = [
    "sessions",
    "search",
    "session_detail",
    "export_markdown",
    "turn_detail",
    "TurnDetail",
    "ToolEvent",
    "Event",
    "fts_query",
    "highlight",
    "decision_label",
    "decision_class",
    "stage_breakdown",
    "PAGE_SIZE",
]
