"""Apple Health import: the three JSON adapters, auto-detection, units, dedup, the inbox sync and export.zip."""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from jarvis.training import health_import as hi
from jarvis.training.health_import import (
    ImportResult,
    detect_format,
    distance_to_m,
    duration_to_s,
    energy_to_kcal,
    import_export_zip,
    import_file,
    normalize_item,
    parse_health_datetime,
    stable_external_id,
    sync_inbox,
    workout_type_from_apple,
)
from jarvis.training.store import WorkoutStore
from jarvis.utils.timeutil import local_tz

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "health"
TZ3 = timezone(timedelta(hours=3))


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def rows(db, type=None):
    return WorkoutStore(db).list(datetime(2000, 1, 1, tzinfo=TZ3), datetime(2100, 1, 1, tzinfo=TZ3), type)


# --- converters ----------------------------------------------------------------------------


def test_unit_conversions():
    assert (
        distance_to_m(2500) == 2500
        and distance_to_m("5.2 km") == 5200
        and distance_to_m({"qty": 1, "units": "mi"}) == pytest.approx(1609.344)
    )
    assert distance_to_m({"qty": 100, "units": "yd"}) == pytest.approx(91.44) and distance_to_m("3,5 km") == 3500
    assert distance_to_m(None) is None and distance_to_m("far") is None and distance_to_m({"qty": 1, "units": "parsec"}) is None
    assert (
        energy_to_kcal(410) == 410
        and energy_to_kcal("300 kcal") == 300
        and energy_to_kcal({"qty": 418.4, "units": "kJ"}) == pytest.approx(100)
    )
    assert energy_to_kcal({"qty": 5, "units": "Cal"}) == 5 and energy_to_kcal(True) is None
    assert duration_to_s(1800) == 1800 and duration_to_s("0:30:00") == 1800 and duration_to_s("5:30") == 330
    assert duration_to_s("1:02:03.5") == 3723.5 and duration_to_s("45 min") == 2700 and duration_to_s("1.5 h") == 5400
    assert duration_to_s(30, "min") == 1800 and duration_to_s({"qty": 2, "units": "hours"}) == 7200
    assert duration_to_s("soon") is None


def test_parse_health_datetime_variants():
    expected = datetime(2026, 10, 10, 18, 30, tzinfo=TZ3)
    assert parse_health_datetime("2026-10-10T18:30:00+03:00") == expected
    assert parse_health_datetime("2026-10-10 18:30:00 +0300") == expected
    assert parse_health_datetime("2026-10-10T15:30:00Z") == expected
    assert parse_health_datetime(expected.timestamp()) == expected
    assert parse_health_datetime(int(expected.timestamp() * 1000)) == expected
    naive = parse_health_datetime("2026-10-10 18:30")
    assert naive is not None and naive.tzinfo is not None and naive.utcoffset() == datetime.now(local_tz()).utcoffset()
    assert parse_health_datetime("yesterday") is None and parse_health_datetime(None) is None and parse_health_datetime([1]) is None


def test_workout_type_from_apple():
    assert workout_type_from_apple("HKWorkoutActivityTypeRunning") == "running"
    assert workout_type_from_apple("HKWorkoutActivityTypeTraditionalStrengthTraining") == "strength"
    assert workout_type_from_apple("HKWorkoutActivityTypeHighIntensityIntervalTraining") == "hiit"
    assert workout_type_from_apple("HKWorkoutActivityTypeStairClimbing") == "stairs"
    assert workout_type_from_apple("HKWorkoutActivityTypeJumpRope") == "jump rope"
    assert workout_type_from_apple("Outdoor Run") == "running"


def test_stable_external_id_is_format_independent():
    start = datetime(2026, 10, 10, 18, 30, tzinfo=TZ3)
    end = start + timedelta(minutes=30)
    a = stable_external_id("running", start, end)
    b = stable_external_id("running", start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    assert a == b and a.startswith("h:") and len(a) == 34
    assert stable_external_id("walking", start, end) != a
    shortcut = normalize_item({"type": "Running", "start": "2026-10-10T18:30:00+03:00", "end": "2026-10-10T19:00:00+03:00"}, "shortcut")
    hae = normalize_item({"name": "Outdoor Run", "start": "2026-10-10 18:30:00 +0300", "end": "2026-10-10 19:00:00 +0300"}, "hae_v2")
    assert shortcut.external_id == hae.external_id == a


# --- detection ------------------------------------------------------------------------------


def test_detect_format():
    assert detect_format(load("shortcut_workouts.json")) == "shortcut"
    assert detect_format(load("hae_v2.json")) == "hae_v2"
    assert detect_format(load("hae_v1.json")) == "hae_v1"
    assert detect_format(load("shortcut_workouts.json")["workouts"]) == "shortcut"  # bare list
    assert detect_format({"workouts": load("hae_v2.json")["data"]["workouts"]}) == "hae_v2"  # HAE without wrapper
    assert detect_format({"type": "Running", "start": "2026-10-10T18:30:00+03:00"}) == "shortcut"  # single object
    assert detect_format({"workouts": []}) == "shortcut"
    assert detect_format({"foo": 1}) == "unknown" and detect_format("text") == "unknown" and detect_format(None) == "unknown"
    assert detect_format({"data": {"metrics": []}}) == "unknown"


# --- adapters --------------------------------------------------------------------------------


def test_shortcut_import(db):
    result = import_file(FIXTURES / "shortcut_workouts.json", WorkoutStore(db))
    assert result.ok and result.format == "shortcut" and (result.new, result.dup) == (3, 0) and result.errors == []
    assert result.one_line() == "shortcut_workouts.json: shortcut, 3 new, 0 duplicate"
    by_type = {r.type: r for r in rows(db)}
    run = by_type["running"]
    assert run.source == "health" and run.external_id == "A1B2C3D4-0001-4000-8000-000000000001"
    assert run.started_at == datetime(2026, 10, 10, 18, 30, tzinfo=TZ3) and run.ended_at == datetime(2026, 10, 10, 19, 0, tzinfo=TZ3)
    assert (run.duration_s, run.distance_m, run.energy_kcal, run.avg_hr, run.max_hr) == (1800, 5200, 410, 152, 178)
    assert run.notes == "source: Apple Watch"
    gym = by_type["strength"]
    assert gym.duration_s == 2700 and gym.energy_kcal == 300 and gym.avg_hr == 120 and gym.max_hr is None
    assert gym.external_id.startswith("h:")
    assert "​" not in gym.notes and "upper body" in gym.notes  # sanitized, kept as text
    walk = by_type["walking"]
    assert walk.started_at == datetime(2026, 10, 12, 9, 0, tzinfo=TZ3)
    assert walk.ended_at == walk.started_at + timedelta(seconds=1800) and walk.distance_m == 2500
    imports = db.conn.execute("SELECT * FROM health_imports").fetchall()
    assert len(imports) == 1 and imports[0]["format"] == "shortcut" and imports[0]["workouts_new"] == 3 and imports[0]["error"] is None


def test_hae_v2_import(db):
    result = import_file(FIXTURES / "hae_v2.json", WorkoutStore(db))
    assert result.ok and result.format == "hae_v2" and (result.new, result.dup) == (2, 0)
    by_type = {r.type: r for r in rows(db)}
    run = by_type["running"]
    assert run.external_id == "8F1C2D3E-1111-4222-8333-444444444444"
    assert run.distance_m == pytest.approx(3.107 * 1609.344) and run.energy_kcal == 410
    assert (run.avg_hr, run.max_hr, run.duration_s) == (152, 178, 1800)
    hiit = by_type["hiit"]
    assert hiit.energy_kcal == pytest.approx(300, abs=0.01)  # 1255.2 kJ
    assert (hiit.avg_hr, hiit.max_hr) == (160, 185)


def test_hae_v1_import(db):
    result = import_file(FIXTURES / "hae_v1.json", WorkoutStore(db))
    assert result.ok and result.format == "hae_v1" and (result.new, result.dup) == (2, 0)
    by_type = {r.type: r for r in rows(db)}
    run = by_type["running"]
    assert run.external_id.startswith("h:") and run.energy_kcal == 400  # active energy preferred over total
    assert run.distance_m == pytest.approx(3.1 * 1609.344) and run.duration_s == 1800
    assert run.avg_hr == 160 and run.max_hr == 180  # computed from the samples
    walk = by_type["walking"]
    assert walk.avg_hr is None and walk.max_hr is None and walk.energy_kcal == 80


def test_reimport_is_all_duplicates(db):
    store = WorkoutStore(db)
    first = import_file(FIXTURES / "shortcut_workouts.json", store)
    second = import_file(FIXTURES / "shortcut_workouts.json", store)
    assert (first.new, first.dup) == (3, 0) and (second.new, second.dup) == (0, 3)
    assert store.count("health") == 3
    # The same run exported by Health Auto Export carries a different id, so it is a separate row (documented).
    third = import_file(FIXTURES / "hae_v2.json", store)
    assert third.new == 2 and store.count("health") == 5


def test_normalize_item_edge_cases():
    base = {"type": "Running", "start": "2026-10-10T18:30:00+03:00"}
    w = normalize_item({**base, "end": "2026-10-10T18:00:00+03:00", "duration": 600}, "shortcut")
    assert w.ended_at == w.started_at + timedelta(seconds=600)  # end before start is dropped, duration wins
    w = normalize_item({**base, "avgHeartRate": 999, "maxHeartRate": 0, "distance": {"qty": 5, "units": "furlong"}}, "shortcut")
    assert w.avg_hr is None and w.max_hr is None and w.distance_m is None
    w = normalize_item({**base, "duration": 30, "durationUnit": "min", "id": "  ../x y​1 "}, "shortcut")
    assert w.duration_s == 1800 and w.external_id == "..xy1"
    w = normalize_item({**base, "heartRate": {"min": {"qty": 90}, "avg": {"qty": 150}, "max": {"qty": 170}}}, "hae_v2")
    assert (w.avg_hr, w.max_hr) == (150, 170)
    w = normalize_item(
        {**base, "heartRateData": [{"qty": 100}, {"qty": {"qty": 120, "units": "count/min"}}, "bad", {"qty": 500}]}, "hae_v1"
    )
    assert (w.avg_hr, w.max_hr) == (110, 120)
    with pytest.raises(ValueError):
        normalize_item({"start": "2026-10-10T18:30:00+03:00"}, "shortcut")
    with pytest.raises(ValueError):
        normalize_item({"type": "Running"}, "shortcut")
    with pytest.raises(ValueError):
        normalize_item({**base, "duration": 10**9}, "shortcut")  # pydantic bound


def test_malformed_json_rejected_without_crashing(db, tmp_path):
    store = WorkoutStore(db)
    result = import_file(FIXTURES / "malformed.json", store)
    assert not result.ok and result.format == "unknown" and (result.new, result.dup) == (0, 0)
    assert result.errors and "cannot read JSON" in result.errors[0] and "failed" in result.one_line()
    not_workouts = tmp_path / "metrics.json"
    not_workouts.write_text('{"data": {"metrics": [{"name": "step_count"}]}}')
    assert not import_file(not_workouts, store).ok
    missing = tmp_path / "missing.json"
    assert not import_file(missing, store).ok
    huge = tmp_path / "huge.json"
    huge.write_text("[]")
    try:
        hi.MAX_JSON_BYTES = 1
        assert "too large" in import_file(huge, store).errors[0]
    finally:
        hi.MAX_JSON_BYTES = 64 * 1024 * 1024
    # record-level problems do not fail the file
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps([{"type": "Running", "start": "2026-10-10T18:30:00+03:00"}, {"type": "Running"}, 42]))
    result = import_file(partial, store)
    assert result.ok and result.new == 1 and len(result.errors) == 2
    assert result.errors == ["record 1: missing or unreadable start date", "record 2: not an object"]
    assert store.count("health") == 1
    errors = [r["error"] for r in db.conn.execute("SELECT error FROM health_imports ORDER BY id").fetchall()]
    assert errors[0].startswith("cannot read JSON") and errors[-1] is None and all(e for e in errors[:-1])


# --- inbox sync -------------------------------------------------------------------------------


def test_sync_inbox_moves_files(config, paths, db, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config.training.health_inbox = str(inbox)
    for name in ("shortcut_workouts.json", "hae_v2.json", "malformed.json"):
        shutil.copy(FIXTURES / name, inbox / name)
    (inbox / ".hidden.json").write_text("[]")
    (inbox / "notes.txt").write_text("not json")
    result = sync_inbox(config, paths, db)
    assert result.processed == ["hae_v2.json", "shortcut_workouts.json"] and result.failed == ["malformed.json"]
    assert (result.new, result.dup) == (
        5,
        0,
    ) and result.one_line() == "2 dosya içe aktarıldı, 1 başarısız, 5 yeni antrenman, 0 tekrar"  # Turkish test config
    assert (inbox / "processed" / "hae_v2.json").is_file() and (inbox / "processed" / "shortcut_workouts.json").is_file()
    assert (inbox / "failed" / "malformed.json").is_file()
    error_text = (inbox / "failed" / "malformed.json.error.txt").read_text()
    assert "cannot read JSON" in error_text
    assert sorted(p.name for p in inbox.iterdir()) == [".hidden.json", "failed", "notes.txt", "processed"]
    assert any("malformed.json" in e for e in result.errors)
    # a second run finds nothing
    again = sync_inbox(config, paths, db)
    assert again.processed == [] and again.failed == [] and again.new == 0
    # a re-exported file is all duplicates and goes to processed/ under a new name
    shutil.copy(FIXTURES / "shortcut_workouts.json", inbox / "shortcut_workouts.json")
    third = sync_inbox(config, paths, db)
    assert third.processed == ["shortcut_workouts.json"] and (third.new, third.dup) == (0, 3)
    assert len(list((inbox / "processed").glob("shortcut_workouts*.json"))) == 2


def test_sync_inbox_missing_or_relative_inbox(config, paths, db):
    config.training.health_inbox = str(paths.home / "nope")
    result = sync_inbox(config, paths, db)
    assert result.processed == [] and result.note and "bulunamadı" in result.note and result.one_line() == result.note
    config.training.health_inbox = "inbox_rel"
    (paths.home / "inbox_rel").mkdir()
    (paths.home / "inbox_rel" / "w.json").write_text(json.dumps([{"type": "Walking", "start": "2026-10-10T09:00:00+03:00"}]))
    result = sync_inbox(config, paths, db)
    assert result.processed == ["w.json"] and result.new == 1 and result.inbox == str(paths.home / "inbox_rel")


def test_sync_inbox_survives_an_unexpected_crash(config, paths, db, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config.training.health_inbox = str(inbox)
    shutil.copy(FIXTURES / "hae_v1.json", inbox / "hae_v1.json")

    def boom(path, store):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(hi, "import_file", boom)
    result = sync_inbox(config, paths, db)
    assert result.failed == ["hae_v1.json"] and "RuntimeError" in result.errors[0]
    assert (inbox / "failed" / "hae_v1.json.error.txt").read_text().startswith("RuntimeError")


# --- export.zip -------------------------------------------------------------------------------


def test_export_zip_streams_workouts(db, monkeypatch):
    calls = []
    real_iterparse = ET.iterparse

    def spy(source, events=None):
        calls.append(events)
        return real_iterparse(source, events=events)

    monkeypatch.setattr(hi.ET, "iterparse", spy)
    result = import_export_zip(FIXTURES / "export.zip", db)
    assert calls == [("start", "end")]
    assert result.ok and result.format == "export_xml" and (result.new, result.dup) == (3, 0)
    assert result.errors == ["workout 3: missing startDate"]
    by_type = {r.type: r for r in rows(db)}
    run = by_type["running"]
    assert run.external_id.startswith("h:") and run.duration_s == 1830 and run.notes == "source: Apple Watch"
    assert run.started_at == datetime(2026, 10, 10, 18, 30, tzinfo=TZ3)
    assert (run.distance_m, run.energy_kcal, run.avg_hr, run.max_hr) == (5020, 320, 150, 180)  # from WorkoutStatistics
    ride = by_type["cycling"]
    assert ride.external_id == "strava-activity-123456"  # HKExternalUUID
    assert (ride.distance_m, ride.energy_kcal, ride.duration_s) == (20500, 600, 3720) and ride.avg_hr is None
    gym = by_type["strength"]
    assert gym.energy_kcal == pytest.approx(300, abs=0.01) and (gym.avg_hr, gym.max_hr) == (118, 152)
    again = import_export_zip(FIXTURES / "export.zip", db)
    assert (again.new, again.dup) == (0, 3) and WorkoutStore(db).count("health") == 3
    imports = db.conn.execute("SELECT format, workouts_new, workouts_dup, error FROM health_imports").fetchall()
    assert [tuple(r) for r in imports] == [("export_xml", 3, 0, None), ("export_xml", 0, 3, None)]


def test_export_xml_memory_stays_flat(db, tmp_path, monkeypatch):
    """Thousands of Record elements before and between workouts never accumulate under the root."""
    record = (
        '<Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min" startDate="2026-10-10 18:31:00 +0300" '
        'endDate="2026-10-10 18:31:00 +0300" value="120"><MetadataEntry key="k" value="v"/></Record>\n'
    )
    workout = (
        '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" '
        'startDate="2026-10-{day:02d} 18:30:00 +0300" endDate="2026-10-{day:02d} 19:00:00 +0300">'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="140" maximum="170" unit="count/min"/>'
        "</Workout>\n"
    )
    xml = tmp_path / "export.xml"
    with xml.open("w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_TR">\n')
        for day in range(1, 6):
            fh.write(record * 2000)
            fh.write(workout.format(day=day))
        fh.write("</HealthData>\n")
    roots = []
    real_iterparse = ET.iterparse

    def spy(source, events=None):
        it = real_iterparse(source, events=events)

        def gen():
            for event, elem in it:
                if not roots:
                    roots.append(elem)
                yield event, elem

        return gen()

    monkeypatch.setattr(hi.ET, "iterparse", spy)
    result = import_export_zip(xml, db)  # a bare export.xml is accepted too
    assert (result.new, result.dup) == (5, 0) and result.errors == []
    assert roots and len(roots[0]) == 0  # the root was emptied as the stream went by


def test_export_zip_errors(db, tmp_path):
    not_zip = tmp_path / "export.zip"
    not_zip.write_bytes(b"this is not a zip")
    result = import_export_zip(not_zip, db)
    assert not result.ok and "cannot open export" in result.errors[0] and "BadZipFile" in result.errors[0]
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("apple_health_export/export_cda.xml", "<x/>")
    result = import_export_zip(empty, db)
    assert not result.ok and "no export.xml" in result.errors[0]
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr(
            "apple_health_export/export.xml",
            '<HealthData><Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
            'startDate="2026-10-10 18:30:00 +0300" endDate="2026-10-10 19:00:00 +0300"/><Workout',
        )
    result = import_export_zip(broken, db)
    assert not result.ok and result.new == 1 and "export.xml unreadable" in result.errors[0]  # what was read stays
    assert import_export_zip(tmp_path / "missing.zip", db).ok is False
    imports = db.conn.execute("SELECT error FROM health_imports").fetchall()
    assert len(imports) == 4 and all(r["error"] for r in imports)


def test_import_result_total_and_one_line():
    r = ImportResult(new=2, dup=1, format="shortcut", errors=["record 3: x"], file="/a/b.json")
    assert r.total == 3 and r.one_line() == "b.json: shortcut, 2 new, 1 duplicate, 1 skipped"
    assert ImportResult().one_line() == "input: failed (unknown error)"
