"""Training log: WorkoutStore CRUD and dedup, the four tools through parse_args/run, range parsing."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.tools.base import RiskLevel
from jarvis.tools.training import DeleteWorkout, ListWorkouts, LogTraining, TrainingSummary, health_checks, resolve_range
from jarvis.training.models import SetInput, WorkoutInput, normalize_type
from jarvis.training.store import WorkoutStore, from_iso, to_iso
from jarvis.utils.timeutil import local_tz, now_local

TZ = local_tz()


def dt(y: int, m: int, d: int, h: int = 10, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=TZ)


# --- store ---------------------------------------------------------------------------------


def test_store_add_get_delete(db):
    store = WorkoutStore(db)
    wid = store.add_workout(type="running", started_at=dt(2026, 10, 10), duration_s=1500, distance_m=5000, notes="easy")
    assert store.add_sets(wid, [SetInput(exercise="stride", distance_m=100, time_s=15.2, is_best=True)]) == 1
    row = store.get(wid)
    assert row is not None and row.id == wid and row.source == "manual" and row.type == "running"
    assert row.started_at == dt(2026, 10, 10) and row.ended_at == dt(2026, 10, 10) + timedelta(seconds=1500)
    assert row.duration_s == 1500 and row.distance_m == 5000 and row.notes == "easy"
    assert len(row.sets) == 1 and row.sets[0].is_best and row.sets[0].time_s == 15.2
    raw = db.conn.execute("SELECT started_at FROM workouts WHERE id = ?", (wid,)).fetchone()["started_at"]
    assert raw == to_iso(dt(2026, 10, 10)) and from_iso(raw) == dt(2026, 10, 10)
    assert "+" in raw or "-" in raw[10:]  # ISO 8601 with an offset
    assert store.delete(wid) is True
    assert store.get(wid) is None
    assert db.conn.execute("SELECT COUNT(*) AS n FROM workout_sets").fetchone()["n"] == 0  # cascade
    assert store.delete(wid) is False


def test_store_list_range_type_and_limit(db):
    store = WorkoutStore(db)
    a = store.add_workout(type="running", started_at=dt(2026, 10, 1))
    b = store.add_workout(type="strength", started_at=dt(2026, 10, 3))
    c = store.add_workout(type="running", started_at=dt(2026, 10, 5))
    store.add_workout(type="running", started_at=dt(2026, 10, 8))  # outside
    rows = store.list(dt(2026, 10, 1, 0), dt(2026, 10, 6, 0))
    assert [r.id for r in rows] == [c, b, a]  # newest first
    assert [r.id for r in store.list(dt(2026, 10, 1, 0), dt(2026, 10, 6, 0), "running")] == [c, a]
    assert [r.id for r in store.list(dt(2026, 10, 1, 0), dt(2026, 10, 6, 0), limit=1)] == [c]
    # the end is exclusive and the margin on the SQL string comparison does not leak rows
    assert [r.id for r in store.list(dt(2026, 10, 1, 0), dt(2026, 10, 5, 10))] == [b, a]
    assert store.count() == 4 and store.count("manual") == 4 and store.count("health") == 0


def test_upsert_health_dedups_on_external_id(db):
    store = WorkoutStore(db)
    assert store.upsert_health("abc", type="running", started_at=dt(2026, 10, 10), distance_m=5000) == "new"
    assert store.upsert_health("abc", type="running", started_at=dt(2026, 10, 10), distance_m=9999) == "dup"
    assert store.upsert_health("xyz", type="running", started_at=dt(2026, 10, 10)) == "new"
    assert store.count("health") == 2
    row = store.list(dt(2026, 10, 9), dt(2026, 10, 11), source="health")
    assert {r.external_id for r in row} == {"abc", "xyz"}
    assert next(r for r in row if r.external_id == "abc").distance_m == 5000  # first import wins
    # a manual workout may carry the same external id: the unique index is per source
    store.add_workout(type="running", started_at=dt(2026, 10, 10), source="manual", external_id="abc")
    assert store.count() == 3


def test_record_import_and_last_health_import(db):
    store = WorkoutStore(db)
    assert store.last_health_import() is None
    store.record_import("a.json", "shortcut", 2, 0)
    store.record_import("bad.json", "unknown", 0, 0, "cannot read JSON")
    store.record_import("b.json", "hae_v2", 1, 3)
    last = store.last_health_import()
    assert last is not None and last.file == "b.json" and last.workouts_new == 1 and last.workouts_dup == 3
    assert last.error is None


def test_add_workout_rejects_unknown_source(db):
    with pytest.raises(ToolExecutionError):
        WorkoutStore(db).add_workout(type="running", started_at=dt(2026, 10, 10), source="telegram")


# --- models ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Koşu", "running"),
        ("yürüyüş", "walking"),
        ("Bisiklet", "cycling"),
        ("ağırlık", "strength"),
        ("kuvvet", "strength"),
        ("HIIT", "hiit"),
        ("Running", "running"),
        ("Traditional Strength Training", "strength"),
        ("  Outdoor   Run ", "running"),
        ("kayak", "kayak"),
        ("Tabata​", "tabata"),
    ],
)
def test_normalize_type_aliases(text, expected):
    assert normalize_type(text) == expected


def test_workout_input_normalizes_and_rejects():
    w = WorkoutInput(type=" KOŞU ", sets=[{"exercise": " Squat ", "reps": 10}], notes="felt\x00 good")
    assert w.type == "running" and w.sets[0].exercise == "squat" and w.notes == "felt good"
    with pytest.raises(ValueError):
        WorkoutInput(type="   ")
    with pytest.raises(ValueError):
        WorkoutInput(type="running", sets=[{"exercise": "squat", "reps": -1}])
    with pytest.raises(ValueError):
        WorkoutInput(type="running", bogus=1)


# --- tools ------------------------------------------------------------------------------------


def test_tool_flags():
    assert LogTraining.risk == RiskLevel.SAFE and LogTraining.confirm_when_tainted and not LogTraining.taints
    assert LogTraining.delegate_ok is False
    assert TrainingSummary.risk == RiskLevel.SAFE and TrainingSummary.delegate_ok is True
    assert ListWorkouts.risk == RiskLevel.SAFE and ListWorkouts.delegate_ok is True
    assert DeleteWorkout.risk == RiskLevel.CONFIRM and DeleteWorkout.delegate_ok is False
    schema = TrainingSummary().api_schema()["input_schema"]
    assert "from" in schema["properties"] and "from_" not in schema["properties"]
    assert LogTraining().api_schema()["input_schema"]["properties"]["sets"]["items"]["$ref"].endswith("SetInput")


def test_tool_without_database_fails_cleanly():
    tool = LogTraining()
    with pytest.raises(ToolExecutionError, match="database"):
        tool.run(tool.parse_args({"type": "running"}))


def test_log_training_sprints_best_only_on_one_set(db):
    """'Bugün 6x60m yaptım, en iyisi 7.1' as the model should produce it."""
    tool = LogTraining(db=db)
    sets = [{"exercise": "sprint", "distance_m": 60} for _ in range(5)]
    sets.append({"exercise": "sprint", "distance_m": 60, "time_s": 7.1, "is_best": True})
    args = tool.parse_args({"type": "sprint", "sets": sets, "notes": "en iyisi 7.1"})
    result = tool.run(args)
    assert not result.is_error and result.content.startswith("Saved workout #1 ")
    assert "6xsprint 60 m (best 7.1 s)" in result.content
    row = WorkoutStore(db).get(1)
    assert row is not None and row.type == "sprint" and len(row.sets) == 6
    assert [s.time_s for s in row.sets].count(None) == 5 and sum(1 for s in row.sets if s.is_best) == 1
    assert row.distance_m == 360  # six reps of 60 m
    assert abs((row.started_at - now_local()).total_seconds()) < 5


def test_log_training_gym_sets(db):
    tool = LogTraining(db=db)
    args = tool.parse_args(
        {
            "date": "2026-10-13 18:30",
            "type": "ağırlık",
            "sets": [{"exercise": "squat", "reps": 10, "weight_kg": 60}] * 3
            + [{"exercise": "bench press", "reps": 8, "weight_kg": 50, "rest_s": 90}] * 2,
            "duration_min": 50,
        }
    )
    result = tool.run(args)
    assert "3x10 squat 60 kg; 2x8 bench press 50 kg" in result.content
    row = WorkoutStore(db).get(1)
    assert row.type == "strength" and row.started_at == dt(2026, 10, 13, 18, 30) and row.duration_s == 3000
    assert row.distance_m is None and row.sets[3].rest_s == 90


def test_log_training_run_without_sets(db):
    tool = LogTraining(db=db)
    result = tool.run(tool.parse_args({"date": "2026-10-12", "type": "koşu", "distance_m": 5000, "duration_min": 25}))
    assert "running" in result.content and "25 min" in result.content and "5 km" in result.content
    row = WorkoutStore(db).get(1)
    assert row.type == "running" and row.distance_m == 5000 and row.duration_s == 1500 and row.sets == []
    assert row.started_at.date() == datetime(2026, 10, 12).date()
    assert (row.started_at.hour, row.started_at.minute) == (now_local().hour, now_local().minute)


def test_log_training_bad_arguments(db):
    tool = LogTraining(db=db)
    with pytest.raises(ToolValidationError):
        tool.parse_args({"sets": []})
    with pytest.raises(ToolValidationError):
        tool.parse_args({"type": "running", "distance_m": -5})
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"type": "running", "date": "next tuesday"}))
    future = (now_local() + timedelta(days=30)).strftime("%Y-%m-%d")
    with pytest.raises(ToolValidationError, match="future"):
        tool.run(tool.parse_args({"type": "running", "date": future}))
    assert WorkoutStore(db).count() == 0


def test_resolve_range():
    now = dt(2026, 10, 14, 15, 30)
    today = dt(2026, 10, 14, 0)
    assert resolve_range("week", now=now) == (today - timedelta(days=6), today + timedelta(days=1))
    assert resolve_range("month", now=now) == (today - timedelta(days=29), today + timedelta(days=1))
    assert resolve_range("custom", "2026-09-01", "2026-09-30", now=now) == (dt(2026, 9, 1, 0), dt(2026, 10, 1, 0))
    assert resolve_range("custom", "2026-10-01", None, now=now) == (dt(2026, 10, 1, 0), today + timedelta(days=1))
    assert resolve_range("today", now=now) == (today, today + timedelta(days=1))
    assert resolve_range("2026-10-01..2026-10-03", now=now) == (dt(2026, 10, 1, 0), dt(2026, 10, 4, 0))
    with pytest.raises(ToolValidationError):
        resolve_range("custom", None, None, now=now)
    with pytest.raises(ToolValidationError):
        resolve_range("custom", "2026-10-10", "2026-10-01", now=now)
    with pytest.raises(ToolValidationError):
        resolve_range("fortnight", now=now)


def test_list_workouts_tool(db):
    store = WorkoutStore(db)
    today = now_local()
    a = store.add_workout(type="running", started_at=today - timedelta(days=1), distance_m=5000, duration_s=1500)
    b = store.add_workout(type="strength", started_at=today - timedelta(days=2), notes="legs")
    store.add_sets(b, [SetInput(exercise="squat", reps=10, weight_kg=60)] * 3)
    store.add_workout(type="running", started_at=today - timedelta(days=20))
    tool = ListWorkouts(db=db)
    out = tool.run(tool.parse_args({})).content
    assert out.startswith("2 workout(s):") and f"#{a} " in out and f"#{b} " in out
    assert "3x10 squat 60 kg" in out and "notes: legs" in out and "5 km" in out
    out = tool.run(tool.parse_args({"range": "month", "type": "Koşu"})).content
    assert out.startswith("2 workout(s):") and "strength" not in out
    out = tool.run(tool.parse_args({"range": "week", "limit": 1})).content
    assert out.startswith("1 workout(s):")
    out = tool.run(tool.parse_args({"range": "custom", "from": "2020-01-01", "to": "2020-01-31"})).content
    assert out.startswith("No workouts between 2020-01-01 and 2020-01-31")
    with pytest.raises(ToolValidationError):
        tool.parse_args({"limit": 0})


def test_training_summary_tool(db):
    store = WorkoutStore(db)
    today = now_local()
    wid = store.add_workout(type="sprint", started_at=today - timedelta(days=1))
    store.add_sets(wid, [SetInput(exercise="sprint", distance_m=60, time_s=7.1, is_best=True)])
    store.add_workout(type="running", started_at=today - timedelta(days=10), distance_m=8000, duration_s=2400)
    tool = TrainingSummary(db=db)
    out = tool.run(tool.parse_args({"range": "week"})).content
    lines = out.splitlines()
    assert lines[0].startswith("period: ") and "type: all" in lines[0]
    assert "sessions: 1 (sprint 1)" in out and "best times: 60 m 7.1 s" in out
    assert "previous period:" in out and "sessions 1" in out and "distance 8 km" in out
    assert "trend: sessions +0" in out
    data = tool.summary_data(tool.parse_args({"range": "month", "type": "running"}))
    assert data["current"]["sessions"] == 1 and data["type"] == "running"
    with pytest.raises(ToolValidationError):
        tool.parse_args({"range": "year"})


def test_delete_workout_tool(db):
    store = WorkoutStore(db)
    wid = store.add_workout(type="running", started_at=dt(2026, 10, 10))
    tool = DeleteWorkout(db=db)
    assert tool.confirm_preview(tool.parse_args({"id": wid})).startswith(f"#{wid} ")
    assert "does not exist" in tool.confirm_preview(tool.parse_args({"id": 999}))
    assert tool.run(tool.parse_args({"id": wid})).content.startswith(f"Deleted workout #{wid} ")
    assert store.get(wid) is None
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"id": wid}))
    with pytest.raises(ToolValidationError):
        tool.parse_args({"id": 0})


# --- doctor -----------------------------------------------------------------------------------


def test_health_checks(config, paths, tmp_path):
    inbox = tmp_path / "inbox"
    config.training.health_inbox = str(inbox)
    checks = {c.name: c for c in health_checks(config, paths)}
    assert checks["Health inbox"].warn and "not set up" in checks["Health inbox"].detail  # optional: a warning, not a failure
    assert checks["Health inbox pending"].detail.startswith("0 file(s)")
    assert checks["Health sync"].ok and "0 workout(s) logged, 0 from Health; no Health import yet" in checks["Health sync"].detail

    inbox.mkdir()
    (inbox / "a.json").write_text("[]")
    (inbox / ".a.json.icloud").write_text("")
    from jarvis.observability.db import Database

    db = Database(paths.db_file)
    store = WorkoutStore(db)
    store.upsert_health("x1", type="running", started_at=dt(2026, 10, 10))
    store.record_import("workouts_2026-10-10.json", "shortcut", 1, 0)
    db.close()
    checks = {c.name: c for c in health_checks(config, paths)}
    assert checks["Health inbox"].ok and checks["Health inbox"].detail == str(inbox)
    assert checks["Health inbox pending"].detail.startswith("1 file(s) waiting; run `jarvis health sync`")
    assert "1 workout(s) logged, 1 from Health; last import" in checks["Health sync"].detail
    assert "workouts_2026-10-10.json: 1 new, 0 dup" in checks["Health sync"].detail


def test_health_checks_never_raise(config, paths, monkeypatch):
    config.training.health_inbox = "~/does/not/exist"
    monkeypatch.setattr("jarvis.observability.db.Database.__init__", lambda self, p: (_ for _ in ()).throw(OSError("disk")))
    checks = health_checks(config, paths)
    sync = next(c for c in checks if c.name == "Health sync")
    assert sync.ok is False and "OSError" in sync.detail
