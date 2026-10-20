"""Football client and tools against hand-written API-Football v3 responses (no network)."""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.football import client as fc
from jarvis.football.client import FootballClient, cache_key, fold, parse_fixture
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.football import LeagueStandings, MatchDetails, TeamFixtures, TeamResults, health_checks
from jarvis.utils import secrets
from jarvis.utils.timeutil import local_tz

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "football"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    """Never touch the real Keychain; tests inject the key explicitly."""
    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    yield
    fc._transport = None


class Api:
    """A fake API-Football server: routes by path and parameters, records every request."""

    def __init__(self, overrides: dict | None = None, headers: dict | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.overrides = overrides or {}
        self.headers = headers if headers is not None else {"x-ratelimit-requests-remaining": "87", "x-ratelimit-requests-limit": "100"}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path.strip("/")
        params = dict(request.url.params)
        if path in self.overrides:
            body = self.overrides[path]
            if callable(body):
                body = body(params)
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body, headers=self.headers)
        if path == "status":
            body = load("status")
        elif path == "leagues":
            body = load("leagues_ucl") if "champions" in params.get("search", "") else load("leagues_super_lig")
        elif path == "teams":
            body = load("teams_fenerbahce")
        elif path == "fixtures" and "id" in params:
            body = load("fixture_details")
        elif path == "fixtures" and "next" in params:
            body = load("fixtures_next")
        elif path == "fixtures" and "last" in params:
            body = load("fixtures_last")
        elif path == "standings":
            body = load("standings")
        else:
            return httpx.Response(404, json={"errors": {"endpoint": "unknown"}})
        return httpx.Response(200, json=body, headers=self.headers)


def make_client(config, db, api: Api | None = None, **kw) -> FootballClient:
    api = api or Api()
    return FootballClient(config.football, db, api_key="test-key", transport=api.transport(), **kw)


def local_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(local_tz()).strftime("%Y-%m-%d %H:%M")


# --- client: resolution and kv caching -------------------------------------------------------------------


def test_resolve_team_by_name_picks_exact_match_and_caches_in_kv(config, db):
    api = Api()
    client = make_client(config, db, api)
    team = client.resolve_team()  # default from config: "Fenerbahçe"
    assert (team.id, team.name) == (611, "Fenerbahce")  # not the U19 row that comes first
    request = api.seen[0]
    assert request.url.path == "/teams" and request.url.params["search"] == "fenerbahce"
    assert request.headers["x-apisports-key"] == "test-key"
    stored = json.loads(db.kv_get("football:team:fenerbahce"))
    assert stored["id"] == 611 and stored["expires_at"] > datetime.now(timezone.utc).isoformat()
    # a second client (new instance, same db) answers from kv without a request
    again = make_client(config, db, api).resolve_team("Fenerbahçe")
    assert again.id == 611 and len(api.seen) == 1


def test_expired_kv_entry_is_refetched(config, db):
    api = Api()
    make_client(config, db, api).resolve_team()
    db.kv_set("football:team:fenerbahce", json.dumps({"id": 611, "name": "x", "expires_at": "2020-01-01T00:00:00+00:00"}))
    db.conn.execute("DELETE FROM api_cache")  # also drop the response cache so the request is visible
    make_client(config, db, api).resolve_team()
    assert len(api.seen) == 2


def test_resolve_league_uses_country_for_the_configured_league_and_current_season(config, db):
    api = Api()
    client = make_client(config, db, api)
    league = client.resolve_league()
    assert (league.id, league.name, league.season) == (203, "Süper Lig", 2026)
    params = api.seen[0].url.params
    assert params["search"] == "super lig" and params["country"] == "Turkey"
    assert json.loads(db.kv_get("football:league:super lig|turkey"))["season"] == 2026
    # UEFA competitions are searched without a country
    ucl = client.resolve_league("UEFA Champions League")
    assert (ucl.id, ucl.season) == (2, 2026)
    assert "country" not in api.seen[1].url.params


def test_resolve_league_retries_without_country_when_nothing_matches(config, db):
    def leagues(params):
        return {"errors": [], "results": 0, "response": []} if "country" in params else load("leagues_super_lig")

    api = Api(overrides={"leagues": leagues})
    assert make_client(config, db, api).resolve_league().id == 203
    assert [("country" in r.url.params) for r in api.seen] == [True, False]


def test_unknown_team_or_league(config, db):
    api = Api(overrides={"teams": {"errors": [], "results": 0, "response": []}, "leagues": {"errors": {}, "response": []}})
    client = make_client(config, db, api)
    with pytest.raises(ToolExecutionError, match="no team named"):
        client.resolve_team("Nowhere FC")
    with pytest.raises(ToolExecutionError, match="no league named"):
        client.resolve_league("Nowhere League")


# --- client: envelope errors, quota, HTTP failures --------------------------------------------------------


def test_envelope_error_with_http_200_is_a_tool_error(config, db):
    api = Api(overrides={"fixtures": load("error_quota")})
    client = make_client(config, db, api)
    with pytest.raises(ToolExecutionError, match="request limit"):
        client.next_fixtures(611, 5)
    assert db.cache_get(cache_key("fixtures", {"team": 611, "next": 5})) is None  # failures are not cached


def test_quota_headers_and_status_are_logged_with_counts_only(config, db, caplog):
    api = Api()
    client = make_client(config, db, api)
    with caplog.at_level(logging.INFO, logger="jarvis.football"):
        quota = client.status()
    assert client.last_quota == (87, 100)
    assert (quota.plan, quota.requests_today, quota.limit_day, quota.active) == ("Free", 12, 100, True)
    messages = [r.getMessage() for r in caplog.records]
    assert any("87 of 100 daily requests remaining" in m for m in messages)
    assert any("plan Free, 12 of 100 requests used today" in m for m in messages)
    assert not any("test-key" in m for m in messages)


def test_missing_key_is_a_permission_error(config, db):
    api = Api()
    client = FootballClient(config.football, db, transport=api.transport())
    with pytest.raises(PermissionMissingError, match="jarvis key set football_api_key"):
        client.next_fixtures(611, 5)
    assert api.seen == []


def test_http_and_network_failures(config, db):
    def explode(request):
        raise httpx.ConnectError("no route")

    client = FootballClient(config.football, db, api_key="k", transport=httpx.MockTransport(explode))
    with pytest.raises(ToolExecutionError, match="ConnectError"):
        client.next_fixtures(611, 5)
    client = FootballClient(config.football, db, api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={})))
    with pytest.raises(ToolExecutionError, match="refused the key"):
        client.next_fixtures(611, 5)
    client = FootballClient(config.football, db, api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(ToolExecutionError, match="429"):
        client.next_fixtures(611, 5)
    client = FootballClient(config.football, db, api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(ToolExecutionError, match="JSON"):
        client.next_fixtures(611, 5)


# --- client: response cache and TTLs -----------------------------------------------------------------------


def _expires_at(db, key: str) -> datetime:
    row = db.conn.execute("SELECT expires_at FROM api_cache WHERE key = ?", (key,)).fetchone()
    assert row is not None, key
    return datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))


def test_cache_hit_avoids_a_second_request_and_uses_the_fixtures_ttl(config, db):
    api = Api()
    client = make_client(config, db, api)
    first = client.next_fixtures(611, 5)
    second = client.next_fixtures(611, 5)
    assert len(api.seen) == 1 and [f.id for f in first] == [f.id for f in second]
    key = cache_key("fixtures", {"team": 611, "next": 5})
    ttl = _expires_at(db, key) - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=58) < ttl <= timedelta(hours=6)
    # standings: 1 h
    client.standings(client.resolve_league())
    ttl = _expires_at(db, cache_key("standings", {"league": 203, "season": 2026})) - datetime.now(timezone.utc)
    assert timedelta(minutes=58) < ttl <= timedelta(hours=1)


def test_cache_key_is_order_independent():
    assert cache_key("fixtures", {"next": 5, "team": 611}) == cache_key("/fixtures/", {"team": 611, "next": 5})
    assert cache_key("fixtures", {"team": 611, "next": 5}) == "football:fixtures?next=5&team=611"


def test_expired_cache_entry_is_refetched(config, db):
    api = Api()
    client = make_client(config, db, api)
    client.last_fixtures(611, 5)
    key = cache_key("fixtures", {"team": 611, "last": 5})
    db.conn.execute("UPDATE api_cache SET expires_at = ? WHERE key = ?", ("2020-01-01T00:00:00.000Z", key))
    client.last_fixtures(611, 5)
    assert len(api.seen) == 2


def _dated(payload: dict, when: datetime, status: str = "NS") -> dict:
    copy_ = copy.deepcopy(payload)
    fx = copy_["response"][0]
    fx["fixture"]["date"] = when.isoformat()
    fx["fixture"]["status"]["short"] = status
    return copy_


def test_todays_fixture_gets_the_live_ttl(config, db):
    now = datetime.now(local_tz())
    kickoff = now.replace(hour=23, minute=59, second=0, microsecond=0)  # still today, not started
    api = Api(overrides={"fixtures": _dated(load("fixtures_next"), kickoff.astimezone(timezone.utc))})
    client = make_client(config, db, api)
    client.next_fixtures(611, 5)
    ttl = _expires_at(db, cache_key("fixtures", {"team": 611, "next": 5})) - datetime.now(timezone.utc)
    assert timedelta(seconds=30) < ttl <= timedelta(minutes=1)


def test_live_fixture_gets_the_live_ttl_even_on_another_day(config, db):
    api = Api(overrides={"fixtures": _dated(load("fixture_details"), datetime(2026, 10, 11, 17, tzinfo=timezone.utc), "2H")})
    client = make_client(config, db, api)
    fx = client.fixture(1300092)
    assert fx is not None and fx.is_live
    ttl = _expires_at(db, cache_key("fixtures", {"id": 1300092})) - datetime.now(timezone.utc)
    assert ttl <= timedelta(minutes=1)


def test_finished_fixture_today_keeps_the_long_ttl(config, db):
    earlier = datetime.now(timezone.utc) - timedelta(hours=3)
    api = Api(overrides={"fixtures": _dated(load("fixture_details"), earlier, "FT")})
    client = make_client(config, db, api)
    client.fixture(1300092)
    ttl = _expires_at(db, cache_key("fixtures", {"id": 1300092})) - datetime.now(timezone.utc)
    assert ttl > timedelta(hours=5)


def test_client_without_db_does_not_cache(config):
    api = Api()
    client = FootballClient(config.football, None, api_key="k", transport=api.transport())
    client.next_fixtures(611, 1)
    client.next_fixtures(611, 1)
    assert len(api.seen) == 2


# --- parsing ----------------------------------------------------------------------------------------------


def test_parse_fixture_tolerates_missing_keys_and_sanitizes_text():
    fx = parse_fixture({"fixture": {"id": "12", "date": "2026-10-18T16:00:00Z"}, "teams": {"home": {"name": "A​B\x1b[31m"}}})
    assert fx.id == 12 and fx.kickoff == datetime(2026, 10, 18, 16, tzinfo=timezone.utc)
    assert fx.home == "AB" and fx.away == "?" and fx.status == "" and fx.events == []
    assert parse_fixture(None).id is None and parse_fixture("junk").kickoff is None


def test_outcome_and_score_text():
    base = parse_fixture(load("fixtures_last")["response"][0])  # Fenerbahce 3-1 Besiktas
    assert base.outcome_for(611) == "W" and base.outcome_for(549) == "L" and base.outcome_for(None, "fenerbahçe") == "W"
    assert base.outcome_for(1) == ""
    draw = parse_fixture(load("fixtures_last")["response"][1])
    assert draw.outcome_for(611) == "D"
    pens = copy.deepcopy(load("fixtures_last")["response"][1])
    pens["fixture"]["status"]["short"] = "PEN"
    pens["score"]["penalty"] = {"home": 4, "away": 5}
    pens["goals"] = {"home": 2, "away": 2}
    fx = parse_fixture(pens)
    assert fx.score_text() == "2-2 (4-5 pens)" and fx.outcome_for(611) == "L"


def test_fold():
    assert fold("Fenerbahçe") == "fenerbahce" and fold("  SÜPER  Lig ") == "super lig" and fold("Beşiktaş") == "besiktas"


# --- tools -------------------------------------------------------------------------------------------------


@pytest.fixture
def ctx(config, paths, db) -> ToolContext:
    context = ToolContext.build(config, paths)
    context.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None)
    return context


def test_tool_metadata():
    for cls in (TeamFixtures, TeamResults, MatchDetails, LeagueStandings):
        assert cls.risk == RiskLevel.SAFE and cls.taints is False and cls.delegate_ok is True
    schema = TeamFixtures().api_schema()["input_schema"]["properties"]["next_n"]
    assert schema["minimum"] == 1 and schema["maximum"] == 20
    assert MatchDetails().api_schema()["input_schema"]["required"] == ["match_id"]


def test_team_fixtures_output_in_local_time(ctx, db):
    api = Api()
    tool = TeamFixtures(ctx, client=make_client(ctx.config, db, api))
    lines = tool.run(tool.parse_args({})).content.splitlines()
    assert lines[0] == "Next 3 match(es) for Fenerbahce (times are local):"
    assert lines[1] == (
        f"{datetime.fromisoformat('2026-10-18T16:00:00+00:00').astimezone(local_tz()).strftime('%a')} "
        f"{local_time('2026-10-18T16:00:00+00:00')}: Fenerbahce vs Galatasaray (home) - Süper Lig, Regular Season - 9 "
        "at Ülker Stadyumu, Istanbul [match id 1300101]"
    )
    assert "Manchester United vs Fenerbahce (away) - UEFA Champions League, League Stage - 3 at Old Trafford, Manchester" in lines[2]
    assert lines[3].endswith("Trabzonspor vs Fenerbahce (away) - Süper Lig, Regular Season - 10 [time to be defined] [match id 1300118]")
    assert api.seen[-1].url.params["next"] == "5" and api.seen[-1].url.params["team"] == "611"


def test_team_fixtures_istanbul_time(ctx, db, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Istanbul")
    time.tzset()
    try:
        tool = TeamFixtures(ctx, client=make_client(ctx.config, db))
        text = tool.run(tool.parse_args({"next_n": 1})).content
        assert "Sun 2026-10-18 19:00: Fenerbahce vs Galatasaray" in text  # 16:00 UTC -> 19:00 Istanbul
        assert "Next 3 match(es)" in text  # the fake API ignores next_n; the line count is the API's
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_team_fixtures_explicit_team_and_empty_result(ctx, db):
    api = Api(overrides={"fixtures": {"errors": [], "results": 0, "response": []}})
    tool = TeamFixtures(ctx, client=make_client(ctx.config, db, api))
    assert tool.run(tool.parse_args({"team": "Fenerbahçe"})).content == "No upcoming matches found for Fenerbahce."
    with pytest.raises(ToolValidationError):
        tool.parse_args({"next_n": 0})
    with pytest.raises(ToolValidationError):
        tool.parse_args({"next_n": 21})


def test_team_results_with_w_d_l(ctx, db):
    tool = TeamResults(ctx, client=make_client(ctx.config, db))
    lines = tool.run(tool.parse_args({"last_n": 3})).content.splitlines()
    assert lines[0] == "Last 3 result(s) for Fenerbahce (W/D/L from Fenerbahce's point of view):"
    assert lines[1].endswith(
        f"{local_time('2026-10-11T17:00:00+00:00')}: W Fenerbahce 3-1 Besiktas - Süper Lig, Regular Season - 8 [match id 1300092]"
    )
    assert ": D Fenerbahce 1-1 Borussia Dortmund - UEFA Champions League, League Stage - 2" in lines[2]
    assert ": L Galatasaray 2-0 Fenerbahce - Süper Lig, Regular Season - 7" in lines[3]


def test_match_details_scores_goals_and_cards(ctx, db):
    tool = MatchDetails(ctx, client=make_client(ctx.config, db))
    text = tool.run(tool.parse_args({"match_id": 1300092})).content
    lines = text.splitlines()
    assert lines[0] == "Fenerbahce 3-1 Besiktas - Süper Lig, Regular Season - 8 - finished"
    assert (
        lines[1].startswith("Kickoff: ") and local_time("2026-10-11T17:00:00+00:00") in lines[1] and "Ülker Stadyumu, Istanbul" in lines[1]
    )
    assert lines[2] == "Score: half-time 1-0, full-time 3-1"
    assert "Goals:" in lines and "  23' E. Dzeko (Fenerbahce, assist D. Tadic)" in lines
    assert "  67' C. Immobile (Besiktas, penalty)" in lines and "  81' D. Tadic (Fenerbahce, penalty)" in lines
    assert "  90+3' S. Szymanski (Fenerbahce, assist F. Kadioglu)" in lines
    assert "Cards:" in lines and "  45+2' Yellow Card: R. Yilmaz (Besiktas)" in lines and "  75' Red Card: G. Paulista (Besiktas)" in lines
    assert "Substitution" not in text and "Goal cancelled" not in text


def test_match_details_live_minute_and_unknown_id(ctx, db):
    live = _dated(load("fixture_details"), datetime(2026, 10, 11, 17, tzinfo=timezone.utc), "2H")
    live["response"][0]["fixture"]["status"]["elapsed"] = 67
    api = Api(overrides={"fixtures": lambda p: live if p.get("id") == "1300092" else {"errors": [], "results": 0, "response": []}})
    tool = MatchDetails(ctx, client=make_client(ctx.config, db, api))
    assert tool.run(tool.parse_args({"match_id": 1300092})).content.splitlines()[0].endswith("- second half, 67'")
    with pytest.raises(ToolExecutionError, match="no match with id 42"):
        tool.run(tool.parse_args({"match_id": 42}))


def test_league_standings_marks_the_configured_team(ctx, db):
    api = Api()
    tool = LeagueStandings(ctx, client=make_client(ctx.config, db, api))
    lines = tool.run(tool.parse_args({})).content.splitlines()
    assert lines[0] == "Süper Lig 2026/27 standings (top 4 of 4):"
    assert lines[2].startswith("  1  Galatasaray") and " 8  7-1-0     21:6    +15   22  WWWDW" in lines[2]
    assert lines[3].startswith("* 2  Fenerbahce") and "+11   19  WDWLW" in lines[3]
    assert lines[-1] == "* = Fenerbahçe"
    assert api.seen[-1].url.path == "/standings" and dict(api.seen[-1].url.params) == {"league": "203", "season": "2026"}
    # top=1 leaves the team out of the table but still reports its rank
    text = tool.run(tool.parse_args({"top": 1})).content
    assert "Fenerbahce" not in text.splitlines()[2:3][0] if len(text.splitlines()) > 2 else True
    assert text.endswith("Fenerbahçe: rank 2, 19 points (outside the top 1)")


def test_league_standings_for_a_named_competition(ctx, db):
    api = Api()
    tool = LeagueStandings(ctx, client=make_client(ctx.config, db, api))
    tool.run(tool.parse_args({"league": "UEFA Champions League"}))
    assert dict(api.seen[-1].url.params) == {"league": "2", "season": "2026"}


def test_tools_report_api_failure_instead_of_inventing_scores(ctx, db):
    api = Api(overrides={"fixtures": load("error_quota")})
    tool = TeamResults(ctx, client=make_client(ctx.config, db, api))
    with pytest.raises(ToolExecutionError, match="football API error: requests: You have reached the request limit"):
        tool.run(tool.parse_args({}))


def test_tool_without_injected_client_uses_runtime_db_and_keychain(ctx, db, monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", lambda name: "kc-key" if name == "football_api_key" else None)
    api = Api()
    fc._transport = api.transport()
    tool = TeamFixtures(ctx)
    tool.run(tool.parse_args({}))
    assert api.seen[0].headers["x-apisports-key"] == "kc-key"
    assert db.kv_get("football:team:fenerbahce") is not None  # cached in the runtime's db


def test_tool_missing_key_is_permission_error(ctx):
    tool = TeamFixtures(ctx)
    with pytest.raises(PermissionMissingError, match="football_api_key"):
        tool.run(tool.parse_args({}))


def test_tool_opens_its_own_db_without_runtime(config, paths, monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", lambda name: "k")
    fc._transport = Api().transport()
    tool = TeamResults(ToolContext.build(config, paths))
    assert "W Fenerbahce 3-1 Besiktas" in tool.run(tool.parse_args({"last_n": 1})).content
    assert tool._own_db is not None and tool._own_db.kv_get("football:team:fenerbahce")
    tool._own_db.close()


# --- doctor ------------------------------------------------------------------------------------------------


def test_health_checks_without_key_is_optional_not_an_error(config, paths):
    checks = health_checks(config, paths)
    assert len(checks) == 1 and checks[0].ok
    assert "optional, not configured" in checks[0].detail and "jarvis key set football_api_key" in checks[0].detail


# --- the tools exist only with a key ------------------------------------------------------------


def test_football_tools_are_hidden_without_a_key(config, paths):
    from jarvis.tools.base import ToolContext
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    names = registry.names()
    assert not any(n in names for n in ("team_fixtures", "team_results", "match_details", "league_standings"))
    assert registry.hidden == {n: "football" for n in ("league_standings", "match_details", "team_fixtures", "team_results")}
    assert "web_search" in names and "daily_brief" in names
    # the remote profile pattern is not reported as a typo, and the remote subset keeps the hidden list
    assert registry.unmatched_patterns(["football.*", "football.team_results", "nope.*"]) == ["nope.*"]
    assert registry.subset_by_patterns(["football.*", "web.web_search"]).names() == ["web_search"]
    assert [t["name"] for t in registry.api_tools() if t["name"].startswith("team_")] == []


def test_football_tools_are_offered_with_a_key(config, paths, monkeypatch):
    import jarvis.tools.football as football_tools
    from jarvis.tools.base import ToolContext
    from jarvis.tools.registry import ToolRegistry

    monkeypatch.setattr(football_tools, "api_key_configured", lambda: True)
    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    assert {"team_fixtures", "team_results", "match_details", "league_standings"} <= set(registry.names())
    assert registry.hidden == {}


def test_availability_probe_never_prompts_and_survives_keyring_errors(monkeypatch):
    import jarvis.tools.football as football_tools

    monkeypatch.undo()  # the conftest stub; test the real probe

    def boom(name):
        raise RuntimeError("keyring backend down")

    monkeypatch.setattr(secrets, "get_secret", boom)
    assert football_tools.api_key_configured() is False
    monkeypatch.setattr(secrets, "get_secret", lambda name: "k")
    assert football_tools.api_key_configured() is True
    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    assert football_tools.api_key_configured() is False


def test_health_checks_with_key_calls_status_once(config, paths, monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", lambda name: "k" if name == "football_api_key" else None)
    api = Api()
    fc._transport = api.transport()
    checks = health_checks(config, paths)
    assert [c.ok for c in checks] == [True, True]
    assert "found in the Keychain" in checks[0].detail
    assert checks[1].detail.startswith("plan Free (active), 12/100 requests used today")
    assert [r.url.path for r in api.seen] == ["/status"]


def test_health_checks_network_failure_is_reported_not_raised(config, paths, monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", lambda name: "k")

    def explode(request):
        raise httpx.ConnectTimeout("slow")

    fc._transport = httpx.MockTransport(explode)
    checks = health_checks(config, paths)
    assert checks[0].ok and not checks[1].ok and "ConnectTimeout" in checks[1].detail
