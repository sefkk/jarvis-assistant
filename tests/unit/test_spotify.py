"""Spotify tools: AppleScript constants with argv, URI validation, token cache, credentials."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.tools import spotify
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.tools.spotify import (
    Next,
    NowPlaying,
    Pause,
    Play,
    Previous,
    SearchAndPlay,
    health_checks,
)

QUERY = 'Kind of Blue" & (do shell script "id") & "'


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    spotify.reset_token_cache()
    yield
    spotify.reset_token_cache()
    spotify._transport = None


@pytest.fixture
def creds(monkeypatch):
    store = {"spotify_client_id": "cid", "spotify_client_secret": "csec"}
    monkeypatch.setattr(spotify.secrets, "get_secret", lambda name: store.get(name))
    return store


@pytest.fixture
def scripts(monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    def fake_run(script, args=(), **kwargs):
        calls.append((script, list(args)))
        if script is spotify.NOW_PLAYING_SCRIPT:
            return "state=playing\nartist=Miles Davis\ntrack=So What\nalbum=Kind of Blue\nposition=75\nduration=562"
        return "ok"

    monkeypatch.setattr(spotify, "run_applescript", fake_run)
    return calls


def _api(routes: dict[str, httpx.Response | list[httpx.Response]], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.method} {request.url.scheme}://{request.url.host}{request.url.path}"
        response = routes.get(key)
        if response is None:
            return httpx.Response(404, text="no route")
        if isinstance(response, list):
            return response.pop(0)
        return response

    return httpx.MockTransport(handler)


def _ok_search(uri: str, kind: str = "track", name: str = "So What") -> httpx.Response:
    item = {"uri": uri, "name": name, "artists": [{"name": "Miles Davis"}]}
    if kind == "playlist":
        item = {"uri": uri, "name": name, "owner": {"display_name": "Spotify"}}
    return httpx.Response(200, json={f"{kind}s": {"items": [item]}})


def _token(expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": expires_in})


# --- playback through AppleScript constants ----------------------------------------------------


def test_simple_playback_tools_use_constant_scripts_without_args(scripts):
    for cls, script in (
        (Play, spotify.PLAY_SCRIPT),
        (Pause, spotify.PAUSE_SCRIPT),
        (Next, spotify.NEXT_SCRIPT),
        (Previous, spotify.PREVIOUS_SCRIPT),
    ):
        tool = cls()
        assert tool.risk == RiskLevel.SAFE and tool.taints is False
        result = tool.run(tool.parse_args({}))
        assert not result.is_error
        assert scripts[-1] == (script, [])
        assert "on run argv" in script and 'tell application "Spotify"' in script


def test_now_playing_parses_fields(scripts):
    result = NowPlaying().run(NowPlaying().parse_args({}))
    assert "So What" in result.content and "Miles Davis" in result.content and "Kind of Blue" in result.content
    assert "Playing" in result.content and "1:15" in result.content and "9:22" in result.content


def test_now_playing_when_stopped(monkeypatch):
    monkeypatch.setattr(spotify, "run_applescript", lambda script, args=(), **kw: "state=stopped")
    assert "nothing is playing" in NowPlaying().run(NowPlaying().parse_args({})).content
    monkeypatch.setattr(spotify, "run_applescript", lambda script, args=(), **kw: "state=not running")
    assert "not running" in NowPlaying().run(NowPlaying().parse_args({})).content


# --- search_and_play -----------------------------------------------------------------------------


def test_search_and_play_passes_uri_as_argv_and_never_the_query(creds, scripts):
    seen: list[httpx.Request] = []
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": _token(),
            "GET https://api.spotify.com/v1/search": _ok_search("spotify:track:7bKmUzNOW0Lr5s0hgmD9cE"),
        },
        seen,
    )
    tool = SearchAndPlay()
    result = tool.run(tool.parse_args({"query": QUERY}))
    assert "So What" in result.content and "spotify:track:7bKmUzNOW0Lr5s0hgmD9cE" in result.content

    script, args = scripts[-1]
    assert script is spotify.PLAY_URI_SCRIPT
    assert args == ["spotify:track:7bKmUzNOW0Lr5s0hgmD9cE"]
    assert "Kind of Blue" not in script and "do shell script" not in script
    assert "item 1 of argv" in script

    token_request, search_request = seen
    assert token_request.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in token_request.content
    assert search_request.url.params["q"] == QUERY
    assert search_request.url.params["type"] == "track" and search_request.url.params["limit"] == "5"
    assert search_request.headers["authorization"] == "Bearer tok"


def test_search_and_play_kind_and_playlist_label(creds, scripts):
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": _token(),
            "GET https://api.spotify.com/v1/search": _ok_search("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "playlist", "Today's Top Hits"),
        },
        [],
    )
    tool = SearchAndPlay()
    result = tool.run(tool.parse_args({"query": "top hits", "kind": "playlist"}))
    assert "playlist 'Today's Top Hits' by Spotify" in result.content
    assert scripts[-1][1] == ["spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"]
    with pytest.raises(ToolValidationError):
        tool.parse_args({"query": "x", "kind": "podcast"})
    with pytest.raises(ToolValidationError):
        tool.run(tool.parse_args({"query": "   "}))


@pytest.mark.parametrize(
    "bad_uri",
    [
        "spotify:track:abc def",
        "spotify:track:abc;rm -rf /",
        "https://open.spotify.com/track/abc",
        "spotify:album:abc123",  # right shape, wrong kind for a track search
        "spotify:track:",
        "",
        None,
        'spotify:track:abc" & (do shell script "id")',
    ],
)
def test_garbage_uris_from_the_api_are_rejected(creds, scripts, bad_uri):
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": _token(),
            "GET https://api.spotify.com/v1/search": httpx.Response(200, json={"tracks": {"items": [{"uri": bad_uri, "name": "x"}]}}),
        },
        [],
    )
    tool = SearchAndPlay()
    with pytest.raises(ToolExecutionError):
        tool.run(tool.parse_args({"query": "x"}))
    assert not any(s is spotify.PLAY_URI_SCRIPT for s, _ in scripts)


def test_null_items_are_skipped_and_empty_results_reported(creds, scripts):
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": _token(),
            "GET https://api.spotify.com/v1/search": [
                httpx.Response(200, json={"tracks": {"items": [None, {"uri": "spotify:track:Abc123", "name": "Y"}]}}),
                httpx.Response(200, json={"tracks": {"items": []}}),
            ],
        },
        [],
    )
    tool = SearchAndPlay()
    assert "spotify:track:Abc123" in tool.run(tool.parse_args({"query": "y"})).content
    with pytest.raises(ToolExecutionError, match="no track found"):
        tool.run(tool.parse_args({"query": "nothing"}))


def test_token_is_cached_until_expiry(creds, scripts, monkeypatch):
    seen: list[httpx.Request] = []
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": [_token(), _token(expires_in=120), _token()],
            "GET https://api.spotify.com/v1/search": _ok_search("spotify:track:Abc123"),
        },
        seen,
    )
    tool = SearchAndPlay()
    tool.run(tool.parse_args({"query": "a"}))
    tool.run(tool.parse_args({"query": "b"}))
    assert [r.url.host for r in seen] == ["accounts.spotify.com", "api.spotify.com", "api.spotify.com"]

    clock = {"t": 10_000.0}
    monkeypatch.setattr(spotify.time, "monotonic", lambda: clock["t"])
    spotify.reset_token_cache()
    tool.run(tool.parse_args({"query": "c"}))  # refetch (expires_in 120 -> valid for 60s)
    clock["t"] += 30
    tool.run(tool.parse_args({"query": "d"}))  # still cached
    clock["t"] += 31
    tool.run(tool.parse_args({"query": "e"}))  # expired -> new token
    hosts = [r.url.host for r in seen[3:]]
    assert hosts.count("accounts.spotify.com") == 2


def test_missing_credentials_raise_permission_error_before_any_network(monkeypatch, scripts):
    monkeypatch.setattr(spotify.secrets, "get_secret", lambda name: None)
    seen: list[httpx.Request] = []
    spotify._transport = _api({}, seen)
    tool = SearchAndPlay()
    with pytest.raises(PermissionMissingError, match="jarvis key set spotify_client_id"):
        tool.run(tool.parse_args({"query": "x"}))
    assert seen == [] and scripts == []


def test_rejected_credentials_and_api_failures(creds, scripts):
    spotify._transport = _api({"POST https://accounts.spotify.com/api/token": httpx.Response(401, json={"error": "invalid_client"})}, [])
    tool = SearchAndPlay()
    with pytest.raises(PermissionMissingError, match="rejected"):
        tool.run(tool.parse_args({"query": "x"}))
    spotify.reset_token_cache()
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": _token(),
            "GET https://api.spotify.com/v1/search": httpx.Response(500, text="boom"),
        },
        [],
    )
    with pytest.raises(ToolExecutionError, match="status 500"):
        tool.run(tool.parse_args({"query": "x"}))

    def explode(request):
        raise httpx.ConnectError("down")

    spotify.reset_token_cache()
    spotify._transport = httpx.MockTransport(explode)
    with pytest.raises(ToolExecutionError, match="ConnectError"):
        tool.run(tool.parse_args({"query": "x"}))
    assert scripts == []


def test_search_and_play_works_through_the_registry_context(config, paths, creds, scripts):
    ctx = ToolContext.build(config, paths)
    spotify._transport = _api(
        {
            "POST https://accounts.spotify.com/api/token": _token(),
            "GET https://api.spotify.com/v1/search": _ok_search("spotify:track:Abc123"),
        },
        [],
    )
    tool = SearchAndPlay(ctx)
    assert json.loads(json.dumps(tool.api_schema()))["input_schema"]["required"] == ["query"]
    assert "Playing track 'So What' by Miles Davis" in tool.run(tool.parse_args({"query": "so what"})).content


# --- health ------------------------------------------------------------------------------------


def test_health_checks_never_raise(config, paths, monkeypatch):
    monkeypatch.setattr(spotify.secrets, "get_secret", lambda name: None)
    checks = health_checks(config, paths)
    by_name = {c.name: c for c in checks}
    creds = by_name["Spotify credentials"]
    assert creds.ok and creds.warn and "not set up" in creds.detail and "jarvis key set spotify_client_id" in creds.detail
    assert "Automation" in by_name["Spotify Automation permission"].detail
    assert "Spotify.app" in by_name

    def boom(name):
        raise RuntimeError("keyring is broken")

    monkeypatch.setattr(spotify.secrets, "get_secret", boom)
    checks = health_checks(config, paths)
    assert any(not c.ok and "Keychain" in c.detail for c in checks)

    monkeypatch.setattr(spotify.secrets, "get_secret", lambda name: "x")
    assert {c.name: c.ok for c in health_checks(config, paths)}["Spotify credentials"]
