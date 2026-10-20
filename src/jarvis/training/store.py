"""WorkoutStore: every SQL statement for `workouts`, `workout_sets` and `health_imports`.

Tables come from migration 2 in observability/db.py. All queries are parameterized. Timestamps are
stored as ISO 8601 with a UTC offset, normalized to the local zone so that the strings sort
chronologically; range queries still re-check the parsed datetimes in Python so that rows stored
across a zone change are never missed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from jarvis.exceptions import ToolExecutionError
from jarvis.training.models import SetInput, SetRow, WorkoutRow
from jarvis.utils.timeutil import local_tz, now_iso, now_local

log = logging.getLogger("jarvis.training")

SOURCES = ("manual", "health")


def to_iso(dt: datetime) -> str:
    """Aware datetime -> 'YYYY-MM-DDTHH:MM:SS+HH:MM' in the local zone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz()).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt


@dataclass
class ImportRow:
    id: int
    file: str
    format: str
    imported_at: str
    workouts_new: int
    workouts_dup: int
    error: str | None


class WorkoutStore:
    def __init__(self, db: Any) -> None:
        self.db = db

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    # --- writes --------------------------------------------------------------------------

    def add_workout(
        self,
        *,
        type: str,
        started_at: datetime,
        ended_at: datetime | None = None,
        duration_s: float | None = None,
        distance_m: float | None = None,
        energy_kcal: float | None = None,
        avg_hr: float | None = None,
        max_hr: float | None = None,
        notes: str | None = None,
        source: str = "manual",
        external_id: str | None = None,
    ) -> int:
        if source not in SOURCES:
            raise ToolExecutionError(f"unknown workout source {source!r}")
        if ended_at is None and duration_s is not None:
            ended_at = started_at + timedelta(seconds=duration_s)
        if duration_s is None and ended_at is not None:
            duration_s = max(0.0, (ended_at - started_at).total_seconds())
        try:
            cur = self.conn.execute(
                "INSERT INTO workouts (source, external_id, started_at, ended_at, type, duration_s, distance_m,"
                " energy_kcal, avg_hr, max_hr, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    external_id,
                    to_iso(started_at),
                    to_iso(ended_at) if ended_at else None,
                    type,
                    duration_s,
                    distance_m,
                    energy_kcal,
                    avg_hr,
                    max_hr,
                    notes,
                    now_iso(),
                ),
            )
        except sqlite3.Error as exc:
            raise ToolExecutionError(f"could not save workout: {type_name(exc)}") from None
        workout_id = int(cur.lastrowid)
        log.info("workout saved id=%s source=%s type=%s", workout_id, source, type)
        return workout_id

    def add_sets(self, workout_id: int, sets: Iterable[SetInput]) -> int:
        rows = [
            (
                workout_id,
                s.exercise,
                s.reps,
                s.distance_m,
                s.time_s,
                s.weight_kg,
                s.rest_s,
                int(s.is_best),
                s.notes,
            )
            for s in sets
        ]
        if not rows:
            return 0
        try:
            self.conn.executemany(
                "INSERT INTO workout_sets (workout_id, exercise, reps, distance_m, time_s, weight_kg, rest_s, is_best, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        except sqlite3.Error as exc:
            raise ToolExecutionError(f"could not save sets: {type_name(exc)}") from None
        return len(rows)

    def delete(self, workout_id: int) -> bool:
        # ON DELETE CASCADE removes the sets (foreign_keys=ON in Database).
        cur = self.conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
        deleted = cur.rowcount > 0
        if deleted:
            log.info("workout deleted id=%s", workout_id)
        return deleted

    def upsert_health(
        self,
        external_id: str,
        *,
        type: str,
        started_at: datetime,
        ended_at: datetime | None = None,
        duration_s: float | None = None,
        distance_m: float | None = None,
        energy_kcal: float | None = None,
        avg_hr: float | None = None,
        max_hr: float | None = None,
        notes: str | None = None,
    ) -> str:
        """Insert a Health workout unless (source='health', external_id) already exists.

        Returns 'new' or 'dup'. A duplicate is left untouched: the first import wins, so a daily
        Shortcut that re-exports the last two days never rewrites anything.
        """
        row = self.conn.execute("SELECT id FROM workouts WHERE source = 'health' AND external_id = ?", (external_id,)).fetchone()
        if row is not None:
            return "dup"
        try:
            self.add_workout(
                type=type,
                started_at=started_at,
                ended_at=ended_at,
                duration_s=duration_s,
                distance_m=distance_m,
                energy_kcal=energy_kcal,
                avg_hr=avg_hr,
                max_hr=max_hr,
                notes=notes,
                source="health",
                external_id=external_id,
            )
        except ToolExecutionError as exc:
            if "IntegrityError" in str(exc):  # raced with another importer: still a duplicate
                return "dup"
            raise
        return "new"

    def record_import(self, file: str, format: str, new: int, dup: int, error: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO health_imports (file, format, imported_at, workouts_new, workouts_dup, error) VALUES (?, ?, ?, ?, ?, ?)",
            (file, format, now_iso(), new, dup, error),
        )
        return int(cur.lastrowid)

    # --- reads ---------------------------------------------------------------------------

    def get(self, workout_id: int) -> WorkoutRow | None:
        row = self.conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        if row is None:
            return None
        workout = _workout_row(row)
        workout.sets = self._sets_for([workout.id]).get(workout.id, [])
        return workout

    def list(
        self,
        start: datetime,
        end: datetime,
        type: str | None = None,
        *,
        limit: int | None = None,
        source: str | None = None,
        with_sets: bool = True,
    ) -> list[WorkoutRow]:
        """Workouts with start <= started_at < end, newest first."""
        # A one-day margin on the string comparison, then an exact check on the parsed datetimes.
        sql = "SELECT * FROM workouts WHERE started_at >= ? AND started_at < ?"
        params: list[Any] = [to_iso(start - timedelta(days=1)), to_iso(end + timedelta(days=1))]
        if type:
            sql += " AND type = ?"
            params.append(type)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY started_at DESC, id DESC"
        rows = [_workout_row(r) for r in self.conn.execute(sql, params).fetchall()]
        rows = [r for r in rows if start <= r.started_at < end]
        if limit is not None:
            rows = rows[:limit]
        if with_sets and rows:
            sets = self._sets_for([r.id for r in rows])
            for r in rows:
                r.sets = sets.get(r.id, [])
        return rows

    def count(self, source: str | None = None) -> int:
        if source:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM workouts WHERE source = ?", (source,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM workouts").fetchone()
        return int(row["n"])

    def last_health_import(self) -> ImportRow | None:
        row = self.conn.execute("SELECT * FROM health_imports WHERE error IS NULL ORDER BY imported_at DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return ImportRow(
            id=row["id"],
            file=row["file"],
            format=row["format"],
            imported_at=row["imported_at"],
            workouts_new=row["workouts_new"],
            workouts_dup=row["workouts_dup"],
            error=row["error"],
        )

    def _sets_for(self, workout_ids: list[int]) -> dict[int, list[SetRow]]:
        out: dict[int, list[SetRow]] = {}
        for chunk_start in range(0, len(workout_ids), 500):
            chunk = workout_ids[chunk_start : chunk_start + 500]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.execute(f"SELECT * FROM workout_sets WHERE workout_id IN ({marks}) ORDER BY id", chunk).fetchall()
            for r in rows:
                out.setdefault(r["workout_id"], []).append(
                    SetRow(
                        id=r["id"],
                        workout_id=r["workout_id"],
                        exercise=r["exercise"],
                        reps=r["reps"],
                        distance_m=r["distance_m"],
                        time_s=r["time_s"],
                        weight_kg=r["weight_kg"],
                        rest_s=r["rest_s"],
                        is_best=bool(r["is_best"]),
                        notes=r["notes"],
                    )
                )
        return out


def _workout_row(r: sqlite3.Row) -> WorkoutRow:
    return WorkoutRow(
        id=r["id"],
        source=r["source"],
        external_id=r["external_id"],
        started_at=from_iso(r["started_at"]),
        ended_at=from_iso(r["ended_at"]) if r["ended_at"] else None,
        type=r["type"],
        duration_s=r["duration_s"],
        distance_m=r["distance_m"],
        energy_kcal=r["energy_kcal"],
        avg_hr=r["avg_hr"],
        max_hr=r["max_hr"],
        notes=r["notes"],
        created_at=r["created_at"],
    )


def type_name(exc: BaseException) -> str:
    return type(exc).__name__


def today_start() -> datetime:
    now = now_local()
    return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
