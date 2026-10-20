"""get_weather: geocoding, default location, WMO code mapping, output shape."""

from __future__ import annotations

import httpx
import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.tools import weather
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.tools.weather import GetWeather, describe_code

FORECAST = {
    "timezone": "Europe/Berlin",
    "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
    "current": {
        "time": "2026-10-14T15:00",
        "temperature_2m": 14.3,
        "apparent_temperature": 12.1,
        "weather_code": 3,
        "wind_speed_10m": 15.4,
        "relative_humidity_2m": 71,
    },
    "daily": {
        "time": ["2026-10-14", "2026-10-15"],
        "weather_code": [3, 61],
        "temperature_2m_max": [16.2, 13.0],
        "temperature_2m_min": [9.8, 7.5],
        "precipitation_probability_max": [10, 80],
    },
}


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    weather._transport = None


def _transport(geocode_results, forecast=FORECAST, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            if geocode_results is None:
                return httpx.Response(200, json={"generationtime_ms": 0.1})
            return httpx.Response(200, json={"results": geocode_results})
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


BERLIN = [{"name": "Berlin", "country": "Germany", "admin1": "Land Berlin", "latitude": 52.52, "longitude": 13.41}]


def _tool(config, paths, default_location=""):
    # `fixed_location` is the current key; `default_location` is the deprecated one both are tried
    config.weather.fixed_location = default_location
    config.weather.default_location = ""
    return GetWeather(ToolContext.build(config, paths))


def test_tool_metadata():
    assert GetWeather.name == "get_weather" and GetWeather.risk == RiskLevel.SAFE and GetWeather.taints is False
    schema = GetWeather().api_schema()["input_schema"]
    assert schema["properties"]["days"]["maximum"] == 7 and schema["properties"]["days"]["minimum"] == 1


def test_forecast_output_and_request_parameters(config, paths):
    seen: list[httpx.Request] = []
    weather._transport = _transport(BERLIN, seen=seen)
    tool = _tool(config, paths)
    result = tool.run(tool.parse_args({"location": "Berlin", "days": 2}))
    lines = result.content.splitlines()
    assert lines[0].startswith("Berlin, Land Berlin, Germany: now 14°C (feels like 12°C), overcast, wind 15 km/h, humidity 71%")
    assert lines[1].startswith("2026-10-14 (Wed): 10-16°C, overcast, precipitation chance 10%")
    assert lines[2].startswith("2026-10-15 (Thu): 8-13°C, light rain, precipitation chance 80%")
    assert len(lines) == 3

    geocode, forecast = seen
    assert geocode.url.params["name"] == "Berlin" and geocode.url.params["count"] == "1" and geocode.url.params["language"] == "en"
    params = forecast.url.params
    assert params["latitude"] == "52.52" and params["longitude"] == "13.41"
    assert params["timezone"] == "auto" and params["forecast_days"] == "2"
    assert "apparent_temperature" in params["current"] and "weather_code" in params["current"]
    assert "precipitation_probability_max" in params["daily"] and "temperature_2m_min" in params["daily"]


def test_default_location_from_config(config, paths):
    seen: list[httpx.Request] = []
    weather._transport = _transport(BERLIN, seen=seen)
    tool = _tool(config, paths, default_location="Berlin")
    tool.run(tool.parse_args({}))
    assert seen[0].url.params["name"] == "Berlin"
    # an explicit location wins
    tool.run(tool.parse_args({"location": "Paris"}))
    assert seen[2].url.params["name"] == "Paris"


def test_no_location_anywhere_is_a_validation_error(config, paths):
    seen: list[httpx.Request] = []
    weather._transport = _transport(BERLIN, seen=seen)
    tool = _tool(config, paths, default_location="")
    with pytest.raises(ToolValidationError, match="jarvis setup"):
        tool.run(tool.parse_args({}))
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"location": "   "}))
    assert seen == []


def test_unknown_location(config, paths):
    weather._transport = _transport(None)
    tool = _tool(config, paths)
    with pytest.raises(ToolExecutionError, match="unknown location"):
        tool.run(tool.parse_args({"location": "Xyzzyqux"}))
    weather._transport = _transport([])
    with pytest.raises(ToolExecutionError, match="unknown location"):
        tool.run(tool.parse_args({"location": "Xyzzyqux"}))


def test_days_bounds(config, paths):
    tool = _tool(config, paths)
    with pytest.raises(ToolValidationError):
        tool.parse_args({"location": "Berlin", "days": 0})
    with pytest.raises(ToolValidationError):
        tool.parse_args({"location": "Berlin", "days": 8})


def test_network_and_server_failures_become_tool_errors(config, paths):
    def explode(request):
        raise httpx.ReadTimeout("slow")

    weather._transport = httpx.MockTransport(explode)
    tool = _tool(config, paths)
    with pytest.raises(ToolExecutionError, match="ReadTimeout"):
        tool.run(tool.parse_args({"location": "Berlin"}))
    weather._transport = httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
    with pytest.raises(ToolExecutionError, match="status 503"):
        tool.run(tool.parse_args({"location": "Berlin"}))
    weather._transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(ToolExecutionError, match="JSON"):
        tool.run(tool.parse_args({"location": "Berlin"}))


@pytest.mark.parametrize(
    "code, word",
    [(0, "clear"), (3, "overcast"), (45, "fog"), (61, "light rain"), (71, "light snow"), (80, "light showers"), (95, "thunderstorm")],
)
def test_wmo_code_mapping(code, word):
    assert describe_code(code) == word
    assert describe_code(str(code)) == word


def test_unknown_codes_do_not_crash():
    assert describe_code(42) == "weather code 42"
    assert describe_code(None) == "unknown conditions"
    assert describe_code("fog") == "unknown conditions"


def test_missing_fields_are_tolerated(config, paths):
    weather._transport = _transport(BERLIN, forecast={"current": {"temperature_2m": 1}, "daily": {"time": ["2026-01-01"]}})
    tool = _tool(config, paths)
    text = tool.run(tool.parse_args({"location": "Berlin"})).content
    assert "now 1°C" in text and "unknown conditions" in text and "2026-01-01" in text
