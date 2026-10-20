"""Aggregation for training_summary: pure, deterministic functions.

`summarize` returns a dict (kept structured so charts can be built from it later, see
`# TODO(setup): charts` in tools/training.py); `format_summary` turns it into the compact text the
model explains to the user.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from jarvis.training.models import WorkoutRow
from jarvis.utils.timeutil import format_local, local_tz

TREND_METRICS = ("sessions", "duration_s", "distance_m", "energy_kcal", "avg_hr")


# --- aggregation ---------------------------------------------------------------------------


def aggregate(rows: list[WorkoutRow], start: datetime, end: datetime) -> dict[str, Any]:
    """Totals for one period. `rows` must already be inside [start, end)."""
    by_type: dict[str, int] = {}
    duration = 0.0
    distance = 0.0
    energy = 0.0
    avg_hrs: list[float] = []
    max_hr: float | None = None
    sets_total = 0
    reps_total = 0
    volume_kg = 0.0
    best: dict[float, dict[str, Any]] = {}
    per_week: dict[str, int] = {w: 0 for w in _week_starts(start, end)}

    for row in rows:
        by_type[row.type] = by_type.get(row.type, 0) + 1
        duration += row.duration_s or 0.0
        distance += row.distance_m or 0.0
        energy += row.energy_kcal or 0.0
        if row.avg_hr is not None:
            avg_hrs.append(float(row.avg_hr))
        if row.max_hr is not None:
            max_hr = float(row.max_hr) if max_hr is None else max(max_hr, float(row.max_hr))
        week = _week_start(row.started_at)
        per_week[week] = per_week.get(week, 0) + 1
        for s in row.sets:
            sets_total += 1
            if s.reps:
                reps_total += int(s.reps)
                if s.weight_kg:
                    volume_kg += float(s.reps) * float(s.weight_kg)
            if s.time_s is not None and s.distance_m:
                d = float(s.distance_m)
                current = best.get(d)
                if current is None or float(s.time_s) < current["time_s"]:
                    best[d] = {
                        "time_s": float(s.time_s),
                        "exercise": s.exercise,
                        "workout_id": row.id,
                        "date": row.started_at.astimezone(local_tz()).strftime("%Y-%m-%d"),
                    }

    return {
        "sessions": len(rows),
        "sessions_by_type": dict(sorted(by_type.items())),
        "duration_s": round(duration, 1),
        "distance_m": round(distance, 1),
        "energy_kcal": round(energy, 1),
        "avg_hr": round(sum(avg_hrs) / len(avg_hrs), 1) if avg_hrs else None,
        "max_hr": max_hr,
        "sets": sets_total,
        "reps": reps_total,
        "volume_kg": round(volume_kg, 1),
        "best_times": {k: best[k] for k in sorted(best)},
        "per_week": dict(sorted(per_week.items())),
    }


def trend(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Deltas current - previous for the headline metrics, with a percentage where previous > 0."""
    out: dict[str, dict[str, float | None]] = {}
    for key in TREND_METRICS:
        cur = current.get(key)
        prev = previous.get(key)
        if cur is None or prev is None:
            out[key] = {"delta": None, "pct": None}
            continue
        delta = round(float(cur) - float(prev), 1)
        pct = round(100.0 * delta / float(prev), 1) if float(prev) > 0 else None
        out[key] = {"delta": delta, "pct": pct}
    return out


def summarize(store: Any, start: datetime, end: datetime, type: str | None = None) -> dict[str, Any]:
    """Aggregate [start, end) and the previous period of equal length, with deltas."""
    if end <= start:
        raise ValueError("end must be after start")
    length = end - start
    prev_start, prev_end = start - length, start
    current_rows = store.list(start, end, type)
    previous_rows = store.list(prev_start, prev_end, type)
    current = aggregate(current_rows, start, end)
    previous = aggregate(previous_rows, prev_start, prev_end)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": round(length.total_seconds() / 86400, 2)},
        "previous_range": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
        "type": type,
        "current": current,
        "previous": previous,
        "trend": trend(current, previous),
    }


# --- formatting ----------------------------------------------------------------------------


def format_summary(data: dict[str, Any]) -> str:
    cur = data["current"]
    prev = data["previous"]
    rng = data["range"]
    start = datetime.fromisoformat(rng["start"])
    end = datetime.fromisoformat(rng["end"])
    lines = [
        f"period: {_day(start)} .. {_day(end - timedelta(seconds=1))} ({_days(rng['days'])}), type: {data.get('type') or 'all'}",
    ]
    if cur["sessions"] == 0:
        lines.append("sessions: 0 (no workouts in this period)")
    else:
        by_type = ", ".join(f"{t} {n}" for t, n in cur["sessions_by_type"].items())
        lines.append(f"sessions: {cur['sessions']} ({by_type})")
        lines.append(f"duration: {fmt_duration(cur['duration_s'])}")
        lines.append(f"distance: {fmt_distance(cur['distance_m'])}")
        lines.append(f"energy: {cur['energy_kcal']:.0f} kcal" if cur["energy_kcal"] else "energy: n/a")
        hr = []
        if cur["avg_hr"] is not None:
            hr.append(f"avg {cur['avg_hr']:.0f} bpm")
        if cur["max_hr"] is not None:
            hr.append(f"max {cur['max_hr']:.0f} bpm")
        lines.append("heart rate: " + (", ".join(hr) if hr else "n/a"))
        if cur["sets"]:
            vol = f", volume {cur['volume_kg']:.0f} kg" if cur["volume_kg"] else ""
            lines.append(f"sets: {cur['sets']} (reps {cur['reps']}{vol})")
        if cur["best_times"]:
            bests = ", ".join(f"{fmt_distance(d)} {b['time_s']:g} s ({b['date']})" for d, b in cur["best_times"].items())
            lines.append(f"best times: {bests}")
        lines.append("per week: " + ", ".join(f"{w}: {n}" for w, n in cur["per_week"].items()))
    prev_start = datetime.fromisoformat(data["previous_range"]["start"])
    prev_end = datetime.fromisoformat(data["previous_range"]["end"])
    lines.append(
        f"previous period: {_day(prev_start)} .. {_day(prev_end - timedelta(seconds=1))}: "
        f"sessions {prev['sessions']}, duration {fmt_duration(prev['duration_s'])}, "
        f"distance {fmt_distance(prev['distance_m'])}"
    )
    lines.append("trend: " + _trend_line(data["trend"]))
    return "\n".join(lines)


def format_workout_line(row: WorkoutRow) -> str:
    """One line per workout for list_workouts and the log_training echo."""
    parts = [f"#{row.id} {format_local(row.started_at)}", row.type]
    if row.duration_s:
        parts.append(fmt_duration(row.duration_s))
    if row.distance_m:
        parts.append(fmt_distance(row.distance_m))
    if row.energy_kcal:
        parts.append(f"{row.energy_kcal:.0f} kcal")
    if row.avg_hr:
        parts.append(f"avg hr {row.avg_hr:.0f}")
    if row.sets:
        parts.append(describe_sets(row.sets))
    if row.source == "health":
        parts.append("[health]")
    if row.notes:
        parts.append(f"notes: {row.notes[:120]}")
    return " | ".join(parts)


def describe_sets(sets: list[Any]) -> str:
    """'6x60 m (best 7.1 s)' or '3x10 squat 60 kg' style, grouped by exercise."""
    groups: dict[str, list[Any]] = {}
    for s in sets:
        groups.setdefault(s.exercise, []).append(s)
    out = []
    for exercise, items in groups.items():
        n = len(items)
        sample = items[0]
        desc = f"{n}x"
        if sample.reps and all(i.reps == sample.reps for i in items):
            desc += f"{sample.reps} "
        desc += exercise
        if sample.distance_m and all(i.distance_m == sample.distance_m for i in items):
            desc += f" {fmt_distance(sample.distance_m)}"
        if sample.weight_kg and all(i.weight_kg == sample.weight_kg for i in items):
            desc += f" {sample.weight_kg:g} kg"
        timed = [i.time_s for i in items if i.time_s is not None]
        if timed:
            best = min(timed)
            desc += f" (best {best:g} s)" if len(timed) < n or n > 1 else f" ({best:g} s)"
        out.append(desc)
    return "; ".join(out)


# --- small helpers -------------------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "0 min"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m} min" if not s else f"{m} min {s} s"
    return f"{s} s"


def fmt_distance(metres: float | None) -> str:
    if not metres:
        return "0 m"
    if metres >= 1000:
        return f"{metres / 1000:.2f} km".replace(".00 km", " km")
    return f"{metres:g} m"


def _day(dt: datetime) -> str:
    return dt.astimezone(local_tz()).strftime("%Y-%m-%d")


def _days(days: float) -> str:
    return f"{int(days)} days" if float(days).is_integer() else f"{days} days"


def _week_start(dt: datetime) -> str:
    local = dt.astimezone(local_tz())
    monday = local - timedelta(days=local.weekday())
    return monday.strftime("%Y-%m-%d")


def _week_starts(start: datetime, end: datetime) -> list[str]:
    """Monday of every ISO week that overlaps [start, end), capped to avoid huge ranges."""
    weeks: list[str] = []
    cursor = start.astimezone(local_tz())
    cursor = datetime(cursor.year, cursor.month, cursor.day, tzinfo=cursor.tzinfo) - timedelta(days=cursor.weekday())
    while cursor < end and len(weeks) < 260:
        weeks.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=7)
    return weeks


def _trend_line(t: dict[str, dict[str, float | None]]) -> str:
    labels = {
        "sessions": ("sessions", lambda v: f"{v:+.0f}"),
        "duration_s": ("duration", lambda v: ("+" if v >= 0 else "-") + fmt_duration(abs(v))),
        "distance_m": ("distance", lambda v: ("+" if v >= 0 else "-") + fmt_distance(abs(v))),
        "energy_kcal": ("energy", lambda v: f"{v:+.0f} kcal"),
        "avg_hr": ("avg hr", lambda v: f"{v:+.0f} bpm"),
    }
    parts = []
    for key, (label, fmt) in labels.items():
        entry = t.get(key, {})
        delta = entry.get("delta")
        if delta is None:
            continue
        pct = entry.get("pct")
        text = f"{label} {fmt(delta)}"
        if pct is not None:
            text += f" ({pct:+.0f}%)"
        parts.append(text)
    return ", ".join(parts) if parts else "no data for comparison"
