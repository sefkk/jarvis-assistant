"""The `Location` record, the parsers for what the sources return, and the precision rules.

Everything that comes from a Shortcut, CoreLocationCLI or an IP service is validated here:
coordinates must be finite and inside their ranges (and not the 0,0 null island), every text
field is stripped of control and invisible characters and of markup brackets, and capped. The
label at a precision (`city` | `neighborhood` | `street`) is what may enter profile.md; the
coordinates never do.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from jarvis.security import sanitize

Precision = Literal["city", "neighborhood", "street"]
PRECISIONS: tuple[str, ...] = ("city", "neighborhood", "street")
SOURCES: tuple[str, ...] = ("shortcut", "corelocation", "ip", "telegram")
MAX_FIELD_CHARS = 120
MAX_INPUT_CHARS = 20_000  # a Shortcut or a web service answer longer than this is not a location


def _normalize_key(key: Any) -> str:
    return re.sub(r"[\s_\-]", "", str(key).strip().casefold())


_RAW_KEY_ALIASES: dict[str, str] = {
    "lat": "latitude",
    "latitude": "latitude",
    "enlem": "latitude",
    "lon": "longitude",
    "lng": "longitude",
    "long": "longitude",
    "longitude": "longitude",
    "boylam": "longitude",
    "street": "street",
    "sokak": "street",
    "cadde": "street",
    "adres": "street",
    "address": "street",
    "thoroughfare": "street",
    "neighborhood": "neighborhood",
    "neighbourhood": "neighborhood",
    "mahalle": "neighborhood",
    "sublocality": "neighborhood",
    "semt": "neighborhood",
    "city": "city",
    "sehir": "city",
    "şehir": "city",
    "ilce": "city",
    "ilçe": "city",
    "locality": "city",
    "town": "city",
    "region": "region",
    "state": "region",
    "il": "region",
    "eyalet": "region",
    "administrativearea": "region",
    "province": "region",
    "country": "country",
    "ulke": "country",
    "ülke": "country",
    "accuracy": "accuracy_m",
    "accuracy_m": "accuracy_m",
    "horizontalaccuracy": "accuracy_m",
    "h_accuracy": "accuracy_m",
}
_KEY_ALIASES: dict[str, str] = {_normalize_key(k): v for k, v in _RAW_KEY_ALIASES.items()}
_INVISIBLE_RE = sanitize._INVISIBLE_RE
_MARKUP_RE = re.compile(r"[<>`\[\]{}|\\]")
_PAIR_RE = re.compile(r"(?<![\d.,-])(-?\d{1,3}(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:[.,]\d+)?)(?![\d.,])")


# --- validation and cleaning --------------------------------------------------------------------------


def valid_coordinates(latitude: Any, longitude: Any) -> bool:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)


def parse_number(value: Any) -> float | None:
    """41.0138, "41.0138", "41,0138" (a Turkish-locale Shortcut writes the decimal comma) -> float;
    None for anything that is not one plain number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value or "").strip().replace("°", "")
    if not text:
        return None
    if "," in text and "." not in text and text.count(",") == 1:
        text = text.replace(",", ".")
    if not re.fullmatch(r"[-+]?\d{1,3}(\.\d+)?", text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def clean_text(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """One address field as plain text: no escape sequences, control or invisible characters, no
    markup brackets (profile.md and the tool results are read by the model), collapsed whitespace,
    capped at `limit` characters."""
    if value is None or isinstance(value, bool):
        return ""
    text = sanitize.strip_controls(sanitize.strip_ansi(str(value)))
    text = _INVISIBLE_RE.sub("", text)
    text = _MARKUP_RE.sub(" ", text)
    text = " ".join(text.split())
    return text[:limit].rstrip(" ,;")


# --- the record ---------------------------------------------------------------------------------------


@dataclass
class Location:
    latitude: float
    longitude: float
    source: str  # shortcut | corelocation | ip | telegram
    updated_at: str  # UTC, "YYYY-MM-DDTHH:MM:SS.mmmZ" (timeutil.now_iso)
    street: str = ""
    neighborhood: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    approximate: bool = False  # IP geolocation: city level at best, wrong behind a VPN
    accuracy_m: float | None = None

    def __post_init__(self) -> None:
        if not valid_coordinates(self.latitude, self.longitude):
            raise ValueError("invalid coordinates")
        if self.source not in SOURCES:
            raise ValueError(f"unknown location source {self.source!r}")
        self.latitude, self.longitude = float(self.latitude), float(self.longitude)
        for name in ("street", "neighborhood", "city", "region", "country"):
            setattr(self, name, clean_text(getattr(self, name)))
        if self.accuracy_m is not None:
            try:
                self.accuracy_m = max(0.0, float(self.accuracy_m))
            except (TypeError, ValueError):
                self.accuracy_m = None

    # -- time --

    def updated_datetime(self) -> datetime:
        return parse_iso(self.updated_at)

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.updated_datetime()).total_seconds())

    # -- text --

    @property
    def place(self) -> str:
        """The town-level name: the city, else the region, else the country."""
        return self.city or self.region or self.country

    def label(self, precision: str) -> str:
        """The text that may stand in profile.md and in tool results. An approximate (IP) location
        is always city level, whatever the precision asks for. Empty when no address is known."""
        if precision not in PRECISIONS:
            raise ValueError(f"unknown precision {precision!r}")
        city = self.place
        country = self.country or (self.region if self.region != city else "")  # a Shortcut may hand the country over as "Region"
        if self.approximate or precision == "city":
            parts = [city, country]
        elif precision == "neighborhood":
            if self.neighborhood:
                parts = [city, self.neighborhood]
            elif self.region and self.region != city:
                parts = [city, self.region]
            else:
                parts = [city, self.country]
        else:
            parts = [self.street, self.neighborhood, city]
            if not self.street and self.region and self.region != city:
                parts.append(self.region)
        seen: list[str] = []
        for part in parts:
            if part and part not in seen:
                seen.append(part)
        return ", ".join(seen)

    def coordinates_text(self, digits: int = 5) -> str:
        return f"{self.latitude:.{digits}f},{self.longitude:.{digits}f}"

    # -- persistence --

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "Location | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                latitude=float(data["latitude"]),
                longitude=float(data["longitude"]),
                source=str(data.get("source") or ""),
                updated_at=str(data.get("updated_at") or ""),
                street=str(data.get("street") or ""),
                neighborhood=str(data.get("neighborhood") or ""),
                city=str(data.get("city") or ""),
                region=str(data.get("region") or ""),
                country=str(data.get("country") or ""),
                approximate=bool(data.get("approximate", False)),
                accuracy_m=data.get("accuracy_m"),
            )
        except (KeyError, TypeError, ValueError):
            return None


def parse_iso(text: str) -> datetime:
    """A UTC timestamp as written by timeutil.now_iso (trailing Z) -> aware datetime; an unreadable
    value counts as very old, never as fresh."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# --- parsing what the sources return -------------------------------------------------------------------


def fields_from_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Pick the location fields out of any dict with recognizable keys (JSON from a Shortcut,
    CoreLocationCLI's -json output, an IP service's answer). Unknown keys are ignored."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        field = _KEY_ALIASES.get(_normalize_key(key))
        if field is None:
            continue
        if field in ("latitude", "longitude", "accuracy_m"):
            number = parse_number(value)
            if number is not None and field not in out:
                out[field] = number
        elif field not in out or not out[field]:
            text = clean_text(value)
            if text:
                out[field] = text
    # ipinfo.io: "loc": "41.0138,28.9497"
    loc = data.get("loc")
    if ("latitude" not in out or "longitude" not in out) and isinstance(loc, str):
        pair = _PAIR_RE.search(loc)
        if pair:
            lat, lon = parse_number(pair.group(1)), parse_number(pair.group(2))
            if lat is not None and lon is not None:
                out["latitude"], out["longitude"] = lat, lon
    # CoreLocationCLI: thoroughfare + subThoroughfare
    if "street" in out and data.get("subThoroughfare"):
        number = clean_text(data.get("subThoroughfare"), 20)
        if number and number not in out["street"]:
            out["street"] = f"{out['street']} {number}"
    # ipapi.co names the country twice; prefer the readable one
    name = data.get("country_name")
    if isinstance(name, str) and clean_text(name):
        out["country"] = clean_text(name)
    return out


def parse_text_output(text: str) -> dict[str, Any]:
    """The Shortcut's text output: a JSON object, or `key: value` lines (`lat: 41.0138`,
    `city: Kadıköy`), or at least one `lat, lon` pair somewhere with the remaining lines as the
    address. Returns the recognized fields (possibly empty)."""
    text = sanitize.strip_controls(sanitize.strip_ansi(text or ""))[:MAX_INPUT_CHARS].strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return fields_from_mapping(data)
    pairs: dict[str, Any] = {}
    leftovers: list[str] = []
    for line in text.splitlines():
        line = line.strip().strip(",")
        if not line:
            continue
        m = re.match(r"^([^:=]{1,40})[:=]\s*(.*)$", line)
        if m and _KEY_ALIASES.get(_normalize_key(m.group(1))):
            pairs[m.group(1).strip()] = m.group(2).strip().strip('"')
        else:
            leftovers.append(line)
    out = fields_from_mapping(pairs)
    if "latitude" not in out or "longitude" not in out:
        for i, line in enumerate(list(leftovers)):
            pair = _PAIR_RE.search(line)
            if pair:
                lat, lon = parse_number(pair.group(1)), parse_number(pair.group(2))
                if lat is not None and lon is not None and valid_coordinates(lat, lon):
                    out["latitude"], out["longitude"] = lat, lon
                    leftovers.pop(i)
                    break
    if leftovers and not any(out.get(k) for k in ("street", "neighborhood", "city")):
        # a plain address block: first line street, last line country, the rest the town
        lines = [clean_text(l) for l in leftovers if clean_text(l)]
        if len(lines) == 1:
            out["city"] = lines[0]
        elif lines:
            out["street"] = lines[0]
            out["country"] = lines[-1] if len(lines) > 2 else ""
            out["city"] = lines[1] if len(lines) > 2 else lines[-1]
    return out


def location_from_fields(fields: dict[str, Any], *, source: str, updated_at: str, approximate: bool = False) -> Location | None:
    """A validated Location, or None when the coordinates are missing or out of range."""
    lat, lon = fields.get("latitude"), fields.get("longitude")
    if not valid_coordinates(lat, lon):
        return None
    try:
        return Location(
            latitude=float(lat),
            longitude=float(lon),
            source=source,
            updated_at=updated_at,
            street=str(fields.get("street") or ""),
            neighborhood=str(fields.get("neighborhood") or ""),
            city=str(fields.get("city") or ""),
            region=str(fields.get("region") or ""),
            country=str(fields.get("country") or ""),
            approximate=approximate,
            accuracy_m=fields.get("accuracy_m"),
        )
    except ValueError:
        return None
