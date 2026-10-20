"""The Location card on the Overview: every source, the stale state, refresh success and
failure, coordinates hidden until clicked, the Maps link format, the status strip text, escaping, and no
external resource on the page."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

import jarvis.location.service as location_service
from jarvis.dashboard.state import Dashboard, LocationView
from jarvis.dashboard.ui_text import UI
from jarvis.location.model import Location
from jarvis.location.store import LocationState, save_state
from tests.unit.test_dashboard import login, make_client, quiet_services  # noqa: F401 - fixture

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone(timedelta(hours=3)))
EVIL = "<img src=x onerror=alert(1)>"  # for the profile line (the Location model itself strips angle brackets)
MARK = 'Caferağa & "x"'  # for address fields: must come out as &amp; and &#34;


def iso(minutes_ago: float) -> str:
    dt = (NOW - timedelta(minutes=minutes_ago)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def iso_real(minutes_ago: float) -> str:
    """Relative to the real clock: the pages compute ages against now, not the fixed NOW of the view tests."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def mac_location(source="shortcut", minutes_ago=12, real_clock=False, **fields) -> Location:
    base = dict(
        latitude=40.98765,
        longitude=29.02345,
        source=source,
        updated_at=(iso_real if real_clock else iso)(minutes_ago),
        street="Moda Cd.",
        neighborhood=MARK,
        city="Kadıköy",
        region="İstanbul",
        country="Türkiye",
    )
    base.update(fields)
    return Location(**base)


@pytest.fixture
def board(config, paths, quiet_services) -> Dashboard:
    paths.profile_file.write_text("# Profile\n\n- Location: Goztepe, Turkiye " + EVIL + "\n", encoding="utf-8")
    return Dashboard(config, paths, auto_full_check=False)


def test_view_for_each_source_and_the_stale_state(board, paths):
    assert board.location_view(now=NOW) == LocationView(enabled=True, home="Goztepe, Turkiye " + EVIL)
    for source, label in (("shortcut", "Shortcut"), ("corelocation", "CoreLocationCLI"), ("ip", "IP (approximate)")):
        save_state(paths.location_state_file, LocationState(mac=mac_location(source, approximate=(source == "ip"))))
        view = board.location_view(now=NOW)
        assert view.source == source and view.source_label == label and view.has_location and not view.stale
        assert view.age_text == "12 min ago" and view.updated_at.strftime("%H:%M") == "11:48"
        assert view.coordinates == "40.98765,29.02345" and view.approximate == (source == "ip")
    assert view.label == "Kadıköy, Türkiye"  # an IP location is city level whatever the precision
    save_state(paths.location_state_file, LocationState(mac=mac_location("shortcut")))
    view = board.location_view(now=NOW)
    assert view.label == "Kadıköy, " + MARK and view.strip_text == MARK
    save_state(paths.location_state_file, LocationState(mac=mac_location("shortcut", minutes_ago=61), last_error="shortcut: timed out"))
    stale = board.location_view(now=NOW)
    assert stale.stale and stale.age_text == "1 h ago" and stale.error == "shortcut: timed out"
    board.config.location.max_age_min = 120
    assert not board.location_view(now=NOW).stale


def test_telegram_location_is_shown_separately(board, paths):
    telegram = Location(latitude=41.0, longitude=28.9, source="telegram", updated_at=iso(5))
    save_state(paths.location_state_file, LocationState(mac=mac_location(), telegram=telegram))
    view = board.location_view(now=NOW)
    assert view.telegram_coordinates == "41.00000,28.90000" and view.telegram_age_text == "5 min ago"
    assert view.telegram_label == UI.LOCATION_ADDRESS_UNKNOWN and view.telegram_maps_url.startswith(
        "https://maps.apple.com/?q=41.00000%2C28.90000"
    )
    assert view.label.startswith("Kadıköy")  # the Mac's location is still the current one


def test_maps_url_follows_the_configured_app(board, paths):
    save_state(paths.location_state_file, LocationState(mac=mac_location()))
    assert board.location_view(now=NOW).maps_url == "https://maps.apple.com/?q=40.98765%2C29.02345"
    board.config.maps.open_with = "google"
    assert board.location_view(now=NOW).maps_url == "https://www.google.com/maps/search/?api=1&query=40.98765%2C29.02345"


def test_refresh_success_and_failure(board, paths, monkeypatch):
    assert board.refresh_location() == UI.LOCATION_FAILED  # every source is unavailable in tests
    state = paths.location_state_file
    assert state.is_file() and "unavailable" in (state.read_text(encoding="utf-8"))
    calls = []

    def fake_refresh(config, p, *, force=False, **kw):
        calls.append(force)
        save_state(p.location_state_file, LocationState(mac=mac_location("corelocation", minutes_ago=0)))
        return location_service.RefreshResult(mac_location("corelocation", minutes_ago=0), True, {})

    monkeypatch.setattr(location_service, "refresh", fake_refresh)
    assert board.refresh_location() is None and calls == [True]
    assert board.location_view(now=NOW).source == "corelocation"
    board.config.location.enabled = False
    assert board.refresh_location() == UI.LOCATION_DISABLED and board.location_view(now=NOW).enabled is False


def test_card_strip_and_refresh_in_the_page(config, paths, board, monkeypatch):
    save_state(
        paths.location_state_file,
        LocationState(
            mac=mac_location(minutes_ago=90, real_clock=True),
            telegram=Location(latitude=41.0, longitude=28.9, source="telegram", updated_at=iso_real(5)),
        ),
    )
    client, store = make_client(config, paths, board)
    csrf = login(client, store)
    page = client.get("/")
    html = page.text
    assert page.status_code == 200
    assert (
        EVIL not in html and "Caferağa &amp; &#34;x&#34;" in html and "Turkiye &lt;img src=x onerror=alert(1)&gt;" in html
    )  # address and profile line escaped
    for text in (
        UI.LOCATION_TITLE,
        UI.LOCATION_CURRENT,
        UI.LOCATION_HOME,
        UI.LOCATION_TELEGRAM,
        UI.LOCATION_SOURCE,
        UI.LOCATION_UPDATED,
        UI.LOCATION_STALE,
        UI.LOCATION_REFRESH,
        UI.LOCATION_OPEN_MAPS,
        UI.LOCATION_SHOW_COORDS,
        UI.LOCATION_HIDE_COORDS,
        "Shortcut",
    ):
        assert text in html, text
    # coordinates sit inside a closed <details>: present for the click, not shown by default
    card = html[html.index('id="location"') : html.index('id="actions"')]
    assert "<details" in card and "<details open" not in card and "40.98765,29.02345" in card
    assert 'href="https://maps.apple.com/?q=40.98765%2C29.02345" target="_blank" rel="noopener noreferrer"' in card
    # the strip links to the card with the short place name
    strip = html[html.index('id="strip"') : html.index("</header>")]
    assert 'href="/#location"' in strip and "Caferağa &amp; &#34;x&#34;" in strip and UI.LOCATION_STALE in strip
    # nothing external is loaded: every src and stylesheet is local; the Maps links are the only external hrefs and they need a click
    assert not re.findall(r'(?:src|<link[^>]*href)="https?://', html)
    externals = re.findall(r'href="(https?://[^"]+)"', html)
    assert externals and all(url.startswith(("https://maps.apple.com/", "https://www.google.com/maps/")) for url in externals)
    # refresh: failure shows the message, success re-renders the card
    response = client.post("/location/refresh", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and UI.LOCATION_FAILED in response.text and 'id="location"' in response.text
    monkeypatch.setattr(
        location_service,
        "refresh",
        lambda c, p, **kw: (
            save_state(p.location_state_file, LocationState(mac=mac_location("ip", minutes_ago=0, real_clock=True, approximate=True)))
            or location_service.RefreshResult(mac_location("ip", minutes_ago=0, real_clock=True, approximate=True), True, {})
        ),
    )
    response = client.post("/location/refresh", headers={"X-CSRF-Token": csrf})
    assert UI.LOCATION_FAILED not in response.text and UI.LOCATION_APPROXIMATE in response.text and "IP (approximate)" in response.text
    assert client.post("/location/refresh").status_code == 403  # CSRF
    assert client.get("/partials/location").status_code == 200


def test_empty_state_without_any_location(config, paths, board):
    client, store = make_client(config, paths, board)
    login(client, store)
    html = client.get("/").text
    assert (
        UI.LOCATION_NONE in html
        and "maps.apple.com" not in html
        and UI.LOCATION_STALE not in html[html.index('id="strip"') : html.index("</header>")]
    )
