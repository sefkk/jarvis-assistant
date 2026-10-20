"""Read-only SQLite access for the dashboard's numbers (analytics read the database in
read-only mode; WAL lets this coexist with the agents that write). Parameterized queries only."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def connect_readonly(path: Path) -> sqlite3.Connection | None:
    """A read-only connection to data/jarvis.db, or None when the file does not exist yet."""
    if not Path(path).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@dataclass
class RecentTurn:
    id: str
    started_at: str
    user_input: str
    channel: str | None
    total_ms: float | None
    error: str | None
    tainted: bool


@dataclass
class TodayGlance:
    turns: int = 0
    cost_usd: float = 0.0
    avg_latency_ms: float | None = None
    errors: int = 0
    recent: list[RecentTurn] = field(default_factory=list)
    budget_usd: float = 0.0  # budget.daily_budget_usd; 0 = no budget

    @property
    def over_budget(self) -> bool:
        """Today's cost reached the daily budget: the Overview shows it in red. The budget is informational:
        Jarvis keeps answering."""
        return self.budget_usd > 0 and self.cost_usd >= self.budget_usd


def today_glance(conn: sqlite3.Connection | None, since_iso: str, *, recent_limit: int = 5) -> TodayGlance:
    """Turns, cost, average latency, errors since `since_iso` (local midnight as UTC) and the last turns."""
    glance = TodayGlance()
    if conn is None:
        return glance
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, AVG(total_ms) AS avg_ms, COALESCE(SUM(error IS NOT NULL), 0) AS errors FROM turns WHERE started_at >= ?",
            (since_iso,),
        ).fetchone()
        glance.turns = int(row["n"] or 0)
        glance.avg_latency_ms = float(row["avg_ms"]) if row["avg_ms"] is not None else None
        glance.errors = int(row["errors"] or 0)
        cost = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM api_calls WHERE created_at >= ?", (since_iso,)).fetchone()
        glance.cost_usd = float(cost["c"] or 0.0)
        rows = conn.execute(
            "SELECT t.id, t.started_at, t.user_input, t.total_ms, t.error, t.tainted, s.channel"
            " FROM turns t LEFT JOIN sessions s ON s.id = t.session_id"
            " WHERE t.started_at >= ? ORDER BY t.started_at DESC, t.rowid DESC LIMIT ?",
            (since_iso, int(recent_limit)),
        ).fetchall()
        glance.recent = [
            RecentTurn(
                id=r["id"],
                started_at=r["started_at"],
                user_input=r["user_input"] or "",
                channel=r["channel"],
                total_ms=r["total_ms"],
                error=r["error"],
                tainted=bool(r["tainted"]),
            )
            for r in rows
        ]
    except sqlite3.Error:
        return glance
    return glance


def spent_since(conn: sqlite3.Connection | None, since_iso: str) -> float:
    if conn is None:
        return 0.0
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM api_calls WHERE created_at >= ?", (since_iso,)).fetchone()
    except sqlite3.Error:
        return 0.0
    return float(row["c"] or 0.0)
