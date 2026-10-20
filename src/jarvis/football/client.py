"""API-Football v3 client (api-sports.io) with SQLite caching and id resolution.

Every GET goes through `cached_get`, keyed by path plus sorted parameters, so the free tier's
daily quota is not wasted. League and team ids are never hardcoded: they are resolved by name
once and kept in the `kv` table for 30 days. API errors arrive as HTTP 200 with a non-empty
`errors` object; they are turned into `ToolExecutionError` so the model says so instead of
inventing scores. All text that comes from the API passes through `sanitize.for_model`.

Nothing here is verified against a live account (web access was blocked while writing it), so
the parsers tolerate missing keys everywhere.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from jarvis.exceptions import PermissionMissingError, ToolExecutionError
from jarvis.security import sanitize
from jarvis.utils import secrets
from jarvis.utils.timeutil import local_tz, now_local

log = logging.getLogger("jarvis.football")

BASE_URL = "https://v3.football.api-sports.io"
KEY_NAME = "football_api_key"
KEY_HEADER = "x-apisports-key"
HTTP_TIMEOUT = 15.0
ID_CACHE_DAYS = 30
LEAGUE_COUNTRY = "Turkey"  # the configured league is the Turkish one; UEFA competitions have no country
UEFA_WORDS = ("uefa", "champions", "europa", "conference")
MAX_TEXT = 120  # longest text field we keep from the API (names, rounds, venues)

# Injectable for tests (httpx.MockTransport). None means the default transport.
_transport: httpx.BaseTransport | None = None

# fixture.status.short values (documented list; unknown values are treated as "other")
LIVE_STATUSES = frozenset({"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"})
FINISHED_STATUSES = frozenset({"FT", "AET", "PEN"})
NOT_STARTED_STATUSES = frozenset({"NS", "TBD"})
STATUS_WORDS: dict[str, str] = {
    "TBD": "time to be defined",
    "NS": "not started",
    "1H": "first half",
    "HT": "half-time",
    "2H": "second half",
    "ET": "extra time",
    "BT": "break",
    "P": "penalty shoot-out",
    "SUSP": "suspended",
    "INT": "interrupted",
    "LIVE": "in play",
    "FT": "finished",
    "AET": "finished after extra time",
    "PEN": "finished on penalties",
    "PST": "postponed",
    "CANC": "cancelled",
    "ABD": "abandoned",
    "AWD": "awarded",
    "WO": "walkover",
}


# --- data shapes ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LeagueRef:
    id: int
    name: str
    season: int | None  # the season marked `current` by the API (year the season started)


@dataclass(frozen=True)
class TeamRef:
    id: int
    name: str


@dataclass
class QuotaStatus:
    plan: str
    requests_today: int | None
    limit_day: int | None
    active: bool | None = None


@dataclass
class MatchEvent:
    minute: int | None
    extra: int | None
    team: str
    player: str
    assist: str
    type: str  # Goal, Card, subst, Var
    detail: str  # Normal Goal, Penalty, Own Goal, Yellow Card, Red Card, ...

    def clock(self) -> str:
        if self.minute is None:
            return "?'"
        return f"{self.minute}+{self.extra}'" if self.extra else f"{self.minute}'"


@dataclass
class Fixture:
    id: int | None
    kickoff: datetime | None  # aware (the API gives an ISO date with offset)
    status: str  # status.short
    elapsed: int | None
    league: str
    league_id: int | None
    round: str
    season: int | None
    home: str
    home_id: int | None
    away: str
    away_id: int | None
    home_goals: int | None
    away_goals: int | None
    halftime: tuple[int | None, int | None] = (None, None)
    fulltime: tuple[int | None, int | None] = (None, None)
    extratime: tuple[int | None, int | None] = (None, None)
    penalty: tuple[int | None, int | None] = (None, None)
    venue: str = ""
    city: str = ""
    events: list[MatchEvent] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES

    def local_date(self) -> date | None:
        return self.kickoff.astimezone(local_tz()).date() if self.kickoff else None

    def status_text(self) -> str:
        word = STATUS_WORDS.get(self.status, self.status.lower() or "unknown status")
        if self.is_live and self.elapsed is not None:
            return f"{word}, {self.elapsed}'"
        return word

    def score_text(self) -> str:
        if self.home_goals is None or self.away_goals is None:
            return "-"
        text = f"{self.home_goals}-{self.away_goals}"
        if self.status == "PEN" and None not in self.penalty:
            text += f" ({self.penalty[0]}-{self.penalty[1]} pens)"
        elif self.status == "AET":
            text += " (aet)"
        return text

    def outcome_for(self, team_id: int | None, team_name: str = "") -> str:
        """W, D or L from one team's point of view; '' when the match is not finished."""
        if not self.is_finished or self.home_goals is None or self.away_goals is None:
            return ""
        if team_id is not None and team_id in (self.home_id, self.away_id):
            at_home = team_id == self.home_id
        elif team_name and fold(team_name) in (fold(self.home), fold(self.away)):
            at_home = fold(team_name) == fold(self.home)
        else:
            return ""
        mine, theirs = (self.home_goals, self.away_goals) if at_home else (self.away_goals, self.home_goals)
        if self.status == "PEN" and None not in self.penalty:
            mine, theirs = (self.penalty[0], self.penalty[1]) if at_home else (self.penalty[1], self.penalty[0])
            return "W" if mine > theirs else "L"
        if mine > theirs:
            return "W"
        return "L" if mine < theirs else "D"


@dataclass
class StandingRow:
    rank: int | None
    team: str
    team_id: int | None
    played: int | None
    win: int | None
    draw: int | None
    lose: int | None
    goals_for: int | None
    goals_against: int | None
    goal_diff: int | None
    points: int | None
    form: str


# --- helpers --------------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Accent-insensitive, case-insensitive key: 'Fenerbahçe' -> 'fenerbahce'."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def is_uefa(name: str) -> bool:
    key = fold(name)
    return any(word in key for word in UEFA_WORDS)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    """A sanitized, single-line, bounded string for any text field the API returns."""
    if value is None:
        return ""
    text = " ".join(sanitize.for_model(str(value)).split())
    return text[:limit]


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pair(node: Any) -> tuple[int | None, int | None]:
    if not isinstance(node, dict):
        return (None, None)
    return (_int(node.get("home")), _int(node.get("away")))


def _dict(node: Any) -> dict[str, Any]:
    return node if isinstance(node, dict) else {}


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_event(node: Any) -> MatchEvent:
    node = _dict(node)
    time = _dict(node.get("time"))
    return MatchEvent(
        minute=_int(time.get("elapsed")),
        extra=_int(time.get("extra")),
        team=_text(_dict(node.get("team")).get("name")),
        player=_text(_dict(node.get("player")).get("name")),
        assist=_text(_dict(node.get("assist")).get("name")),
        type=_text(node.get("type")),
        detail=_text(node.get("detail")),
    )


def parse_fixture(node: Any) -> Fixture:
    node = _dict(node)
    fx = _dict(node.get("fixture"))
    status = _dict(fx.get("status"))
    venue = _dict(fx.get("venue"))
    league = _dict(node.get("league"))
    teams = _dict(node.get("teams"))
    home = _dict(teams.get("home"))
    away = _dict(teams.get("away"))
    goals = _dict(node.get("goals"))
    score = _dict(node.get("score"))
    events_node = node.get("events")
    events = [parse_event(e) for e in events_node] if isinstance(events_node, list) else []
    return Fixture(
        id=_int(fx.get("id")),
        kickoff=_parse_dt(fx.get("date")),
        status=_text(status.get("short"), 8).upper(),
        elapsed=_int(status.get("elapsed")),
        league=_text(league.get("name")),
        league_id=_int(league.get("id")),
        round=_text(league.get("round")),
        season=_int(league.get("season")),
        home=_text(home.get("name")) or "?",
        home_id=_int(home.get("id")),
        away=_text(away.get("name")) or "?",
        away_id=_int(away.get("id")),
        home_goals=_int(goals.get("home")),
        away_goals=_int(goals.get("away")),
        halftime=_pair(score.get("halftime")),
        fulltime=_pair(score.get("fulltime")),
        extratime=_pair(score.get("extratime")),
        penalty=_pair(score.get("penalty")),
        venue=_text(venue.get("name")),
        city=_text(venue.get("city")),
        events=events,
    )


def parse_standings(response: Any) -> tuple[str, int | None, list[StandingRow]]:
    """(league name, season, rows) from `standings` -> response[0].league.standings[0][]."""
    if not isinstance(response, list) or not response:
        return "", None, []
    league = _dict(_dict(response[0]).get("league"))
    tables = league.get("standings")
    rows_node: list[Any] = []
    if isinstance(tables, list) and tables:
        first = tables[0]
        rows_node = first if isinstance(first, list) else []
    rows: list[StandingRow] = []
    for node in rows_node:
        node = _dict(node)
        team = _dict(node.get("team"))
        all_ = _dict(node.get("all"))
        goals = _dict(all_.get("goals"))
        rows.append(
            StandingRow(
                rank=_int(node.get("rank")),
                team=_text(team.get("name")) or "?",
                team_id=_int(team.get("id")),
                played=_int(all_.get("played")),
                win=_int(all_.get("win")),
                draw=_int(all_.get("draw")),
                lose=_int(all_.get("lose")),
                goals_for=_int(goals.get("for")),
                goals_against=_int(goals.get("against")),
                goal_diff=_int(node.get("goalsDiff")),
                points=_int(node.get("points")),
                form=_text(node.get("form"), 10),
            )
        )
    return _text(league.get("name")), _int(league.get("season")), rows


def cache_key(path: str, params: dict[str, Any]) -> str:
    query = urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))
    return f"football:{path.strip('/')}?{query}"


# --- the client ---------------------------------------------------------------------------------


class FootballClient:
    """One instance per tool call or brief. `db` may be None (no caching; used by the doctor)."""

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
        self.last_quota: tuple[int | None, int | None] = (None, None)  # (remaining, limit) from headers

    # --- configuration -------------------------------------------------------------------------

    @property
    def team_name(self) -> str:
        return str(getattr(self.config, "team", "") or "").strip()

    @property
    def league_name(self) -> str:
        return str(getattr(self.config, "league", "") or "").strip()

    def ttl_fixtures(self) -> float:
        return float(getattr(self.config, "cache_ttl_fixtures_min", 360)) * 60

    def ttl_standings(self) -> float:
        return float(getattr(self.config, "cache_ttl_standings_min", 60)) * 60

    def ttl_live(self) -> float:
        return float(getattr(self.config, "cache_ttl_live_min", 1)) * 60

    def today(self) -> date:
        return self._now().astimezone(local_tz()).date()

    def api_key(self) -> str:
        key = self._api_key or secrets.get_secret(KEY_NAME)
        if not key:
            raise PermissionMissingError(
                f"no API-Football key: run `jarvis key set {KEY_NAME}` (free account at dashboard.api-football.com)"
            )
        return key

    # --- HTTP ----------------------------------------------------------------------------------------

    def _request(self, path: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """One GET. Returns (response payload, whole envelope). Raises ToolExecutionError."""
        key = self.api_key()
        url = f"{BASE_URL}/{path.strip('/')}"
        clean = {k: v for k, v in params.items() if v is not None}
        transport = self._transport if self._transport is not None else _transport
        self.requests_made += 1
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, transport=transport) as client:
                response = client.get(url, params=clean, headers={KEY_HEADER: key, "accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"football API request failed: {type(exc).__name__}") from None
        self._log_quota_headers(response.headers)
        if response.status_code == 401 or response.status_code == 403:
            raise ToolExecutionError(f"football API refused the key (status {response.status_code}); check `jarvis key set {KEY_NAME}`")
        if response.status_code == 429:
            raise ToolExecutionError("football API rate limit hit (status 429); try again in a minute")
        if response.status_code != 200:
            raise ToolExecutionError(f"football API request failed with status {response.status_code}")
        try:
            envelope = response.json()
        except ValueError:
            raise ToolExecutionError("football API response was not JSON") from None
        if not isinstance(envelope, dict):
            raise ToolExecutionError("football API response was not understood")
        errors = envelope.get("errors")
        if errors:  # a dict like {"requests": "..."} or a non-empty list; [] / {} mean no error
            if isinstance(errors, dict):
                text = "; ".join(f"{_text(k, 40)}: {_text(v, 200)}" for k, v in errors.items())
            elif isinstance(errors, list):
                text = "; ".join(_text(e, 200) for e in errors)
            else:
                text = _text(errors, 200)
            log.warning("football API error on %s: %s", path, text)
            raise ToolExecutionError(f"football API error: {text}")
        return envelope.get("response"), envelope

    def _log_quota_headers(self, headers: Any) -> None:
        remaining = _int(headers.get("x-ratelimit-requests-remaining"))
        limit = _int(headers.get("x-ratelimit-requests-limit"))
        if remaining is not None or limit is not None:
            self.last_quota = (remaining, limit)
            log.info("football API quota: %s of %s daily requests remaining", remaining, limit)

    def cached_get(
        self,
        path: str,
        params: dict[str, Any],
        ttl_s: float,
        *,
        ttl_of: Callable[[Any], float] | None = None,
    ) -> Any:
        """The `response` part of an endpoint, from the SQLite cache when fresh.

        `ttl_of(response)` may shorten the TTL after looking at the data (today's or live
        fixtures are kept for `cache_ttl_live_min` only).
        """
        key = cache_key(path, params)
        if self.db is not None:
            cached = self.db.cache_get(key)
            if cached is not None:
                try:
                    return json.loads(cached)
                except ValueError:
                    log.warning("football cache entry %s is corrupt; refetching", key)
        payload, _ = self._request(path, params)
        if ttl_of is not None:
            try:
                ttl_s = min(ttl_s, float(ttl_of(payload)))
            except Exception:  # a TTL heuristic must never fail a fetch
                log.debug("football ttl heuristic failed", exc_info=True)
        if self.db is not None and ttl_s > 0:
            self.db.cache_set(key, json.dumps(payload, ensure_ascii=False), ttl_s)
        return payload

    # --- status ---------------------------------------------------------------------------------------

    def status(self) -> QuotaStatus:
        """`/status`: the plan and today's request count. Costs one request; never cached."""
        payload, _ = self._request("status", {})
        node = _dict(payload)
        subscription = _dict(node.get("subscription"))
        requests_ = _dict(node.get("requests"))
        quota = QuotaStatus(
            plan=_text(subscription.get("plan"), 40) or "unknown",
            requests_today=_int(requests_.get("current")),
            limit_day=_int(requests_.get("limit_day")),
            active=subscription.get("active") if isinstance(subscription.get("active"), bool) else None,
        )
        log.info("football API status: plan %s, %s of %s requests used today", quota.plan, quota.requests_today, quota.limit_day)
        return quota

    # --- id resolution (kv, 30 days) ------------------------------------------------------------------------

    def _kv_get(self, key: str) -> dict[str, Any] | None:
        if self.db is None:
            return None
        raw = self.db.kv_get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        expires = _parse_dt(data.get("expires_at"))
        if expires is None or expires <= datetime.now(timezone.utc):
            return None
        return data

    def _kv_set(self, key: str, data: dict[str, Any]) -> None:
        if self.db is None:
            return
        expires = datetime.now(timezone.utc) + timedelta(days=ID_CACHE_DAYS)
        self.db.kv_set(key, json.dumps({**data, "expires_at": expires.isoformat()}, ensure_ascii=False))

    def resolve_league(self, name: str | None = None, country: str | None = None) -> LeagueRef:
        """League id and current season by name. The configured league is searched in Turkey,
        UEFA competitions without a country; other names are searched world-wide."""
        name = (name or self.league_name).strip()
        if not name:
            raise ToolExecutionError("no league name given and football.league is not set")
        if country is None and not is_uefa(name) and fold(name) == fold(self.league_name):
            country = LEAGUE_COUNTRY
        kv_key = f"football:league:{fold(name)}|{fold(country or '')}"
        cached = self._kv_get(kv_key)
        if cached and _int(cached.get("id")) is not None:
            return LeagueRef(int(cached["id"]), str(cached.get("name") or name), _int(cached.get("season")))
        rows = self.cached_get("leagues", {"search": fold(name), "country": country}, self.ttl_fixtures())
        if not rows and country:  # the API may spell the country differently; retry without it
            rows = self.cached_get("leagues", {"search": fold(name)}, self.ttl_fixtures())
        best = _pick(rows, name, lambda row: _dict(row.get("league")).get("name"))
        if best is None:
            raise ToolExecutionError(f"football API knows no league named {name!r}")
        league = _dict(best.get("league"))
        league_id = _int(league.get("id"))
        if league_id is None:
            raise ToolExecutionError(f"football API returned no id for league {name!r}")
        season = _current_season(best.get("seasons"))
        ref = LeagueRef(league_id, _text(league.get("name")) or name, season)
        self._kv_set(kv_key, {"id": ref.id, "name": ref.name, "season": ref.season})
        log.info("football: resolved league %r to id %s (season %s)", name, ref.id, ref.season)
        return ref

    def resolve_team(self, name: str | None = None) -> TeamRef:
        name = (name or self.team_name).strip()
        if not name:
            raise ToolExecutionError("no team name given and football.team is not set")
        kv_key = f"football:team:{fold(name)}"
        cached = self._kv_get(kv_key)
        if cached and _int(cached.get("id")) is not None:
            return TeamRef(int(cached["id"]), str(cached.get("name") or name))
        rows = self.cached_get("teams", {"search": fold(name)}, self.ttl_fixtures())
        best = _pick(rows, name, lambda row: _dict(row.get("team")).get("name"))
        if best is None:
            raise ToolExecutionError(f"football API knows no team named {name!r}")
        team = _dict(best.get("team"))
        team_id = _int(team.get("id"))
        if team_id is None:
            raise ToolExecutionError(f"football API returned no id for team {name!r}")
        ref = TeamRef(team_id, _text(team.get("name")) or name)
        self._kv_set(kv_key, {"id": ref.id, "name": ref.name})
        log.info("football: resolved team %r to id %s", name, ref.id)
        return ref

    # --- fixtures and standings -------------------------------------------------------------------------

    def _fixture_ttl(self, payload: Any) -> float:
        """Live TTL when any fixture in the payload is in play or kicks off today and is not over."""
        today = self.today()
        for node in payload if isinstance(payload, list) else []:
            fx = parse_fixture(node)
            if fx.is_live or (fx.local_date() == today and not fx.is_finished):
                return self.ttl_live()
        return self.ttl_fixtures()

    def _fixtures(self, params: dict[str, Any]) -> list[Fixture]:
        payload = self.cached_get("fixtures", params, self.ttl_fixtures(), ttl_of=self._fixture_ttl)
        if not isinstance(payload, list):
            return []
        return [parse_fixture(node) for node in payload]

    def next_fixtures(self, team_id: int, n: int) -> list[Fixture]:
        return self._fixtures({"team": team_id, "next": max(1, min(int(n), 99))})

    def last_fixtures(self, team_id: int, n: int) -> list[Fixture]:
        return self._fixtures({"team": team_id, "last": max(1, min(int(n), 99))})

    def fixture(self, fixture_id: int) -> Fixture | None:
        """One fixture with its events (goals, cards). None when the id is unknown."""
        found = self._fixtures({"id": int(fixture_id)})
        return found[0] if found else None

    def standings(self, league: LeagueRef) -> tuple[str, int | None, list[StandingRow]]:
        if league.season is None:
            raise ToolExecutionError(f"football API reported no current season for {league.name!r}")
        payload = self.cached_get("standings", {"league": league.id, "season": league.season}, self.ttl_standings())
        name, season, rows = parse_standings(payload)
        return name or league.name, season or league.season, rows


def _pick(rows: Any, wanted: str, name_of: Callable[[dict[str, Any]], Any]) -> dict[str, Any] | None:
    """The row whose name matches `wanted` exactly (accent-insensitive), else the first row."""
    if not isinstance(rows, list):
        return None
    candidates = [r for r in rows if isinstance(r, dict)]
    key = fold(wanted)
    for row in candidates:
        if fold(str(name_of(row) or "")) == key:
            return row
    for row in candidates:
        if fold(str(name_of(row) or "")).startswith(key):
            return row
    return candidates[0] if candidates else None


def _current_season(seasons: Any) -> int | None:
    if not isinstance(seasons, list):
        return None
    years = [(_int(_dict(s).get("year")), bool(_dict(s).get("current"))) for s in seasons]
    for year, current in years:
        if current and year is not None:
            return year
    known = [y for y, _ in years if y is not None]
    return max(known) if known else None
