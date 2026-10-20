"""History of internet tests in SQLite (`network_tests`): one row per test, and the
`internet_history` summary (averages, worst times, failures) over a day, a week or a month.
The summary stays structured text. # TODO(setup): charts
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from jarvis.network.diagnose import FAILURE_VERDICTS, SLOW, Verdict
from jarvis.utils.timeutil import format_local, now_iso, now_utc

if TYPE_CHECKING:
    from jarvis.network.probes import Diagnostics
    from jarvis.network.speedtest import SpeedResult
    from jarvis.observability.db import Database

RANGES = {"day": 1, "week": 7, "month": 30}
WORST_COUNT = 3
DETAIL_MAX_CHARS = 4000


def iso_utc(dt: datetime) -> str:
    """The same shape as `now_iso()` (UTC, millisecond precision, trailing Z)."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class WorstEntry:
    at: str  # local time
    value: float
    verdict: str


@dataclass
class HistorySummary:
    range: str
    since: datetime
    count: int = 0
    failures: int = 0
    slow: int = 0
    avg_download_mbps: float | None = None
    avg_upload_mbps: float | None = None
    avg_latency_ms: float | None = None
    avg_gateway_latency_ms: float | None = None
    worst_latency: list[WorstEntry] = field(default_factory=list)
    slowest_download: list[WorstEntry] = field(default_factory=list)
    last: dict[str, Any] | None = None

    def format(self) -> str:
        if self.count == 0:
            return f"No internet tests stored in the last {self.range} (run internet_test first)."
        lines = [
            f"Internet history, last {self.range}: {self.count} tests, {self.failures} failures, {self.slow} slow",
            f"averages: download {_fmt(self.avg_download_mbps)} Mbit/s, upload {_fmt(self.avg_upload_mbps)} Mbit/s, "
            f"latency {_fmt(self.avg_latency_ms)} ms, router {_fmt(self.avg_gateway_latency_ms)} ms",
        ]
        if self.worst_latency:
            lines.append("worst latency: " + ", ".join(f"{w.at} {w.value:g} ms ({w.verdict})" for w in self.worst_latency))
        if self.slowest_download:
            lines.append("slowest download: " + ", ".join(f"{w.at} {w.value:g} Mbit/s ({w.verdict})" for w in self.slowest_download))
        if self.last:
            lines.append(
                f"last test: {self.last['at']} {self.last['kind']}, verdict {self.last['verdict']} ({self.last.get('likely_cause') or '-'})"
            )
        return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "?" if value is None else f"{value:g}"


def _avg(values: list[Any]) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    return round(sum(numbers) / len(numbers), 1) if numbers else None


def _local(created_at: str) -> str:
    parsed = parse_iso(created_at)
    return format_local(parsed) if parsed else created_at


class NetworkStore:
    def __init__(self, db: "Database") -> None:
        self.db = db

    def add(self, kind: str, diag: "Diagnostics", speed: "SpeedResult | None", verdict: Verdict, *, now: datetime | None = None) -> int:
        if kind not in ("full", "quick"):
            raise ValueError(f"unknown test kind {kind!r}")
        detail: dict[str, Any] = {"diagnostics": diag.to_dict()}
        if speed is not None:
            detail["speed"] = {"interface": speed.interface, "duration_s": speed.duration_s, "raw": speed.raw}
        detail_json = json.dumps(detail, ensure_ascii=False, separators=(",", ":"), default=str)[:DETAIL_MAX_CHARS]
        cur = self.db.conn.execute(
            "INSERT INTO network_tests (created_at, kind, download_mbps, upload_mbps, responsiveness_rpm, latency_ms,"
            " gateway_latency_ms, gateway_loss_pct, online, verdict, likely_cause, on_home_network, wifi_rssi, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iso_utc(now) if now else now_iso(),
                kind,
                speed.download_mbps if speed else None,
                speed.upload_mbps if speed else None,
                speed.responsiveness_rpm if speed else None,
                speed.latency_ms if speed else None,
                diag.gateway_latency_ms,
                diag.gateway_loss_pct,
                int(diag.online),
                verdict.key,
                verdict.cause,
                None if diag.on_home_network is None else int(diag.on_home_network),
                diag.wifi.rssi if diag.wifi else None,
                detail_json,
            ),
        )
        return int(cur.lastrowid or 0)

    def last(self) -> sqlite3.Row | None:
        return self.db.conn.execute("SELECT * FROM network_tests ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()

    def count(self) -> int:
        row = self.db.conn.execute("SELECT COUNT(*) AS n FROM network_tests").fetchone()
        return int(row["n"]) if row else 0

    def history(self, range: str = "week", *, now: datetime | None = None) -> HistorySummary:
        if range not in RANGES:
            raise ValueError(f"range must be one of {', '.join(RANGES)}")
        now = now or now_utc()
        since = now - timedelta(days=RANGES[range])
        rows = self.db.conn.execute(
            "SELECT * FROM network_tests WHERE created_at >= ? AND created_at <= ? ORDER BY created_at",
            (iso_utc(since), iso_utc(now)),
        ).fetchall()
        summary = HistorySummary(range=range, since=since, count=len(rows))
        if not rows:
            return summary
        summary.failures = sum(1 for r in rows if not r["online"] or r["verdict"] in FAILURE_VERDICTS)
        summary.slow = sum(1 for r in rows if r["verdict"] == SLOW)
        summary.avg_download_mbps = _avg([r["download_mbps"] for r in rows])
        summary.avg_upload_mbps = _avg([r["upload_mbps"] for r in rows])
        summary.avg_latency_ms = _avg([r["latency_ms"] for r in rows])
        summary.avg_gateway_latency_ms = _avg([r["gateway_latency_ms"] for r in rows])
        # Worst times: the highest latency (speed test RTT, else the router's) and the slowest downloads.
        with_latency = [(r["latency_ms"] if r["latency_ms"] is not None else r["gateway_latency_ms"], r) for r in rows]
        with_latency = [(v, r) for v, r in with_latency if v is not None]
        with_latency.sort(key=lambda item: -item[0])
        summary.worst_latency = [WorstEntry(_local(r["created_at"]), float(v), r["verdict"]) for v, r in with_latency[:WORST_COUNT]]
        with_download = sorted(((r["download_mbps"], r) for r in rows if r["download_mbps"] is not None), key=lambda item: item[0])
        summary.slowest_download = [WorstEntry(_local(r["created_at"]), float(v), r["verdict"]) for v, r in with_download[:WORST_COUNT]]
        last = rows[-1]
        summary.last = {
            "at": _local(last["created_at"]),
            "kind": last["kind"],
            "verdict": last["verdict"],
            "likely_cause": last["likely_cause"],
        }
        return summary
