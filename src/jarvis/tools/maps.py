"""Google Maps tools: search_places, place_details, travel_time, geocode, open_in_maps,
plus `health_checks` for the doctor and `usage_report` for `jarvis usage`.

All five are SAFE. `search_places` and `place_details` return third-party text (names, addresses,
opening hours) and therefore taint the turn; `travel_time`, `geocode` and `open_in_maps` do not.

Routes start from where the user is (`_MapsTool.resolve_origin`): an empty `origin` is
the fresh Current Location as exact coordinates; an `origin` the user did not name (the model copying the
home `Location` or the `Current Location` label from the profile) is dropped; the home Location is the
origin only for "evden". With no fresh location `travel_time` asks instead of guessing and `open_in_maps`
opens the route with the destination only, so the Maps app starts from the device's own live location.
`open_in_maps` is the only one that acts on the Mac (it opens Apple Maps or Google Maps through
`open <url>` as argv) and is therefore not in the Telegram remote profile and never available to
the delegate. The HTTP side, caching, the daily call limit and the pricing notes live in
`jarvis.maps.client` (read its docstring before touching the field masks: they decide the SKU).

The user must enable Places API (New), Routes API and Geocoding API on the Google Cloud project,
restrict the key to exactly those three APIs, and have billing enabled (docs/integrations/maps.md).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.location import service as location_service
from jarvis.maps.client import (
    MODES,
    SERVICE,
    GeoPoint,
    MapsClient,
    Place,
    PlaceDetails,
    Route,
    TransitStep,
    format_distance,
    haversine_km,
    leave_by,
    normalize_address,
    parse_coordinates,
)
from jarvis.observability.db import Database
from jarvis.observability.usage import start_of_local_day_utc
from jarvis.tools.base import RiskLevel, Tool, ToolContext, ToolResult, did_you_mean
from jarvis.utils import secrets
from jarvis.utils.timeutil import format_local, parse_datetime
from jarvis.voice import names

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.maps")

Mode = Literal["transit", "driving", "walking", "bicycling"]
MAX_QUERY = 200
MAX_ADDRESS = 300
OPEN_TIMEOUT_S = 10
APPLE_DIRFLG = {"transit": "r", "driving": "d", "walking": "w", "bicycling": "c"}
APP_NAMES = {"apple": "Apple Maps", "google": "Google Maps"}


def open_url(url: str) -> None:
    """`open <url>` as argv (never a shell). Tests replace this."""
    subprocess.run(["open", url], check=False, timeout=OPEN_TIMEOUT_S)


# --- doctor and usage ----------------------------------------------------------------------------


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Key present or not (optional either way) and today's call count against the limit. No
    network call: a restricted key shows up on first use with a clear message. Never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    try:
        key = secrets.get_secret(secrets.GOOGLE_MAPS_API_KEY)
    except Exception as exc:  # keyring problems must not break the doctor
        checks.append(Check("Google Maps key", False, f"could not read the Keychain: {type(exc).__name__}"))
        key = None
    else:
        if key:
            checks.append(
                Check("Google Maps key", True, "found in the Keychain (Places API (New), Routes API, Geocoding API must be enabled)")
            )
        else:
            checks.append(
                Check(
                    "Google Maps key",
                    True,
                    f"optional, not configured: run `jarvis key set {secrets.GOOGLE_MAPS_API_KEY}` (docs/integrations/maps.md)",
                )
            )
    limit = int(getattr(config.maps, "daily_call_limit", 0) or 0)
    try:
        db = Database(paths.db_file)
        try:
            since = start_of_local_day_utc()
            network = db.external_calls_since(SERVICE, since)
            cached = db.external_calls_since(SERVICE, since, include_cached=True) - network
        finally:
            db.close()
    except Exception as exc:
        checks.append(Check("Google Maps usage", False, f"could not read the call log: {type(exc).__name__}: {exc}"))
        return checks
    limit_text = f"of {limit}" if limit > 0 else "(no daily limit set)"
    checks.append(
        Check("Google Maps usage", limit <= 0 or network < limit, f"{network} network call(s) {limit_text} today, {cached} cached hit(s)")
    )
    return checks


def usage_report(db: Database, days: int = 7, *, daily_call_limit: int | None = None) -> str:
    """A "Google Maps" section for `jarvis usage`: calls per day and API, network vs cached, and
    today's count. Empty string when there is nothing to report."""
    from jarvis.observability.reports import _since, _table  # noqa: PLC0415

    rows = db.conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, operation, SUM(cached = 0 AND status = 'ok') AS network,"
        " SUM(cached = 1) AS cached, SUM(cached = 0 AND status != 'ok') AS failed"
        " FROM external_calls WHERE service = ? AND created_at >= ? GROUP BY day, operation ORDER BY day, operation",
        (SERVICE, _since(days)),
    ).fetchall()
    if not rows:
        return ""
    table = [[r["day"], r["operation"], r["network"] or 0, r["cached"] or 0, r["failed"] or 0] for r in rows]
    network_total = sum(int(r["network"] or 0) for r in rows)
    cached_total = sum(int(r["cached"] or 0) for r in rows)
    today = db.external_calls_since(SERVICE, start_of_local_day_utc())
    today_text = f"Today: {today} network call(s)"
    if daily_call_limit:
        today_text += f" of {daily_call_limit} (maps.daily_call_limit)"
    return "\n".join(
        [
            f"Google Maps (last {days} day(s)):",
            _table(table, ["day", "api", "network", "cached", "failed"]),
            f"Total: {network_total} network call(s), {cached_total} cached hit(s). {today_text}.",
        ]
    )


# --- shared ----------------------------------------------------------------------------------------


class _MapsTool(Tool):
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = True

    def __init__(self, ctx: ToolContext | None = None, client: MapsClient | None = None) -> None:
        super().__init__(ctx)
        self._client = client
        self._own_db: Database | None = None

    @property
    def client(self) -> MapsClient:
        if self._client is not None:
            return self._client
        ctx = self.require_ctx()
        db = None
        if ctx.runtime is not None and getattr(ctx.runtime, "db", None) is not None:
            db = ctx.runtime.db
        else:  # no Assistant around (evals, scripts): open the project db so cache and limit still work
            if self._own_db is None:
                self._own_db = Database(ctx.paths.db_file)
            db = self._own_db
        self._client = MapsClient(ctx.config.maps, db)
        return self._client

    def profile_location(self) -> str | None:
        """The user's home from profile.md (the permanent `Location` line; the fallback origin/near)."""
        return location_service.home_location(self.require_ctx().paths)

    def default_place(self, what: str) -> location_service.Place:
        """The default `origin` / `near`: the Current Location (local turns: the
        Mac's, refreshed when stale; remote turns: only a location shared on Telegram, never the Mac's),
        else the home Location. Raises ToolValidationError when neither is known."""
        return location_service.resolve_default_place(self.require_ctx(), order=("current", "home"), what=what)

    def resolve_origin(self, origin: str | None) -> tuple[str | None, str, list[str]]:
        """The origin rule for a route: (value for the API or None, label or the text to
        answer with when None, notes for the model). An origin the user did not name is dropped
        (`location_service.check_origin`); an empty origin is the fresh Current Location as exact coordinates;
        when none is known the value is None and the label is the question to relay, never the home Location."""
        check = location_service.check_origin(self.require_ctx(), origin)
        notes = [check.note] if check.note else []
        if check.origin is None:
            place = location_service.resolve_origin(self.require_ctx())
            if place.kind == "ask" or place.point is None:
                return None, place.ask or location_service.ASK_FOR_LOCATION, notes
            return self.coordinates(place), place.label, notes
        if location_service.is_last_known_alias(check.origin):
            place = location_service.last_known_place(self.require_ctx())
            if place is None or place.point is None:
                return None, location_service.ASK_FOR_ORIGIN.format(detail=" (no last known location either)", last_known=""), notes
            return self.coordinates(place), place.label, notes
        value, label = self.resolve_named(check.origin, "origin")
        return value, label, notes

    @staticmethod
    def coordinates(place: location_service.Place) -> str:
        """Exact coordinates as a latLng waypoint / URL origin; never part of a label shown to the model."""
        assert place.point is not None
        return f"{place.point[0]:.6f},{place.point[1]:.6f}"

    def resolve_named(self, text: str, what: str) -> tuple[str, str]:
        """(value for the API, label for the result) of a place the model named; 'home' / 'ev' / 'eve'
        become the profile's Location line."""
        if location_service.is_home_alias(text):
            home = self.profile_location()
            if home is None:
                raise ToolValidationError(f"{what} is 'home' but profile.md has no Location line; run `jarvis setup`")
            return home, f"{home} (home)"
        return text, text

    def misheard(self, fields: dict[str, str | None], confirmed: bool) -> ToolResult | None:
        """A spoken request naming a place that is close to, but not, a known one (earlier searches and
        routes, profile, calendar locations, corrections): ask "Did you mean 'X'?" before paying for an
        API call with a misheard name. None when there is nothing to ask."""
        if confirmed or not names.spoken(self.ctx):
            return None
        values = {k: v for k, v in fields.items() if v and not location_service.is_home_alias(v)}
        if not values:
            return None
        known = names.known_names(self.ctx)
        for field_name, value in values.items():
            suggestion = names.suggest(value, known)
            if suggestion is None:
                continue
            corrected = value.replace(suggestion.heard, suggestion.term) if suggestion.heard in value else suggestion.term
            return ToolResult(
                f"{field_name} {value!r} is close to the known place {suggestion.term!r}. "
                + did_you_mean(suggestion.term, retry=f"call {self.name} again with {field_name}={corrected!r} and confirmed=true")
            )
        return None

    def notice(self, text: str) -> None:
        runtime = self.ctx.runtime if self.ctx is not None else None
        sink = getattr(runtime, "sink", None)
        if sink is not None:
            sink.notice(text)


def _clean(value: str | None, what: str, limit: int) -> str | None:
    text = " ".join((value or "").split())
    if len(text) > limit:
        raise ToolValidationError(f"{what} is too long (max {limit} characters)")
    return text or None


def _minutes(seconds: int) -> str:
    minutes = max(1, (int(seconds) + 59) // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min"


# --- formatting --------------------------------------------------------------------------------------


def _rating(place: Place) -> str:
    if place.rating is None:
        return "no rating"
    count = f" ({place.rating_count} reviews)" if place.rating_count is not None else ""
    return f"rating {place.rating:.1f}{count}"


def _open_state(place: Place) -> str:
    return {True: "open now", False: "closed now"}.get(place.open_now, "opening status unknown")


def format_place_line(index: int, place: Place, origin: GeoPoint | None) -> str:
    parts = [place.address or "address unknown", _rating(place), _open_state(place)]
    if origin is not None and place.latitude is not None and place.longitude is not None:
        km = haversine_km(origin.latitude, origin.longitude, place.latitude, place.longitude)
        parts.append(f"{format_distance(km)} away")
    line = f"{index}. {place.name} - " + "; ".join(parts)
    if place.maps_url:
        line += f"\n   link: {place.maps_url}"
    if place.id:
        line += f"\n   place id: {place.id}"
    return line


def format_place_details(place: PlaceDetails) -> str:
    lines = [f"{place.name} - {place.address or 'address unknown'}", f"{_rating(place)}; {_open_state(place)}"]
    lines.append("Hours: " + ("; ".join(place.hours) if place.hours else "unknown"))
    lines.append(f"Phone: {place.phone or 'unknown'}")
    lines.append(f"Website: {place.website or 'unknown'}")
    if place.maps_url:
        lines.append(f"Link: {place.maps_url}")
    return "\n".join(lines)


def transit_step_lines(steps: list[TransitStep]) -> list[str]:
    """Consecutive walking steps are folded into one line; rides name the line, stops and headsign."""
    lines: list[str] = []
    walk_s = 0
    for step in steps:
        if step.mode == "TRANSIT":
            if walk_s:
                lines.append(f"walk {_minutes(walk_s)} to {step.from_stop or 'the stop'}")
                walk_s = 0
            stops = f" ({step.stop_count} stop{'s' if step.stop_count != 1 else ''})" if step.stop_count is not None else ""
            towards = f" towards {step.headsign}" if step.headsign else ""
            times = f", {step.departure}-{step.arrival}" if step.departure and step.arrival else ""
            lines.append(f"{step.line or 'transit'} from {step.from_stop or '?'} to {step.to_stop or '?'}{stops}{towards}{times}")
        elif step.mode == "WALK":
            walk_s += step.duration_s or 0
        else:
            lines.append(f"{step.mode.lower()}: {step.instruction or '-'}")
    if walk_s:
        lines.append(f"walk {_minutes(walk_s)}" + (" to the destination" if lines else ""))
    return lines


def format_route(
    origin: str,
    destination: str,
    mode: str,
    route: Route,
    *,
    depart_at: datetime | None,
    arrive_by: datetime | None,
    buffer_min: int,
) -> str:
    duration = route.duration_text or _minutes(route.duration_s)
    distance = route.distance_text or format_distance(route.distance_m / 1000)
    head = f"{origin} -> {destination} by {mode}: {duration} ({distance})"
    if route.description:
        head += f" via {route.description}"
    lines = [head]
    if depart_at is not None:
        lines.append(f"Departing {format_local(depart_at)}")
    if mode == "transit" and route.steps:
        lines.append("Steps: " + "; ".join(transit_step_lines(route.steps)))
        lines.append(f"Transfers: {route.transfers}")
    if arrive_by is not None:
        when = leave_by(arrive_by, route.duration_s, buffer_min)
        same_day = when.astimezone(arrive_by.tzinfo).date() == arrive_by.date()
        leave_text = format_local(when, with_date=not same_day)
        note = "" if mode == "transit" else " (estimate based on current conditions)"
        lines.append(
            f"Leave by {leave_text} (travel {_minutes(route.duration_s)} + {buffer_min} min buffer) "
            f"to arrive by {format_local(arrive_by)}{note}"
        )
    return "\n".join(lines)


# --- search_places -----------------------------------------------------------------------------------


class SearchPlacesInput(BaseModel):
    query: str = Field(
        min_length=1, max_length=MAX_QUERY, description="What to look for, e.g. 'open pharmacy' or 'good Turkish restaurant in Amsterdam'."
    )
    near: str | None = Field(
        default=None,
        max_length=MAX_ADDRESS,
        description="Place or address to search around; 'home' for the user's home. Default: the user's Current Location (where they are now), else their home.",
    )
    open_now: bool = Field(default=False, description="Only places that are open right now.")
    max_results: int = Field(default=5, ge=1, le=10, description="How many places (1-10).")
    confirmed: bool = Field(
        default=False,
        description="True only after the user said yes to a \"Did you mean 'X'?\" question from this tool: use the names as given without asking again.",
    )


class SearchPlaces(_MapsTool):
    name = "search_places"
    description = (
        "Find places with Google Maps: name, address, rating, open/closed now, distance from `near` and a "
        "Google Maps link plus a place id for place_details. Costs a paid API call."
    )
    input_model = SearchPlacesInput
    taints = True
    taint_source = "maps"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SearchPlacesInput)
        query = _clean(args.query, "query", MAX_QUERY)
        if query is None:
            raise ToolValidationError("query must not be empty")
        near = _clean(args.near, "near", MAX_ADDRESS)
        question = self.misheard({"query": query, "near": near}, args.confirmed)
        if question is not None:
            return question
        note = ""
        center: GeoPoint | None = None
        where_label: str | None = None
        if near is None:
            try:
                place = self.default_place("near")
            except ToolValidationError:
                note = "No `near` given and no location in profile.md (and no current location): results are not biased to a place."
                place = None
            if place is not None and place.kind == "ask":
                return ToolResult(place.ask or location_service.ASK_FOR_LOCATION)
            if place is not None and place.point is not None:
                center = GeoPoint(latitude=place.point[0], longitude=place.point[1], address=place.label)
                where_label = place.label
            elif place is not None:
                near, where_label = place.query, place.label
        else:
            near, label = self.resolve_named(near, "near")
            where_label = label if label != near else None  # a named place keeps Google's formatted address below
        if center is None and near is not None:
            center = self.client.geocode(near)
            if center is None:
                raise ToolExecutionError(f"could not locate {near!r}; give a more specific `near`")
        places = self.client.search_places(query, center, open_now=args.open_now, max_results=args.max_results)
        if not places:
            where = f" near {where_label or near}" if (where_label or near) else ""
            return ToolResult(f"No places found for {query!r}{where}" + (" that are open now." if args.open_now else "."))
        where = f" near {where_label or center.address or near}" if center is not None else ""
        lines = [f"{len(places)} place(s) for {query!r}{where}" + (" (open now)" if args.open_now else "") + ":"]
        if note:
            lines.append(note)
        lines += [format_place_line(i, p, center) for i, p in enumerate(places, 1)]
        return ToolResult("\n".join(lines))


# --- place_details ------------------------------------------------------------------------------------


class PlaceDetailsInput(BaseModel):
    place_id: str = Field(min_length=10, max_length=300, description="The place id shown by search_places.")


class PlaceDetailsTool(_MapsTool):
    name = "place_details"
    description = "Opening hours, phone, website, address and rating of one place by its Google place id (from search_places)."
    input_model = PlaceDetailsInput
    taints = True
    taint_source = "maps"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PlaceDetailsInput)
        return ToolResult(format_place_details(self.client.place_details(args.place_id)))


# --- travel_time ----------------------------------------------------------------------------------------


class TravelTimeInput(BaseModel):
    destination: str = Field(
        min_length=1,
        max_length=MAX_ADDRESS,
        description='Address or place name; \'home\' for the user\'s home ("how long to get home", "eve ne kadar sürer").',
    )
    origin: str | None = Field(
        default=None,
        max_length=MAX_ADDRESS,
        description="Leave empty unless the user names a starting point: the tool then starts from the user's Current Location (where they are now). 'home' only when they say the route starts at home (\"evden\", \"evimden\"). Never copy the profile's Current Location or Location line here.",
    )
    mode: Mode | None = Field(default=None, description="transit, driving, walking or bicycling. Default: config maps.default_mode.")
    depart_at: str | None = Field(default=None, description="Local departure time 'YYYY-MM-DD HH:MM'. Default: now.")
    arrive_by: str | None = Field(
        default=None, description="Local arrival deadline 'YYYY-MM-DD HH:MM'; adds a 'leave by' line with the configured buffer."
    )
    confirmed: bool = Field(
        default=False,
        description="True only after the user said yes to a \"Did you mean 'X'?\" question from this tool: use the names as given without asking again.",
    )


class TravelTime(_MapsTool):
    name = "travel_time"
    description = (
        "Travel time and distance between two places with Google Maps, with a route summary (for transit: "
        "lines, stops and transfers). With arrive_by it says when to leave, buffer included. Use for "
        "'when should I leave' questions with the event's location. Costs a paid API call."
    )
    input_model = TravelTimeInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, TravelTimeInput)
        ctx = self.require_ctx()
        destination = _clean(args.destination, "destination", MAX_ADDRESS)
        if destination is None:
            raise ToolValidationError("destination must not be empty")
        question = self.misheard({"destination": destination, "origin": _clean(args.origin, "origin", MAX_ADDRESS)}, args.confirmed)
        if question is not None:
            return question
        destination, destination_label = self.resolve_named(destination, "destination")
        origin, origin_label, notes = self.resolve_origin(_clean(args.origin, "origin", MAX_ADDRESS))
        if origin is None:
            return ToolResult("\n".join(notes + [origin_label]))  # ask the user; never the home Location unasked
        if normalize_address(origin) == normalize_address(destination):
            return ToolResult(f"Origin and destination are the same place ({destination_label}).")
        mode = args.mode or ctx.config.maps.default_mode
        if mode not in MODES:
            raise ToolValidationError(f"mode must be one of {', '.join(MODES)}")
        if args.depart_at and args.arrive_by:
            raise ToolValidationError("give either depart_at or arrive_by, not both")
        depart_at = parse_datetime(args.depart_at) if args.depart_at else None
        arrive_by = parse_datetime(args.arrive_by) if args.arrive_by else None
        route = self.client.route(origin, destination, mode, depart_at=depart_at, arrive_by=arrive_by)
        if route is None:
            return ToolResult(f"No {mode} route found from {origin_label} to {destination_label}.")
        text = format_route(
            origin_label,
            destination_label,
            mode,
            route,
            depart_at=depart_at,
            arrive_by=arrive_by,
            buffer_min=ctx.config.maps.leave_buffer_min,
        )
        return ToolResult("\n".join(notes + [text]))


# --- geocode ----------------------------------------------------------------------------------------------


class GeocodeInput(BaseModel):
    address: str = Field(min_length=1, max_length=MAX_ADDRESS, description="Address or place name.")
    confirmed: bool = Field(
        default=False,
        description="True only after the user said yes to a \"Did you mean 'X'?\" question from this tool: use the names as given without asking again.",
    )


class Geocode(_MapsTool):
    name = "geocode"
    description = "Coordinates (latitude, longitude) and the normalized address of a place or address. Cached for a week."
    input_model = GeocodeInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, GeocodeInput)
        address = _clean(args.address, "address", MAX_ADDRESS)
        if address is None:
            raise ToolValidationError("address must not be empty")
        question = self.misheard({"address": address}, args.confirmed)
        if question is not None:
            return question
        point = self.client.geocode(address)
        if point is None:
            return ToolResult(f"No location found for {address!r}.")
        return ToolResult(f"{point.address or address}: {point.latitude:.6f}, {point.longitude:.6f}")


# --- open_in_maps ------------------------------------------------------------------------------------------


class OpenInMapsInput(BaseModel):
    query: str | None = Field(default=None, max_length=MAX_QUERY, description="A place or address to show.")
    place_id: str | None = Field(default=None, max_length=300, description="A place id from search_places to show.")
    destination: str | None = Field(default=None, max_length=MAX_ADDRESS, description="Directions to this place.")
    origin: str | None = Field(
        default=None,
        max_length=MAX_ADDRESS,
        description="Directions from here (only with destination). Leave empty unless the user names a starting point: the route then starts from the user's Current Location, or from the Maps app's own live location when none is known. 'home' only for \"evden\".",
    )
    mode: Mode | None = Field(default=None, description="Directions mode. Default: config maps.default_mode.")
    confirmed: bool = Field(
        default=False,
        description="True only after the user said yes to a \"Did you mean 'X'?\" question from this tool: use the names as given without asking again.",
    )


def build_maps_url(
    app: str,
    *,
    query: str | None = None,
    place_id: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
    mode: str = "transit",
) -> str:
    """The deep link for Apple Maps or Google Maps; every value is URL-encoded, never raw."""
    if destination:
        if app == "apple":
            params = {"saddr": origin} if origin else {}
            params.update({"daddr": destination, "dirflg": APPLE_DIRFLG.get(mode, "r")})
            return "https://maps.apple.com/?" + urlencode(params)
        params = {"api": "1"}
        if origin:
            params["origin"] = origin
        params.update({"destination": destination, "travelmode": mode})
        return "https://www.google.com/maps/dir/?" + urlencode(params)
    if app == "apple":
        return "https://maps.apple.com/?" + urlencode({"q": query or ""})
    params = {"api": "1", "query": query or ""}
    if place_id:
        params["query_place_id"] = place_id
    return "https://www.google.com/maps/search/?" + urlencode(params)


class OpenInMaps(_MapsTool):
    name = "open_in_maps"
    description = (
        "Open Apple Maps or Google Maps on this Mac (config maps.open_with) showing a place (query or place_id) "
        "or directions to a destination. Give exactly one of query, place_id or destination."
    )
    input_model = OpenInMapsInput
    delegate_ok = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, OpenInMapsInput)
        ctx = self.require_ctx()
        query = _clean(args.query, "query", MAX_QUERY)
        place_id = _clean(args.place_id, "place_id", 300)
        destination = _clean(args.destination, "destination", MAX_ADDRESS)
        origin = _clean(args.origin, "origin", MAX_ADDRESS)
        given = [x for x in (query, place_id, destination) if x]
        if len(given) != 1:
            raise ToolValidationError("give exactly one of query, place_id or destination")
        if origin and not destination:
            raise ToolValidationError("origin only makes sense together with destination")
        question = self.misheard({"query": query, "destination": destination, "origin": origin}, args.confirmed)
        if question is not None:
            return question
        notes: list[str] = []
        app = ctx.config.maps.open_with
        app_name = APP_NAMES.get(app, app)
        mode = args.mode or ctx.config.maps.default_mode
        shown_url: str | None = None
        if destination:
            destination, destination_label = self.resolve_named(destination, "destination")
            origin, origin_label, notes = self.resolve_origin(origin)
            if origin is None:
                # no fresh current location: no origin at all, so the Maps app starts from the device's live location
                # (maps.apple.com with daddr only; Google Maps dir with api=1 and destination only)
                origin_label = "the device's live location (Maps' own; no fresh current location known)"
            url = build_maps_url(app, origin=origin, destination=destination, mode=mode)
            if origin is not None and parse_coordinates(origin) is not None:  # exact coordinates never reach the model or the transcript
                shown_url = url.replace(urlencode({"o": origin})[2:], "current-location")
            label = f"directions from {origin_label} -> {destination_label} ({mode})"
        elif place_id:
            # Apple Maps has no place-id deep link, so the (cached) name and address are looked up and
            # searched by text; Google Maps gets the id on top of the text so it lands on the exact place.
            place = self.client.place_details(place_id)
            query = ", ".join(p for p in (place.name, place.address) if p) or place_id
            url = build_maps_url(app, query=query, place_id=place_id)
            label = query
        else:
            url = build_maps_url(app, query=query)
            label = query or ""
        if destination:
            shown = t(
                "tools.maps.directions",
                origin=t("tools.maps.live_location") if origin is None else origin_label,
                destination=destination_label,
                mode=t(f"tools.maps.mode.{mode}") if mode in MODES else mode,
            )
        else:
            shown = label
        self.notice(t("tools.maps.opening", app=app_name, target=shown))
        try:
            open_url(url)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolExecutionError(f"could not open {app_name}: {type(exc).__name__}") from None
        log.info("open_in_maps: %s (%s)", app_name, "directions" if destination else "place")
        lines = [f"Opened {app_name}: {label}", shown_url or url]
        if destination and notes:
            lines = notes + lines
        return ToolResult("\n".join(lines))
