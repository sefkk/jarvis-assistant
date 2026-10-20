"""Google Maps Platform client with SQLite caching and a daily call limit.

APIs (all with one API key from the Keychain, `jarvis key set google_maps_api_key`):

- Places API (New), Text Search: `POST https://places.googleapis.com/v1/places:searchText`
  (field mask `places.id,places.displayName,places.formattedAddress,places.rating,
  places.userRatingCount,places.currentOpeningHours.openNow,places.location,places.googleMapsUri`).
- Places API (New), Place Details: `GET https://places.googleapis.com/v1/places/{id}`
  (field mask `id,displayName,formattedAddress,internationalPhoneNumber,nationalPhoneNumber,
  websiteUri,regularOpeningHours.weekdayDescriptions,currentOpeningHours.openNow,rating,
  userRatingCount,googleMapsUri,location`; never `reviews`).
- Routes API: `POST https://routes.googleapis.com/directions/v2:computeRoutes`.
- Geocoding API: `GET https://maps.googleapis.com/maps/api/geocode/json` (the key travels in the
  query string here: only the path is ever logged and the URL never reaches an exception text).

SKUs and pricing (2026, per-SKU free monthly allowance, no rollover; billing must be enabled on
the Google Cloud project): Essentials SKUs (Geocoding, Compute Routes Essentials, Place Details
Essentials) 10,000 free calls/month, Pro SKUs (Text Search Pro, Compute Routes Pro, Place
Details Pro) 5,000/month, Enterprise SKUs (Text Search Enterprise, Place Details Enterprise)
1,000/month; above that roughly $5 (Essentials) to $17-35 (Pro/Enterprise) per 1,000 calls.
The Places field masks decide the SKU: `id` is Essentials, displayName/formattedAddress/
location/googleMapsUri are Pro, and `rating`/`currentOpeningHours` are Enterprise. The tools
want the rating and the opening status, so Text Search and Place Details here bill at the
Enterprise SKU (the most expensive one); drop those two fields from the masks to fall back to
Pro. Routes with TRAFFIC_AWARE (driving) or transit are billed as Compute Routes Pro.

The user must enable exactly three APIs on the project: Places API (New), Routes API and
Geocoding API, and restrict the key to those three (API restrictions) so a leaked key cannot
be used for anything else. `maps.daily_call_limit` in config.toml caps the network calls per
local day (all APIs together, cached hits excluded); geocoding and place details are cached in
SQLite for 7 days, search results and routes are never cached.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.i18n import current_language
from jarvis.observability import logging_setup
from jarvis.observability.usage import start_of_local_day_utc
from jarvis.security import sanitize
from jarvis.utils import secrets
from jarvis.utils.timeutil import monotonic_ms, now_local

log = logging.getLogger("jarvis.maps")

SERVICE = "maps"  # external_calls.service
HTTP_TIMEOUT = 15.0
CACHE_TTL_S = 7 * 24 * 3600  # geocoding and place details
SEARCH_BIAS_RADIUS_M = 5000
MAX_TEXT = 200

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

TEXT_SEARCH_FIELDS = (
    "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,"
    "places.currentOpeningHours.openNow,places.location,places.googleMapsUri"
)
PLACE_DETAILS_FIELDS = (
    "id,displayName,formattedAddress,internationalPhoneNumber,nationalPhoneNumber,websiteUri,"
    "regularOpeningHours.weekdayDescriptions,currentOpeningHours.openNow,rating,userRatingCount,"
    "googleMapsUri,location"
)
ROUTES_FIELDS = (
    "routes.duration,routes.staticDuration,routes.distanceMeters,routes.description,routes.localizedValues,"
    "routes.legs.steps.travelMode,routes.legs.steps.staticDuration,routes.legs.steps.transitDetails,"
    "routes.legs.steps.navigationInstruction"
)

MODES = ("transit", "driving", "walking", "bicycling")
ROUTES_TRAVEL_MODE = {"transit": "TRANSIT", "driving": "DRIVE", "walking": "WALK", "bicycling": "BICYCLE"}
PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,300}$")

# Injectable for tests (httpx.MockTransport). None means the default transport.
_transport: httpx.BaseTransport | None = None


def language_code() -> str:
    """Names, addresses and instructions in the main language (`general.language`)."""
    return current_language()


# --- data shapes ------------------------------------------------------------------------------


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    address: str  # normalized (formatted) address


@dataclass
class Place:
    id: str
    name: str
    address: str
    rating: float | None
    rating_count: int | None
    open_now: bool | None  # None = unknown
    latitude: float | None
    longitude: float | None
    maps_url: str


@dataclass
class PlaceDetails(Place):
    phone: str = ""
    website: str = ""
    hours: list[str] = field(default_factory=list)  # weekday descriptions


@dataclass
class TransitStep:
    mode: str  # WALK or TRANSIT (anything else is shown as is)
    duration_s: int | None
    instruction: str = ""
    line: str = ""  # "M4 metro"
    headsign: str = ""
    from_stop: str = ""
    to_stop: str = ""
    stop_count: int | None = None
    departure: str = ""  # localized clock text
    arrival: str = ""


@dataclass
class Route:
    duration_s: int
    distance_m: int
    description: str
    duration_text: str
    distance_text: str
    steps: list[TransitStep] = field(default_factory=list)

    @property
    def transfers(self) -> int:
        rides = sum(1 for s in self.steps if s.mode == "TRANSIT")
        return max(0, rides - 1)


# --- helpers --------------------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    """A sanitized, single-line, bounded string for any text field the API returns."""
    if value is None:
        return ""
    text = " ".join(sanitize.for_model(str(value)).split())
    return text[:limit]


def _dict(node: Any) -> dict[str, Any]:
    return node if isinstance(node, dict) else {}


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> int | None:
    """Routes durations arrive as protobuf strings: "1234s" (or "1234.5s")."""
    if value is None:
        return None
    text = str(value).strip().rstrip("s")
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def normalize_address(address: str) -> str:
    return " ".join(address.casefold().split())


_COORDS_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """'41.0138,28.9497' -> (lat, lng) when both are in range; None for an address."""
    m = _COORDS_RE.match(text or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def waypoint(place: str) -> dict[str, Any]:
    """A Routes API waypoint: exact coordinates (the Current Location) go as latLng, so no geocoding
    guess is involved; anything else is an address string."""
    coords = parse_coordinates(place)
    if coords is not None:
        return {"location": {"latLng": {"latitude": coords[0], "longitude": coords[1]}}}
    return {"address": place}


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def format_distance(km: float) -> str:
    return f"{int(round(km * 1000))} m" if km < 1 else f"{km:.1f} km"


def leave_by(arrive_by: datetime, duration_s: int, buffer_min: int) -> datetime:
    """When to leave to arrive by `arrive_by` with `buffer_min` minutes of slack."""
    return arrive_by - timedelta(seconds=int(duration_s)) - timedelta(minutes=int(buffer_min))


def rfc3339_utc(dt: datetime) -> str:
    """Routes API timestamps: RFC 3339 in UTC with a Z suffix and no fractions."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_place_id(place_id: str) -> str:
    text = (place_id or "").strip()
    if not PLACE_ID_RE.match(text):
        raise ToolValidationError("place_id must be a Google place id (letters, digits, _ and -, 10-300 characters)")
    return text


# --- parsing --------------------------------------------------------------------------------------


def parse_place(node: Any) -> Place:
    node = _dict(node)
    location = _dict(node.get("location"))
    opening = _dict(node.get("currentOpeningHours"))
    open_now = opening.get("openNow")
    return Place(
        id=_text(node.get("id"), 300),
        name=_text(_dict(node.get("displayName")).get("text"), 120) or "?",
        address=_text(node.get("formattedAddress")),
        rating=_float(node.get("rating")),
        rating_count=_int(node.get("userRatingCount")),
        open_now=open_now if isinstance(open_now, bool) else None,
        latitude=_float(location.get("latitude")),
        longitude=_float(location.get("longitude")),
        maps_url=_text(node.get("googleMapsUri"), 300),
    )


def parse_place_details(node: Any) -> PlaceDetails:
    node = _dict(node)
    base = parse_place(node)
    hours = _dict(node.get("regularOpeningHours")).get("weekdayDescriptions")
    return PlaceDetails(
        **base.__dict__,
        phone=_text(node.get("internationalPhoneNumber") or node.get("nationalPhoneNumber"), 40),
        website=_text(node.get("websiteUri"), 300),
        hours=[_text(h, 80) for h in hours if _text(h, 80)][:7] if isinstance(hours, list) else [],
    )


def parse_step(node: Any) -> TransitStep:
    node = _dict(node)
    transit = _dict(node.get("transitDetails"))
    stops = _dict(transit.get("stopDetails"))
    localized = _dict(transit.get("localizedValues"))
    line = _dict(transit.get("transitLine"))
    vehicle = _text(_dict(line.get("vehicle")).get("type"), 30).lower().replace("_", " ")
    line_name = _text(line.get("nameShort"), 40) or _text(line.get("name"), 80)
    return TransitStep(
        mode=_text(node.get("travelMode"), 20).upper() or "?",
        duration_s=_seconds(node.get("staticDuration")),
        instruction=_text(_dict(node.get("navigationInstruction")).get("instructions"), 160),
        line=" ".join(p for p in (line_name, vehicle) if p),
        headsign=_text(transit.get("headsign"), 80),
        from_stop=_text(_dict(stops.get("departureStop")).get("name"), 80),
        to_stop=_text(_dict(stops.get("arrivalStop")).get("name"), 80),
        stop_count=_int(transit.get("stopCount")),
        departure=_text(_dict(_dict(localized.get("departureTime")).get("time")).get("text"), 20),
        arrival=_text(_dict(_dict(localized.get("arrivalTime")).get("time")).get("text"), 20),
    )


def parse_route(node: Any) -> Route:
    node = _dict(node)
    localized = _dict(node.get("localizedValues"))
    duration = _seconds(node.get("duration"))
    if duration is None:
        duration = _seconds(node.get("staticDuration"))
    steps: list[TransitStep] = []
    for leg in node.get("legs") if isinstance(node.get("legs"), list) else []:
        for step in _dict(leg).get("steps") if isinstance(_dict(leg).get("steps"), list) else []:
            steps.append(parse_step(step))
    return Route(
        duration_s=duration if duration is not None else 0,
        distance_m=_int(node.get("distanceMeters")) or 0,
        description=_text(node.get("description"), 160),
        duration_text=_text(_dict(localized.get("duration")).get("text"), 40),
        distance_text=_text(_dict(localized.get("distance")).get("text"), 40),
        steps=steps,
    )


# --- the client -----------------------------------------------------------------------------------


class MapsClient:
    """One instance per tool call. `db` may be None (no cache, no daily limit; the doctor never
    uses the network, so this only happens in scripts)."""

    def __init__(
        self,
        config: Any,
        db: Any = None,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._api_key = api_key
        self._transport = transport
        self._now = now or now_local
        self.requests_made = 0  # network requests this instance sent (tests and logs)

    # --- configuration ----------------------------------------------------------------------------

    @property
    def daily_call_limit(self) -> int:
        return int(getattr(self.config, "daily_call_limit", 0) or 0)

    def now(self) -> datetime:
        return self._now()

    def api_key(self) -> str:
        key = self._api_key or secrets.get_secret(secrets.GOOGLE_MAPS_API_KEY)
        if not key:
            raise PermissionMissingError(
                f"no Google Maps API key: run `jarvis key set {secrets.GOOGLE_MAPS_API_KEY}` (docs/integrations/maps.md)"
            )
        return key

    # --- accounting and the daily limit -------------------------------------------------------------

    def calls_today(self) -> int:
        """Network calls made today (cached hits excluded). 0 without a db."""
        if self.db is None:
            return 0
        return int(self.db.external_calls_since(SERVICE, start_of_local_day_utc()))

    def _check_limit(self) -> None:
        limit = self.daily_call_limit
        if limit <= 0 or self.db is None:
            return
        if self.calls_today() >= limit:
            raise ToolExecutionError(
                f"daily Google Maps call limit ({limit}) reached; raise maps.daily_call_limit in config.toml if this was expected"
            )

    def _record(self, operation: str, *, cached: bool, status: str, duration_ms: float | None) -> None:
        if self.db is not None:
            try:
                self.db.add_external_call(
                    service=SERVICE, operation=operation, units=1, cached=cached, status=status, duration_ms=duration_ms
                )
            except Exception:  # accounting must never fail a lookup
                log.exception("could not record the maps call")
        logging_setup.usage({"kind": "maps", "api": operation, "status": status, "duration_ms": duration_ms, "cached": cached})

    # --- HTTP -------------------------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        operation: str,
    ) -> dict[str, Any]:
        """One network call: limit check, request, status handling, accounting. Returns the JSON
        body (a dict). Raises ToolExecutionError with a short message that never contains the URL."""
        self._check_limit()
        transport = self._transport if self._transport is not None else _transport
        path = httpx.URL(url).path
        self.requests_made += 1
        started = monotonic_ms()
        status = "error"
        try:
            try:
                with httpx.Client(timeout=HTTP_TIMEOUT, transport=transport) as client:
                    response = client.request(method, url, params=params, json=json_body, headers=headers or {})
            except httpx.HTTPError as exc:
                status = f"network:{type(exc).__name__}"
                log.warning("maps %s request to %s failed: %s", operation, path, type(exc).__name__)
                raise ToolExecutionError(f"Google Maps request failed: {type(exc).__name__}") from None
            status = f"http_{response.status_code}"
            if response.status_code in (401, 403):
                raise ToolExecutionError("the Maps key is restricted or the API is not enabled (see docs/integrations/maps.md)")
            if response.status_code == 429:
                raise ToolExecutionError("Google Maps quota exceeded (status 429); try again later")
            if response.status_code != 200:
                log.warning("maps %s request to %s failed with status %s", operation, path, response.status_code)
                raise ToolExecutionError(f"Google Maps request failed with status {response.status_code}")
            try:
                payload = response.json()
            except ValueError:
                status = "bad_json"
                raise ToolExecutionError("Google Maps response was not JSON") from None
            if not isinstance(payload, dict):
                status = "bad_json"
                raise ToolExecutionError("Google Maps response was not understood")
            status = _geocode_status(payload) if operation == "geocode" else "ok"
            if status == "REQUEST_DENIED":
                raise ToolExecutionError("the Maps key is restricted or the API is not enabled (see docs/integrations/maps.md)")
            if status == "OVER_QUERY_LIMIT" or status == "OVER_DAILY_LIMIT":
                raise ToolExecutionError("Google Maps quota exceeded (OVER_QUERY_LIMIT); try again later")
            if status not in ("ok", "ZERO_RESULTS"):
                raise ToolExecutionError(f"Google Maps geocoding failed: {status}")
            return payload
        finally:
            # ZERO_RESULTS is a successful (billed) call; it counts toward the daily limit.
            final = "ok" if status in ("ok", "ZERO_RESULTS") else status
            self._record(operation, cached=False, status=final, duration_ms=round(monotonic_ms() - started, 1))

    def _places_headers(self, field_mask: str) -> dict[str, str]:
        return {"X-Goog-Api-Key": self.api_key(), "X-Goog-FieldMask": field_mask, "Content-Type": "application/json"}

    # --- cache ----------------------------------------------------------------------------------------

    def _cache_get(self, key: str, operation: str) -> dict[str, Any] | None:
        if self.db is None:
            return None
        raw = self.db.cache_get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("maps cache entry %s is corrupt; refetching", key)
            return None
        if not isinstance(data, dict):
            return None
        self._record(operation, cached=True, status="ok", duration_ms=0.0)
        return data

    def _cache_set(self, key: str, data: dict[str, Any]) -> None:
        if self.db is not None:
            self.db.cache_set(key, json.dumps(data, ensure_ascii=False), CACHE_TTL_S)

    # --- geocoding ---------------------------------------------------------------------------------------

    def geocode(self, address: str) -> GeoPoint | None:
        """Coordinates and the normalized address, cached 7 days. None when nothing matches."""
        address = " ".join((address or "").split())
        if not address:
            raise ToolValidationError("address must not be empty")
        key = f"maps:geocode:{normalize_address(address)}"
        hit = self._cache_get(key, "geocode")
        if hit is not None:
            return _geopoint_from_cache(hit)
        payload = self._request(
            "GET", GEOCODE_URL, params={"address": address, "key": self.api_key(), "language": language_code()}, operation="geocode"
        )
        results = payload.get("results")
        first = _dict(results[0]) if isinstance(results, list) and results else {}
        location = _dict(_dict(first.get("geometry")).get("location"))
        lat, lng = _float(location.get("lat")), _float(location.get("lng"))
        if lat is None or lng is None:
            return None  # ZERO_RESULTS; not cached so a typo fixed later is retried
        point = GeoPoint(latitude=lat, longitude=lng, address=_text(first.get("formatted_address")))
        self._cache_set(key, {"latitude": lat, "longitude": lng, "address": point.address})
        return point

    # --- places ---------------------------------------------------------------------------------------------

    def search_places(self, query: str, bias: GeoPoint | None, *, open_now: bool = False, max_results: int = 5) -> list[Place]:
        """Text Search, never cached (results change with the hour)."""
        body: dict[str, Any] = {"textQuery": query, "languageCode": language_code(), "pageSize": max(1, min(int(max_results), 20))}
        if bias is not None:
            body["locationBias"] = {
                "circle": {"center": {"latitude": bias.latitude, "longitude": bias.longitude}, "radius": SEARCH_BIAS_RADIUS_M}
            }
        if open_now:
            body["openNow"] = True
        payload = self._request(
            "POST", TEXT_SEARCH_URL, json_body=body, headers=self._places_headers(TEXT_SEARCH_FIELDS), operation="places_text_search"
        )
        places = payload.get("places")
        if not isinstance(places, list):
            return []
        return [parse_place(p) for p in places[: body["pageSize"]]]

    def place_details(self, place_id: str) -> PlaceDetails:
        """Place Details, cached 7 days."""
        place_id = validate_place_id(place_id)
        key = f"maps:place:{place_id}"
        hit = self._cache_get(key, "place_details")
        if hit is None:
            hit = self._request(
                "GET", PLACE_DETAILS_URL + place_id, headers=self._places_headers(PLACE_DETAILS_FIELDS), operation="place_details"
            )
            self._cache_set(key, hit)
        return parse_place_details(hit)

    # --- routes ----------------------------------------------------------------------------------------------

    def route(
        self,
        origin: str,
        destination: str,
        mode: str,
        *,
        depart_at: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> Route | None:
        """One route (never cached). `arrive_by` is only sent for transit (the Routes API ignores it
        otherwise); callers estimate other modes from a departure-now route. None = no route."""
        if mode not in ROUTES_TRAVEL_MODE:
            raise ToolValidationError(f"mode must be one of {', '.join(MODES)}")
        if depart_at is not None and arrive_by is not None:
            raise ToolValidationError("give either depart_at or arrive_by, not both")
        body: dict[str, Any] = {
            "origin": waypoint(origin),
            "destination": waypoint(destination),
            "travelMode": ROUTES_TRAVEL_MODE[mode],
            "languageCode": language_code(),
            "units": "METRIC",
        }
        if mode == "driving":
            body["routingPreference"] = "TRAFFIC_AWARE"
        if depart_at is not None:
            if mode != "transit" and depart_at < self.now():
                raise ToolValidationError("depart_at is in the past; only transit routes accept past departure times")
            body["departureTime"] = rfc3339_utc(depart_at)
        elif arrive_by is not None and mode == "transit":
            body["arrivalTime"] = rfc3339_utc(arrive_by)
        payload = self._request(
            "POST",
            ROUTES_URL,
            json_body=body,
            headers={"X-Goog-Api-Key": self.api_key(), "X-Goog-FieldMask": ROUTES_FIELDS, "Content-Type": "application/json"},
            operation="routes",
        )
        routes = payload.get("routes")
        if not isinstance(routes, list) or not routes:
            return None
        return parse_route(routes[0])


def _geocode_status(payload: dict[str, Any]) -> str:
    status = _text(payload.get("status"), 40).upper()
    return "ok" if status == "OK" else (status or "UNKNOWN_STATUS")


def _geopoint_from_cache(data: dict[str, Any]) -> GeoPoint | None:
    lat, lng = _float(data.get("latitude")), _float(data.get("longitude"))
    if lat is None or lng is None:
        return None
    return GeoPoint(latitude=lat, longitude=lng, address=_text(data.get("address")))
