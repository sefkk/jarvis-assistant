"""google_auth: Keychain-backed OAuth without any network or browser."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError
from jarvis.utils import google_auth, secrets

CLIENT_JSON = json.dumps(
    {
        "installed": {
            "client_id": "id.apps.googleusercontent.com",
            "client_secret": "cs",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
)


def token_json(*, expiry: datetime | None, scopes=None, refresh_token="rt") -> str:
    info = {
        "token": "old-access-token",
        "refresh_token": refresh_token,
        "client_id": "id",
        "client_secret": "cs",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": list(google_auth.SCOPES) if scopes is None else scopes,
    }
    if expiry is not None:
        info["expiry"] = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps(info)


@pytest.fixture
def keychain(monkeypatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get_secret", lambda name: store.get(name))
    monkeypatch.setattr(secrets, "set_secret", store.__setitem__)
    monkeypatch.setattr(secrets, "delete_secret", lambda name: store.pop(name, None))
    return store


# google-auth stores expiry as a naive UTC datetime
NOW = datetime.now(timezone.utc).replace(tzinfo=None)
FUTURE = NOW + timedelta(hours=1)
PAST = NOW - timedelta(hours=1)


# --- scopes ---------------------------------------------------------------------------------


CAL_GMAIL = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
SHEETS = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.metadata.readonly"]


def test_scopes_are_the_minimal_ones_grouped_per_feature():
    assert google_auth.CALENDAR_GMAIL_SCOPES == CAL_GMAIL
    assert google_auth.SHEETS_SCOPES == SHEETS
    assert google_auth.SCOPES == CAL_GMAIL + SHEETS
    assert not any("send" in s or s.endswith("/gmail") or s.endswith("/calendar") for s in google_auth.SCOPES)
    # Drive: metadata only, never file contents (drive, drive.readonly, drive.file are all out)
    assert not any(s.endswith(("/drive", "/drive.readonly", "/drive.file")) for s in google_auth.SCOPES)


def test_google_libraries_are_not_imported_at_module_import():
    code = (
        "import sys, jarvis.utils.google_auth, jarvis.tools.google_calendar, jarvis.tools.gmail, jarvis.tools.google_sheets; "
        "bad=[m for m in sys.modules if m.startswith(('googleapiclient','google.oauth2','google_auth_oauthlib'))]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- login / logout ---------------------------------------------------------------------------


class FakeCreds:
    def __init__(self, scopes):
        self.scopes = scopes

    def to_json(self):
        return token_json(expiry=FUTURE, scopes=self.scopes)


def test_login_stores_client_secret_and_token(keychain, monkeypatch, tmp_path: Path):
    seen = {}

    def fake_flow(client_config):
        seen["config"] = client_config
        return FakeCreds(list(google_auth.SCOPES))

    monkeypatch.setattr(google_auth, "_run_flow", fake_flow)
    path = tmp_path / "client.json"
    path.write_text(CLIENT_JSON)
    granted = google_auth.login(path)
    assert granted == google_auth.SCOPES
    assert keychain["google_client_secret"] == CLIENT_JSON
    assert json.loads(keychain["google_token"])["refresh_token"] == "rt"
    assert seen["config"]["installed"]["client_id"] == "id.apps.googleusercontent.com"

    # second login without a path reuses the stored client secret
    google_auth.login(None)
    assert seen["config"]["installed"]["client_secret"] == "cs"


def test_login_requests_incremental_consent_and_reports_partial_grants(keychain, monkeypatch):
    """The real flow asks for the union of old and new scopes; an unticked box is reported, not hidden."""
    import google_auth_oauthlib.flow as flow_module

    keychain["google_client_secret"] = CLIENT_JSON
    seen = {}

    class FakeFlow:
        @classmethod
        def from_client_config(cls, client_config, scopes, **kwargs):
            seen["scopes"] = list(scopes)
            return cls()

        def run_local_server(self, **kwargs):
            seen["kwargs"] = kwargs
            return FakeCreds(CAL_GMAIL + SHEETS[:1])  # the user unticked the Drive metadata box

    monkeypatch.setattr(flow_module, "InstalledAppFlow", FakeFlow)
    granted = google_auth.login(None)
    assert seen["scopes"] == CAL_GMAIL + SHEETS
    assert seen["kwargs"]["include_granted_scopes"] == "true"
    assert granted == CAL_GMAIL + SHEETS[:1]
    assert set(google_auth.SCOPES) - set(granted) == {SHEETS[1]}
    # oauthlib must not turn Google's (different) granted scope set into a failed login
    import os

    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")


def test_login_without_client_secret_is_permission_error(keychain, monkeypatch):
    monkeypatch.setattr(google_auth, "_run_flow", lambda cfg: pytest.fail("flow must not run"))
    with pytest.raises(PermissionMissingError, match="jarvis google login --client-secret"):
        google_auth.login(None)


def test_login_rejects_non_desktop_client_json(keychain, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(google_auth, "_run_flow", lambda cfg: pytest.fail("flow must not run"))
    path = tmp_path / "bad.json"
    path.write_text('{"service_account": {}}')
    with pytest.raises(PermissionMissingError, match="Desktop-app"):
        google_auth.login(path)
    assert "google_client_secret" not in keychain


def test_login_flow_failure_is_permission_error(keychain, monkeypatch):
    keychain["google_client_secret"] = CLIENT_JSON

    def boom(cfg):
        raise RuntimeError("user closed the browser")

    monkeypatch.setattr(google_auth, "_run_flow", boom)
    with pytest.raises(PermissionMissingError, match="did not complete"):
        google_auth.login(None)
    assert "google_token" not in keychain


def test_logout_removes_only_the_token(keychain):
    keychain["google_client_secret"] = CLIENT_JSON
    keychain["google_token"] = token_json(expiry=FUTURE)
    google_auth.logout()
    assert "google_token" not in keychain and "google_client_secret" in keychain
    google_auth.logout()  # idempotent


# --- get_credentials --------------------------------------------------------------------------


def test_missing_token_is_permission_error(keychain):
    with pytest.raises(PermissionMissingError, match="jarvis google login --client-secret"):
        google_auth.get_credentials()
    with pytest.raises(PermissionMissingError):
        google_auth.build_service("calendar", "v3")


def test_corrupt_or_incomplete_token_is_permission_error(keychain):
    keychain["google_token"] = "not json"
    with pytest.raises(PermissionMissingError, match="unreadable"):
        google_auth.get_credentials()
    keychain["google_token"] = json.dumps({"token": "x"})  # no refresh token / client id
    with pytest.raises(PermissionMissingError, match="incomplete"):
        google_auth.get_credentials()


def test_token_with_missing_scope_is_permission_error(keychain):
    keychain["google_token"] = token_json(expiry=FUTURE, scopes=["https://www.googleapis.com/auth/calendar.events"])
    with pytest.raises(PermissionMissingError, match="gmail.readonly"):
        google_auth.get_credentials()


def test_old_token_without_sheets_scopes_still_works_for_calendar_and_gmail(keychain, monkeypatch):
    """Per-feature `required`: an older token without the Sheets scopes must keep working."""
    keychain["google_token"] = token_json(expiry=FUTURE, scopes=CAL_GMAIL)
    monkeypatch.setattr(google_auth, "_discovery_build", lambda api, version, creds: f"{api}-service")
    assert google_auth.get_credentials().valid
    assert google_auth.build_service("gmail", "v1") == "gmail-service"
    assert google_auth.build_service("calendar", "v3", required=google_auth.CALENDAR_GMAIL_SCOPES) == "calendar-service"
    with pytest.raises(PermissionMissingError, match="spreadsheets.*jarvis google login` once more"):
        google_auth.get_credentials(google_auth.SHEETS_SCOPES)
    with pytest.raises(PermissionMissingError, match="once more"):
        google_auth.build_service("sheets", "v4", required=google_auth.SHEETS_SCOPES)
    # the union token serves both
    keychain["google_token"] = token_json(expiry=FUTURE)
    assert google_auth.build_service("sheets", "v4", required=google_auth.SHEETS_SCOPES) == "sheets-service"
    assert google_auth.build_service("drive", "v3", required=google_auth.SHEETS_SCOPES) == "drive-service"
    # a Sheets-only token (never produced by our login) does not serve Gmail
    keychain["google_token"] = token_json(expiry=FUTURE, scopes=SHEETS)
    with pytest.raises(PermissionMissingError, match="calendar.events.*--client-secret"):
        google_auth.build_service("gmail", "v1")


def test_stored_scopes_reads_the_keychain_only(keychain):
    assert google_auth.stored_scopes() is None
    keychain["google_token"] = "not json"
    assert google_auth.stored_scopes() is None
    keychain["google_token"] = token_json(expiry=FUTURE, scopes=CAL_GMAIL)
    assert google_auth.stored_scopes() == CAL_GMAIL


def test_valid_token_is_used_without_refresh(keychain, monkeypatch):
    keychain["google_token"] = token_json(expiry=FUTURE)
    monkeypatch.setattr(google_auth, "_refresh", lambda creds: pytest.fail("must not refresh"))
    creds = google_auth.get_credentials()
    assert creds.token == "old-access-token" and creds.valid


def test_expired_token_is_refreshed_and_stored(keychain, monkeypatch):
    keychain["google_token"] = token_json(expiry=PAST)
    calls = []

    def fake_refresh(creds):
        calls.append(creds.refresh_token)
        creds.token = "new-access-token"
        creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(google_auth, "_refresh", fake_refresh)
    creds = google_auth.get_credentials()
    assert calls == ["rt"]
    assert creds.token == "new-access-token"
    stored = json.loads(keychain["google_token"])
    assert stored["token"] == "new-access-token" and stored["refresh_token"] == "rt"


def test_refresh_rejected_by_google_is_permission_error(keychain, monkeypatch):
    from google.auth.exceptions import RefreshError

    keychain["google_token"] = token_json(expiry=PAST)

    def fake_refresh(creds):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(google_auth, "_refresh", fake_refresh)
    with pytest.raises(PermissionMissingError, match="jarvis google login"):
        google_auth.get_credentials()


def test_refresh_network_failure_is_execution_error(keychain, monkeypatch):
    from google.auth.exceptions import TransportError

    keychain["google_token"] = token_json(expiry=PAST)

    def fake_refresh(creds):
        raise TransportError("connection reset")

    monkeypatch.setattr(google_auth, "_refresh", fake_refresh)
    with pytest.raises(ToolExecutionError, match="could not reach Google"):
        google_auth.get_credentials()


# --- build_service / execute ---------------------------------------------------------------


def test_build_service_passes_credentials_and_disables_discovery_cache(keychain, monkeypatch):
    import googleapiclient.discovery

    keychain["google_token"] = token_json(expiry=FUTURE)
    seen = {}

    def fake_build(api, version, **kwargs):
        seen.update(api=api, version=version, **kwargs)
        return "service"

    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)
    assert google_auth.build_service("gmail", "v1") == "service"
    assert seen["api"] == "gmail" and seen["version"] == "v1"
    assert seen["cache_discovery"] is False
    assert seen["credentials"].token == "old-access-token"


def test_build_service_failure_is_execution_error(keychain, monkeypatch):
    keychain["google_token"] = token_json(expiry=FUTURE)

    def boom(api, version, credentials):
        raise RuntimeError("no discovery document")

    monkeypatch.setattr(google_auth, "_discovery_build", boom)
    with pytest.raises(ToolExecutionError, match="calendar client"):
        google_auth.build_service("calendar", "v3")


class _Req:
    def __init__(self, outcome):
        self.outcome = outcome

    def execute(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def http_error(status: int, message: str):
    import httplib2
    from googleapiclient.errors import HttpError

    return HttpError(
        httplib2.Response({"status": status, "reason": "x"}),
        json.dumps({"error": {"message": message, "errors": []}}).encode(),
        uri="https://www.googleapis.com/x",
    )


def test_execute_maps_errors():
    assert google_auth.execute(_Req({"ok": 1}), "x") == {"ok": 1}
    with pytest.raises(PermissionMissingError, match="401"):
        google_auth.execute(_Req(http_error(401, "Invalid Credentials")), "listing")
    with pytest.raises(ToolExecutionError, match="not found"):
        google_auth.execute(_Req(http_error(404, "Not Found")), "deleting")
    with pytest.raises(ToolExecutionError, match="enable the API"):
        google_auth.execute(_Req(http_error(403, "Gmail API has not been used in project 123 before or it is disabled")), "x")
    with pytest.raises(ToolExecutionError, match="network error"):
        google_auth.execute(_Req(ConnectionResetError("reset")), "x")
    with pytest.raises(ToolExecutionError, match="network error"):
        google_auth.execute(_Req(TimeoutError()), "x")


# --- health -------------------------------------------------------------------------------


def names_ok(checks):
    return {c.name: (c.ok, c.detail) for c in checks}


def test_health_checks_without_anything(keychain, config, paths):
    result = names_ok(google_auth.health_checks(config, paths))
    # nothing set up: Google is optional, so a hint rather than a failure
    assert result["Google OAuth client"][0] is True and "not set up" in result["Google OAuth client"][1]
    assert "jarvis google login" in result["Google OAuth client"][1]
    assert result["Google token"][0] is True and "not set up" in result["Google token"][1]


def test_health_checks_client_without_token_is_a_broken_login(keychain, config, paths):
    keychain["google_client_secret"] = CLIENT_JSON
    result = names_ok(google_auth.health_checks(config, paths))
    assert result["Google token"][0] is False and "missing" in result["Google token"][1]


def test_health_checks_valid_token(keychain, config, paths):
    keychain["google_client_secret"] = CLIENT_JSON
    keychain["google_token"] = token_json(expiry=FUTURE)
    result = names_ok(google_auth.health_checks(config, paths))
    assert result["Google OAuth client"] == (True, "found in Keychain")
    assert result["Google token"][0] is True and "valid for" in result["Google token"][1]
    assert result["Google scopes"] == (True, "calendar.events, gmail.readonly, gmail.compose, spreadsheets, drive.metadata.readonly")
    assert result["Google Sheets scopes"] == (True, "spreadsheets, drive.metadata.readonly")


def test_health_checks_old_token_passes_but_flags_sheets(keychain, config, paths):
    keychain["google_token"] = token_json(expiry=FUTURE, scopes=CAL_GMAIL)
    result = names_ok(google_auth.health_checks(config, paths))
    assert result["Google token"][0] is True
    assert result["Google scopes"] == (True, "calendar.events, gmail.readonly, gmail.compose")
    ok, detail = result["Google Sheets scopes"]
    assert ok is False and "jarvis google login` once more" in detail and "spreadsheets, drive.metadata.readonly" in detail


def test_health_checks_expired_and_missing_scope(keychain, config, paths):
    keychain["google_token"] = token_json(expiry=PAST)
    result = names_ok(google_auth.health_checks(config, paths))
    assert result["Google token"][0] is True and "expired" in result["Google token"][1]
    keychain["google_token"] = token_json(expiry=FUTURE, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    result = names_ok(google_auth.health_checks(config, paths))
    assert result["Google token"][0] is False and "calendar.events" in result["Google token"][1]
    assert result["Google scopes"][0] is False


def test_health_checks_never_raise(monkeypatch, config, paths):
    def boom(name):
        raise RuntimeError("keyring backend exploded")

    monkeypatch.setattr(secrets, "get_secret", boom)
    checks = google_auth.health_checks(config, paths)
    assert checks and all(not c.ok for c in checks)
    assert "RuntimeError" in checks[-1].detail
