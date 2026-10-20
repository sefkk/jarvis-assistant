"""Google OAuth for Calendar, Gmail and Sheets.

- Scopes are the minimum: calendar events, Gmail read, Gmail compose (drafts), Sheets, Drive
  metadata (read-only, to find spreadsheets by name). No send scope, no Drive file content.
- Scopes are grouped per feature. `login()` requests all of them with incremental authorization
  (`include_granted_scopes`), so a second login adds the Sheets scopes to an older token without
  taking anything away. Callers state the scopes they need (`required=`): Calendar and Gmail keep
  working with an old token that lacks the Sheets scopes, and the Sheets tools say precisely
  which login is missing.
- The OAuth client JSON and the token live in the macOS Keychain, never on disk, never in logs.
- `googleapiclient`, `google.oauth2` and `google_auth_oauthlib` are imported lazily so that
  importing Jarvis never pays for them and a broken install only breaks the Google tools.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.exceptions import PermissionMissingError, ToolExecutionError
from jarvis.observability.health import Check
from jarvis.utils import secrets

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

log = logging.getLogger("jarvis.google")

CALENDAR_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
SCOPES = CALENDAR_GMAIL_SCOPES + SHEETS_SCOPES  # what `login()` requests
_SCOPE_PREFIX = "https://www.googleapis.com/auth/"

CLIENT_SECRET_NAME = "google_client_secret"
TOKEN_NAME = "google_token"
LOGIN_HINT = "run `jarvis google login --client-secret <path to the OAuth client JSON>`"
SHEETS_LOGIN_HINT = "run `jarvis google login` once more to add the Sheets permission (incremental consent keeps the others)"


def scope_hint(missing: list[str]) -> str:
    """The login hint for a set of missing scopes: a re-login for Sheets-only gaps, the full one otherwise."""
    if missing and all(s in SHEETS_SCOPES for s in missing):
        return SHEETS_LOGIN_HINT
    return LOGIN_HINT


def short_scopes(scopes: list[str] | None) -> str:
    return ", ".join(s[len(_SCOPE_PREFIX) :] if s.startswith(_SCOPE_PREFIX) else s for s in (scopes or [])) or "none"


# --- client secret ------------------------------------------------------------------------------


def _parse_client_config(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise PermissionMissingError(f"the Google OAuth client JSON is not valid JSON; {LOGIN_HINT}") from None
    if not isinstance(data, dict) or not any(k in data for k in ("installed", "web")):
        raise PermissionMissingError(
            "the Google OAuth client JSON must be a Desktop-app client file (with an 'installed' section); " + LOGIN_HINT
        )
    return data


def _load_client_config() -> dict[str, Any]:
    raw = secrets.get_secret(CLIENT_SECRET_NAME)
    if not raw:
        raise PermissionMissingError(f"no Google OAuth client secret in the Keychain; {LOGIN_HINT}")
    return _parse_client_config(raw)


# --- login / logout -----------------------------------------------------------------------------


def _run_flow(client_config: dict[str, Any]) -> Any:
    """Open the browser consent flow and return google.oauth2 Credentials (monkeypatched in tests).

    `include_granted_scopes` (incremental authorization) makes the new token cover the union of
    the scopes granted earlier and now. oauthlib would otherwise fail the login when Google
    returns a different scope set than requested (an unticked box): relaxing that check keeps the
    credentials, and `login()` reports what is actually missing.
    """
    import os

    from google_auth_oauthlib.flow import InstalledAppFlow

    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    return flow.run_local_server(port=0, open_browser=True, include_granted_scopes="true")


def login(client_secret_path: Path | None = None) -> list[str]:
    """Store the client JSON (if a path is given), run the consent flow, store the token.

    Returns the granted scopes so the CLI can warn when the user unticked one.
    """
    if client_secret_path is not None:
        path = Path(client_secret_path).expanduser()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PermissionMissingError(f"cannot read the OAuth client JSON at {path}: {exc.strerror or exc}") from None
        _parse_client_config(raw)  # validate before storing
        secrets.set_secret(CLIENT_SECRET_NAME, raw)
        log.info("stored Google OAuth client secret in the Keychain")
    client_config = _load_client_config()
    try:
        creds = _run_flow(client_config)
    except PermissionMissingError:
        raise
    except Exception as exc:  # oauthlib raises many things: bad client, user closed the browser, port in use
        raise PermissionMissingError(f"Google login did not complete ({type(exc).__name__}: {exc}); {LOGIN_HINT}") from None
    secrets.set_secret(TOKEN_NAME, creds.to_json())
    granted = list(creds.scopes or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        log.warning("Google login granted fewer scopes than requested; missing: %s", short_scopes(missing))
    else:
        log.info("Google login complete")
    return granted


def logout() -> None:
    """Forget the token. The client secret stays so the next login needs no file."""
    secrets.delete_secret(TOKEN_NAME)
    log.info("Google token removed from the Keychain")


# --- credentials --------------------------------------------------------------------------------


def _refresh(creds: Any) -> None:
    """Refresh the access token over the network (monkeypatched in tests)."""
    from google.auth.transport.requests import Request

    creds.refresh(Request())


def get_credentials(required: list[str] | None = None) -> Any:
    """Credentials from the Keychain, refreshed when needed. Raises PermissionMissingError if unusable.

    `required`: the scopes the caller needs (default: Calendar and Gmail). An older token
    without the Sheets scopes must keep working for Calendar and Gmail.
    """
    required = CALENDAR_GMAIL_SCOPES if required is None else list(required)
    raw = secrets.get_secret(TOKEN_NAME)
    if not raw:
        raise PermissionMissingError(f"Google account not connected; {LOGIN_HINT}")
    try:
        info = json.loads(raw)
        if not isinstance(info, dict):
            raise ValueError("token is not an object")
    except ValueError:
        raise PermissionMissingError(f"the stored Google token is unreadable; {LOGIN_HINT}") from None

    from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
    from google.oauth2.credentials import Credentials

    try:
        creds = Credentials.from_authorized_user_info(info)
    except ValueError:
        raise PermissionMissingError(f"the stored Google token is incomplete; {LOGIN_HINT}") from None

    granted = list(creds.scopes or [])
    missing = [s for s in required if s not in granted]
    if missing:
        raise PermissionMissingError(f"the Google token lacks the scope(s) {short_scopes(missing)}; {scope_hint(missing)}")

    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise PermissionMissingError(f"the Google token is not usable; {LOGIN_HINT}")
        try:
            _refresh(creds)
        except RefreshError as exc:
            # Revoked, or the client secret was deleted in the Cloud Console.
            raise PermissionMissingError(f"Google refused to refresh the token ({_first_line(exc)}); {LOGIN_HINT}") from None
        except (TransportError, OSError) as exc:
            raise ToolExecutionError(f"could not reach Google to refresh the token ({type(exc).__name__})") from None
        except GoogleAuthError as exc:
            raise ToolExecutionError(f"Google token refresh failed ({type(exc).__name__})") from None
        secrets.set_secret(TOKEN_NAME, creds.to_json())
        log.info("Google token refreshed")
    return creds


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:120] if text else type(exc).__name__


# --- services -----------------------------------------------------------------------------------


def _discovery_build(api: str, version: str, credentials: Any) -> Any:
    """The real googleapiclient build (monkeypatched in tests)."""
    from googleapiclient.discovery import build

    return build(api, version, credentials=credentials, cache_discovery=False)


def build_service(api: str, version: str, *, required: list[str] | None = None) -> Any:
    """A googleapiclient resource for `api` (e.g. "calendar", "v3") with valid credentials.

    `required`: the scopes this service needs (default: Calendar and Gmail, see get_credentials)."""
    creds = get_credentials(required)
    try:
        return _discovery_build(api, version, creds)
    except (PermissionMissingError, ToolExecutionError):
        raise
    except Exception as exc:  # UnknownApiNameOrVersion, httplib2 errors, ...
        raise ToolExecutionError(f"could not initialise the Google {api} client ({type(exc).__name__})") from None


def execute(request: Any, what: str) -> Any:
    """Run a googleapiclient request and map every failure to a Jarvis error.

    401 -> PermissionMissingError (token rejected), everything else -> ToolExecutionError with a short,
    model-readable message. Nothing in the message comes from mail or event content.
    """
    import httplib2
    from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
    from googleapiclient.errors import Error as ApiClientError
    from googleapiclient.errors import HttpError

    try:
        return request.execute()
    except HttpError as exc:
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
        reason = _first_line(getattr(exc, "reason", None) or exc)
        if status == 401:
            raise PermissionMissingError(f"{what}: Google rejected the token (401); {LOGIN_HINT}") from None
        if status == 403:
            hint = ""
            lowered = reason.lower()
            if "has not been used" in lowered or "disabled" in lowered or "not enabled" in lowered:
                hint = " (enable the API for this project in Google Cloud Console)"
            elif "insufficient" in lowered or "scope" in lowered:
                hint = f" ({LOGIN_HINT})"
            raise ToolExecutionError(f"{what}: forbidden (403): {reason}{hint}") from None
        if status == 404:
            raise ToolExecutionError(f"{what}: not found (404): {reason}") from None
        if status == 429:
            raise ToolExecutionError(f"{what}: Google rate limit hit (429); try again in a moment") from None
        raise ToolExecutionError(f"{what}: Google API error {status or 'unknown'}: {reason}") from None
    except RefreshError as exc:
        raise PermissionMissingError(f"{what}: token refresh failed ({_first_line(exc)}); {LOGIN_HINT}") from None
    except (TransportError, httplib2.HttpLib2Error, OSError) as exc:
        raise ToolExecutionError(f"{what}: network error ({type(exc).__name__})") from None
    except (GoogleAuthError, ApiClientError) as exc:
        raise ToolExecutionError(f"{what}: {type(exc).__name__}: {_first_line(exc)}") from None


# --- health -------------------------------------------------------------------------------------


def _parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.rstrip("Z").split(".")[0]
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def stored_scopes() -> list[str] | None:
    """The scopes of the token in the Keychain, or None without a (readable) token. No network."""
    raw = secrets.get_secret(TOKEN_NAME)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(s) for s in scopes]


def health_checks(config: "Config", paths: "Paths") -> list[Check]:
    """Keychain-only checks for `jarvis doctor`: no network, no browser, never raises.

    The Calendar/Gmail scopes decide whether the token passes; the Sheets scopes get their own
    line so an old token is not reported as broken."""
    checks: list[Check] = []
    try:
        raw_secret = secrets.get_secret(CLIENT_SECRET_NAME)
        if raw_secret:
            try:
                _parse_client_config(raw_secret)
                checks.append(Check("Google OAuth client", True, "found in Keychain"))
            except PermissionMissingError:
                checks.append(Check("Google OAuth client", False, f"stored JSON is not a Desktop-app client; {LOGIN_HINT}"))
        else:
            checks.append(Check("Google OAuth client", True, f"optional, not set up; {LOGIN_HINT}", warn=True))

        raw_token = secrets.get_secret(TOKEN_NAME)
        if not raw_token:
            # nothing set up at all is a hint (Google is optional); a client without a token is a broken login
            checks.append(
                Check(
                    "Google token",
                    not raw_secret,
                    f"{'optional, not set up' if not raw_secret else 'missing'}; {LOGIN_HINT}",
                    warn=not raw_secret,
                )
            )
            return checks
        try:
            info = json.loads(raw_token)
            if not isinstance(info, dict):
                raise ValueError
        except ValueError:
            checks.append(Check("Google token", False, f"stored token is unreadable; {LOGIN_HINT}"))
            return checks

        scopes = info.get("scopes") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        missing = [s for s in CALENDAR_GMAIL_SCOPES if s not in scopes]
        missing_sheets = [s for s in SHEETS_SCOPES if s not in scopes]
        has_refresh = bool(info.get("refresh_token"))
        expiry = _parse_expiry(info.get("expiry"))
        now = datetime.now(timezone.utc)

        if missing:
            checks.append(Check("Google token", False, f"missing scope(s) {short_scopes(missing)}; {LOGIN_HINT}"))
        elif not has_refresh:
            checks.append(Check("Google token", False, f"no refresh token; {LOGIN_HINT}"))
        elif expiry is None:
            checks.append(Check("Google token", True, "present, expiry unknown (refreshes on first use)"))
        elif expiry <= now:
            checks.append(Check("Google token", True, "expired, refresh token present (refreshes on first use)"))
        else:
            left = expiry - now
            minutes = int(left / timedelta(minutes=1))
            checks.append(Check("Google token", True, f"valid for {minutes} more minute(s)"))
        checks.append(Check("Google scopes", not missing, short_scopes(scopes)))
        if missing_sheets:
            checks.append(
                Check(
                    "Google Sheets scopes",
                    False,
                    f"missing {short_scopes(missing_sheets)}: run `jarvis google login` once more "
                    "(incremental consent adds Sheets and Drive metadata)",
                )
            )
        else:
            checks.append(Check("Google Sheets scopes", True, short_scopes(SHEETS_SCOPES)))
    except Exception as exc:  # the doctor must always print something
        checks.append(Check("Google token", False, f"check failed: {type(exc).__name__}"))
    return checks
