"""Weather tool: get_weather via Open-Meteo, no API key. The default place
is the user's Current Location (exact coordinates from the local state,
no geocoding), then the home Location from profile.md, then `weather.fixed_location`; the result's
first line names the place and the basis so the model can say which one it used."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.location import service as location_service
from jarvis.tools.base import RiskLevel, Tool, ToolResult

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 15.0
CURRENT_FIELDS = "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m"
DAILY_FIELDS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"

# Injectable for tests (httpx.MockTransport). None means the default transport.
_transport: httpx.BaseTransport | None = None

# WMO weather interpretation codes (the subset Open-Meteo emits) -> short words.
WMO_CODES: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "icy fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def describe_code(code: Any) -> str:
    try:
        return WMO_CODES.get(int(code), f"weather code {int(code)}")
    except (TypeError, ValueError):
        return "unknown conditions"


def _get_json(url: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, transport=_transport) as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ToolExecutionError(f"{what} request failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise ToolExecutionError(f"{what} request failed with status {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise ToolExecutionError(f"{what} response was not JSON") from None
    if not isinstance(payload, dict):
        raise ToolExecutionError(f"{what} response was not understood")
    return payload


def geocode(name: str) -> dict[str, Any]:
    """The best Open-Meteo match for a place name (keys: name, country, admin1, latitude, longitude)."""
    payload = _get_json(GEOCODE_URL, {"name": name, "count": 1, "language": "en", "format": "json"}, "geocoding")
    results = payload.get("results")
    if not results or not isinstance(results, list) or not isinstance(results[0], dict):
        raise ToolExecutionError(f"unknown location: {name!r}")
    hit = results[0]
    if "latitude" not in hit or "longitude" not in hit:
        raise ToolExecutionError(f"geocoding result for {name!r} has no coordinates")
    return hit


def _num(value: Any, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "?"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    text = f"{number:.{digits}f}"
    return f"{text}{unit}"


def _weekday(day: str) -> str:
    try:
        return date.fromisoformat(day).strftime("%a")
    except ValueError:
        return ""


def format_forecast(place: str, data: dict[str, Any]) -> str:
    current = data.get("current") or {}
    units = data.get("current_units") or {}
    temp_unit = units.get("temperature_2m", "°C")
    wind_unit = units.get("wind_speed_10m", "km/h")
    lines = [
        f"{place}: now {_num(current.get('temperature_2m'), temp_unit)} "
        f"(feels like {_num(current.get('apparent_temperature'), temp_unit)}), "
        f"{describe_code(current.get('weather_code'))}, wind {_num(current.get('wind_speed_10m'), ' ' + wind_unit)}, "
        f"humidity {_num(current.get('relative_humidity_2m'), '%')}" + (f" (local time {current['time']})" if current.get("time") else "")
    ]
    daily = data.get("daily") or {}
    days = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_probability_max") or []

    def at(seq: list[Any], i: int) -> Any:
        return seq[i] if i < len(seq) else None

    for i, day in enumerate(days):
        weekday = _weekday(str(day))
        lines.append(
            f"{day}{' (' + weekday + ')' if weekday else ''}: "
            f"{_num(at(lows, i), '')}-{_num(at(highs, i), temp_unit)}, {describe_code(at(codes, i))}, "
            f"precipitation chance {_num(at(rain, i), '%')}"
        )
    return "\n".join(lines)


class GetWeatherInput(BaseModel):
    location: str | None = Field(
        default=None,
        description=(
            "City or place name, e.g. 'Berlin' or 'Austin, Texas'; 'home' for the user's home Location. "
            "Default: the user's Current Location (where they are now), falling back to the home Location."
        ),
    )
    days: int = Field(default=1, ge=1, le=7, description="Number of forecast days (1-7).")


# The fallback chain per `weather.location_source`.
LOCATION_ORDER: dict[str, tuple[str, ...]] = {
    "current": ("current", "home", "fixed"),
    "home": ("home", "fixed"),
    "fixed": ("fixed", "home"),
}


def default_place(ctx: Any) -> location_service.Place:
    """Where the weather is for when no place is named: the Current Location (local turns: the Mac's,
    refreshed when older than max_age_min; remote turns: only a location shared on Telegram), then the
    home Location from profile.md, then `weather.fixed_location` (the old `default_location`)."""
    weather = ctx.config.weather
    fixed = weather.fixed_location or weather.default_location
    return location_service.resolve_default_place(ctx, order=LOCATION_ORDER[weather.location_source], fixed=fixed, what="location")


class GetWeather(Tool):
    name = "get_weather"
    description = (
        "Current weather and a short daily forecast for a place (Open-Meteo). Without a location it uses "
        "the user's Current Location (where they are right now) and falls back to their home Location; "
        "the first line says which one was used."
    )
    input_model = GetWeatherInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, GetWeatherInput)
        ctx = self.require_ctx()
        requested = (args.location or "").strip()
        if len(requested) > 200:
            raise ToolValidationError("location is too long")
        if requested and location_service.is_home_alias(requested):
            home = location_service.home_location(ctx.paths)
            if not home:
                raise ToolValidationError("'home' was asked for but profile.md has no Location line; run `jarvis setup`")
            place_basis = location_service.Place("home", query=home, label=f"{home} (home)")
        elif requested:
            place_basis = location_service.Place("given", query=requested)
        else:
            place_basis = default_place(ctx)
        if place_basis.kind == "ask":
            return ToolResult(place_basis.ask or location_service.ASK_FOR_LOCATION)

        if place_basis.point is not None:
            latitude, longitude = place_basis.point
            place = place_basis.label
        else:
            assert place_basis.query is not None
            hit = geocode(place_basis.query)
            place_parts = [str(hit.get("name") or place_basis.query)]
            for key in ("admin1", "country"):
                value = hit.get(key)
                if value and str(value) not in place_parts:
                    place_parts.append(str(value))
            place = ", ".join(place_parts)
            basis_note = place_basis.label[place_basis.label.find("(") :] if "(" in place_basis.label else ""
            if place_basis.kind != "given" and basis_note:
                place += " " + basis_note  # "(home location; current location unknown)"
            latitude, longitude = hit["latitude"], hit["longitude"]
        data = _get_json(
            FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": CURRENT_FIELDS,
                "daily": DAILY_FIELDS,
                "forecast_days": args.days,
                "timezone": "auto",
            },
            "forecast",
        )
        return ToolResult(format_forecast(place, data))
