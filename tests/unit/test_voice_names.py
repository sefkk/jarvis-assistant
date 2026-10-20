"""Fuzzy names for spoken requests (voice/names.py) and the did-you-mean questions of Spotify and Maps."""

from __future__ import annotations

import httpx
import pytest

from jarvis.tools import spotify
from jarvis.tools.base import Runtime, ToolContext
from jarvis.tools.spotify import SearchAndPlay
from jarvis.voice import names


def test_match_score():
    assert names.match_score("Sezen Aksu Gülümse", "Gülümse Sezen Aksu") == 1.0
    assert names.match_score("Tarkan'dan Kuzu Kuzu şarkısını", "Kuzu Kuzu Tarkan") == 1.0
    assert names.match_score("Sezen Aksa", "Sezen Aksu") >= 0.8
    assert names.match_score("Coldplay Daniel Oğuzçal", "Yellow Coldplay") < names.LOW_MATCH
    assert names.match_score("", "x") == 0.0


def test_suggest():
    terms = ["Sezen Aksu", "Bağdat Caddesi", "Taksim", "Kadıköy"]
    s = names.suggest("Sezen Aksa Gülümse", terms)
    assert s is not None and (s.heard, s.term) == ("Sezen Aksa", "Sezen Aksu")
    assert names.suggest("Bağdat Cattesi", terms).term == "Bağdat Caddesi"
    assert names.suggest("Bağdat Çaddesi", terms) is None  # only accents differ: the same name
    assert names.suggest("Taksim'e", terms) is None  # heard right (with a suffix)
    assert names.suggest("kadıköy", terms) is None
    assert names.suggest("eczane", terms) is None
    assert names.suggest("", terms) is None


def test_spoken():
    assert not names.spoken(None)
    ctx = type("C", (), {"runtime": Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, channel="voice")})()
    assert names.spoken(ctx)
    ctx.runtime.channel = "telegram"
    assert not names.spoken(ctx)
    ctx.runtime.spoken = True
    assert names.spoken(ctx)


# --- Spotify ------------------------------------------------------------------------------------------


@pytest.fixture
def spotify_env(monkeypatch, config, paths):
    store = {"spotify_client_id": "cid", "spotify_client_secret": "csec"}
    monkeypatch.setattr(spotify.secrets, "get_secret", lambda name: store.get(name))
    played = []
    monkeypatch.setattr(spotify, "run_applescript", lambda script, args=(), **kw: played.append(list(args)) or "ok")
    spotify.reset_token_cache()
    searches = []

    def install(results_by_query):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "accounts.spotify.com":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            q = request.url.params["q"]
            searches.append(q)
            items = [
                {"uri": f"spotify:track:{uri}", "name": name, "artists": [{"name": artist}]}
                for uri, name, artist in results_by_query.get(q, [])
            ]
            return httpx.Response(200, json={"tracks": {"items": items}})

        spotify._transport = httpx.MockTransport(handler)

    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, channel="voice")
    yield type("E", (), {"install": staticmethod(install), "played": played, "searches": searches, "ctx": ctx, "paths": paths})
    spotify._transport = None
    spotify.reset_token_cache()


def test_spoken_request_with_a_poor_match_asks_instead_of_playing(spotify_env):
    spotify_env.install({"Coldplay Daniel Oğuzçal": [("A1", "Daniel", "Some Band"), ("A2", "Yellow", "Coldplay")]})
    tool = SearchAndPlay(spotify_env.ctx)
    result = tool.run(tool.parse_args({"query": "Coldplay Daniel Oğuzçal"}))
    assert spotify_env.played == []
    assert "mi demek istedin" in result.content and "confirmed=true" in result.content
    confirmed = tool.run(tool.parse_args({"query": "Coldplay Daniel Oğuzçal", "confirmed": True}))
    assert spotify_env.played and confirmed.content.startswith("Playing")


def test_a_known_name_from_the_vocabulary_is_searched_and_offered(spotify_env):
    spotify_env.paths.vocabulary_file.parent.mkdir(parents=True, exist_ok=True)
    spotify_env.paths.vocabulary_file.write_text("- Sezen Aksu\n", encoding="utf-8")
    spotify_env.install(
        {
            "Sezen Aksa Gülümse": [("B1", "Aksam Gunesi", "Other Artist")],
            "Sezen Aksu Gülümse": [("B2", "Gülümse", "Sezen Aksu")],
        }
    )
    tool = SearchAndPlay(spotify_env.ctx)
    result = tool.run(tool.parse_args({"query": "Sezen Aksa Gülümse"}))
    assert spotify_env.played == [] and spotify_env.searches == ["Sezen Aksa Gülümse", "Sezen Aksu Gülümse"]
    assert "'Sezen Aksu - Gülümse' mi demek istedin" in result.content


def test_good_matches_typed_requests_and_playlists_play_at_once(spotify_env):
    spotify_env.install({"Sezen Aksu Gülümse": [("C1", "Gülümse", "Sezen Aksu")], "zzz": [("C2", "Something Else", "Nobody")]})
    tool = SearchAndPlay(spotify_env.ctx)
    assert tool.run(tool.parse_args({"query": "Sezen Aksu Gülümse"})).content.startswith("Playing")
    spotify_env.ctx.runtime.channel = "cli"  # typed: Spotify's first result, as before
    assert tool.run(tool.parse_args({"query": "zzz"})).content.startswith("Playing")
    assert len(spotify_env.played) == 2


def test_nothing_close_asks_the_user_to_repeat(spotify_env):
    spotify_env.install({"qwxz": [("D1", "Totally Different", "Someone")]})
    tool = SearchAndPlay(spotify_env.ctx)
    result = tool.run(tool.parse_args({"query": "qwxz"}))
    assert spotify_env.played == [] and "say or spell the name again" in result.content


def test_pick_prefers_a_clearly_better_lower_result():
    found = [spotify.Found("spotify:track:A", "a", "Daniel", "Some Band"), spotify.Found("spotify:track:B", "b", "Yellow", "Coldplay")]
    best, score = spotify.pick("Coldplay Yellow", found)
    assert best.uri == "spotify:track:B" and score == 1.0
    best, _ = spotify.pick("Daniel", found)
    assert best.uri == "spotify:track:A"


# --- Maps ---------------------------------------------------------------------------------------------


def test_maps_tools_ask_about_a_misheard_known_place(config, paths, monkeypatch):
    from jarvis.tools import maps as maps_tools

    paths.vocabulary_file.parent.mkdir(parents=True, exist_ok=True)
    paths.vocabulary_file.write_text("- Bağdat Caddesi\n", encoding="utf-8")
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, channel="voice")

    class NoClient:
        def __getattr__(self, name):
            raise AssertionError("no paid API call before the user answered")

    for tool_cls, args in (
        (maps_tools.TravelTime, {"destination": "Bağdat Cattesi"}),
        (maps_tools.SearchPlaces, {"query": "eczane", "near": "Bağdat Cattesi"}),
        (maps_tools.Geocode, {"address": "Bağdat Cattesi"}),
        (maps_tools.OpenInMaps, {"query": "Bağdat Cattesi"}),
    ):
        tool = tool_cls(ctx, client=NoClient())
        result = tool.run(tool.parse_args(args))
        assert "'Bağdat Caddesi' mi demek istedin" in result.content and "confirmed=true" in result.content, tool_cls


def test_maps_check_is_quiet_for_typed_confirmed_and_exact_requests(config, paths):
    from jarvis.tools import maps as maps_tools

    paths.vocabulary_file.parent.mkdir(parents=True, exist_ok=True)
    paths.vocabulary_file.write_text("- Bağdat Caddesi\n", encoding="utf-8")
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, channel="cli")
    tool = maps_tools.Geocode(ctx, client=None)
    assert tool.misheard({"address": "Bağdat Cattesi"}, False) is None  # typed
    ctx.runtime.channel = "voice"
    assert tool.misheard({"address": "Bağdat Cattesi"}, True) is None  # confirmed
    assert tool.misheard({"address": "Bağdat Caddesi'nde"}, False) is None  # heard right
    assert tool.misheard({"address": "home"}, False) is None
    assert tool.misheard({"address": "Bağdat Cattesi"}, False) is not None
