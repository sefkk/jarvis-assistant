"""The Analytics page's numbers: every function takes a read-only connection and the page's
filters and returns plain dicts and lists, so the templates render the tables and `/analytics/data` hands
the same data to the charts as JSON. Dates are bucketed by the Mac's local day; the database stores UTC.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from jarvis.dashboard.ui_text import UI, channel_label
from jarvis.observability.reports import percentile
from jarvis.utils.timeutil import now_local

if TYPE_CHECKING:
    import sqlite3

    from jarvis.config import Config

RANGE_KEYS = ("today", "7d", "30d", "custom")
CHANNELS = ("cli", "voice", "telegram", "scheduler")
PROVIDER_ANTHROPIC, PROVIDER_ELEVENLABS, PROVIDER_MAPS = "Anthropic", "ElevenLabs", "Google Maps"
ERROR_TYPE_LABELS = {
    "timeout": UI.ERR_TIMEOUT,
    "overloaded": UI.ERR_OVERLOADED,
    "rate_limit": UI.ERR_RATE_LIMIT,
    "auth": UI.ERR_AUTH,
    "network": UI.ERR_NETWORK,
    "validation": UI.ERR_VALIDATION,
    "other": UI.ERR_OTHER,
}
COMPOSITION_PARTS = (
    "persona",
    "rules",
    "profile",
    "memory",
    "tool_definitions",
    "conversation_history",
    "tool_results",
    "user_input",
    "thinking",
)
COMPOSITION_LABELS = {
    "persona": UI.PART_PERSONA,
    "rules": UI.PART_RULES,
    "profile": UI.PART_PROFILE,
    "memory": UI.PART_MEMORY,
    "tool_definitions": UI.PART_TOOLS,
    "conversation_history": UI.PART_HISTORY,
    "tool_results": UI.PART_TOOL_RESULTS,
    "user_input": UI.PART_USER_INPUT,
    "thinking": UI.PART_THINKING,
}
STAGE_LABELS = {
    "stt": UI.STAGE_STT,
    "ttft": UI.STAGE_TTFT,
    "api": UI.STAGE_API,
    "tools": UI.STAGE_TOOLS,
    "first_audio": UI.STAGE_FIRST_AUDIO,
}
REQUEST_SORTS = {
    "time": "s.started_at",
    "latency": "s.total_ms",
    "cost": "s.cost_usd",
    "tokens": "(s.input_tokens + s.cache_read_tokens + s.cache_write_tokens + s.output_tokens)",
}
PAGE_SIZE = 50
REPEAT_WINDOW_S = 120
DEFAULT_CORRECTION_WORDS = ("no ", "not that", "wrong", "i meant")  # the real list is dashboard.correction_words in config.toml


# --- filters -----------------------------------------------------------------------------------------------


@dataclass
class Filters:
    range_key: str
    start: datetime  # local, inclusive
    end: datetime  # local, exclusive
    channel: str | None = None
    model: str | None = None

    @classmethod
    def from_query(
        cls,
        *,
        range_key: str | None,
        channel: str | None,
        model: str | None,
        start: str | None,
        end: str | None,
        now: datetime | None = None,
    ) -> "Filters":
        now = now or now_local()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        key = range_key if range_key in RANGE_KEYS else "7d"
        if key == "today":
            begin, finish = today, today + timedelta(days=1)
        elif key == "30d":
            begin, finish = today - timedelta(days=29), today + timedelta(days=1)
        elif key == "custom":
            begin = _parse_day(start, today - timedelta(days=6), now.tzinfo)
            finish = _parse_day(end, today, now.tzinfo) + timedelta(days=1)
            if finish <= begin:
                finish = begin + timedelta(days=1)
        else:
            key, begin, finish = "7d", today - timedelta(days=6), today + timedelta(days=1)
        return cls(key, begin, finish, channel if channel in CHANNELS else None, (model or "").strip() or None)

    @property
    def start_iso(self) -> str:
        return _utc_iso(self.start)

    @property
    def end_iso(self) -> str:
        return _utc_iso(self.end)

    @property
    def start_day(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def end_day(self) -> str:
        return (self.end - timedelta(days=1)).strftime("%Y-%m-%d")

    def days(self) -> list[str]:
        out = []
        day = self.start.date()
        while day < self.end.date():
            out.append(day.isoformat())
            day += timedelta(days=1)
        return out

    def query_string(self, **extra: Any) -> str:
        from urllib.parse import urlencode  # noqa: PLC0415

        params: dict[str, Any] = {"range": self.range_key}
        if self.range_key == "custom":
            params["start"], params["end"] = self.start_day, self.end_day
        if self.channel:
            params["channel"] = self.channel
        if self.model:
            params["model"] = self.model
        params.update({k: v for k, v in extra.items() if v not in (None, "")})
        return urlencode(params)


def _parse_day(text: str | None, default: datetime, tz) -> datetime:
    if text and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            pass
    return default


def _utc_iso(local: datetime) -> str:
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _day_modifier(sample: datetime) -> str:
    """SQLite `date(created_at, ?)` modifier that turns a UTC timestamp into the local day."""
    offset = sample.utcoffset() or timedelta(0)
    return f"{int(offset.total_seconds()):+d} seconds"


def _fill(days: list[str], values: dict[str, float], default: float | None = 0.0) -> list[float | None]:
    return [values.get(day, default) for day in days]


# --- cost -------------------------------------------------------------------------------------------------------


def _api_where(f: Filters, alias: str = "a") -> tuple[str, list[Any]]:
    """WHERE for api_calls (alias a) LEFT JOIN turn_summaries s: range, optional channel and model."""
    sql = f"{alias}.created_at >= ? AND {alias}.created_at < ?"
    args: list[Any] = [f.start_iso, f.end_iso]
    if f.channel:
        sql += " AND s.channel = ?"
        args.append(f.channel)
    if f.model:
        sql += f" AND {alias}.model = ?"
        args.append(f.model)
    return sql, args


def _turn_where(f: Filters, alias: str = "s") -> tuple[str, list[Any]]:
    sql = f"{alias}.started_at >= ? AND {alias}.started_at < ?"
    args: list[Any] = [f.start_iso, f.end_iso]
    if f.channel:
        sql += f" AND {alias}.channel = ?"
        args.append(f.channel)
    if f.model:
        sql += f" AND {alias}.model = ?"
        args.append(f.model)
    return sql, args


def _provider_cost(units: int, service: str, config: "Config") -> float:
    if service == "elevenlabs":
        return units / 1000.0 * config.budget.elevenlabs_usd_per_1k_chars
    if service == "maps":
        return units * config.budget.maps_usd_per_call
    return 0.0


def daily_cost(conn: "sqlite3.Connection", f: Filters, config: "Config") -> dict[str, Any]:
    days = f.days()
    modifier = _day_modifier(f.start)
    where, args = _api_where(f)
    rows = conn.execute(
        f"SELECT date(a.created_at, ?) AS day, COALESCE(SUM(a.cost_usd), 0) AS cost FROM api_calls a"
        f" LEFT JOIN turn_summaries s ON s.turn_id = a.turn_id WHERE {where} GROUP BY day",
        [modifier, *args],
    ).fetchall()
    anthropic = {r["day"]: float(r["cost"]) for r in rows}
    providers: dict[str, dict[str, float]] = {"elevenlabs": {}, "maps": {}}
    for r in conn.execute(
        "SELECT service, date(created_at, ?) AS day, COALESCE(SUM(units), 0) AS units FROM external_calls"
        " WHERE created_at >= ? AND created_at < ? AND status = 'ok' AND cached = 0 GROUP BY service, day",
        [modifier, f.start_iso, f.end_iso],
    ):
        if r["service"] in providers:
            providers[r["service"]][r["day"]] = _provider_cost(int(r["units"]), r["service"], config)
    return {
        "days": days,
        "anthropic": _fill(days, anthropic),
        "elevenlabs": _fill(days, providers["elevenlabs"]),
        "maps": _fill(days, providers["maps"]),
        "budget": float(config.budget.daily_budget_usd),
    }


def cost_by_provider(conn: "sqlite3.Connection", f: Filters, config: "Config") -> dict[str, float]:
    where, args = _api_where(f)
    anthropic = conn.execute(
        f"SELECT COALESCE(SUM(a.cost_usd), 0) AS c FROM api_calls a LEFT JOIN turn_summaries s ON s.turn_id = a.turn_id WHERE {where}", args
    ).fetchone()["c"]
    out = {PROVIDER_ANTHROPIC: float(anthropic), PROVIDER_ELEVENLABS: 0.0, PROVIDER_MAPS: 0.0}
    for r in conn.execute(
        "SELECT service, COALESCE(SUM(units), 0) AS units FROM external_calls"
        " WHERE created_at >= ? AND created_at < ? AND status = 'ok' AND cached = 0 GROUP BY service",
        [f.start_iso, f.end_iso],
    ):
        label = {"elevenlabs": PROVIDER_ELEVENLABS, "maps": PROVIDER_MAPS}.get(r["service"])
        if label:
            out[label] = _provider_cost(int(r["units"]), r["service"], config)
    return out


def monthly(conn: "sqlite3.Connection", config: "Config", now: datetime | None = None) -> dict[str, Any]:
    now = now or now_local()
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (first + timedelta(days=32)).replace(day=1)
    month = Filters("custom", first, next_month)
    total = sum(cost_by_provider(conn, month, config).values())
    elapsed_days = (now - first).total_seconds() / 86400.0
    days_in_month = (next_month - first).days
    projected = total / elapsed_days * days_in_month if elapsed_days >= 1 else total * days_in_month
    return {"month": first.strftime("%B %Y"), "total": total, "projected": projected, "days_in_month": days_in_month}


# --- models, composition, cache -----------------------------------------------------------------------------


def model_usage(conn: "sqlite3.Connection", f: Filters) -> list[dict[str, Any]]:
    where, args = _api_where(f)
    rows = conn.execute(
        f"SELECT a.model, a.is_delegate, COUNT(*) AS calls, COALESCE(SUM(a.cost_usd), 0) AS cost,"
        f" COALESCE(SUM(a.input_tokens + a.cache_read_tokens + a.cache_write_tokens + a.output_tokens), 0) AS tokens"
        f" FROM api_calls a LEFT JOIN turn_summaries s ON s.turn_id = a.turn_id WHERE {where} AND a.status = 'ok'"
        f" GROUP BY a.model, a.is_delegate ORDER BY cost DESC",
        args,
    ).fetchall()
    return [
        {
            "model": r["model"],
            "role": "delegate" if r["is_delegate"] else "main",
            "label": r["model"] + (" (delegate)" if r["is_delegate"] else ""),
            "tokens": int(r["tokens"]),
            "cost": float(r["cost"]),
            "calls": int(r["calls"]),
        }
        for r in rows
    ]


def composition(conn: "sqlite3.Connection", f: Filters) -> dict[str, Any]:
    """Totals per prompt part and the average tokens per call per day (stacked bar)."""
    where, args = _api_where(f)
    modifier = _day_modifier(f.start)
    rows = conn.execute(
        f"SELECT date(a.created_at, ?) AS day, a.composition_json FROM api_calls a LEFT JOIN turn_summaries s ON s.turn_id = a.turn_id"
        f" WHERE {where} AND a.composition_json IS NOT NULL",
        [modifier, *args],
    ).fetchall()
    totals = {part: 0 for part in COMPOSITION_PARTS}
    per_day: dict[str, dict[str, int]] = {}
    counts: dict[str, int] = {}
    exact = 0
    for r in rows:
        try:
            data = json.loads(r["composition_json"])
        except (TypeError, ValueError):
            continue
        exact += 1 if data.get("exact") else 0
        day = per_day.setdefault(r["day"], {part: 0 for part in COMPOSITION_PARTS})
        counts[r["day"]] = counts.get(r["day"], 0) + 1
        for part in COMPOSITION_PARTS:
            value = int(data.get(part, 0) or 0)
            totals[part] += value
            day[part] += value
    days = f.days()
    series = {part: [round(per_day[d][part] / counts[d]) if d in counts else 0 for d in days] for part in COMPOSITION_PARTS}
    return {"parts": totals, "labels": COMPOSITION_LABELS, "days": days, "series": series, "calls": len(rows), "exact_calls": exact}


def cache_stats(conn: "sqlite3.Connection", f: Filters) -> dict[str, Any]:
    where, args = _api_where(f)
    modifier = _day_modifier(f.start)
    rows = conn.execute(
        f"SELECT date(a.created_at, ?) AS day, COALESCE(SUM(a.input_tokens), 0) AS inp, COALESCE(SUM(a.cache_read_tokens), 0) AS cr,"
        f" COALESCE(SUM(a.cache_write_tokens), 0) AS cw FROM api_calls a LEFT JOIN turn_summaries s ON s.turn_id = a.turn_id"
        f" WHERE {where} AND a.status = 'ok' GROUP BY day",
        [modifier, *args],
    ).fetchall()
    days = f.days()
    rate: dict[str, float | None] = {}
    saved: dict[str, float] = {}
    total_read = total_prompt = 0
    for r in rows:
        prompt = int(r["inp"]) + int(r["cr"]) + int(r["cw"])
        rate[r["day"]] = round(int(r["cr"]) / prompt * 100, 1) if prompt else None
        saved[r["day"]] = int(r["cr"])
        total_read += int(r["cr"])
        total_prompt += prompt
    return {
        "days": days,
        "hit_rate": _fill(days, rate, None),
        "saved_tokens": _fill(days, saved),
        "overall_hit_rate": round(total_read / total_prompt * 100, 1) if total_prompt else None,
        "total_saved": total_read,
    }


# --- latency ----------------------------------------------------------------------------------------------------


def stage_breakdown(stages: dict[str, float]) -> dict[str, float]:
    """The five dashboard stages from a turn's timings: STT, time to first token, the rest of the API time,
    the tools, and end of speech to first audio (voice)."""
    api_total = float(stages.get("api_call", 0.0))
    ttft = float(stages.get("ttft", 0.0))
    tools = sum(float(v) for k, v in stages.items() if k.startswith("tool:") or k.startswith("delegate:tool:"))
    return {
        "stt": float(stages.get("stt", 0.0)),
        "ttft": min(ttft, api_total) if api_total else ttft,
        "api": max(0.0, api_total - min(ttft, api_total)),
        "tools": tools,
        "first_audio": float(stages.get("first_audio", 0.0)),
    }


def latency(conn: "sqlite3.Connection", f: Filters) -> dict[str, Any]:
    where, args = _turn_where(f)
    modifier = _day_modifier(f.start)
    rows = conn.execute(
        f"SELECT date(s.started_at, ?) AS day, s.total_ms, s.stages_json FROM turn_summaries s WHERE {where} AND s.total_ms IS NOT NULL",
        [modifier, *args],
    ).fetchall()
    days = f.days()
    totals: dict[str, list[float]] = {}
    stage_sums: dict[str, dict[str, float]] = {}
    for r in rows:
        totals.setdefault(r["day"], []).append(float(r["total_ms"]))
        try:
            stages = json.loads(r["stages_json"] or "{}")
        except ValueError:
            stages = {}
        day = stage_sums.setdefault(r["day"], {k: 0.0 for k in STAGE_LABELS})
        for key, value in stage_breakdown(stages).items():
            day[key] += value
    p50 = {d: percentile(v, 0.5) for d, v in totals.items()}
    p90 = {d: percentile(v, 0.9) for d, v in totals.items()}
    p95 = {d: percentile(v, 0.95) for d, v in totals.items()}
    stages_series = {key: [round(stage_sums[d][key] / len(totals[d])) if d in totals else 0 for d in days] for key in STAGE_LABELS}
    everything = [v for values in totals.values() for v in values]
    return {
        "days": days,
        "p50": _fill(days, p50, None),
        "p90": _fill(days, p90, None),
        "p95": _fill(days, p95, None),
        "stages": stages_series,
        "stage_labels": STAGE_LABELS,
        "turns": len(everything),
        "overall_p50": percentile(everything, 0.5) if everything else None,
        "overall_p90": percentile(everything, 0.9) if everything else None,
        "overall_p95": percentile(everything, 0.95) if everything else None,
    }


# --- requests ---------------------------------------------------------------------------------------------------


def requests(
    conn: "sqlite3.Connection", f: Filters, *, sort: str = "time", direction: str = "desc", page: int = 1, page_size: int = PAGE_SIZE
) -> dict[str, Any]:
    where, args = _turn_where(f)
    order = REQUEST_SORTS.get(sort, REQUEST_SORTS["time"])
    sort = sort if sort in REQUEST_SORTS else "time"
    direction = "asc" if direction == "asc" else "desc"
    total = int(conn.execute(f"SELECT COUNT(*) FROM turn_summaries s WHERE {where}", args).fetchone()[0])
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), pages)
    rows = conn.execute(
        f"SELECT s.*, t.user_input FROM turn_summaries s LEFT JOIN turns t ON t.id = s.turn_id WHERE {where}"
        f" ORDER BY {order} {direction.upper()}, s.started_at DESC LIMIT ? OFFSET ?",
        [*args, page_size, (page - 1) * page_size],
    ).fetchall()
    out = []
    for r in rows:
        try:
            stages = json.loads(r["stages_json"] or "{}")
            tools = json.loads(r["tools_json"] or "[]")
        except ValueError:
            stages, tools = {}, []
        breakdown = stage_breakdown(stages)
        total_ms = float(r["total_ms"] or 0.0)
        covered = sum(breakdown.values())
        base = max(total_ms, covered) or 1.0
        segments = [
            {"key": k, "label": STAGE_LABELS[k], "ms": round(v), "pct": round(v / base * 100, 1)} for k, v in breakdown.items() if v > 0
        ]
        flags = []
        if r["error"]:
            flags.append("error")
        if r["tainted"]:
            flags.append("tainted")
        if r["denied"]:
            flags.append("denied")
        if r["confirmations"]:
            flags.append("confirmed")
        if r["api_errors"]:
            flags.append("retried")
        out.append(
            {
                "turn_id": r["turn_id"],
                "started_at": r["started_at"],
                "channel": r["channel"],
                "channel_label": channel_label(r["channel"]),
                "request": r["user_input"] or "",
                "total_ms": r["total_ms"],
                "segments": segments,
                "tools": tools,
                "tokens": int(r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] + r["output_tokens"]),
                "cost": float(r["cost_usd"]),
                "flags": flags,
                "model": r["model"],
            }
        )
    return {"rows": out, "total": total, "page": page, "pages": pages, "sort": sort, "direction": direction}


# --- errors and tools -----------------------------------------------------------------------------------------


def _external_category(status: str) -> str:
    text = (status or "").lower()
    if "timeout" in text:
        return "timeout"
    if "denied" in text or "401" in text or "403" in text or "auth" in text:
        return "auth"
    if "429" in text or "quota" in text or "limit" in text:
        return "rate_limit"
    if text.startswith("http_4") or "invalid" in text:
        return "validation"
    if "connect" in text or "network" in text:
        return "network"
    return "other"


def errors(conn: "sqlite3.Connection", f: Filters) -> dict[str, Any]:
    where, args = _api_where(f)
    by_type = {key: 0 for key in ERROR_TYPE_LABELS}
    retries = final = 0
    top: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in conn.execute(
        f"SELECT a.status, a.error_category, a.final_failure, a.created_at, a.turn_id FROM api_calls a"
        f" LEFT JOIN turn_summaries s ON s.turn_id = a.turn_id WHERE {where} AND a.status != 'ok' ORDER BY a.created_at DESC",
        args,
    ):
        category = r["error_category"] or "other"
        by_type[category] = by_type.get(category, 0) + 1
        if r["final_failure"]:
            final += 1
        else:
            retries += 1
        key = (PROVIDER_ANTHROPIC, "messages", r["status"])
        entry = top.setdefault(
            key,
            {
                "error": r["status"],
                "provider": PROVIDER_ANTHROPIC,
                "endpoint": "messages",
                "type": ERROR_TYPE_LABELS.get(category, UI.ERR_OTHER),
                "count": 0,
                "last_seen": r["created_at"],
                "example_turn": r["turn_id"],
            },
        )
        entry["count"] += 1
    for r in conn.execute(
        "SELECT service, operation, status, created_at FROM external_calls WHERE created_at >= ? AND created_at < ? AND status != 'ok'"
        " ORDER BY created_at DESC",
        [f.start_iso, f.end_iso],
    ):
        provider = {"elevenlabs": PROVIDER_ELEVENLABS, "maps": PROVIDER_MAPS}.get(r["service"], r["service"])
        category = _external_category(r["status"])
        by_type[category] = by_type.get(category, 0) + 1
        final += 1
        key = (provider, r["operation"], r["status"])
        entry = top.setdefault(
            key,
            {
                "error": r["status"],
                "provider": provider,
                "endpoint": r["operation"],
                "type": ERROR_TYPE_LABELS.get(category, UI.ERR_OTHER),
                "count": 0,
                "last_seen": r["created_at"],
                "example_turn": None,
            },
        )
        entry["count"] += 1
    tool_where, tool_args = _tool_where(f)
    failing = [
        {
            "tool": r["tool"],
            "calls": int(r["calls"]),
            "errors": int(r["errors"]),
            "rate": round(int(r["errors"]) / int(r["calls"]) * 100, 1),
            "last_error": r["last_error"] or "",
        }
        for r in conn.execute(
            f"SELECT c.tool, COUNT(*) AS calls, SUM(c.decision IN ('error', 'validation_error')) AS errors,"
            f" MAX(CASE WHEN c.decision IN ('error', 'validation_error') THEN c.error END) AS last_error"
            f" FROM tool_calls c LEFT JOIN turn_summaries s ON s.turn_id = c.turn_id WHERE {tool_where}"
            f" GROUP BY c.tool HAVING errors > 0 ORDER BY errors DESC, calls DESC LIMIT 10",
            tool_args,
        )
    ]
    return {
        "by_type": {ERROR_TYPE_LABELS[k]: v for k, v in by_type.items()},
        "retries": retries,
        "final": final,
        "top": sorted(top.values(), key=lambda e: (-e["count"], e["last_seen"]))[:20],
        "failing_tools": failing,
    }


def _tool_where(f: Filters) -> tuple[str, list[Any]]:
    sql = "c.created_at >= ? AND c.created_at < ?"
    args: list[Any] = [f.start_iso, f.end_iso]
    if f.channel:
        sql += " AND s.channel = ?"
        args.append(f.channel)
    if f.model:
        sql += " AND s.model = ?"
        args.append(f.model)
    return sql, args


def tool_usage(conn: "sqlite3.Connection", f: Filters) -> list[dict[str, Any]]:
    where, args = _tool_where(f)
    rows = conn.execute(
        f"SELECT c.tool, COUNT(*) AS calls, AVG(c.duration_ms) AS avg_ms, SUM(c.decision IN ('error', 'validation_error')) AS errors,"
        f" SUM(c.decision = 'denied') AS denied, SUM(c.decision = 'blocked') AS blocked"
        f" FROM tool_calls c LEFT JOIN turn_summaries s ON s.turn_id = c.turn_id WHERE {where} GROUP BY c.tool ORDER BY calls DESC",
        args,
    ).fetchall()
    return [
        {
            "tool": r["tool"],
            "calls": int(r["calls"]),
            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
            "errors": int(r["errors"] or 0),
            "error_rate": round(int(r["errors"] or 0) / int(r["calls"]) * 100, 1),
            "denied": int(r["denied"] or 0),
            "blocked": int(r["blocked"] or 0),
        }
        for r in rows
    ]


def channels(conn: "sqlite3.Connection", f: Filters) -> dict[str, int]:
    where, args = _turn_where(f)
    rows = conn.execute(f"SELECT s.channel, COUNT(*) AS n FROM turn_summaries s WHERE {where} GROUP BY s.channel ORDER BY n DESC", args)
    return {channel_label(r["channel"]): int(r["n"]) for r in rows}


def confirmations(conn: "sqlite3.Connection", f: Filters) -> list[dict[str, Any]]:
    """Approved / denied confirmations by reason: a CONFIRM-risk tool, or a SAFE tool in a tainted turn."""
    where, args = _tool_where(f)
    rows = conn.execute(
        f"SELECT (c.risk = 'CONFIRM') AS risky, SUM(c.decision = 'confirmed') AS approved, SUM(c.decision = 'denied') AS denied"
        f" FROM tool_calls c LEFT JOIN turn_summaries s ON s.turn_id = c.turn_id WHERE {where} AND c.decision IN ('confirmed', 'denied')"
        f" GROUP BY risky ORDER BY risky DESC",
        args,
    ).fetchall()
    return [
        {
            "reason": UI.CONFIRM_REASON_RISKY if r["risky"] else UI.CONFIRM_REASON_TAINTED,
            "approved": int(r["approved"] or 0),
            "denied": int(r["denied"] or 0),
        }
        for r in rows
    ]


# --- voice ------------------------------------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", (text or "").casefold()).strip()


def voice(
    conn: "sqlite3.Connection", f: Filters, correction_words: tuple[str, ...] | list[str] = DEFAULT_CORRECTION_WORDS
) -> dict[str, Any]:
    events = {
        r["kind"]: int(r["n"])
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM voice_events WHERE created_at >= ? AND created_at < ? GROUP BY kind", [f.start_iso, f.end_iso]
        )
    }
    triggers = {
        r["detail"] or "?": int(r["n"])
        for r in conn.execute(
            "SELECT detail, COUNT(*) AS n FROM voice_events WHERE kind = 'trigger' AND created_at >= ? AND created_at < ? GROUP BY detail",
            [f.start_iso, f.end_iso],
        )
    }
    follow_ups = {
        r["outcome"]: int(r["n"])
        for r in conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM voice_follow_ups WHERE created_at >= ? AND created_at < ? GROUP BY outcome",
            [f.start_iso, f.end_iso],
        )
    }
    opened = sum(follow_ups.values())
    used = follow_ups.get("turn", 0) + follow_ups.get("pause", 0)
    first_audio = [
        float(r["duration_ms"])
        for r in conn.execute(
            "SELECT t.duration_ms FROM timings t JOIN turns u ON u.id = t.turn_id WHERE t.stage = 'first_audio' AND u.started_at >= ? AND u.started_at < ?",
            [f.start_iso, f.end_iso],
        )
    ]
    turns = conn.execute(
        "SELECT s.session_id, s.started_at, t.user_input FROM turn_summaries s JOIN turns t ON t.id = s.turn_id"
        " WHERE s.channel = 'voice' AND s.started_at >= ? AND s.started_at < ? ORDER BY s.session_id, s.started_at",
        [f.start_iso, f.end_iso],
    ).fetchall()
    repeats = corrections = 0
    previous: tuple[str, datetime, str] | None = None
    for r in turns:
        started = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
        text = _normalize(r["user_input"])
        if previous and previous[0] == r["session_id"] and (started - previous[1]).total_seconds() <= REPEAT_WINDOW_S:
            if text and text == previous[2]:
                repeats += 1
            elif any(text.startswith(_normalize(word)) for word in correction_words if _normalize(word)):
                corrections += 1
        previous = (r["session_id"], started, text)
    voice_turns = len(turns)
    return {
        "triggers": events.get("trigger", 0),
        "triggers_by_kind": triggers,
        "transcribed": events.get("transcribed", 0),
        "dropped": events.get("transcript_dropped", 0),
        "dropped_rate": round(
            events.get("transcript_dropped", 0) / (events.get("transcribed", 0) + events.get("transcript_dropped", 0)) * 100, 1
        )
        if events.get("transcribed", 0) + events.get("transcript_dropped", 0)
        else None,
        "follow_ups_opened": opened,
        "follow_ups_used": used,
        "follow_up_rate": round(used / opened * 100, 1) if opened else None,
        "follow_ups": follow_ups,
        "voice_turns": voice_turns,
        "repeats": repeats,
        "corrections": corrections,
        "repeat_rate": round(repeats / voice_turns * 100, 1) if voice_turns else None,
        "correction_rate": round(corrections / voice_turns * 100, 1) if voice_turns else None,
        "first_audio_p50": percentile(first_audio, 0.5) if first_audio else None,
        "first_audio_p90": percentile(first_audio, 0.9) if first_audio else None,
        "spoken_turns": len(first_audio),
    }


# --- everything for the charts ----------------------------------------------------------------------------------


def models_available(conn: "sqlite3.Connection") -> list[str]:
    return [r["model"] for r in conn.execute("SELECT DISTINCT model FROM api_calls ORDER BY model")]


def chart_data(conn: "sqlite3.Connection", f: Filters, config: "Config", now: datetime | None = None) -> dict[str, Any]:
    """Everything the charts need, as one JSON document (`/analytics/data`)."""
    return {
        "range": {"key": f.range_key, "start": f.start_day, "end": f.end_day, "channel": f.channel, "model": f.model},
        "daily_cost": daily_cost(conn, f, config),
        "cost_by_provider": cost_by_provider(conn, f, config),
        "monthly": monthly(conn, config, now),
        "model_usage": model_usage(conn, f),
        "composition": composition(conn, f),
        "cache": cache_stats(conn, f),
        "latency": latency(conn, f),
        "errors": {k: v for k, v in errors(conn, f).items() if k in ("by_type", "retries", "final")},
        "channels": channels(conn, f),
        "confirmations": confirmations(conn, f),
    }


def empty_chart_data(f: Filters, config: "Config") -> dict[str, Any]:
    """When there is no database yet."""
    days = f.days()
    zero = [0.0 for _ in days]
    return {
        "range": {"key": f.range_key, "start": f.start_day, "end": f.end_day, "channel": f.channel, "model": f.model},
        "daily_cost": {"days": days, "anthropic": zero, "elevenlabs": zero, "maps": zero, "budget": float(config.budget.daily_budget_usd)},
        "cost_by_provider": {PROVIDER_ANTHROPIC: 0.0, PROVIDER_ELEVENLABS: 0.0, PROVIDER_MAPS: 0.0},
        "monthly": {"month": now_local().strftime("%B %Y"), "total": 0.0, "projected": 0.0, "days_in_month": 30},
        "model_usage": [],
        "composition": {
            "parts": {p: 0 for p in COMPOSITION_PARTS},
            "labels": COMPOSITION_LABELS,
            "days": days,
            "series": {p: [0 for _ in days] for p in COMPOSITION_PARTS},
            "calls": 0,
            "exact_calls": 0,
        },
        "cache": {"days": days, "hit_rate": [None for _ in days], "saved_tokens": zero, "overall_hit_rate": None, "total_saved": 0},
        "latency": {
            "days": days,
            "p50": [None for _ in days],
            "p90": [None for _ in days],
            "p95": [None for _ in days],
            "stages": {k: [0 for _ in days] for k in STAGE_LABELS},
            "stage_labels": STAGE_LABELS,
            "turns": 0,
            "overall_p50": None,
            "overall_p90": None,
            "overall_p95": None,
        },
        "errors": {"by_type": {label: 0 for label in ERROR_TYPE_LABELS.values()}, "retries": 0, "final": 0},
        "channels": {},
        "confirmations": [],
    }
