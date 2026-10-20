"""Current Location: parsing and validation, the auto-managed profile block
(only the block changes, atomic write, precision, never coordinates), the source fallback order,
non-blocking startup, lazy refresh by age, failure keeps the last known value, the weather and
maps defaults with their fallbacks, "eve ne kadar sürer" using the home Location, the Telegram
location for remote turns, the config migration and the CLI."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from jarvis import main as main_mod
from jarvis.config import load_config
from jarvis.exceptions import ConfigError, ToolValidationError
from jarvis.location import service, sources
from jarvis.location.model import (
    Location,
    clean_text,
    fields_from_mapping,
    location_from_fields,
    parse_number,
    parse_text_output,
    valid_coordinates,
)
from jarvis.location.sources import SourceError
from jarvis.location.store import LocationState, load_state, save_state
from jarvis.maps.client import waypoint
from jarvis.profile import AUTO_BLOCK_KEY, auto_block_body, find_auto_block, replace_auto_block
from jarvis.tools import weather
from jarvis.tools.base import Runtime, ToolContext
from jarvis.tools.maps import SearchPlaces, TravelTime
from jarvis.tools.weather import GetWeather
from jarvis.utils import fileio
from tests.conftest import RecordingSink
from tests.unit.test_maps import Api, make_client
from tests.unit.test_weather import BERLIN, _transport

NOW = datetime(2026, 10, 16, 14, 30, tzinfo=timezone.utc)
KADIKOY = dict(
    latitude=40.9903,
    longitude=29.0290,
    street="Moda Caddesi 12",
    neighborhood="Caferağa",
    city="Kadıköy",
    region="İstanbul",
    country="Türkiye",
)
PROFILE = "# Profile\n\n- Name: Kerem Yilmaz\n- Address me as: Kerem\n- Location: Goztepe, Turkiye\n\n## Preferences\n\nBe nice.\n"
COORD_RE = re.compile(r"\d{1,3}\.\d{3,}")  # anything that looks like a coordinate


def loc(source="shortcut", age_min=0, base: datetime | None = None, **overrides) -> Location:
    """A location `age_min` minutes old, relative to the real clock (the tools use it) unless a test
    that passes `now=NOW` to the service asks for `base=NOW`."""
    fields = {**KADIKOY, **overrides}
    stamp = ((base or datetime.now(timezone.utc)) - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return Location(source=source, updated_at=stamp, **fields)


def source_returning(location: Location | None, calls: list[str] | None = None, name: str = "x", delay: float = 0.0):
    """A fake source: records the call, fails when `location` is None, else answers with the refresh time."""
    from dataclasses import replace

    def fn(config, *, timeout, now_iso):
        if calls is not None:
            calls.append(name)
        if delay:
            time.sleep(delay)
        if location is None:
            raise SourceError(f"{name} failed")
        return replace(location, updated_at=now_iso)

    return fn


def write_profile(paths, text: str = PROFILE) -> None:
    paths.profile_file.write_text(text, encoding="utf-8")


def ctx_for(config, paths, db, channel="cli") -> ToolContext:
    context = ToolContext.build(config, paths)
    context.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None, channel=channel, sink=RecordingSink())
    return context


# --- parsing and validation ---------------------------------------------------------------------------------


def test_numbers_accept_the_decimal_comma_and_reject_junk():
    assert parse_number("41,0138") == 41.0138 and parse_number("41.0138") == 41.0138 and parse_number(29) == 29.0
    assert parse_number("41,013,8") is None and parse_number("abc") is None and parse_number("1e400") is None
    assert parse_number(True) is None and parse_number("") is None


def test_coordinates_must_be_in_range_and_not_null_island():
    assert valid_coordinates(40.99, 29.03) and valid_coordinates(-33.9, 151.2)
    assert not valid_coordinates(91, 0) and not valid_coordinates(0, 181) and not valid_coordinates(0, 0)
    assert not valid_coordinates("nan", 1) and not valid_coordinates(float("inf"), 1)
    with pytest.raises(ValueError):
        Location(latitude=95, longitude=0, source="ip", updated_at="2026-10-16T00:00:00.000Z")
    with pytest.raises(ValueError):
        Location(latitude=1, longitude=1, source="gps", updated_at="2026-10-16T00:00:00.000Z")


def test_text_fields_are_sanitized():
    assert clean_text("Moda\x1b[31m Cad.​ <b>12</b>\n") == "Moda Cad. b 12 /b"
    assert clean_text("x" * 500) == "x" * 120
    assert clean_text(None) == "" and clean_text(True) == ""


def test_shortcut_output_as_key_value_lines_json_or_coordinates_plus_address():
    lines = "lat: 41,0138\nlon: 28,9497\nstreet: Moda Caddesi 12\ncity: Kadıköy\nregion: İstanbul\ncountry: Türkiye\n"
    fields = parse_text_output(lines)
    assert (
        fields["latitude"] == 41.0138 and fields["longitude"] == 28.9497 and fields["city"] == "Kadıköy" and fields["region"] == "İstanbul"
    )
    as_json = json.dumps({"latitude": 40.99, "longitude": 29.03, "City": "Kadıköy", "Country": "TR", "ignored": "x"})
    assert parse_text_output(as_json) == {"latitude": 40.99, "longitude": 29.03, "city": "Kadıköy", "country": "TR"}
    free = "40.9903, 29.0290\nModa Caddesi 12\nKadıköy İstanbul\nTürkiye"
    fields = parse_text_output(free)
    assert (fields["latitude"], fields["longitude"]) == (40.9903, 29.029) and fields["street"] == "Moda Caddesi 12"
    assert fields["city"] == "Kadıköy İstanbul" and fields["country"] == "Türkiye"
    assert parse_text_output("") == {} and parse_text_output("hello") == {"city": "hello"}
    assert location_from_fields(parse_text_output("hello"), source="shortcut", updated_at="2026-10-16T00:00:00.000Z") is None


def test_ip_services_and_corelocation_json_are_understood():
    ipinfo = {"ip": "1.2.3.4", "city": "Istanbul", "region": "Istanbul", "country": "TR", "loc": "41.0138,28.9497"}
    f = fields_from_mapping(ipinfo)
    assert (f["latitude"], f["longitude"], f["city"], f["country"]) == (41.0138, 28.9497, "Istanbul", "TR")
    ipapi = {
        "city": "Istanbul",
        "region": "Istanbul",
        "country": "TR",
        "country_name": "Turkey",
        "latitude": 41.01384,
        "longitude": 28.94966,
    }
    f = fields_from_mapping(ipapi)
    assert f["country"] == "Turkey" and f["latitude"] == 41.01384
    corelocation = {
        "latitude": 40.9903,
        "longitude": 29.029,
        "h_accuracy": 35,
        "thoroughfare": "Moda Caddesi",
        "subThoroughfare": "12",
        "subLocality": "Caferağa",
        "locality": "Kadıköy",
        "administrativeArea": "İstanbul",
        "country": "Türkiye",
    }
    f = fields_from_mapping(corelocation)
    assert f["street"] == "Moda Caddesi 12" and f["neighborhood"] == "Caferağa" and f["accuracy_m"] == 35.0
    assert fields_from_mapping({"latitude": "91", "longitude": 1}).get("latitude") == 91.0
    assert location_from_fields(fields_from_mapping({"latitude": "91", "longitude": 1}), source="ip", updated_at="x") is None


# --- precision ------------------------------------------------------------------------------------------------


def test_precision_levels_and_the_ip_rule():
    full = loc()
    assert full.label("city") == "Kadıköy, Türkiye"
    assert full.label("neighborhood") == "Kadıköy, Caferağa"
    assert full.label("street") == "Moda Caddesi 12, Caferağa, Kadıköy"
    no_hood = loc(neighborhood="")
    assert no_hood.label("neighborhood") == "Kadıköy, İstanbul"
    assert loc(neighborhood="", region="Kadıköy").label("neighborhood") == "Kadıköy, Türkiye"
    assert loc(street="", neighborhood="").label("street") == "Kadıköy, İstanbul"
    approx = loc(source="ip", approximate=True, street="", neighborhood="", city="Istanbul", region="Istanbul", country="TR")
    assert approx.label("street") == approx.label("neighborhood") == approx.label("city") == "Istanbul, TR"
    assert loc(street="", neighborhood="", city="", region="", country="").label("street") == ""
    with pytest.raises(ValueError):
        full.label("exact")


def test_profile_line_has_label_time_source_and_no_coordinates():
    line = service.profile_line(loc(base=NOW), "neighborhood")
    assert line.startswith("- Current Location: Kadıköy, Caferağa (updated 2026-10-16 ") and line.endswith(", source: shortcut)")
    assert not COORD_RE.search(line)
    approx = service.profile_line(loc(source="ip", approximate=True, street="", neighborhood=""), "street")
    assert "Kadıköy, Türkiye (approximate, IP-based; updated" in approx


# --- the auto-managed block -------------------------------------------------------------------------------------


def test_replace_auto_block_touches_only_the_block():
    body = "- Current Location: Kadıköy, Caferağa (updated now, source: shortcut)"
    created = replace_auto_block(PROFILE, AUTO_BLOCK_KEY, body)
    start, end = find_auto_block(created)
    assert created[:start].endswith("- Location: Goztepe, Turkiye\n\n")
    assert created[end:].startswith("\n\n## Preferences\n\nBe nice.\n")
    assert auto_block_body(created) == body
    # everything outside the markers is byte-identical to the original
    assert created[:start] + created[end:].lstrip("\n") == PROFILE.replace(
        "- Location: Goztepe, Turkiye\n\n", "- Location: Goztepe, Turkiye\n\n"
    )
    assert created.replace(created[start:end] + "\n\n", "") == PROFILE
    # a second write replaces the body only
    updated = replace_auto_block(created, AUTO_BLOCK_KEY, "- Current Location: Amsterdam, De Pijp (updated later, source: ip)")
    assert updated.count("jarvis:auto current_location START") == 1 and updated.count("jarvis:auto current_location END") == 1
    assert updated.replace(updated[find_auto_block(updated)[0] : find_auto_block(updated)[1]], created[start:end]) == created
    assert (
        replace_auto_block(updated, AUTO_BLOCK_KEY, "- Current Location: Amsterdam, De Pijp (updated later, source: ip)") == updated
    )  # idempotent


def test_block_is_created_at_the_end_without_a_preferences_heading_and_user_edits_survive():
    text = "# Profile\n- Name: X\n"
    out = replace_auto_block(text, AUTO_BLOCK_KEY, "- Current Location: A")
    assert (
        out
        == "# Profile\n- Name: X\n\n<!-- jarvis:auto current_location START -->\n- Current Location: A\n<!-- jarvis:auto current_location END -->\n"
    )
    edited = out.replace("- Name: X", "- Name: Y\n- Job: dev") + "\n## Preferences\n\nmore\n"
    again = replace_auto_block(edited, AUTO_BLOCK_KEY, "- Current Location: B")
    assert "- Name: Y\n- Job: dev" in again and again.endswith("\n## Preferences\n\nmore\n") and "- Current Location: B" in again
    assert "- Current Location: A" not in again
    # a start marker without an end marker is not a block: a new one is created, the stray line is left alone
    broken = "# Profile\n<!-- jarvis:auto current_location START -->\n- Name: Z\n\n## Preferences\n"
    fixed = replace_auto_block(broken, AUTO_BLOCK_KEY, "- Current Location: C")
    assert fixed.count("START") == 2 and fixed.count("END") == 1 and "- Name: Z" in fixed


def test_write_profile_block_is_atomic_and_keeps_the_file_on_failure(paths, monkeypatch):
    write_profile(paths)
    assert service.write_profile_block(paths, loc(), "neighborhood") is True
    text = paths.profile_file.read_text()
    assert "- Current Location: Kadıköy, Caferağa" in text and "- Location: Goztepe, Turkiye" in text
    assert not [p for p in paths.data_dir.iterdir() if p.suffix == ".tmp"]  # the temp file is gone
    replaced: list[tuple[str, str]] = []
    real_replace = fileio.os.replace

    def failing_replace(src, dst):
        replaced.append((str(src), str(dst)))
        raise OSError("disk full")

    monkeypatch.setattr(fileio.os, "replace", failing_replace)
    assert service.write_profile_block(paths, loc(city="Amsterdam", neighborhood="De Pijp"), "neighborhood") is False
    assert paths.profile_file.read_text() == text  # untouched
    assert replaced and Path(replaced[0][0]).parent == paths.profile_file.parent and not Path(replaced[0][0]).exists()
    monkeypatch.setattr(fileio.os, "replace", real_replace)
    assert service.write_profile_block(paths, loc(), "neighborhood") is True  # unchanged content: no write needed
    assert paths.profile_file.read_text() == text


def test_profile_never_receives_coordinates(paths):
    write_profile(paths)
    for precision in ("city", "neighborhood", "street"):
        service.write_profile_block(paths, loc(), precision)
        text = paths.profile_file.read_text()
        assert not COORD_RE.search(text), text
        assert "40.99" not in text and "29.02" not in text
    state = load_state(paths.location_state_file) if paths.location_state_file.exists() else None
    assert state is None  # write_profile_block alone touches no state


def test_setup_wizard_preserves_the_block(paths):
    from jarvis.setup_wizard import run_setup

    write_profile(paths)
    service.write_profile_block(paths, loc(), "neighborhood")
    before = paths.profile_file.read_text()
    assert run_setup(paths.home, Console(file=StringIO(), force_terminal=False), answers={"language": "tr", "run_doctor": False}) == 0
    text = paths.profile_file.read_text()
    assert text == before  # an existing profile is never rewritten
    assert "- Current Location: Kadıköy, Caferağa" in text and text.count("jarvis:auto current_location START") == 1


# --- refresh: order, background, age, failure ------------------------------------------------------------------


def test_sources_are_tried_in_order_and_the_first_answer_wins(config, paths, logs):
    write_profile(paths)
    calls: list[str] = []
    ip = loc(source="ip", approximate=True, street="", neighborhood="", city="Istanbul", region="Istanbul", country="TR")
    chain = [
        ("shortcut", source_returning(None, calls, "shortcut")),
        ("corelocation", source_returning(None, calls, "corelocation")),
        ("ip", source_returning(ip, calls, "ip")),
    ]
    result = service.refresh(config, paths, sources=chain, now=NOW)
    assert calls == ["shortcut", "corelocation", "ip"] and result.refreshed and result.location.source == "ip"
    assert result.source_errors == {"shortcut": "shortcut failed", "corelocation": "corelocation failed"}
    assert result.profile_updated and "Istanbul, TR (approximate, IP-based; updated" in paths.profile_file.read_text()
    state = load_state(paths.location_state_file)
    assert state.mac.source == "ip" and state.mac.latitude == KADIKOY["latitude"] and state.last_error is None
    calls.clear()
    chain[0] = ("shortcut", source_returning(loc(), calls, "shortcut"))
    result = service.refresh(config, paths, sources=chain, now=NOW + timedelta(minutes=5))
    assert calls == ["shortcut"] and result.location.source == "shortcut" and result.source_errors == {}
    assert "Kadıköy, Caferağa (updated" in paths.profile_file.read_text()


def test_refresh_is_debounced_and_respects_enabled(config, paths):
    calls: list[str] = []
    chain = [("shortcut", source_returning(loc(), calls, "shortcut"))]
    assert service.refresh(config, paths, sources=chain, now=NOW).refreshed
    skipped = service.refresh(config, paths, sources=chain, now=NOW + timedelta(seconds=10))
    assert skipped.skipped and not skipped.refreshed and skipped.location is not None and calls == ["shortcut"]
    assert service.refresh(config, paths, sources=chain, now=NOW + timedelta(seconds=10), force=True).refreshed and len(calls) == 2
    config.location.enabled = False
    off = service.refresh(config, paths, sources=chain, now=NOW + timedelta(hours=1))
    assert off.skipped == "location.enabled = false" and len(calls) == 2 and off.location.source == "shortcut"


def test_background_refresh_never_blocks_startup(config, paths, monkeypatch, location_lookups_off):
    refresh_in_background = location_lookups_off  # the real one; the autouse fixture replaced the module attribute
    started = threading.Event()
    release = threading.Event()

    def slow_source(cfg, *, timeout, now_iso):
        started.set()
        release.wait(5)
        return loc()

    monkeypatch.setattr(sources, "SOURCES", {"shortcut": slow_source})
    monkeypatch.setattr(sources, "SOURCE_ORDER", ("shortcut",))
    t0 = time.perf_counter()
    thread = refresh_in_background(config, paths)
    elapsed = time.perf_counter() - t0
    assert thread is not None and thread.daemon and elapsed < 0.2, elapsed
    assert started.wait(2)
    assert refresh_in_background(config, paths) is None  # one in flight: the second start is skipped
    release.set()
    thread.join(5)
    assert load_state(paths.location_state_file).mac is not None
    config.location.refresh_on_start = False
    assert refresh_in_background(config, paths) is None


def test_lazy_refresh_only_when_older_than_max_age(config, paths, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sources, "SOURCES", {"shortcut": source_returning(loc(city="Beşiktaş"), calls, "shortcut")})
    monkeypatch.setattr(sources, "SOURCE_ORDER", ("shortcut",))
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=30, base=NOW)))
    fresh = service.ensure_fresh(config, paths, now=NOW)
    assert fresh.city == "Kadıköy" and calls == []  # 30 min < 60: no lookup
    config.location.max_age_min = 20
    stale = service.ensure_fresh(config, paths, now=NOW)
    assert stale.city == "Beşiktaş" and calls == ["shortcut"]
    assert service.ensure_fresh(config, paths, now=NOW).city == "Beşiktaş" and calls == ["shortcut"]  # just refreshed
    config.location.enabled = False
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=500, base=NOW)))
    assert service.ensure_fresh(config, paths, now=NOW).city == "Kadıköy" and calls == ["shortcut"]  # disabled: last known, no lookup


def test_failure_keeps_the_last_known_location_and_says_its_age(config, paths, logs):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=180, base=NOW)))
    service.write_profile_block(paths, loc(age_min=180, base=NOW), "neighborhood")
    before = paths.profile_file.read_text()
    chain = [(name, source_returning(None, None, name)) for name in ("shortcut", "corelocation", "ip")]
    result = service.refresh(config, paths, sources=chain, now=NOW, force=True)
    assert not result.refreshed and result.location is not None and result.location.city == "Kadıköy"
    assert result.error == "shortcut: shortcut failed; corelocation: corelocation failed; ip: ip failed"
    state = load_state(paths.location_state_file)
    assert state.mac.age_seconds(NOW) == 180 * 60 and state.last_error.startswith("shortcut: shortcut failed") and state.last_attempt_at
    assert paths.profile_file.read_text() == before  # the old block stays
    assert service.last_known_text(result.location, now=NOW) == "Kadıköy, Caferağa (son bilinen konum, 3 saat önce, kaynak: shortcut)"
    assert (
        service.age_text(30) == "az önce" and service.age_text(12 * 60) == "12 dakika önce" and service.age_text(3 * 86400) == "3 gün önce"
    )
    assert service.age_text(90 * 60, "en") == "1 h ago" and service.age_text(5, "en") == "just now"
    lines = service.status_lines(config, paths, now=NOW)
    assert lines[0].startswith("Mac konumu (eski): Moda Caddesi 12, Caferağa, Kadıköy") and "3 saat önce" in lines[1]
    assert any(l.startswith("Son hata") and "shortcut failed" in l for l in lines)


def test_status_lines_and_ages_follow_the_main_language(config, paths):
    from jarvis import i18n

    i18n.set_language("en")
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=180, base=NOW)))
    assert service.age_text(30) == "just now" and service.age_text(3 * 86400) == "3 days ago"
    assert service.age_text(12 * 60, "tr") == "12 dakika önce"  # an explicit language wins
    assert (
        service.last_known_text(loc(age_min=180, base=NOW), now=NOW) == "Kadıköy, Caferağa (last known location, 3 h ago, source: shortcut)"
    )
    lines = service.status_lines(config, paths, now=NOW)
    assert lines[0].startswith("Mac location (stale): Moda Caddesi 12, Caferağa, Kadıköy") and "3 h ago" in lines[1]
    assert "Location shared on Telegram: none" in lines[-2] and lines[-1].startswith("Settings: at most")


def test_history_is_kept_only_when_asked(config, paths):
    chain = [("shortcut", source_returning(loc(), None, "shortcut"))]
    service.refresh(config, paths, sources=chain, now=NOW)
    assert not paths.location_history_file.exists()
    config.location.keep_history = True
    service.refresh(config, paths, sources=chain, now=NOW + timedelta(hours=1), force=True)
    rows = [json.loads(l) for l in paths.location_history_file.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["source"] == "shortcut"


# --- the real sources with fake commands --------------------------------------------------------------------------


def test_shortcut_source_uses_the_bridge_only_for_an_allowlisted_shortcut(config, paths, monkeypatch, tmp_path):
    import subprocess

    argvs: list[list[str]] = []

    def fake_run(argv, *, timeout):
        argvs.append(argv)
        Path(argv[argv.index("--output-path") + 1]).write_text(
            "lat: 40,9903\nlon: 29,0290\nstreet: Moda Caddesi 12\ncity: Kadıköy\nregion: İstanbul\ncountry: Türkiye\n"
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sources, "run_shortcut_command", fake_run)
    monkeypatch.setattr(sources.shutil, "which", lambda name: "/usr/bin/shortcuts")
    found = sources.shortcut_source(config, timeout=5, now_iso="2026-10-16T14:30:00.000Z")
    assert found.source == "shortcut" and found.city == "Kadıköy" and found.latitude == 40.9903
    assert argvs[0][:3] == ["shortcuts", "run", "Jarvis Location"] and "--output-path" in argvs[0]
    config.location.shortcut_name = "Not Allowlisted"
    with pytest.raises(SourceError, match="not in \\[\\[shortcuts.allowed\\]\\]"):
        sources.shortcut_source(config, timeout=5, now_iso="x")
    assert len(argvs) == 1  # never ran

    def failing(argv, *, timeout):
        return subprocess.CompletedProcess(argv, 1, "", "No shortcut with that name")

    config.location.shortcut_name = "Jarvis Location"
    monkeypatch.setattr(sources, "run_shortcut_command", failing)
    with pytest.raises(SourceError, match="failed: No shortcut"):
        sources.shortcut_source(config, timeout=5, now_iso="x")

    def junk(argv, *, timeout):
        Path(argv[argv.index("--output-path") + 1]).write_text("done")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sources, "run_shortcut_command", junk)
    with pytest.raises(SourceError, match="no usable coordinates"):
        sources.shortcut_source(config, timeout=5, now_iso="x")


def test_corelocation_and_ip_sources(config, monkeypatch):
    import subprocess

    monkeypatch.setattr(sources, "corelocation_installed", lambda: False)
    with pytest.raises(SourceError, match="not installed"):
        sources.corelocation_source(config, timeout=5, now_iso="x")
    monkeypatch.setattr(sources, "corelocation_installed", lambda: True)
    payload = json.dumps(
        {"latitude": 40.9903, "longitude": 29.029, "locality": "Kadıköy", "administrativeArea": "İstanbul", "country": "Türkiye"}
    )
    monkeypatch.setattr(sources, "run_corelocation_command", lambda argv, timeout: subprocess.CompletedProcess(argv, 0, payload, ""))
    found = sources.corelocation_source(config, timeout=5, now_iso="2026-10-16T14:30:00.000Z")
    assert found.source == "corelocation" and found.city == "Kadıköy" and not found.approximate
    monkeypatch.setattr(sources, "run_corelocation_command", lambda argv, timeout: subprocess.CompletedProcess(argv, 1, "", "denied"))
    with pytest.raises(SourceError, match="Location Services"):
        sources.corelocation_source(config, timeout=5, now_iso="x")

    seen: list[str] = []
    monkeypatch.setattr(
        sources,
        "fetch_ip_json",
        lambda url, timeout: (
            seen.append(url)
            or {"city": "Istanbul", "region": "Istanbul", "country": "TR", "loc": "41.0138,28.9497", "street": "should not survive"}
        ),
    )
    found = sources.ip_source(config, timeout=5, now_iso="2026-10-16T14:30:00.000Z")
    assert (
        seen == ["https://ipinfo.io/json"]
        and found.source == "ip"
        and found.approximate
        and found.street == ""
        and found.city == "Istanbul"
    )
    config.location.ip_fallback = False
    with pytest.raises(SourceError, match="disabled"):
        sources.ip_source(config, timeout=5, now_iso="x")


def test_fetch_ip_json_requires_https():
    with pytest.raises(SourceError, match="https"):
        sources.fetch_ip_json("http://ipinfo.io/json", timeout=1)


# --- weather defaults and fallbacks -------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_weather_transport():
    yield
    weather._transport = None


def test_weather_defaults_to_the_current_location_without_geocoding(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=10)))
    seen: list[httpx.Request] = []
    weather._transport = _transport(BERLIN, seen=seen)
    tool = GetWeather(ctx_for(config, paths, db))
    out = tool.run(tool.parse_args({})).content
    assert [r.url.host for r in seen] == ["api.open-meteo.com"]  # no geocoding: the exact coordinates are used
    assert seen[0].url.params["latitude"] == str(KADIKOY["latitude"]) and seen[0].url.params["longitude"] == str(KADIKOY["longitude"])
    assert out.startswith("Kadıköy, Caferağa (current location, source shortcut, ") and "ago): now 14°C" in out
    assert not COORD_RE.search(out.splitlines()[0])


def test_weather_falls_back_to_home_then_fixed_and_honors_location_source(config, paths, db):
    seen: list[httpx.Request] = []
    weather._transport = _transport(BERLIN, seen=seen)
    geocoded = lambda: [r.url.params["name"] for r in seen if r.url.host == "geocoding-api.open-meteo.com"]  # noqa: E731
    config.weather.fixed_location = "Berlin"
    tool = GetWeather(ctx_for(config, paths, db))
    # no current location (sources fail in tests), no profile: the fixed place
    out = tool.run(tool.parse_args({})).content
    assert geocoded() == ["Berlin"] and out.startswith("Berlin, Land Berlin, Germany (configured fallback; current location unknown): now")
    # with a profile the home Location comes before the fixed place
    write_profile(paths)
    out = tool.run(tool.parse_args({})).content
    assert geocoded()[-1] == "Goztepe, Turkiye" and "(home; current location unknown)" in out.splitlines()[0]
    # location_source = home ignores the current location even when it is known
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=1)))
    config.weather.location_source = "home"
    out = tool.run(tool.parse_args({})).content
    assert geocoded()[-1] == "Goztepe, Turkiye"
    assert (
        out.splitlines()[0]
        == "Berlin, Land Berlin, Germany (home): now 14°C (feels like 12°C), overcast, wind 15 km/h, humidity 71% (local time 2026-10-14T15:00)"
    )
    config.weather.location_source = "fixed"
    tool.run(tool.parse_args({}))
    assert geocoded()[-1] == "Berlin"
    # back to current: the stored location is used, no geocoding
    config.weather.location_source = "current"
    before = len(geocoded())
    out = tool.run(tool.parse_args({})).content
    assert len(geocoded()) == before and out.startswith("Kadıköy, Caferağa (current location, source shortcut, 1 min ago)")
    # "home" as an explicit location
    out = tool.run(tool.parse_args({"location": "home"})).content
    assert geocoded()[-1] == "Goztepe, Turkiye" and "(home)" in out.splitlines()[0]
    # nothing anywhere
    paths.profile_file.unlink()
    paths.location_state_file.unlink()
    config.weather.fixed_location = ""
    with pytest.raises(ToolValidationError, match="no location given and no location in profile.md"):
        tool.run(tool.parse_args({}))


def test_weather_home_alias_without_a_profile_is_an_error(config, paths, db):
    tool = GetWeather(ctx_for(config, paths, db))
    with pytest.raises(ToolValidationError, match="no Location line"):
        tool.run(tool.parse_args({"location": "ev"}))


# --- maps defaults, "eve ne kadar sürer" ------------------------------------------------------------------------------


def test_travel_time_origin_defaults_to_the_current_location_as_a_latlng_waypoint(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    api = Api()
    context = ctx_for(config, paths, db)
    tool = TravelTime(context, client=make_client(config, db, api))
    out = tool.run(tool.parse_args({"destination": "Sirkeci"})).content
    body = api.body()
    assert body["origin"] == {"location": {"latLng": {"latitude": KADIKOY["latitude"], "longitude": KADIKOY["longitude"]}}}
    assert body["destination"] == {"address": "Sirkeci"}
    assert out.startswith("Kadıköy, Caferağa (current location, source shortcut, 5 min ago) -> Sirkeci by transit")
    assert not COORD_RE.search(out)


def test_eve_ne_kadar_surer_routes_from_the_current_location_to_home(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    api = Api()
    tool = TravelTime(ctx_for(config, paths, db), client=make_client(config, db, api))
    for word in ("eve", "home", "Evime"):
        out = tool.run(tool.parse_args({"destination": word, "mode": "driving"})).content
        assert api.body()["destination"] == {"address": "Goztepe, Turkiye"}
        assert "-> Goztepe, Turkiye (home) by driving" in out
    # from home to somewhere: origin = "home"
    tool.run(tool.parse_args({"origin": "home", "destination": "Sirkeci"}))
    assert api.body()["origin"] == {"address": "Goztepe, Turkiye"}
    # the same place twice is not a route
    assert tool.run(tool.parse_args({"origin": "home", "destination": "eve"})).content.startswith(
        "Origin and destination are the same place"
    )
    paths.profile_file.unlink()
    with pytest.raises(ToolValidationError, match="destination is 'home' but profile.md has no Location"):
        tool.run(tool.parse_args({"destination": "eve"}))


def test_search_places_near_defaults_to_the_current_location_without_a_geocoding_call(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    api = Api()
    tool = SearchPlaces(ctx_for(config, paths, db), client=make_client(config, db, api))
    out = tool.run(tool.parse_args({"query": "eczane"})).content
    assert [r.url.host for r in api.seen] == ["places.googleapis.com"]
    center = api.body(0)["locationBias"]["circle"]["center"]
    assert center == {"latitude": KADIKOY["latitude"], "longitude": KADIKOY["longitude"]}
    assert out.startswith("2 place(s) for 'eczane' near Kadıköy, Caferağa (current location, source shortcut, 5 min ago):")
    assert "away" in out  # distances from the current location
    # near = "home" geocodes the profile's Location
    tool.run(tool.parse_args({"query": "eczane", "near": "home"}))
    assert api.seen[1].url.params["address"] == "Goztepe, Turkiye"


# --- the origin rule: "X'e nasıl giderim" starts from where the user is -------------------------------------------------


def ctx_saying(config, paths, db, text: str, channel: str = "cli") -> ToolContext:
    context = ctx_for(config, paths, db, channel=channel)
    context.runtime.user_text = text
    return context


def test_from_home_markers_and_user_named():
    for text in (
        "Evden Taksim'e ne kadar sürer?",
        "evimden spora yürüyerek kaç dk",
        "Evdeyim, ofise gidiş süresi?",
        "how long from home to Taksim",
    ):
        assert service.from_home_requested(text), text
    for text in ("Eve ne kadar sürer?", "evime yol tarifi", "Taksim'e nasıl giderim", "ev adresim ne", None):
        assert not service.from_home_requested(text), text
    assert service.user_named("Kadıköy'den Taksim'e ne kadar sürer?", "Kadıköy, İstanbul")
    assert service.user_named("kadıköyden taksime", "Kadıköy")
    assert not service.user_named("Taksim'e ne kadar sürer?", "Kadıköy, İstanbul")
    assert not service.user_named(None, "Kadıköy")


def test_check_origin_overrides_home_and_the_current_label_unless_the_user_said_so(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    # the model copied the home Location / "home" although the user said nothing about home: dropped
    for origin in ("home", "ev", "Goztepe, Turkiye", "goztepe, turkiye"):
        check = service.check_origin(ctx_saying(config, paths, db, "Taksim'e ne kadar sürer?"), origin)
        assert check.origin is None and "did not say the route starts at home" in (check.note or ""), origin
    # "evden": home is the origin
    for text in ("Evden Taksim'e ne kadar sürer?", "evimden Taksim'e yol tarifi"):
        assert service.check_origin(ctx_saying(config, paths, db, text), "home") == service.OriginCheck("home")
    # the model copied the profile's Current Location label (any precision): dropped, the exact location is used
    for origin in ("Kadıköy, Caferağa", "Kadıköy, Türkiye", "Moda Caddesi 12, Caferağa, Kadıköy", "Kadıköy, İstanbul"):
        check = service.check_origin(ctx_saying(config, paths, db, "Taksim'e nasıl giderim?"), origin)
        assert check.origin is None and "Current Location label" in (check.note or ""), origin
    # but a place the user named stays, even when it is where they are
    assert service.check_origin(
        ctx_saying(config, paths, db, "Kadıköy'den Taksim'e ne kadar sürer?"), "Kadıköy, İstanbul"
    ) == service.OriginCheck("Kadıköy, İstanbul")
    assert service.check_origin(ctx_saying(config, paths, db, "Beşiktaş'tan Taksim'e"), "Beşiktaş, İstanbul") == service.OriginCheck(
        "Beşiktaş, İstanbul"
    )
    # without the user's message (no runtime text) nothing is second-guessed
    assert service.check_origin(ctx_for(config, paths, db), "home") == service.OriginCheck("home")
    assert service.check_origin(ctx_saying(config, paths, db, "x"), None) == service.OriginCheck(None)


def test_travel_time_overrides_a_home_origin_the_user_did_not_ask_for(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    api = Api()
    tool = TravelTime(ctx_saying(config, paths, db, "Taksim'e arabayla ne kadar sürer?"), client=make_client(config, db, api))
    out = tool.run(tool.parse_args({"origin": "Goztepe, Turkiye", "destination": "Taksim", "mode": "driving"})).content
    assert api.body()["origin"] == {"location": {"latLng": {"latitude": KADIKOY["latitude"], "longitude": KADIKOY["longitude"]}}}
    lines = out.splitlines()
    assert lines[0].startswith("Note: origin 'Goztepe, Turkiye' was ignored: the user did not say the route starts at home")
    assert lines[1].startswith("Kadıköy, Caferağa (current location, source shortcut, 5 min ago) -> Taksim by driving")
    assert not COORD_RE.search(out)
    # the Current Location label copied from the profile: same, exact coordinates instead
    out = tool.run(tool.parse_args({"origin": "Kadıköy, Caferağa", "destination": "Taksim"})).content
    assert api.body()["origin"] == {"location": {"latLng": {"latitude": KADIKOY["latitude"], "longitude": KADIKOY["longitude"]}}}
    assert "was ignored: that is the Current Location label" in out


def test_evden_routes_from_home_and_eve_routes_to_home(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    api = Api()
    tool = TravelTime(ctx_saying(config, paths, db, "Evden Taksim'e ne kadar sürer?"), client=make_client(config, db, api))
    out = tool.run(tool.parse_args({"origin": "home", "destination": "Taksim"})).content
    assert api.body()["origin"] == {"address": "Goztepe, Turkiye"} and out.startswith("Goztepe, Turkiye (home) -> Taksim by transit")
    tool = TravelTime(ctx_saying(config, paths, db, "Eve ne kadar sürer?"), client=make_client(config, db, api))
    out = tool.run(tool.parse_args({"destination": "eve"})).content
    assert api.body()["origin"] == {"location": {"latLng": {"latitude": KADIKOY["latitude"], "longitude": KADIKOY["longitude"]}}}
    assert api.body()["destination"] == {"address": "Goztepe, Turkiye"}
    assert out.startswith("Kadıköy, Caferağa (current location, source shortcut, 5 min ago) -> Goztepe, Turkiye (home) by transit")


def test_travel_time_with_a_stale_or_missing_current_location_asks_and_offers_the_last_known_one(config, paths, db):
    write_profile(paths)
    api = Api()
    # stale: older than max_age_min and every source fails (conftest) -> ask, never the home Location
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=config.location.max_age_min * 3)))
    tool = TravelTime(ctx_saying(config, paths, db, "Taksim'e ne kadar sürer?"), client=make_client(config, db, api))
    out = tool.run(tool.parse_args({"destination": "Taksim"})).content
    assert out.startswith(
        "Current location unknown on this Mac (last known: Kadıköy, Türkiye, 3 h ago, older than 60 min); last refresh failed:"
    )
    assert "Do not assume the home Location" in out and "origin = 'home'" in out and "origin = 'last known'" in out
    assert "Goztepe" not in out and not COORD_RE.search(out) and api.seen == []
    # the user says the old one is still right: origin = "last known" uses it, labelled as such
    out = tool.run(tool.parse_args({"origin": "last known", "destination": "Taksim"})).content
    assert api.body()["origin"] == {"location": {"latLng": {"latitude": KADIKOY["latitude"], "longitude": KADIKOY["longitude"]}}}
    assert out.startswith("Kadıköy, Caferağa (last known location, source shortcut, 3 h ago) -> Taksim by transit")
    # missing altogether
    save_state(paths.location_state_file, LocationState())
    out = tool.run(tool.parse_args({"destination": "Taksim"})).content
    assert out.startswith("Current location unknown on this Mac; last refresh failed:") and "last known" not in out
    assert tool.run(tool.parse_args({"origin": "last known", "destination": "Taksim"})).content.startswith(
        "Current location unknown on this Mac (no last known location either)"
    )
    assert len(api.seen) == 1


def test_open_in_maps_route_starts_from_the_fresh_current_location_else_from_the_device(config, paths, db, monkeypatch):
    from jarvis.tools import maps as maps_tools
    from jarvis.tools.maps import OpenInMaps

    write_profile(paths)
    urls: list[str] = []
    monkeypatch.setattr(maps_tools, "open_url", lambda url: urls.append(url))
    # fresh current location: its coordinates are the origin, but they never reach the model
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=5)))
    tool = OpenInMaps(ctx_saying(config, paths, db, "Taksim'e gidişi haritada göster"), client=make_client(config, db))
    out = tool.run(tool.parse_args({"destination": "Taksim"})).content
    assert urls[-1] == "https://maps.apple.com/?saddr=40.990300%2C29.029000&daddr=Taksim&dirflg=r"
    assert out.splitlines() == [
        "Opened Apple Maps: directions from Kadıköy, Caferağa (current location, source shortcut, 5 min ago) -> Taksim (transit)",
        "https://maps.apple.com/?saddr=current-location&daddr=Taksim&dirflg=r",
    ]
    assert not COORD_RE.search(out)
    # the model copied the home Location into origin without "evden": dropped, the current location again
    out = tool.run(tool.parse_args({"origin": "Goztepe, Turkiye", "destination": "Taksim"})).content
    assert urls[-1] == "https://maps.apple.com/?saddr=40.990300%2C29.029000&daddr=Taksim&dirflg=r"
    assert out.startswith("Note: origin 'Goztepe, Turkiye' was ignored")
    # stale current location: destination only, Apple Maps starts from its own live location
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=config.location.max_age_min * 3)))
    out = tool.run(tool.parse_args({"destination": "Taksim", "mode": "driving"})).content
    assert urls[-1] == "https://maps.apple.com/?daddr=Taksim&dirflg=d"
    assert out.startswith(
        "Opened Apple Maps: directions from the device's live location (Maps' own; no fresh current location known) -> Taksim (driving)"
    )
    # Google Maps: api=1 and destination only
    config.maps.open_with = "google"
    tool.run(tool.parse_args({"destination": "Taksim", "mode": "driving"}))
    assert urls[-1] == "https://www.google.com/maps/dir/?api=1&destination=Taksim&travelmode=driving"
    # "evden Taksim'e": home is the origin; "eve gidişi göster": home is the destination
    tool = OpenInMaps(ctx_saying(config, paths, db, "Evden Taksim'e gidişi haritada göster"), client=make_client(config, db))
    tool.run(tool.parse_args({"origin": "home", "destination": "Taksim"}))
    assert urls[-1] == "https://www.google.com/maps/dir/?api=1&origin=Goztepe%2C+Turkiye&destination=Taksim&travelmode=transit"
    tool = OpenInMaps(ctx_saying(config, paths, db, "Eve gidişi haritada göster"), client=make_client(config, db))
    out = tool.run(tool.parse_args({"destination": "eve"})).content
    assert urls[-1] == "https://www.google.com/maps/dir/?api=1&destination=Goztepe%2C+Turkiye&travelmode=transit"
    assert "-> Goztepe, Turkiye (home) (transit)" in out


def test_telegram_turn_routes_from_the_shared_location_never_the_mac_and_asks_when_it_is_stale(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=1)))
    api = Api()
    remote = ctx_saying(config, paths, db, "Taksim'e ne kadar sürer?", channel="telegram")
    tool = TravelTime(remote, client=make_client(config, db, api))
    assert (
        tool.run(tool.parse_args({"destination": "Taksim"})).content.startswith("Current location unknown from the phone")
        and api.seen == []
    )
    # the model falls back to the home Location on its own: overridden, still asks
    out = tool.run(tool.parse_args({"origin": "home", "destination": "Taksim"})).content
    assert "was ignored" in out and "Current location unknown from the phone" in out and api.seen == []
    service.store_telegram_location(paths, 52.3547, 4.8921)
    out = tool.run(tool.parse_args({"destination": "Taksim"})).content
    assert api.body()["origin"] == {"location": {"latLng": {"latitude": 52.3547, "longitude": 4.8921}}}
    assert out.startswith("the shared location (current location, source telegram, just now) -> Taksim by transit")
    # a stale share: ask for a new one (with the last one offered), the Mac's fresh location is still not used
    state = load_state(paths.location_state_file)
    state.telegram = loc(
        source="telegram",
        age_min=config.location.max_age_min * 2,
        latitude=52.3547,
        longitude=4.8921,
        street="",
        neighborhood="",
        city="Amsterdam",
        region="",
        country="",
    )
    save_state(paths.location_state_file, state)
    out = tool.run(tool.parse_args({"destination": "Taksim"})).content
    assert (
        out.startswith("Current location unknown from the phone")
        and "Last shared location: Amsterdam, 2 h ago" in out
        and len(api.seen) == 1
    )


def test_waypoint_distinguishes_coordinates_from_addresses():
    assert waypoint("40.990300,29.029000") == {"location": {"latLng": {"latitude": 40.9903, "longitude": 29.029}}}
    assert waypoint("Kadıköy, Istanbul") == {"address": "Kadıköy, Istanbul"}
    assert waypoint("95,29") == {"address": "95,29"}


# --- Telegram: remote turns -------------------------------------------------------------------------------------------


def test_remote_turns_use_the_shared_telegram_location_and_never_the_mac(config, paths, db):
    write_profile(paths)
    mac = loc(age_min=1, base=NOW)
    save_state(paths.location_state_file, LocationState(mac=mac))
    remote = ctx_for(config, paths, db, channel="telegram")
    resolved = service.current_location(remote, now=NOW)
    assert resolved.location is None and resolved.remote and resolved.stale is None
    shared = service.store_telegram_location(paths, 52.3547, 4.8921, accuracy_m=12, now=NOW - timedelta(minutes=20))
    assert shared.source == "telegram" and load_state(paths.location_state_file).mac == mac  # the Mac's entry is untouched
    resolved = service.current_location(remote, now=NOW)
    assert resolved.location == shared and resolved.remote
    place = service.resolve_default_place(remote, now=NOW)
    assert (
        place.kind == "current"
        and place.point == (52.3547, 4.8921)
        and place.label == "the shared location (current location, source telegram, 20 min ago)"
    )
    # the local channel still sees the Mac
    local = service.current_location(ctx_for(config, paths, db, channel="voice"), now=NOW)
    assert local.location == mac and not local.remote
    # an old shared location is not used: the tool asks for a new share instead of guessing
    resolved = service.current_location(remote, now=NOW + timedelta(hours=2))
    assert resolved.location is None and resolved.stale == shared
    place = service.resolve_default_place(remote, now=NOW + timedelta(hours=2))
    assert (
        place.kind == "ask"
        and "Mac's location is not the user's" in place.ask
        and "Last shared location: coordinates only, 2 h ago" in place.ask
    )


def test_remote_weather_and_maps_ask_for_a_location_instead_of_using_the_mac(config, paths, db):
    write_profile(paths)
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=1)))
    remote = ctx_for(config, paths, db, channel="telegram")
    seen: list[httpx.Request] = []
    weather._transport = _transport(BERLIN, seen=seen)
    out = GetWeather(remote).run(GetWeather.input_model()).content
    assert out.startswith("Current location unknown from the phone") and seen == []
    api = Api()
    travel = TravelTime(remote, client=make_client(config, db, api))
    assert travel.run(travel.parse_args({"destination": "Sirkeci"})).content.startswith("Current location unknown from the phone")
    search = SearchPlaces(remote, client=make_client(config, db, api))
    assert search.run(search.parse_args({"query": "eczane"})).content.startswith("Current location unknown from the phone")
    assert api.seen == []
    # with a fresh shared location the tools work from the phone
    service.store_telegram_location(paths, 52.3547, 4.8921)
    out = GetWeather(remote).run(GetWeather.input_model()).content
    assert seen[0].url.params["latitude"] == "52.3547" and out.startswith(
        "the shared location (current location, source telegram, just now)"
    )
    travel.run(travel.parse_args({"destination": "eve"}))
    assert api.body()["origin"] == {"location": {"latLng": {"latitude": 52.3547, "longitude": 4.8921}}}
    assert api.body()["destination"] == {"address": "Goztepe, Turkiye"}
    # a named place never needs the location
    out = GetWeather(remote).run(GetWeather.input_model(location="Berlin")).content
    assert out.startswith("Berlin, Land Berlin, Germany: now")


def test_telegram_bot_stores_a_shared_location_and_offers_the_button(config, paths, logs):
    from tests.unit.test_telegram_bot import audit_lines, make_bot, message_update

    bot, api = make_bot(config, paths)
    api.batches.extend(
        [
            [message_update("/location")],
            [message_update(location={"latitude": 52.3547, "longitude": 4.8921, "horizontal_accuracy": 30})],
            [message_update(location={"latitude": 52.3547, "longitude": 4.8921}, forward_date=1)],
            [message_update(location={"latitude": 95, "longitude": 4.8921})],
        ]
    )
    bot.run()
    sent = api.of("sendMessage")
    assert sent[0]["reply_markup"] == {
        "keyboard": [[{"text": "📍 Konumumu paylaş", "request_location": True}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    assert sent[1]["text"].startswith("📍 Konum alındı (") and sent[1]["reply_markup"] == {"remove_keyboard": True}
    assert "başkasının konumu" in sent[2]["text"] and sent[3]["text"] == "Konum okunamadı (geçersiz koordinat)."
    state = load_state(paths.location_state_file)
    assert state.telegram.latitude == 52.3547 and state.telegram.accuracy_m == 30 and state.mac is None
    decisions = [(l["reason"]) for l in audit_lines(logs) if l.get("tool") == "telegram" and l.get("decision") in ("location", "ignored")]
    assert decisions == ["telegram_location_stored", "forwarded_location", "invalid_location"]
    assert bot._assistant is None or bot._assistant.calls == []  # a location message is not a model turn
    logged = (logs / "jarvis.log").read_text() + (logs / "audit.jsonl").read_text()
    assert "52.35" not in logged and "4.89" not in logged


# --- config: migration and validation -------------------------------------------------------------------------------------


def test_weather_default_location_migrates_to_fixed_location(paths):
    text = paths.config_file.read_text()
    text = re.sub(r"^location_source = .*\n", "", text, flags=re.M)
    text = re.sub(r"^fixed_location = .*$", 'default_location = "Istanbul"', text, flags=re.M)
    paths.config_file.write_text(text)
    config = load_config(paths.config_file)
    assert (
        config.weather.location_source == "current"
        and config.weather.fixed_location == "Istanbul"
        and config.weather.default_location == "Istanbul"
    )
    assert config.deprecated_keys == ["weather.default_location (use weather.fixed_location and weather.location_source)"]
    checks = {c.name: c for c in service.health_checks(config, paths)}
    assert checks["weather.default_location"].warn and "fixed_location" in checks["weather.default_location"].detail
    # both keys: the new one wins, the old is still flagged
    paths.config_file.write_text(text.replace('default_location = "Istanbul"', 'default_location = "Istanbul"\nfixed_location = "Ankara"'))
    config = load_config(paths.config_file)
    assert config.weather.fixed_location == "Ankara" and config.deprecated_keys
    # the shipped example config has no deprecated key
    assert load_config(Path(__file__).resolve().parents[2] / "config.example.toml").deprecated_keys == []


def test_location_config_defaults_and_validation(paths):
    config = load_config(paths.config_file)
    lc = config.location
    assert (lc.enabled, lc.timeout_s, lc.max_age_min, lc.profile_precision, lc.keep_history, lc.ip_fallback) == (
        True,
        5.0,
        60,
        "neighborhood",
        False,
        True,
    )
    assert (
        lc.shortcut_name == "Jarvis Location"
        and config.shortcuts.entry("Jarvis Location").risk == "safe"
        and config.shortcuts.entry("Jarvis Location").remote is False
    )
    text = paths.config_file.read_text()
    paths.config_file.write_text(text.replace('ip_lookup_url = "https://ipinfo.io/json"', 'ip_lookup_url = "http://ipinfo.io/json"'))
    with pytest.raises(ConfigError, match="https"):
        load_config(paths.config_file)
    paths.config_file.write_text(text.replace('profile_precision = "neighborhood"', 'profile_precision = "exact"'))
    with pytest.raises(ConfigError, match="profile_precision"):
        load_config(paths.config_file)
    paths.config_file.write_text(text.replace('shortcut_name = "Jarvis Location"', 'shortcut_name = "-x"'))
    with pytest.raises(ConfigError, match="shortcut_name"):
        load_config(paths.config_file)


def test_state_file_round_trip_and_tolerance(paths):
    state = LocationState(mac=loc(), telegram=None, last_error="x", last_attempt_at="2026-10-16T14:30:00.000Z")
    save_state(paths.location_state_file, state)
    loaded = load_state(paths.location_state_file)
    assert loaded.mac == state.mac and loaded.telegram is None and loaded.last_error == "x"
    paths.location_state_file.write_text("{not json")
    assert load_state(paths.location_state_file).mac is None
    paths.location_state_file.write_text(json.dumps({"mac": {"latitude": 500, "longitude": 1, "source": "ip", "updated_at": "x"}}))
    assert load_state(paths.location_state_file).mac is None
    assert load_state(paths.home / "missing.json").mac is None


# --- CLI and doctor --------------------------------------------------------------------------------------------------------


def test_cli_location_show_and_refresh(config, paths, monkeypatch):
    write_profile(paths)
    console = Console(file=StringIO(), force_terminal=False, width=200)
    args = main_mod.build_parser().parse_args(["location"])
    assert args.location_command == "show"
    main_mod.cmd_location(args, config, paths, console)
    out = console.file.getvalue()
    assert "Mac konumu: bilinmiyor" in out and "Telegram'dan paylaşılan konum: yok" in out and "Jarvis Location" in out
    calls: list[str] = []
    monkeypatch.setattr(
        sources,
        "SOURCES",
        {
            "shortcut": source_returning(None, calls, "shortcut"),
            "ip": source_returning(loc(source="ip", approximate=True, street="", neighborhood=""), calls, "ip"),
        },
    )
    monkeypatch.setattr(sources, "SOURCE_ORDER", ("shortcut", "ip"))
    console = Console(file=StringIO(), force_terminal=False, width=200)
    main_mod.cmd_location(main_mod.build_parser().parse_args(["location", "refresh"]), config, paths, console)
    out = console.file.getvalue()
    assert "Konum güncellendi, kaynak: ip; önce denenen: shortcut (shortcut failed)" in out
    assert "Mac konumu: Kadıköy, Türkiye (yaklaşık, IP tabanlı" in out and "kaynak: ip" in out
    assert "- Current Location: Kadıköy, Türkiye (approximate, IP-based" in paths.profile_file.read_text()
    assert "location" in main_mod._subcommand_tree(main_mod.build_parser())[""]
    assert main_mod._subcommand_tree(main_mod.build_parser())["location"] == ["show", "refresh"]


def test_doctor_lines(config, paths, monkeypatch):
    monkeypatch.setattr("jarvis.tools.shortcuts.installed_shortcuts", lambda timeout=15.0: ["Jarvis Location"])
    monkeypatch.setattr(sources, "corelocation_installed", lambda: False)
    checks = {c.name: c for c in service.health_checks(config, paths)}
    assert checks["location shortcut"].ok and "in the Shortcuts app" in checks["location shortcut"].detail
    assert "not installed" in checks["location CoreLocationCLI"].detail
    assert checks["current location"].warn and "not detected yet" in checks["current location"].detail
    monkeypatch.setattr("jarvis.tools.shortcuts.installed_shortcuts", lambda timeout=15.0: [])
    save_state(paths.location_state_file, LocationState(mac=loc(age_min=2)))
    checks = {c.name: c for c in service.health_checks(config, paths)}
    assert checks["location shortcut"].warn and "create it" in checks["location shortcut"].detail
    assert checks["current location"].detail.startswith("Kadıköy, Türkiye, source shortcut, 2 min ago")
    assert not COORD_RE.search(" ".join(c.detail for c in checks.values()))
    config.location.enabled = False
    assert [c.detail for c in service.health_checks(config, paths)] == ["disabled (location.enabled = false)"]
    from jarvis.observability.health import INTEGRATION_MODULES

    assert "jarvis.location.service" in INTEGRATION_MODULES


# --- the Assistant: session start and the profile re-read -----------------------------------------------------------------


def test_assistant_starts_a_background_refresh_for_local_sessions_only(config, paths, monkeypatch):
    from tests.unit.test_assistant import make_assistant

    started: list[str] = []
    monkeypatch.setattr(service, "refresh_in_background", lambda cfg, p, **kw: started.append(cfg.location.shortcut_name))
    make_assistant(config, paths, [], channel="cli")
    make_assistant(config, paths, [], channel="voice").new_session()
    assert started == ["Jarvis Location"] * 3
    make_assistant(config, paths, [], channel="telegram")
    assert len(started) == 3


def test_assistant_rereads_the_profile_when_the_block_changes(config, paths):
    import os

    from tests.unit.test_assistant import make_assistant

    write_profile(paths)
    assistant = make_assistant(config, paths, [])
    assert "Current Location" not in (assistant.profile_text or "")
    system = assistant.build_system()
    assert "Current Location" not in system[2]["text"]
    service.write_profile_block(paths, loc(), "neighborhood")
    os.utime(paths.profile_file, (time.time() + 2, time.time() + 2))  # a filesystem with coarse mtimes must still notice
    system = assistant.build_system()
    assert "- Current Location: Kadıköy, Caferağa" in system[2]["text"] and "- Location: Goztepe, Turkiye" in system[2]["text"]
    assert not COORD_RE.search(system[2]["text"])
    assert assistant.refresh_profile_if_changed() is False
