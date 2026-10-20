"""Read-only reports over the SQLite store for `jarvis stats`, `jarvis usage`, `jarvis export`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis.observability.db import Database


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    fmt = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths))  # noqa: E731
    return "\n".join([fmt(header), fmt(["-" * w for w in widths]), *(fmt(r) for r in rows)])


# --- stats ---------------------------------------------------------------------------------------


def stats_report(db: Database, days: int = 7) -> str:
    since = _since(days)
    c = db.conn
    turns = c.execute(
        "SELECT COUNT(*) AS n, AVG(total_ms) AS avg_ms, SUM(error IS NOT NULL) AS errors,"
        " SUM(tainted) AS tainted FROM turns WHERE started_at >= ?",
        (since,),
    ).fetchone()
    sessions = c.execute("SELECT COUNT(*) AS n FROM sessions WHERE started_at >= ?", (since,)).fetchone()["n"]
    lines = [
        f"Last {days} day(s): {sessions} session(s), {turns['n']} turn(s), "
        f"{turns['errors'] or 0} failed, {turns['tainted'] or 0} tainted, "
        f"average {turns['avg_ms'] or 0:.0f} ms per turn",
        "",
    ]

    stages = c.execute(
        "SELECT t.stage, COUNT(*) AS n, AVG(t.duration_ms) AS avg_ms, MAX(t.duration_ms) AS max_ms"
        " FROM timings t JOIN turns u ON u.id = t.turn_id WHERE u.started_at >= ?"
        " GROUP BY t.stage ORDER BY avg_ms DESC",
        (since,),
    ).fetchall()
    if stages:
        lines.append("Latency per stage:")
        lines.append(
            _table(
                [[r["stage"], r["n"], f"{r['avg_ms']:.0f}", f"{r['max_ms']:.0f}"] for r in stages], ["stage", "count", "avg ms", "max ms"]
            )
        )
        lines.append("")

    voice = voice_latency_report(db, days)
    if voice:
        lines.append(voice)
        lines.append("")
    follow_ups = follow_up_report(db, days)
    if follow_ups:
        lines.append(follow_ups)
        lines.append("")

    tools = c.execute(
        "SELECT tool, COUNT(*) AS n,"
        " SUM(decision IN ('error','validation_error','blocked','denied')) AS failed,"
        " SUM(decision = 'denied') AS denied, AVG(duration_ms) AS avg_ms, SUM(is_delegate) AS by_delegate"
        " FROM tool_calls WHERE created_at >= ? GROUP BY tool ORDER BY n DESC",
        (since,),
    ).fetchall()
    if tools:
        lines.append("Tools (most used first):")
        lines.append(
            _table(
                [
                    [
                        r["tool"],
                        r["n"],
                        f"{(r['failed'] or 0) / r['n']:.0%}",
                        r["denied"] or 0,
                        f"{r['avg_ms'] or 0:.0f}",
                        r["by_delegate"] or 0,
                    ]
                    for r in tools
                ],
                ["tool", "calls", "error rate", "denied", "avg ms", "by delegate"],
            )
        )
    else:
        lines.append("No tool calls in this period.")
    return "\n".join(lines)


# Voice stages: `first_audio` (end of speech -> first audio) is the number that matters.
VOICE_STAGES = ("wake_detected", "vad_end", "stt", "ttft", "first_audio", "tts_total")


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; SQLite has none built in."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def voice_latency_report(db: Database, days: int = 7) -> str:
    """p50 / p90 per voice stage over turns that have a `first_audio` timing (voice turns only)."""
    since = _since(days)
    c = db.conn
    rows = c.execute(
        "SELECT t.stage, t.duration_ms FROM timings t JOIN turns u ON u.id = t.turn_id"
        " WHERE u.started_at >= ? AND t.stage IN (%s)"
        " AND u.id IN (SELECT turn_id FROM timings WHERE stage = 'first_audio')" % ",".join("?" * len(VOICE_STAGES)),
        (since, *VOICE_STAGES),
    ).fetchall()
    if not rows:
        return ""
    by_stage: dict[str, list[float]] = {}
    for r in rows:
        by_stage.setdefault(r["stage"], []).append(float(r["duration_ms"]))
    table = [
        [stage, len(values), f"{percentile(values, 0.5):.0f}", f"{percentile(values, 0.9):.0f}", f"{max(values):.0f}"]
        for stage in VOICE_STAGES
        if (values := by_stage.get(stage))
    ]
    first = by_stage.get("first_audio", [])
    header = f"Voice latency ({len(first)} spoken turn(s); first_audio = end of speech -> first audio):"
    return header + "\n" + _table(table, ["stage", "count", "p50 ms", "p90 ms", "max ms"])


def follow_up_report(db: Database, days: int = 7) -> str:
    """How the voice follow-up windows (listening after a reply, no wake word) were used."""
    since = _since(days)
    counts = db.follow_up_counts(since)
    opened = sum(counts.values())
    if not opened:
        return ""
    used = counts.get("turn", 0) + counts.get("pause", 0)
    noise = counts.get("noise", 0)
    silence = counts.get("silence", 0)
    cancelled = counts.get("cancelled", 0)
    row = db.conn.execute(
        "SELECT AVG(speech_after_ms) AS avg_ms FROM voice_follow_ups WHERE created_at >= ? AND speech_after_ms IS NOT NULL", (since,)
    ).fetchone()
    avg = f", speech started {row['avg_ms'] / 1000:.1f} s after the window opened on average" if row and row["avg_ms"] is not None else ""
    return (
        f"Voice follow-ups: {opened} window(s) opened, {used} used ({used / opened:.0%}), "
        f"{silence} silent, {noise} noise ({noise / opened:.0%}), {cancelled} cancelled{avg}."
    )


# --- usage -----------------------------------------------------------------------------------------


def usage_report(db: Database, days: int = 7, config: Any = None) -> str:
    since = _since(days)
    c = db.conn
    rows = c.execute(
        "SELECT substr(created_at, 1, 10) AS day, model, COUNT(*) AS calls,"
        " SUM(input_tokens) AS input, SUM(output_tokens) AS output, SUM(cache_read_tokens) AS cache_read,"
        " SUM(cache_write_tokens) AS cache_write, SUM(cost_usd) AS cost, SUM(is_delegate) AS delegate_calls"
        " FROM api_calls WHERE created_at >= ? AND status = 'ok' GROUP BY day, model ORDER BY day, model",
        (since,),
    ).fetchall()
    if not rows:
        extra = external_usage_sections(db, days, config)
        return "\n\n".join([f"No API calls in the last {days} day(s).", *extra])
    table = []
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "calls": 0}
    for r in rows:
        prompt = (r["input"] or 0) + (r["cache_read"] or 0) + (r["cache_write"] or 0)
        hit = (r["cache_read"] or 0) / prompt if prompt else 0.0
        table.append(
            [
                r["day"],
                r["model"],
                r["calls"],
                r["input"] or 0,
                r["output"] or 0,
                r["cache_read"] or 0,
                r["cache_write"] or 0,
                f"{hit:.0%}",
                f"${r['cost'] or 0:.4f}",
                r["delegate_calls"] or 0,
            ]
        )
        for k in ("input", "output", "cache_read", "cache_write", "calls"):
            totals[k] += r[k] or 0
        totals["cost"] += r["cost"] or 0
    prompt_total = totals["input"] + totals["cache_read"] + totals["cache_write"]
    hit_total = totals["cache_read"] / prompt_total if prompt_total else 0.0
    errors = c.execute("SELECT COUNT(*) AS n FROM api_calls WHERE created_at >= ? AND status != 'ok'", (since,)).fetchone()["n"]
    lines = [
        _table(table, ["day", "model", "calls", "input", "output", "cache read", "cache write", "hit", "cost", "delegate"]),
        "",
        f"Total: {totals['calls']} call(s), {totals['input']} input, {totals['output']} output tokens, "
        f"cache hit ratio {hit_total:.0%}, estimated ${totals['cost']:.4f}; {errors} failed call(s).",
    ]
    if totals["cost"] == 0 and totals["calls"]:
        lines.append("Costs are 0: fill in [budget.prices] in config.toml.")
    for section in external_usage_sections(db, days, config):
        lines += ["", section]
    return "\n".join(lines)


def external_usage_sections(db: Database, days: int, config: Any = None) -> list[str]:
    """The paid third-party services report their own section (ElevenLabs characters,
    Google Maps calls). `config` adds the budget and the daily limit to the text when given. A
    missing or broken module must not break `jarvis usage`."""
    import importlib

    sections: list[str] = []
    budget = config.voice.elevenlabs_monthly_char_budget if config is not None else None
    limit = config.maps.daily_call_limit if config is not None else None
    for module_name, label, kwargs in (
        ("jarvis.voice.elevenlabs", "ElevenLabs", {"budget": budget}),
        ("jarvis.tools.maps", "Google Maps", {"daily_call_limit": limit}),
    ):
        try:
            text = importlib.import_module(module_name).usage_report(db, days, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a report must never crash the CLI
            text = f"{label}: report unavailable ({type(exc).__name__}: {exc})"
        if text:
            sections.append(text)
    return sections


# --- export ----------------------------------------------------------------------------------------


def _block_lines(block: dict) -> list[str]:
    kind = block.get("type")
    if kind == "text":
        return [block.get("text", "")]
    if kind == "thinking":
        text = (block.get("thinking") or "").strip()
        return [f"> thinking: {text}"] if text else ["> thinking: (not shown)"]
    if kind == "tool_use":
        args = json.dumps(block.get("input", {}), ensure_ascii=False)
        return [f"> tool call `{block.get('name')}` {args}"]
    if kind == "server_tool_use":
        return [f"> server tool `{block.get('name')}` {json.dumps(block.get('input', {}), ensure_ascii=False)}"]
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = str(content or "")
        if len(content) > 1500:
            content = content[:1500] + " [...]"
        tag = "tool error" if block.get("is_error") else "tool result"
        return [f"> {tag}:", *("> " + line for line in content.splitlines())]
    if kind in ("web_search_tool_result", "web_fetch_tool_result"):
        return [f"> {kind} (content omitted)"]
    return [f"> {kind} block"]


def export_session(db: Database, session_id: str) -> str:
    c = db.conn
    session = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise ValueError(f"no session with id {session_id!r}")
    lines = [
        f"# Jarvis session {session_id}",
        "",
        f"Started {session['started_at']}"
        + (f", ended {session['ended_at']}" if session["ended_at"] else "")
        + f". Main model {session['model_main']}, delegate {session['model_delegate']}.",
        "",
    ]
    turns = c.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY started_at", (session_id,)).fetchall()
    for n, turn in enumerate(turns, 1):
        flags = []
        if turn["tainted"]:
            flags.append("tainted")
        if turn["error"]:
            flags.append(f"error: {turn['error']}")
        lines.append(f"## Turn {n} — {turn['started_at']}" + (f" ({'; '.join(flags)})" if flags else ""))
        lines.append("")
        messages = c.execute("SELECT role, content_json FROM messages WHERE turn_id = ? ORDER BY id", (turn["id"],)).fetchall()
        if not messages:
            lines += [f"**You:** {turn['user_input']}", "", f"**Jarvis:** {turn['final_reply'] or '(no reply)'}", ""]
            continue
        for m in messages:
            content = json.loads(m["content_json"])
            who = "You" if m["role"] == "user" else "Jarvis"
            if isinstance(content, str):
                lines += [f"**{who}:** {content}", ""]
                continue
            body: list[str] = []
            for block in content:
                body += _block_lines(block)
            if body:
                lines += [f"**{who}:**", *body, ""]
        ms = turn["total_ms"] or 0
        lines += [f"_{ms:.0f} ms_", ""]
    if not turns:
        lines.append("(no turns)")
    return "\n".join(lines)
