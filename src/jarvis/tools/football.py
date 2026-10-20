"""Football tools (optional): team_fixtures, team_results, match_details,
league_standings, plus `health_checks` for the doctor.

Football questions are answered with `web_search` first; these tools are the
fallback and exist only when an API-Football key is in the Keychain. Without a key
`available()` is False, so the registry never offers them to the model and the doctor reports
"optional, not configured" instead of an error.

All four are SAFE and do not taint the turn (structured sports data is not an injection risk);
every text field from the API is still sanitized in the client. Times are shown in the local
zone. Data comes from API-Football v3 through `jarvis.football.FootballClient`, which caches in
SQLite; when the API fails the error is returned to the model so it says so (never a made-up
score).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError
from jarvis.football import client as football
from jarvis.football.client import Fixture, FootballClient, StandingRow, fold
from jarvis.observability.db import Database
from jarvis.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from jarvis.utils import secrets
from jarvis.utils.timeutil import format_local, local_tz

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.football")

MAX_MATCHES = 20
MAX_TEAM_NAME = 60


# --- doctor ---------------------------------------------------------------------------------------


def api_key_configured() -> bool:
    """Is an API-Football key in the Keychain? Never raises (a keyring problem reads as "no key")."""
    try:
        return bool(secrets.get_secret(football.KEY_NAME))
    except Exception as exc:
        log.warning("could not read the football API key from the Keychain: %s", type(exc).__name__)
        return False


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Optional key: without it the tools are hidden and that is fine. With it, the plan and today's
    quota from `/status` (one request). Only `jarvis doctor` calls this. Never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    try:
        key = secrets.get_secret(football.KEY_NAME)
    except Exception as exc:  # keyring problems must not break the doctor
        return [Check("Football API key", False, f"could not read the Keychain: {type(exc).__name__}")]
    if not key:
        checks.append(
            Check(
                "Football API key",
                True,
                "optional, not configured: football questions use web_search; the football.* tools are hidden"
                f" (`jarvis key set {football.KEY_NAME}` enables them)",
            )
        )
        return checks
    checks.append(Check("Football API key", True, "found in the Keychain (the football.* tools are offered as a fallback to web_search)"))
    try:
        quota = FootballClient(config.football, None, api_key=key).status()
    except Exception as exc:
        checks.append(Check("Football API (api-football)", False, f"status call failed: {exc}"))
        return checks
    used = "?" if quota.requests_today is None else str(quota.requests_today)
    limit = "?" if quota.limit_day is None else str(quota.limit_day)
    active = "" if quota.active is None else (" (active)" if quota.active else " (NOT active)")
    checks.append(
        Check(
            "Football API (api-football)",
            quota.active is not False,
            f"plan {quota.plan}{active}, {used}/{limit} requests used today; team {config.football.team!r}, league {config.football.league!r}",
        )
    )
    return checks


# --- shared ------------------------------------------------------------------------------------------


class _FootballTool(Tool):
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = True

    @classmethod
    def available(cls, ctx: ToolContext | None) -> bool:
        """Only with an API key: web_search is the first choice for football questions."""
        return api_key_configured()

    def __init__(self, ctx: ToolContext | None = None, client: FootballClient | None = None) -> None:
        super().__init__(ctx)
        self._client = client
        self._own_db: Database | None = None

    @property
    def client(self) -> FootballClient:
        if self._client is not None:
            return self._client
        ctx = self.require_ctx()
        db = None
        if ctx.runtime is not None and getattr(ctx.runtime, "db", None) is not None:
            db = ctx.runtime.db
        else:  # no Assistant around (evals, scripts): open the project db so caching still works
            if self._own_db is None:
                self._own_db = Database(ctx.paths.db_file)
            db = self._own_db
        self._client = FootballClient(ctx.config.football, db)
        return self._client

    def team_label(self) -> str:
        """The configured team name (for marking rows and the W/D/L point of view)."""
        return self.client.team_name


def _team_arg(value: str | None) -> str | None:
    text = (value or "").strip()
    if len(text) > MAX_TEAM_NAME:
        raise ToolExecutionError("team name is too long")
    return text or None


def _when(fx: Fixture) -> str:
    if fx.kickoff is None:
        return "date unknown"
    local = fx.kickoff.astimezone(local_tz())
    return f"{local.strftime('%a')} {format_local(fx.kickoff)}"


def _competition(fx: Fixture) -> str:
    parts = [p for p in (fx.league, fx.round) if p]
    return ", ".join(parts) or "competition unknown"


def _venue(fx: Fixture) -> str:
    if fx.venue and fx.city:
        return f"{fx.venue}, {fx.city}"
    return fx.venue or fx.city


def format_fixture_line(fx: Fixture, team_id: int | None, team_name: str) -> str:
    side = ""
    if team_id is not None and team_id in (fx.home_id, fx.away_id):
        side = " (home)" if team_id == fx.home_id else " (away)"
    elif team_name and fold(team_name) in (fold(fx.home), fold(fx.away)):
        side = " (home)" if fold(team_name) == fold(fx.home) else " (away)"
    status = "" if fx.status == "NS" else f" [{fx.status_text()}]"  # TBD, postponed, live, ... are worth a word
    venue = f" at {_venue(fx)}" if _venue(fx) else ""
    ident = f" [match id {fx.id}]" if fx.id is not None else ""
    return f"{_when(fx)}: {fx.home} vs {fx.away}{side} - {_competition(fx)}{venue}{status}{ident}"


def format_result_line(fx: Fixture, team_id: int | None, team_name: str) -> str:
    outcome = fx.outcome_for(team_id, team_name) or "-"
    status = "" if fx.status == "FT" else f" [{fx.status_text()}]"
    ident = f" [match id {fx.id}]" if fx.id is not None else ""
    return f"{_when(fx)}: {outcome} {fx.home} {fx.score_text()} {fx.away} - {_competition(fx)}{status}{ident}"


def format_match_details(fx: Fixture) -> str:
    lines = [f"{fx.home} {fx.score_text()} {fx.away} - {_competition(fx)} - {fx.status_text()}"]
    kickoff = f"Kickoff: {_when(fx)}" if fx.kickoff else "Kickoff: unknown"
    if _venue(fx):
        kickoff += f", {_venue(fx)}"
    lines.append(kickoff)
    score_parts = []
    for label, pair in (("half-time", fx.halftime), ("full-time", fx.fulltime), ("extra time", fx.extratime), ("penalties", fx.penalty)):
        if None not in pair:
            score_parts.append(f"{label} {pair[0]}-{pair[1]}")
    if score_parts:
        lines.append("Score: " + ", ".join(score_parts))
    goals = [e for e in fx.events if e.type.casefold() == "goal" and e.detail.casefold() != "missed penalty"]
    cards = [e for e in fx.events if e.type.casefold() == "card"]
    if goals:
        lines.append("Goals:")
        for e in goals:
            extra = []
            detail = e.detail.casefold()
            if "penalty" in detail:
                extra.append("penalty")
            if "own" in detail:
                extra.append("own goal")
            if e.assist:
                extra.append(f"assist {e.assist}")
            lines.append(f"  {e.clock()} {e.player or '?'} ({e.team}{', ' + ', '.join(extra) if extra else ''})")
    elif fx.is_finished or fx.is_live:
        lines.append("Goals: none recorded")
    if cards:
        lines.append("Cards:")
        for e in cards:
            lines.append(f"  {e.clock()} {e.detail or 'card'}: {e.player or '?'} ({e.team})")
    return "\n".join(lines)


def format_standings(league: str, season: int | None, rows: list[StandingRow], top: int, mark: str) -> str:
    shown = rows[:top]
    season_text = f" {season}/{str(season + 1)[-2:]}" if season is not None else ""
    lines = [f"{league}{season_text} standings (top {len(shown)} of {len(rows)}):"]
    lines.append("  #  Team                       P   W-D-L    GF:GA   GD  Pts  Form")
    marked = False
    for row in shown:
        is_mine = bool(mark) and fold(row.team) == fold(mark)
        marked = marked or is_mine
        flag = "*" if is_mine else " "
        wdl = f"{_n(row.win)}-{_n(row.draw)}-{_n(row.lose)}"
        gd = "?" if row.goal_diff is None else f"{row.goal_diff:+d}"
        lines.append(
            f"{flag}{_n(row.rank):>2}  {row.team[:25]:<25}  {_n(row.played):>2}  {wdl:<8} "
            f"{_n(row.goals_for):>3}:{_n(row.goals_against):<3} {gd:>4}  {_n(row.points):>3}  {row.form or '-'}"
        )
    if not shown:
        lines.append("(no rows)")
    if marked:
        lines.append(f"* = {mark}")
    elif mark:
        mine = next((r for r in rows if fold(r.team) == fold(mark)), None)
        if mine is not None:
            lines.append(f"{mark}: rank {_n(mine.rank)}, {_n(mine.points)} points (outside the top {top})")
    return "\n".join(lines)


def _n(value: int | None) -> str:
    return "?" if value is None else str(value)


# --- team_fixtures -------------------------------------------------------------------------------------


class TeamFixturesInput(BaseModel):
    team: str | None = Field(default=None, description="Team name. Default: the configured team (`football.team`).")
    next_n: int = Field(default=5, ge=1, le=MAX_MATCHES, description="How many upcoming matches (1-20).")


class TeamFixtures(_FootballTool):
    name = "team_fixtures"
    description = (
        "Upcoming matches of a football team: local kickoff time, competition and round, home vs away, "
        "venue. Defaults to the configured team. Use for 'when is the next match'."
    )
    input_model = TeamFixturesInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, TeamFixturesInput)
        team = self.client.resolve_team(_team_arg(args.team))
        fixtures = self.client.next_fixtures(team.id, args.next_n)
        if not fixtures:
            return ToolResult(f"No upcoming matches found for {team.name}.")
        lines = [f"Next {len(fixtures)} match(es) for {team.name} (times are local):"]
        lines += [format_fixture_line(fx, team.id, team.name) for fx in fixtures]
        return ToolResult("\n".join(lines))


# --- team_results -----------------------------------------------------------------------------------------


class TeamResultsInput(BaseModel):
    team: str | None = Field(default=None, description="Team name. Default: the configured team (`football.team`).")
    last_n: int = Field(default=5, ge=1, le=MAX_MATCHES, description="How many finished matches (1-20).")


class TeamResults(_FootballTool):
    name = "team_results"
    description = (
        "Latest finished matches of a football team with scores and the result from the team's point "
        "of view (W/D/L). Defaults to the configured team. Use for 'how did the last match end'."
    )
    input_model = TeamResultsInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, TeamResultsInput)
        team = self.client.resolve_team(_team_arg(args.team))
        fixtures = self.client.last_fixtures(team.id, args.last_n)
        if not fixtures:
            return ToolResult(f"No recent results found for {team.name}.")
        lines = [f"Last {len(fixtures)} result(s) for {team.name} (W/D/L from {team.name}'s point of view):"]
        lines += [format_result_line(fx, team.id, team.name) for fx in fixtures]
        return ToolResult("\n".join(lines))


# --- match_details ----------------------------------------------------------------------------------------


class MatchDetailsInput(BaseModel):
    match_id: int = Field(ge=1, description="The match id shown by team_fixtures or team_results.")


class MatchDetails(_FootballTool):
    name = "match_details"
    description = "Details of one match by id: score by half, scorers with minutes, cards, and the live minute if the match is in play."
    input_model = MatchDetailsInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, MatchDetailsInput)
        fx = self.client.fixture(args.match_id)
        if fx is None:
            raise ToolExecutionError(f"football API knows no match with id {args.match_id}")
        return ToolResult(format_match_details(fx))


# --- league_standings -----------------------------------------------------------------------------------


class LeagueStandingsInput(BaseModel):
    league: str | None = Field(
        default=None,
        description="League or competition name, e.g. 'UEFA Champions League'. Default: the configured league (`football.league`).",
    )
    top: int = Field(default=20, ge=1, le=40, description="How many rows from the top (1-40).")


class LeagueStandings(_FootballTool):
    name = "league_standings"
    description = (
        "Current standings of a league: rank, team, played, W-D-L, goal difference, points, form. "
        "The configured team is marked. Defaults to the configured league."
    )
    input_model = LeagueStandingsInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, LeagueStandingsInput)
        name = (args.league or "").strip()
        if len(name) > MAX_TEAM_NAME:
            raise ToolExecutionError("league name is too long")
        league = self.client.resolve_league(name or None)
        league_name, season, rows = self.client.standings(league)
        if not rows:
            return ToolResult(f"No standings available for {league_name} (season {season}).")
        return ToolResult(format_standings(league_name, season, rows, args.top, self.team_label()))
