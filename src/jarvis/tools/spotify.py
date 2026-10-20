"""Spotify tools: play, pause, next, previous, now_playing, search_and_play.

Playback control goes through AppleScript constants (values only ever travel as argv).
`search_and_play` looks the query up with the Web API (client-credentials flow, no user login)
and then hands the resulting URI to AppleScript. Credentials live in the Keychain.
"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.observability.health import Check
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolResult, did_you_mean
from jarvis.utils import secrets
from jarvis.utils.applescript import run_applescript
from jarvis.voice import names
from jarvis.voice.names import LOW_MATCH

CLIENT_ID_SECRET = "spotify_client_id"
CLIENT_SECRET_SECRET = "spotify_client_secret"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
SPOTIFY_APP = "/Applications/Spotify.app"
HTTP_TIMEOUT = 15.0
TOKEN_SAFETY_MARGIN = 60  # seconds subtracted from expires_in so a token is never used at the edge

MIN_OFFER = 0.3  # below this fit even the closest result is not offered; the user is asked to repeat
URI_RE = re.compile(r"^spotify:(track|album|playlist|artist):[A-Za-z0-9]+$")
Kind = Literal["track", "album", "playlist", "artist"]

MISSING_CREDENTIALS = (
    "Spotify credentials are missing. Create an app at https://developer.spotify.com/dashboard, then run "
    "`jarvis key set spotify_client_id` and `jarvis key set spotify_client_secret`."
)

# Injectable for tests (httpx.MockTransport). None means the default transport.
_transport: httpx.BaseTransport | None = None
# (access token, expiry as time.monotonic()) cached in memory only.
_token_cache: tuple[str, float] | None = None

# --- AppleScript constants (never interpolate; values arrive in argv) --------------

PLAY_SCRIPT = """on run argv
    tell application "Spotify" to play
    return "playing"
end run"""

PAUSE_SCRIPT = """on run argv
    if application "Spotify" is not running then return "Spotify is not running"
    tell application "Spotify" to pause
    return "paused"
end run"""

NEXT_SCRIPT = """on run argv
    if application "Spotify" is not running then return "Spotify is not running"
    tell application "Spotify" to next track
    return "skipped to the next track"
end run"""

PREVIOUS_SCRIPT = """on run argv
    if application "Spotify" is not running then return "Spotify is not running"
    tell application "Spotify" to previous track
    return "went back to the previous track"
end run"""

PLAY_URI_SCRIPT = """on run argv
    tell application "Spotify" to play track (item 1 of argv)
    return "playing " & (item 1 of argv)
end run"""

NOW_PLAYING_SCRIPT = """on run argv
    if application "Spotify" is not running then return "state=not running"
    tell application "Spotify"
        set st to (player state as text)
        if st is "stopped" then return "state=stopped"
        set t to current track
        set pos to (player position as integer)
        set dur to ((duration of t) / 1000) as integer
        return "state=" & st & linefeed & "artist=" & (artist of t) & linefeed & "track=" & (name of t) & linefeed & "album=" & (album of t) & linefeed & "position=" & pos & linefeed & "duration=" & dur
    end tell
end run"""


# --- playback tools --------------------------------------------------------------------


class Play(Tool):
    name = "play"
    description = "Resume playback in Spotify (opens Spotify if needed)."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(run_applescript(PLAY_SCRIPT) or "playing")


class Pause(Tool):
    name = "pause"
    description = "Pause Spotify playback."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(run_applescript(PAUSE_SCRIPT) or "paused")


class Next(Tool):
    name = "next"
    description = "Skip to the next track in Spotify."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(run_applescript(NEXT_SCRIPT) or "skipped to the next track")


class Previous(Tool):
    name = "previous"
    description = "Go back to the previous track in Spotify."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(run_applescript(PREVIOUS_SCRIPT) or "went back to the previous track")


def _format_seconds(value: str) -> str:
    try:
        seconds = int(value)
    except ValueError:
        return value
    return f"{seconds // 60}:{seconds % 60:02d}"


def _parse_now_playing(raw: str) -> str:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    state = fields.get("state", "")
    if state in ("not running", "stopped", ""):
        return f"Spotify is {state or 'not reporting a state'}; nothing is playing."
    track, artist, album = fields.get("track", "?"), fields.get("artist", "?"), fields.get("album", "")
    text = f"{state.capitalize()}: {track} by {artist}"
    if album:
        text += f" (album: {album})"
    if "position" in fields:
        text += f", at {_format_seconds(fields['position'])}"
        if "duration" in fields:
            text += f" of {_format_seconds(fields['duration'])}"
    return text + "."


class NowPlaying(Tool):
    name = "now_playing"
    description = "What Spotify is playing right now: track, artist, album, state and position."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(_parse_now_playing(run_applescript(NOW_PLAYING_SCRIPT)))


# --- Web API (client-credentials flow) ----------------------------------------------------


def reset_token_cache() -> None:
    global _token_cache
    _token_cache = None


def load_credentials() -> tuple[str, str]:
    client_id = secrets.get_secret(CLIENT_ID_SECRET)
    client_secret = secrets.get_secret(CLIENT_SECRET_SECRET)
    if not client_id or not client_secret:
        raise PermissionMissingError(MISSING_CREDENTIALS)
    return client_id, client_secret


def get_app_token(client_id: str, client_secret: str) -> str:
    """An app access token, cached in memory until shortly before it expires."""
    global _token_cache
    if _token_cache is not None and time.monotonic() < _token_cache[1]:
        return _token_cache[0]
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, transport=_transport) as client:
            response = client.post(TOKEN_URL, data={"grant_type": "client_credentials"}, auth=(client_id, client_secret))
    except httpx.HTTPError as exc:
        raise ToolExecutionError(f"could not reach Spotify for a token: {type(exc).__name__}") from None
    if response.status_code in (400, 401):
        raise PermissionMissingError(
            "Spotify rejected the client id/secret. Check them at https://developer.spotify.com/dashboard and "
            "store them again with `jarvis key set spotify_client_id` / `jarvis key set spotify_client_secret`."
        )
    if response.status_code != 200:
        raise ToolExecutionError(f"Spotify token request failed with status {response.status_code}")
    try:
        payload = response.json()
        token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise ToolExecutionError("Spotify token response was not understood") from None
    if not token:
        raise ToolExecutionError("Spotify returned an empty access token")
    _token_cache = (token, time.monotonic() + max(expires_in - TOKEN_SAFETY_MARGIN, 0))
    return token


def search_uri(query: str, kind: Kind, token: str) -> tuple[str, str]:
    """The first search result's (uri, human label). Only well-formed URIs are accepted."""
    first = search_items(query, kind, token)[0]
    return first.uri, first.label


class Found:
    """One search result: its URI, the label shown to the model and the text a request is matched against."""

    def __init__(self, uri: str, label: str, name: str, who: str) -> None:
        self.uri, self.label, self.name, self.who = uri, label, name, who

    @property
    def match_text(self) -> str:
        return f"{self.name} {self.who}".strip()

    @property
    def spoken_name(self) -> str:
        return f"{self.who} - {self.name}" if self.who else self.name


def search_items(query: str, kind: Kind, token: str) -> list[Found]:
    """Up to five results in Spotify's order. Only well-formed URIs are accepted; none found raises."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, transport=_transport) as client:
            response = client.get(
                SEARCH_URL,
                params={"q": query, "type": kind, "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise ToolExecutionError(f"Spotify search failed: {type(exc).__name__}") from None
    if response.status_code == 401:
        reset_token_cache()
        raise ToolExecutionError("Spotify rejected the access token; try again")
    if response.status_code != 200:
        raise ToolExecutionError(f"Spotify search failed with status {response.status_code}")
    try:
        items = response.json()[f"{kind}s"]["items"]
    except (ValueError, KeyError, TypeError):
        raise ToolExecutionError("Spotify search response was not understood") from None
    found: list[Found] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not isinstance(uri, str):
            continue
        match = URI_RE.match(uri)
        if match is None or match.group(1) != kind:
            raise ToolExecutionError(f"Spotify returned an unexpected URI for a {kind}; refusing to play it")
        found.append(Found(uri, _describe_item(item, kind), str(item.get("name") or ""), _who(item, kind)))
    if not found:
        raise ToolExecutionError(f"no {kind} found for {query!r}")
    return found


def _who(item: dict[str, Any], kind: Kind) -> str:
    artists = item.get("artists")
    if isinstance(artists, list) and artists:
        return ", ".join(str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name"))
    if kind == "playlist" and isinstance(item.get("owner"), dict):
        return str(item["owner"].get("display_name") or "")
    return ""


BETTER_BY = 0.25  # a lower-ranked result must fit the request this much better to beat Spotify's first one


def pick(query: str, found: list[Found]) -> tuple[Found, float]:
    """Spotify's first result, unless a later one clearly fits the request better; with its fit (0..1)."""
    from jarvis.voice.names import match_score  # noqa: PLC0415

    scored = [(item, match_score(query, item.match_text)) for item in found]
    best, best_score = scored[0]
    for item, score in scored[1:]:
        if score >= best_score + BETTER_BY:
            best, best_score = item, score
    return best, best_score


def _describe_item(item: dict[str, Any], kind: Kind) -> str:
    name = str(item.get("name") or "?")
    who = ""
    artists = item.get("artists")
    if isinstance(artists, list) and artists:
        names = [str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")]
        who = ", ".join(names)
    elif kind == "playlist" and isinstance(item.get("owner"), dict):
        who = str(item["owner"].get("display_name") or "")
    return f"{kind} '{name}'" + (f" by {who}" if who else "")


class SearchAndPlayInput(BaseModel):
    query: str = Field(description="What to search for, e.g. 'Kind of Blue Miles Davis' or 'lofi beats playlist'.")
    kind: Kind = Field(default="track", description="What kind of result to play: track, album, playlist or artist.")
    confirmed: bool = Field(
        default=False,
        description="True only after the user said yes to a \"Did you mean 'X'?\" question from this tool: play that match without asking again.",
    )


class SearchAndPlay(Tool):
    name = "search_and_play"
    description = "Search Spotify for a track, album, playlist or artist and start playing the best match in the Spotify app."
    input_model = SearchAndPlayInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SearchAndPlayInput)
        query = args.query.strip()
        if not query:
            raise ToolValidationError("query is empty")
        if len(query) > 300:
            raise ToolValidationError("query is too long (max 300 characters)")
        client_id, client_secret = load_credentials()
        token = get_app_token(client_id, client_secret)
        best, score = pick(query, search_items(query, args.kind, token))
        if score < LOW_MATCH and args.kind != "playlist" and not args.confirmed and names.spoken(self.ctx):
            question = self._did_you_mean(query, args.kind, token, best, score)
            if question is not None:
                return question
        uri, label = best.uri, best.label
        if not URI_RE.match(uri):  # belt and braces: this is the value that reaches osascript
            raise ToolExecutionError("refusing to play a malformed Spotify URI")
        run_applescript(PLAY_URI_SCRIPT, [uri])
        return ToolResult(f"Playing {label} ({uri}).")

    def _did_you_mean(self, query: str, kind: Kind, token: str, best: "Found", score: float) -> ToolResult | None:
        """A spoken request whose best result fits poorly: try the closest known name (speech vocabulary),
        then ask the user instead of playing something else."""
        suggestion = names.suggest(query, names.known_names(self.ctx))
        if suggestion is not None:
            corrected = query.replace(suggestion.heard, suggestion.term) if suggestion.heard in query else suggestion.term
            try:
                alt, alt_score = pick(corrected, search_items(corrected, kind, token))
            except ToolExecutionError:
                alt, alt_score = None, 0.0
            if alt is not None and alt_score > score:
                best, score = alt, alt_score
        if score < MIN_OFFER:
            return ToolResult(
                f"No {kind} on Spotify fits {query!r} well (closest: {best.label}). The request was spoken: "
                "ask the user to say or spell the name again."
            )
        return ToolResult(
            f"Closest {kind} for {query!r}: {best.label}, fit {score:.2f}. "
            + did_you_mean(best.spoken_name, retry=f"call search_and_play with query {best.match_text!r}, kind {kind!r} and confirmed=true")
        )


# --- health -------------------------------------------------------------------------------


def health_checks(config: Any, paths: Any) -> list[Check]:
    """Spotify entries for `jarvis doctor`. Never raises."""
    checks: list[Check] = []
    try:
        have_id = bool(secrets.get_secret(CLIENT_ID_SECRET))
        have_secret = bool(secrets.get_secret(CLIENT_SECRET_SECRET))
        ok = have_id and have_secret
        missing = [n for n, present in ((CLIENT_ID_SECRET, have_id), (CLIENT_SECRET_SECRET, have_secret)) if not present]
        hint = " and ".join(f"`jarvis key set {n}`" for n in missing)
        if ok:
            checks.append(Check("Spotify credentials", True, "found in the Keychain"))
        elif len(missing) == 2:  # not set up at all: Spotify is optional
            checks.append(Check("Spotify credentials", True, f"optional, not set up: {hint}", warn=True))
        else:
            checks.append(Check("Spotify credentials", False, f"missing: run {hint}"))
    except Exception as exc:  # keyring problems must not break the doctor
        checks.append(Check("Spotify credentials", False, f"could not read the Keychain: {type(exc).__name__}"))
    try:
        from pathlib import Path

        installed = Path(SPOTIFY_APP).exists()
        checks.append(
            Check(
                "Spotify.app",
                installed,
                f"installed at {SPOTIFY_APP}" if installed else f"not found at {SPOTIFY_APP}; install Spotify",
            )
        )
    except Exception as exc:
        checks.append(Check("Spotify.app", False, f"could not check: {type(exc).__name__}"))
    checks.append(
        Check(
            "Spotify Automation permission",
            True,
            "cannot be checked without a prompt; macOS asks on the first playback command (allow it under "
            "System Settings > Privacy & Security > Automation if it was refused)",
        )
    )
    return checks
