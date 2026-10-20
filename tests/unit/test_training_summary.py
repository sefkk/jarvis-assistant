"""training_summary aggregation math: pure functions on WorkoutRow lists, no database."""

from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.training.models import SetRow, WorkoutRow
from jarvis.training.summary import (
    aggregate,
    describe_sets,
    fmt_distance,
    fmt_duration,
    format_summary,
    format_workout_line,
    summarize,
    trend,
)
from jarvis.utils.timeutil import local_tz

TZ = local_tz()


def dt(y: int, m: int, d: int, h: int = 10) -> datetime:
    return datetime(y, m, d, h, tzinfo=TZ)


_ids = iter(range(1, 10_000))


def workout(
    started: datetime,
    type: str = "running",
    *,
    duration_s=None,
    distance_m=None,
    energy=None,
    avg_hr=None,
    max_hr=None,
    sets=None,
    source="manual",
    notes=None,
) -> WorkoutRow:
    wid = next(_ids)
    rows = []
    for i, s in enumerate(sets or []):
        rows.append(
            SetRow(
                id=i + 1,
                workout_id=wid,
                exercise=s.get("exercise", "sprint"),
                reps=s.get("reps"),
                distance_m=s.get("distance_m"),
                time_s=s.get("time_s"),
                weight_kg=s.get("weight_kg"),
                rest_s=None,
                is_best=s.get("is_best", False),
                notes=None,
            )
        )
    return WorkoutRow(
        id=wid,
        source=source,
        external_id=None,
        started_at=started,
        ended_at=None,
        type=type,
        duration_s=duration_s,
        distance_m=distance_m,
        energy_kcal=energy,
        avg_hr=avg_hr,
        max_hr=max_hr,
        notes=notes,
        created_at="",
        sets=rows,
    )


class FakeStore:
    def __init__(self, rows: list[WorkoutRow]) -> None:
        self.rows = rows

    def list(self, start, end, type=None):
        return [r for r in self.rows if start <= r.started_at < end and (type is None or r.type == type)]


# 2026-10-05 is a Monday.
START, END = dt(2026, 10, 5, 0), dt(2026, 10, 19, 0)


def sample_rows() -> list[WorkoutRow]:
    return [
        workout(dt(2026, 10, 6), "running", duration_s=1500, distance_m=5000, energy=400, avg_hr=150, max_hr=175),
        workout(
            dt(2026, 10, 8),
            "sprint",
            duration_s=3600,
            sets=[
                {"distance_m": 60},
                {"distance_m": 60},
                {"distance_m": 60, "time_s": 7.3},
                {"distance_m": 60, "time_s": 7.1, "is_best": True},
                {"distance_m": 100, "time_s": 12.4},
            ],
        ),
        workout(
            dt(2026, 10, 13),
            "strength",
            duration_s=2700,
            energy=300,
            avg_hr=120,
            max_hr=160,
            sets=[
                {"exercise": "squat", "reps": 10, "weight_kg": 60},
                {"exercise": "squat", "reps": 10, "weight_kg": 60},
                {"exercise": "squat", "reps": 8, "weight_kg": 70},
            ],
        ),
        workout(dt(2026, 10, 15), "running", duration_s=3000, distance_m=10000, energy=800, avg_hr=156, max_hr=182),
        workout(dt(2026, 10, 16), "sprint", sets=[{"distance_m": 60, "time_s": 7.0}, {"distance_m": 60, "time_s": 7.4}]),
    ]


def test_aggregate_totals_and_best_times():
    cur = aggregate(sample_rows(), START, END)
    assert cur["sessions"] == 5
    assert cur["sessions_by_type"] == {"running": 2, "sprint": 2, "strength": 1}
    assert cur["duration_s"] == 1500 + 3600 + 2700 + 3000
    assert cur["distance_m"] == 15000 and cur["energy_kcal"] == 1500
    assert cur["avg_hr"] == pytest.approx((150 + 120 + 156) / 3, abs=0.05) and cur["max_hr"] == 182
    assert cur["sets"] == 10 and cur["reps"] == 28 and cur["volume_kg"] == 10 * 60 * 2 + 8 * 70
    best = cur["best_times"]
    assert list(best) == [60.0, 100.0]  # sorted by distance
    assert best[60.0]["time_s"] == 7.0 and best[60.0]["date"] == "2026-10-16"  # min across all sets, not is_best
    assert best[100.0]["time_s"] == 12.4 and best[100.0]["workout_id"] == 2
    assert cur["per_week"] == {"2026-10-05": 2, "2026-10-12": 3}


def test_aggregate_empty_period_has_zero_week_buckets():
    cur = aggregate([], START, END)
    assert cur["sessions"] == 0 and cur["duration_s"] == 0 and cur["avg_hr"] is None and cur["max_hr"] is None
    assert cur["best_times"] == {} and cur["per_week"] == {"2026-10-05": 0, "2026-10-12": 0}
    assert cur["sessions_by_type"] == {}


def test_aggregate_ignores_sets_without_distance_or_time():
    rows = [workout(dt(2026, 10, 6), "sprint", sets=[{"distance_m": 60}, {"time_s": 9.9}, {"distance_m": 0, "time_s": 1}])]
    assert aggregate(rows, START, END)["best_times"] == {}


def test_aggregate_is_deterministic():
    rows = sample_rows()
    assert aggregate(rows, START, END) == aggregate(list(reversed(rows)), START, END)


def test_trend_deltas_and_percentages():
    cur = {"sessions": 5, "duration_s": 3000.0, "distance_m": 0.0, "energy_kcal": 100.0, "avg_hr": None}
    prev = {"sessions": 4, "duration_s": 4000.0, "distance_m": 0.0, "energy_kcal": 0.0, "avg_hr": 140.0}
    t = trend(cur, prev)
    assert t["sessions"] == {"delta": 1.0, "pct": 25.0}
    assert t["duration_s"] == {"delta": -1000.0, "pct": -25.0}
    assert t["distance_m"] == {"delta": 0.0, "pct": None}  # previous 0: no percentage
    assert t["energy_kcal"] == {"delta": 100.0, "pct": None}
    assert t["avg_hr"] == {"delta": None, "pct": None}


def test_summarize_compares_with_previous_period_of_equal_length():
    rows = sample_rows() + [
        workout(dt(2026, 9, 25), "running", duration_s=1800, distance_m=6000, energy=500, avg_hr=140),
        workout(dt(2026, 9, 30), "strength", duration_s=1800),
        workout(dt(2026, 9, 10), "running", distance_m=99999),  # before the previous period
    ]
    data = summarize(FakeStore(rows), START, END)
    assert data["range"]["days"] == 14 and data["previous_range"]["start"] == dt(2026, 9, 21, 0).isoformat()
    assert data["current"]["sessions"] == 5 and data["previous"]["sessions"] == 2
    assert data["previous"]["distance_m"] == 6000 and data["trend"]["distance_m"] == {"delta": 9000.0, "pct": 150.0}
    assert data["trend"]["sessions"] == {"delta": 3.0, "pct": 150.0}
    only_running = summarize(FakeStore(rows), START, END, "running")
    assert only_running["current"]["sessions"] == 2 and only_running["previous"]["sessions"] == 1
    assert only_running["type"] == "running"
    with pytest.raises(ValueError):
        summarize(FakeStore(rows), END, START)


def test_format_summary_lines():
    rows = sample_rows() + [workout(dt(2026, 9, 25), "running", duration_s=1800, distance_m=6000, energy=500)]
    text = format_summary(summarize(FakeStore(rows), START, END))
    lines = text.splitlines()
    assert lines[0] == "period: 2026-10-05 .. 2026-10-18 (14 days), type: all"
    assert lines[1] == "sessions: 5 (running 2, sprint 2, strength 1)"
    assert "duration: 3h 00m" in lines and "distance: 15 km" in lines and "energy: 1500 kcal" in lines
    assert "heart rate: avg 142 bpm, max 182 bpm" in lines
    assert "sets: 10 (reps 28, volume 1760 kg)" in lines
    assert "best times: 60 m 7 s (2026-10-16), 100 m 12.4 s (2026-10-08)" in lines
    assert "per week: 2026-10-05: 2, 2026-10-12: 3" in lines
    assert "previous period: 2026-09-21 .. 2026-10-04: sessions 1, duration 30 min, distance 6 km" in lines
    assert lines[-1].startswith("trend: sessions +4 (+400%), duration +2h 30m (+500%), distance +9 km (+150%), energy +1000 kcal (+200%)")


def test_format_summary_empty_period():
    text = format_summary(summarize(FakeStore([]), START, END, "running"))
    lines = text.splitlines()
    assert lines[0].endswith("type: running") and lines[1] == "sessions: 0 (no workouts in this period)"
    assert lines[-1] == "trend: sessions +0, duration +0 min, distance +0 m, energy +0 kcal"
    assert "best times" not in text


def test_format_helpers():
    assert fmt_duration(None) == "0 min" and fmt_duration(59) == "59 s" and fmt_duration(1500) == "25 min"
    assert fmt_duration(1530) == "25 min 30 s" and fmt_duration(3600) == "1h 00m" and fmt_duration(5400) == "1h 30m"
    assert fmt_distance(60) == "60 m" and fmt_distance(5000) == "5 km" and fmt_distance(5230) == "5.23 km"
    sets = sample_rows()[1].sets
    assert describe_sets(sets) == "5xsprint (best 7.1 s)"  # mixed distances: no single distance shown
    assert describe_sets(sample_rows()[2].sets) == "3xsquat"  # mixed reps/weights: exercise only
    single = sample_rows()[4].sets[:1]
    assert describe_sets(single) == "1xsprint 60 m (7 s)"
    row = workout(
        dt(2026, 10, 6, 7),
        "running",
        duration_s=1500,
        distance_m=5000,
        energy=400,
        avg_hr=150,
        source="health",
        notes="source: Apple Watch",
    )
    line = format_workout_line(row)
    assert line.startswith(f"#{row.id} 2026-10-06 07:00 | running | 25 min | 5 km | 400 kcal | avg hr 150 | [health]")
    assert line.endswith("| notes: source: Apple Watch")
