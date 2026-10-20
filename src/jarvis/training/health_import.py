"""Apple Health import.

Three JSON adapters, detected from the parsed shape:
  - "shortcut": the Jarvis iPhone Shortcut format (docs/integrations/health.md), tolerant key names
  - "hae_v2":   Health Auto Export, JSON v2 (`{"data": {"workouts": [...]}}`, `heartRate {avg, max}`)
  - "hae_v1":   Health Auto Export, JSON v1 (`heartRateData [{date, qty}]`)
plus the full-export backfill (`export.zip` -> `export.xml`, streamed with iterparse).

Health data is the owner's own device data: trusted (no taint), but every record is validated
with Pydantic, text fields are sanitized and the logs carry counts and ids only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from jarvis.i18n import t
from jarvis.security import sanitize
from jarvis.training.models import HealthWorkout, clean_notes, normalize_type
from jarvis.training.store import WorkoutStore
from jarvis.utils.timeutil import local_tz, now_local

log = logging.getLogger("jarvis.training.health")

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_WORKOUTS_PER_FILE = 50_000
FORMAT_SHORTCUT = "shortcut"
FORMAT_HAE_V1 = "hae_v1"
FORMAT_HAE_V2 = "hae_v2"
FORMAT_EXPORT_XML = "export_xml"
FORMAT_UNKNOWN = "unknown"

MILE_M = 1609.344
YARD_M = 0.9144
FOOT_M = 0.3048


@dataclass
class ImportResult:
    new: int = 0
    dup: int = 0
    format: str = FORMAT_UNKNOWN
    errors: list[str] = field(default_factory=list)
    file: str = ""

    @property
    def ok(self) -> bool:
        """False when the file as a whole could not be read; record-level problems are in `errors`."""
        return self.format != FORMAT_UNKNOWN

    @property
    def total(self) -> int:
        return self.new + self.dup

    def one_line(self) -> str:
        """The result for the terminal, in the main language."""
        name = Path(self.file).name if self.file else t("cli.health.input")
        if not self.ok:
            return t("cli.health.file_failed", name=name, error=self.errors[0] if self.errors else t("cli.health.unknown_error"))
        text = t("cli.health.file_ok", name=name, format=self.format, new=self.new, dup=self.dup)
        if self.errors:
            text += t("cli.health.file_skipped", count=len(self.errors))
        return text


@dataclass
class SyncResult:
    inbox: str
    processed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    new: int = 0
    dup: int = 0
    errors: list[str] = field(default_factory=list)
    note: str | None = None

    def one_line(self) -> str:
        """The result for the terminal, in the main language."""
        if self.note and not self.processed and not self.failed:
            return self.note
        return t("cli.health.synced", processed=len(self.processed), failed=len(self.failed), new=self.new, dup=self.dup)


# --- small converters ------------------------------------------------------------------------


_HMS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$")
_NUM_UNIT_RE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*([a-zA-Z/%]+)?\s*$")
_HAE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{2}):?(\d{2})$")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUM_UNIT_RE.match(value)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def _qty_unit(value: Any, default_unit: str | None) -> tuple[float | None, str | None]:
    """number | 'number unit' | {qty, units} | {value, unit} -> (number, unit)."""
    if isinstance(value, dict):
        qty = value.get("qty", value.get("value", value.get("quantity")))
        unit = value.get("units", value.get("unit"))
        return _number(qty), (str(unit).strip().lower() if unit else default_unit)
    if isinstance(value, str):
        m = _NUM_UNIT_RE.match(value)
        if not m:
            return None, default_unit
        return float(m.group(1).replace(",", ".")), (m.group(2).lower() if m.group(2) else default_unit)
    return _number(value), default_unit


def distance_to_m(value: Any, default_unit: str = "m") -> float | None:
    qty, unit = _qty_unit(value, default_unit)
    if qty is None:
        return None
    unit = (unit or default_unit).lower()
    if unit in ("m", "meter", "meters", "metre", "metres"):
        return qty
    if unit in ("km", "kilometer", "kilometers", "kilometre", "kilometres"):
        return qty * 1000.0
    if unit in ("mi", "mile", "miles"):
        return qty * MILE_M
    if unit in ("yd", "yard", "yards"):
        return qty * YARD_M
    if unit in ("ft", "foot", "feet"):
        return qty * FOOT_M
    if unit in ("cm",):
        return qty / 100.0
    return None


def energy_to_kcal(value: Any, default_unit: str = "kcal") -> float | None:
    qty, unit = _qty_unit(value, default_unit)
    if qty is None:
        return None
    unit = (unit or default_unit).lower()
    if unit in ("kcal", "cal", "calories", "kilocalorie", "kilocalories", "calorie"):
        # Apple uses "Cal" (large calorie) and HAE "kcal"; both mean kcal.
        return qty
    if unit in ("kj", "kilojoule", "kilojoules"):
        return qty / 4.184
    if unit in ("j", "joule", "joules"):
        return qty / 4184.0
    return None


def duration_to_s(value: Any, default_unit: str = "s") -> float | None:
    if isinstance(value, str):
        m = _HMS_RE.match(value.strip())
        if m:
            h = float(m.group(1) or 0)
            return h * 3600 + float(m.group(2)) * 60 + float(m.group(3))
    qty, unit = _qty_unit(value, default_unit)
    if qty is None:
        return None
    unit = (unit or default_unit).lower()
    if unit in ("s", "sec", "secs", "second", "seconds"):
        return qty
    if unit in ("min", "mins", "minute", "minutes", "m"):
        return qty * 60.0
    if unit in ("h", "hr", "hrs", "hour", "hours"):
        return qty * 3600.0
    if unit in ("ms", "millisecond", "milliseconds"):
        return qty / 1000.0
    return None


def heart_rate(value: Any) -> float | None:
    qty, _ = _qty_unit(value, "count/min")
    return qty


def parse_health_datetime(value: Any) -> datetime | None:
    """ISO 8601 (with Z or offset), 'yyyy-MM-dd HH:mm:ss +0300' (Apple/HAE), or epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 1e12:  # milliseconds
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(local_tz())
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    m = _HAE_DATE_RE.match(text)
    if m:
        text = f"{m.group(1)}T{m.group(2)}{m.group(3)}:{m.group(4)}"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt


def stable_external_id(type_: str, start: datetime | None, end: datetime | None) -> str:
    """Hash of (type, start, end) in UTC so the same workout gets the same id from every adapter."""
    s = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if start else ""
    e = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if end else ""
    digest = hashlib.sha256(f"{type_}|{s}|{e}".encode("utf-8")).hexdigest()
    return f"h:{digest[:32]}"


def _clean_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = sanitize.for_model(str(value)).strip()
    text = re.sub(r"[^A-Za-z0-9:_\-.]", "", text)
    return text[:128] or None


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def workout_type_from_apple(name: str) -> str:
    """'HKWorkoutActivityTypeTraditionalStrengthTraining' / 'Traditional Strength Training' -> 'strength'."""
    text = name or ""
    if text.startswith("HKWorkoutActivityType"):
        text = text[len("HKWorkoutActivityType") :]
    text = _CAMEL_RE.sub(" ", text)
    return normalize_type(text)


# --- JSON adapters ---------------------------------------------------------------------------


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _workout_items(payload: Any) -> list[Any] | None:
    """The list of workout objects inside a parsed JSON document, whatever the wrapper."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("workouts"), list):
        return data["workouts"]
    if isinstance(payload.get("workouts"), list):
        return payload["workouts"]
    if isinstance(data, list):
        return data
    # A single workout object written without a list wrapper.
    if any(k in payload for k in ("type", "workoutType", "name", "start", "startDate")):
        return [payload]
    return None


def detect_format(payload: Any) -> str:
    if isinstance(payload, dict):
        marker = str(payload.get("format") or payload.get("source") or "").lower()
        if "jarvis" in marker or "shortcut" in marker:
            return FORMAT_SHORTCUT
    items = _workout_items(payload)
    if items is None:
        return FORMAT_UNKNOWN
    sample = next((i for i in items if isinstance(i, dict)), None)
    if sample is None:
        return FORMAT_SHORTCUT if items == [] else FORMAT_UNKNOWN
    if "heartRateData" in sample or ("totalEnergy" in sample and "id" not in sample and "heartRate" not in sample):
        return FORMAT_HAE_V1
    hae_v2_keys = ("activeEnergyBurned", "heartRate", "stepCount", "intensity", "humidity", "temperature")
    if any(k in sample for k in hae_v2_keys) and "name" in sample:
        return FORMAT_HAE_V2
    if "id" in sample and "name" in sample and isinstance(sample.get("start"), str) and _HAE_DATE_RE.match(sample["start"]):
        return FORMAT_HAE_V2
    return FORMAT_SHORTCUT


def _hr_from_samples(samples: Any) -> tuple[float | None, float | None]:
    if not isinstance(samples, list):
        return None, None
    values: list[float] = []
    for s in samples[:100_000]:
        if isinstance(s, dict):
            v = heart_rate(_first(s, "qty", "value", "avg", "Avg"))
        else:
            v = _number(s)
        if v is not None and 0 < v < 300:
            values.append(v)
    if not values:
        return None, None
    return sum(values) / len(values), max(values)


def normalize_item(item: dict[str, Any], fmt: str) -> HealthWorkout:
    """One raw workout object (any of the three JSON formats) -> validated HealthWorkout.

    Raises ValueError / pydantic.ValidationError for records that cannot be used.
    """
    raw_type = _first(item, "type", "workoutType", "activityType", "name", "workoutActivityType")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError("missing workout type")
    type_ = workout_type_from_apple(raw_type)
    if not type_:
        raise ValueError("empty workout type")

    start = parse_health_datetime(_first(item, "start", "startDate", "start_date", "began"))
    end = parse_health_datetime(_first(item, "end", "endDate", "end_date", "finished"))
    if start is None:
        raise ValueError("missing or unreadable start date")
    if end is not None and end < start:
        end = None

    duration_unit = item.get("durationUnit") or item.get("duration_unit")
    duration = duration_to_s(_first(item, "duration", "totalTime"), str(duration_unit).lower() if duration_unit else "s")
    if duration is None and end is not None:
        duration = (end - start).total_seconds()
    if end is None and duration is not None:
        end = start + timedelta(seconds=duration)

    distance_unit = item.get("distanceUnit") or item.get("totalDistanceUnit")
    distance = distance_to_m(
        _first(item, "distance", "totalDistance", "distance_m", "distanceWalkingRunning", "distanceCycling"),
        str(distance_unit).lower() if distance_unit else "m",
    )

    energy_unit = item.get("energyUnit") or item.get("totalEnergyBurnedUnit")
    energy = energy_to_kcal(
        _first(item, "energy", "activeEnergy", "activeEnergyBurned", "totalEnergy", "totalEnergyBurned", "calories"),
        str(energy_unit).lower() if energy_unit else "kcal",
    )

    avg_hr: float | None = None
    max_hr: float | None = None
    hr = item.get("heartRate")
    if isinstance(hr, dict) and any(k in hr for k in ("avg", "max", "min", "Avg", "Max")):
        avg_hr = heart_rate(_first(hr, "avg", "Avg", "average"))
        max_hr = heart_rate(_first(hr, "max", "Max", "maximum"))
    elif hr is not None:
        avg_hr = heart_rate(hr)
    if avg_hr is None:
        avg_hr = heart_rate(_first(item, "avgHeartRate", "averageHeartRate", "heartRateAverage", "avg_hr"))
    if max_hr is None:
        max_hr = heart_rate(_first(item, "maxHeartRate", "heartRateMax", "max_hr"))
    if avg_hr is None and max_hr is None:
        avg_hr, max_hr = _hr_from_samples(item.get("heartRateData"))
    if avg_hr is not None and not (0 < avg_hr < 300):
        avg_hr = None
    if max_hr is not None and not (0 < max_hr < 300):
        max_hr = None

    external_id = _clean_id(_first(item, "id", "uuid", "UUID", "identifier", "externalId"))
    if external_id is None:
        external_id = stable_external_id(type_, start, end)

    note_bits = []
    source = _first(item, "source", "sourceName", "device")
    if isinstance(source, str) and source.strip():
        note_bits.append(f"source: {sanitize.for_model(source).strip()[:80]}")
    notes_raw = _first(item, "notes", "note", "comment")
    if isinstance(notes_raw, str) and notes_raw.strip():
        note_bits.append(sanitize.for_model(notes_raw).strip()[:500])

    return HealthWorkout(
        external_id=external_id,
        type=type_,
        started_at=start,
        ended_at=end,
        duration_s=duration,
        distance_m=distance,
        energy_kcal=energy,
        avg_hr=avg_hr,
        max_hr=max_hr,
        notes=clean_notes("; ".join(note_bits)) if note_bits else None,
    )


def parse_payload(payload: Any) -> tuple[str, list[HealthWorkout], list[str]]:
    """Detect the format and normalize every record. Bad records are reported, not raised."""
    fmt = detect_format(payload)
    if fmt == FORMAT_UNKNOWN:
        return fmt, [], ["JSON does not look like a workout export (no workouts list)"]
    items = _workout_items(payload) or []
    if len(items) > MAX_WORKOUTS_PER_FILE:
        return FORMAT_UNKNOWN, [], [f"too many workouts in one file ({len(items)} > {MAX_WORKOUTS_PER_FILE})"]
    workouts: list[HealthWorkout] = []
    errors: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"record {index}: not an object")
            continue
        try:
            workouts.append(normalize_item(item, fmt))
        except (ValueError, TypeError, ValidationError) as exc:
            errors.append(f"record {index}: {_short_error(exc)}")
    return fmt, workouts, errors


def _short_error(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "input"
        return f"{loc}: {first.get('msg', 'invalid')}"
    return str(exc)[:200] or type(exc).__name__


def _store_all(workouts: list[HealthWorkout], store: WorkoutStore, result: ImportResult) -> None:
    for w in workouts:
        outcome = store.upsert_health(
            w.external_id,
            type=w.type,
            started_at=w.started_at,
            ended_at=w.ended_at,
            duration_s=w.duration_s,
            distance_m=w.distance_m,
            energy_kcal=w.energy_kcal,
            avg_hr=w.avg_hr,
            max_hr=w.max_hr,
            notes=w.notes,
        )
        if outcome == "new":
            result.new += 1
        else:
            result.dup += 1


def _load_json(path: Path) -> Any:
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(f"file is too large ({size} bytes > {MAX_JSON_BYTES})")
    with path.open("rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8-sig")
    return json.loads(text)


def import_file(path: Path | str, store: WorkoutStore) -> ImportResult:
    """Import one inbox JSON file. Never raises for a bad file: see `ImportResult.ok` and `errors`."""
    path = Path(path)
    result = ImportResult(file=str(path))
    try:
        payload = _load_json(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        message = f"cannot read JSON: {type(exc).__name__}: {str(exc)[:200]}"
        result.errors.append(message)
        store.record_import(path.name, FORMAT_UNKNOWN, 0, 0, message)
        log.warning("health import failed file=%s error=%s", path.name, type(exc).__name__)
        return result
    fmt, workouts, errors = parse_payload(payload)
    result.format = fmt
    result.errors.extend(errors)
    if fmt == FORMAT_UNKNOWN:
        store.record_import(path.name, fmt, 0, 0, errors[0] if errors else "unknown format")
        log.warning("health import rejected file=%s reason=unknown-format", path.name)
        return result
    try:
        _store_all(workouts, store, result)
    except Exception as exc:  # database trouble: the file is not consumed
        result.format = FORMAT_UNKNOWN
        result.errors.append(f"database error: {type(exc).__name__}")
        store.record_import(path.name, fmt, result.new, result.dup, f"database error: {type(exc).__name__}")
        log.exception("health import database error file=%s", path.name)
        return result
    store.record_import(path.name, fmt, result.new, result.dup, None)
    log.info(
        "health import file=%s format=%s new=%s dup=%s skipped=%s",
        path.name,
        fmt,
        result.new,
        result.dup,
        len(result.errors),
    )
    return result


# --- inbox sync --------------------------------------------------------------------------------


def inbox_path(config: Any, paths: Any) -> Path:
    raw = Path(str(config.training.health_inbox)).expanduser()
    if not raw.is_absolute():
        raw = Path(paths.home) / raw
    return raw


def pending_files(inbox: Path) -> list[Path]:
    """JSON files waiting in the inbox (not the iCloud placeholders, which start with a dot)."""
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.glob("*.json") if p.is_file() and not p.name.startswith("."))


def _move(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / src.name
    if target.exists():
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        target = folder / f"{src.stem}.{stamp}{src.suffix}"
    shutil.move(str(src), str(target))
    return target


def sync_inbox(config: Any, paths: Any, db: Any) -> SyncResult:
    """Import every *.json in the inbox, moving each to processed/ or failed/. Never raises."""
    try:
        inbox = inbox_path(config, paths)
    except Exception as exc:
        return SyncResult(inbox="?", note=t("cli.health.inbox_bad", error=type(exc).__name__))
    result = SyncResult(inbox=str(inbox))
    if not inbox.is_dir():
        result.note = t("cli.health.inbox_missing", path=inbox)
        return result
    store = WorkoutStore(db)
    try:
        files = pending_files(inbox)
    except OSError as exc:
        result.note = t("cli.health.inbox_unreadable", error=type(exc).__name__)
        return result
    for path in files:
        try:
            outcome = import_file(path, store)
        except Exception as exc:  # belt and braces: import_file already catches everything expected
            outcome = ImportResult(file=str(path), errors=[f"{type(exc).__name__}: {str(exc)[:200]}"])
            log.exception("health import crashed file=%s", path.name)
        try:
            if outcome.ok:
                _move(path, inbox / "processed")
                result.processed.append(path.name)
                result.new += outcome.new
                result.dup += outcome.dup
                result.errors.extend(f"{path.name}: {e}" for e in outcome.errors)
            else:
                moved = _move(path, inbox / "failed")
                (moved.parent / f"{moved.name}.error.txt").write_text("\n".join(outcome.errors) + "\n", encoding="utf-8")
                result.failed.append(path.name)
                result.errors.extend(f"{path.name}: {e}" for e in outcome.errors)
        except OSError as exc:
            result.errors.append(f"{path.name}: could not move file: {type(exc).__name__}")
            log.warning("health inbox move failed file=%s error=%s", path.name, type(exc).__name__)
    log.info(
        "health sync inbox files=%s processed=%s failed=%s new=%s dup=%s",
        len(files),
        len(result.processed),
        len(result.failed),
        result.new,
        result.dup,
    )
    return result


# --- export.zip (full backfill) -----------------------------------------------------------------


_HK_PREFIX = "HKQuantityTypeIdentifier"
_STAT_DISTANCE = {
    "DistanceWalkingRunning",
    "DistanceCycling",
    "DistanceSwimming",
    "DistanceWheelchair",
    "DistanceDownhillSnowSports",
    "DistanceRowing",
    "DistancePaddleSports",
    "DistanceCrossCountrySkiing",
}


def _workout_from_element(elem: ET.Element) -> HealthWorkout:
    a = elem.attrib
    type_ = workout_type_from_apple(a.get("workoutActivityType", ""))
    if not type_:
        raise ValueError("missing workoutActivityType")
    start = parse_health_datetime(a.get("startDate"))
    end = parse_health_datetime(a.get("endDate"))
    if start is None:
        raise ValueError("missing startDate")
    if end is not None and end < start:
        end = None
    duration = duration_to_s(a.get("duration"), (a.get("durationUnit") or "min").lower())
    if duration is None and end is not None:
        duration = (end - start).total_seconds()
    distance = distance_to_m(a.get("totalDistance"), (a.get("totalDistanceUnit") or "km").lower())
    energy = energy_to_kcal(a.get("totalEnergyBurned"), (a.get("totalEnergyBurnedUnit") or "kcal").lower())
    avg_hr: float | None = None
    max_hr: float | None = None
    external_id: str | None = None
    stat_distance: float | None = None
    stat_energy: float | None = None
    for child in elem:
        if child.tag == "WorkoutStatistics":
            kind = child.get("type", "")
            if kind.startswith(_HK_PREFIX):
                kind = kind[len(_HK_PREFIX) :]
            unit = (child.get("unit") or "").lower()
            if kind == "HeartRate":
                avg_hr = heart_rate(child.get("average"))
                max_hr = heart_rate(child.get("maximum"))
            elif kind in _STAT_DISTANCE:
                value = distance_to_m(child.get("sum"), unit or "km")
                if value is not None:
                    stat_distance = (stat_distance or 0.0) + value
            elif kind == "ActiveEnergyBurned":
                stat_energy = energy_to_kcal(child.get("sum"), unit or "kcal")
        elif child.tag == "MetadataEntry" and child.get("key") == "HKExternalUUID":
            external_id = _clean_id(child.get("value"))
    if distance is None:
        distance = stat_distance
    if energy is None:
        energy = stat_energy
    if external_id is None:
        external_id = stable_external_id(type_, start, end)
    source = a.get("sourceName")
    notes = f"source: {sanitize.for_model(source).strip()[:80]}" if source else None
    return HealthWorkout(
        external_id=external_id,
        type=type_,
        started_at=start,
        ended_at=end,
        duration_s=duration,
        distance_m=distance,
        energy_kcal=energy,
        avg_hr=avg_hr if avg_hr and 0 < avg_hr < 300 else None,
        max_hr=max_hr if max_hr and 0 < max_hr < 300 else None,
        notes=clean_notes(notes),
    )


def iter_export_workouts(xml_file: Any) -> Iterator[tuple[int, HealthWorkout | None, str | None]]:
    """Stream `Workout` elements from an open export.xml. Yields (index, workout, error).

    Memory stays flat: every top-level element (millions of `Record`s) is cleared once it ends
    and the root is emptied, so the tree never grows.
    """
    context = ET.iterparse(xml_file, events=("start", "end"))
    root: ET.Element | None = None
    depth = 0
    index = 0
    for event, elem in context:
        if event == "start":
            if root is None:
                root = elem
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            continue  # only direct children of HealthData matter; Workout children are read at Workout end
        if elem.tag == "Workout":
            try:
                yield index, _workout_from_element(elem), None
            except (ValueError, TypeError, ValidationError) as exc:
                yield index, None, _short_error(exc)
            index += 1
        elem.clear()
        if root is not None:
            root.clear()


def _find_export_member(archive: zipfile.ZipFile) -> str | None:
    # Apple ships apple_health_export/export.xml next to export_cda.xml (clinical records): only the former.
    for info in archive.infolist():
        if not info.is_dir() and Path(info.filename).name == "export.xml":
            return info.filename
    return None


def import_export_zip(zip_path: Path | str, db: Any) -> ImportResult:
    """Backfill from the Health app's 'Export All Health Data' archive. Never loads export.xml whole."""
    path = Path(zip_path)
    store = WorkoutStore(db)
    result = ImportResult(file=str(path))
    try:
        if path.suffix.lower() == ".xml":
            opener = path.open("rb")
            member = path.name
            archive = None
        else:
            archive = zipfile.ZipFile(path)
            member = _find_export_member(archive)
            if member is None:
                raise ValueError("no export.xml inside the archive")
            opener = archive.open(member)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        message = f"cannot open export: {type(exc).__name__}: {str(exc)[:200]}"
        result.errors.append(message)
        store.record_import(path.name, FORMAT_EXPORT_XML, 0, 0, message)
        log.warning("health export import failed file=%s error=%s", path.name, type(exc).__name__)
        return result
    result.format = FORMAT_EXPORT_XML
    skipped = 0
    try:
        with opener as fh:
            for index, workout, error in iter_export_workouts(fh):
                if workout is None:
                    skipped += 1
                    if len(result.errors) < 50:
                        result.errors.append(f"workout {index}: {error}")
                    continue
                outcome = store.upsert_health(
                    workout.external_id,
                    type=workout.type,
                    started_at=workout.started_at,
                    ended_at=workout.ended_at,
                    duration_s=workout.duration_s,
                    distance_m=workout.distance_m,
                    energy_kcal=workout.energy_kcal,
                    avg_hr=workout.avg_hr,
                    max_hr=workout.max_hr,
                    notes=workout.notes,
                )
                if outcome == "new":
                    result.new += 1
                else:
                    result.dup += 1
    except (ET.ParseError, OSError, zipfile.BadZipFile) as exc:
        message = f"export.xml unreadable: {type(exc).__name__}: {str(exc)[:200]}"
        result.errors.append(message)
        result.format = FORMAT_UNKNOWN
        store.record_import(path.name, FORMAT_EXPORT_XML, result.new, result.dup, message)
        log.warning("health export import failed file=%s member=%s error=%s", path.name, member, type(exc).__name__)
        return result
    finally:
        if archive is not None:
            archive.close()
    if skipped > 50:
        result.errors.append(f"... {skipped - 50} more workouts skipped")
    store.record_import(path.name, FORMAT_EXPORT_XML, result.new, result.dup, None)
    log.info("health export import file=%s new=%s dup=%s skipped=%s", path.name, result.new, result.dup, skipped)
    return result
