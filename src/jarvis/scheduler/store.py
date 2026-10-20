"""Timer persistence: the `timers` table from migration 2, parameterized SQL only.

`fire_at` is stored as ISO 8601 with the local offset. Rows are small and few, so due-ness is
decided in Python on parsed datetimes rather than by string comparison (offsets can differ
across DST changes, which would make lexical ordering wrong).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jarvis.utils.timeutil import local_tz, now_iso, now_local

if TYPE_CHECKING:
    from jarvis.observability.db import Database

KIND_TIMER = "timer"
KIND_POMODORO = "pomodoro"
# Mac power (tools/power.py): the sleep itself and the one-minute notice before it. Both are fired
# by the scheduler; `sleep` runs `pmset sleepnow` instead of only notifying.
KIND_SLEEP = "sleep"
KIND_SLEEP_NOTICE = "sleep_notice"
# Alarms (scheduler/alarm.py, tools/alarms.py): the row's `pomodoro_state_json` holds an `AlarmState`
# (clock time, repeat days, snooze origin, the phone-backup reminder id). The scheduler rings instead
# of notifying.
KIND_ALARM = "alarm"
KINDS = (KIND_TIMER, KIND_POMODORO, KIND_SLEEP, KIND_SLEEP_NOTICE, KIND_ALARM)


def iso_local(dt: datetime) -> str:
    """Store times with an explicit offset in the local zone (naive input is taken as local)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz()).isoformat(timespec="seconds")


@dataclass
class TimerRow:
    id: int
    label: str
    fire_at: datetime  # aware
    kind: str
    pomodoro_state_json: str | None
    status: str
    created_at: str
    fired_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TimerRow":
        return cls(
            id=int(row["id"]),
            label=row["label"],
            fire_at=datetime.fromisoformat(row["fire_at"]),
            kind=row["kind"],
            pomodoro_state_json=row["pomodoro_state_json"],
            status=row["status"],
            created_at=row["created_at"],
            fired_at=row["fired_at"],
        )


class TimerStore:
    def __init__(self, db: "Database") -> None:
        self.db = db

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    # --- writes --------------------------------------------------------------------------

    def add(self, label: str, fire_at: datetime, kind: str = KIND_TIMER, pomodoro_state: Any = None) -> int:
        """Insert a pending timer and return its id. `pomodoro_state` is a JSON string or an
        object with `model_dump_json()` (the engine's PomodoroState)."""
        if kind not in KINDS:
            raise ValueError(f"unknown timer kind {kind!r}")
        state_json: str | None
        if pomodoro_state is None or isinstance(pomodoro_state, str):
            state_json = pomodoro_state
        else:
            state_json = pomodoro_state.model_dump_json()
        cur = self.conn.execute(
            "INSERT INTO timers (label, fire_at, kind, pomodoro_state_json, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (label, iso_local(fire_at), kind, state_json, now_iso()),
        )
        return int(cur.lastrowid)

    def mark_fired(self, timer_id: int, fired_at: datetime | None = None) -> bool:
        """Atomically move a pending timer to fired. Returns False when another process got
        there first (or the timer was cancelled), so callers can skip the notification."""
        stamp = iso_local(fired_at or now_local())
        cur = self.conn.execute(
            "UPDATE timers SET status = 'fired', fired_at = ? WHERE id = ? AND status = 'pending'",
            (stamp, timer_id),
        )
        return cur.rowcount == 1

    def cancel(self, timer_id: int) -> bool:
        cur = self.conn.execute("UPDATE timers SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (timer_id,))
        return cur.rowcount == 1

    def cancel_by_label(self, label: str) -> int:
        """Cancel every pending timer whose label matches (case-insensitive, Unicode-aware:
        SQLite's lower() only folds ASCII, so the comparison happens in Python). Returns the count."""
        needle = label.strip().casefold()
        ids = [t.id for t in self.pending() if t.label.strip().casefold() == needle]
        return sum(1 for timer_id in ids if self.cancel(timer_id))

    def cancel_by_kind(self, *kinds: str) -> int:
        """Cancel every pending row of the given kinds (the scheduled sleep and its notice)."""
        if not kinds:
            return 0
        marks = ", ".join("?" for _ in kinds)
        cur = self.conn.execute(f"UPDATE timers SET status = 'cancelled' WHERE status = 'pending' AND kind IN ({marks})", tuple(kinds))
        return int(cur.rowcount)

    def cancel_pomodoro(self) -> int:
        """Cancel every pending pomodoro phase (there is normally at most one)."""
        cur = self.conn.execute("UPDATE timers SET status = 'cancelled' WHERE status = 'pending' AND kind = ?", (KIND_POMODORO,))
        return int(cur.rowcount)

    # --- reads ---------------------------------------------------------------------------

    def get(self, timer_id: int) -> TimerRow | None:
        row = self.conn.execute("SELECT * FROM timers WHERE id = ?", (timer_id,)).fetchone()
        return TimerRow.from_row(row) if row else None

    def pending(self, kind: str | None = None) -> list[TimerRow]:
        """Pending rows, earliest first; only those of `kind` when given."""
        rows = self.conn.execute("SELECT * FROM timers WHERE status = 'pending'").fetchall()
        items = (TimerRow.from_row(r) for r in rows)
        return sorted((t for t in items if kind is None or t.kind == kind), key=lambda t: (t.fire_at, t.id))

    def due(self, now: datetime | None = None) -> list[TimerRow]:
        """Pending timers whose fire_at is at or before `now`, earliest first."""
        now = now or now_local()
        if now.tzinfo is None:
            now = now.replace(tzinfo=local_tz())
        return [t for t in self.pending() if t.fire_at <= now]

    def active_pomodoro(self) -> TimerRow | None:
        """The pending pomodoro phase, if a pomodoro is running."""
        for timer in self.pending():
            if timer.kind == KIND_POMODORO:
                return timer
        return None

    def pending_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM timers WHERE status = 'pending'").fetchone()
        return int(row["n"])
