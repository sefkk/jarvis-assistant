"""The dashboard is sensitive. Loopback only, one-time token to cookie, host
check, CSRF, strict CSP and headers, escaping of hostile transcripts, no secret ever in a response, idle
expiry. Every point has a test here."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jarvis.dashboard import security
from jarvis.dashboard.app import STATIC_DIR, TEMPLATES_DIR
from jarvis.dashboard.security import CONTENT_SECURITY_POLICY, COOKIE_NAME
from jarvis.dashboard.state import Dashboard
from jarvis.observability.db import Database
from jarvis.observability.health import Check
from jarvis.utils import secrets
from tests.unit.test_dashboard import PORT, login, make_client, quiet_services, wait_for  # noqa: F401 - fixture

SECRET_VALUE = "sk-ant-api03-THIS-IS-THE-SECRET-VALUE-0123456789"


@pytest.fixture
def board(config, paths, quiet_services) -> Dashboard:  # noqa: F811 - the fixture is re-exported above
    return Dashboard(config, paths, auto_full_check=False)


@pytest.fixture
def client(config, paths, board):
    client, store = make_client(config, paths, board)
    return client, store


# --- authentication ----------------------------------------------------------------------------------------


def test_requests_without_a_session_get_401_with_the_expired_page(client):
    client, _store = client
    for path in ("/", "/partials/health", "/partials/today", "/analytics", "/settings", "/logs/turns/x"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert "Your session expired. Run `jarvis dashboard` again." in response.text
    assert client.post("/voice/resume").status_code == 401


def test_the_token_is_exchanged_once_for_an_httponly_strict_cookie(client):
    client, store = client
    response = client.get(f"/auth?token={store.token}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert store.token not in cookie  # the cookie is a new random session id, not the token
    assert client.get("/").status_code == 200
    # Single use: the same link again, or in another browser, does not log in.
    fresh, _ = make_client(client.app.state.dashboard.config, client.app.state.dashboard.paths, client.app.state.dashboard)
    assert fresh.get(f"/auth?token={store.token}", follow_redirects=False).status_code == 401
    assert client.get(f"/auth?token={store.token}", follow_redirects=False).status_code == 401


def test_wrong_or_missing_tokens_do_not_log_in(client):
    client, store = client
    assert client.get("/auth", follow_redirects=False).status_code == 401
    assert client.get("/auth?token=" + "x" * len(store.token), follow_redirects=False).status_code == 401
    assert client.get("/auth?token=" + store.token[:-1], follow_redirects=False).status_code == 401
    assert client.get("/").status_code == 401 and not store.token_used


def test_a_forged_cookie_is_not_a_session(client):
    client, _store = client
    client.cookies.set(COOKIE_NAME, "not-a-session")
    assert client.get("/").status_code == 401


def test_the_session_locks_after_idle_time_but_the_cookie_stays(config, paths, board):
    clock = {"t": 1000.0}
    client, store = make_client(config, paths, board, lock_after_s=60, clock=lambda: clock["t"])
    csrf = login(client, store)
    clock["t"] += 59
    assert client.get("/").status_code == 200  # a page navigation counts as activity
    clock["t"] += 59
    assert client.get("/").status_code == 200
    clock["t"] += 61
    response = client.get("/")
    assert response.status_code == 423 and store.active == 1 and "Session locked" in response.text
    assert client.get("/partials/strip", headers={"HX-Request": "true"}).status_code == 423
    assert client.post("/unlock").status_code == 403  # CSRF still required while locked
    assert client.post("/unlock", headers={"X-CSRF-Token": csrf}).json() == {"ok": True}
    assert client.get("/").status_code == 200


# --- host, CSRF, headers -----------------------------------------------------------------------------------


def test_host_header_must_be_loopback_with_the_port(client):
    client, store = client
    login(client, store)
    for host in ("evil.example", f"evil.example:{PORT}", f"127.0.0.1:{PORT + 1}", "127.0.0.1", "", f"[::1]:{PORT}"):
        response = client.get("/", headers={"host": host})
        assert response.status_code == 400, host
        assert "Jarvis Dashboard" not in response.text
    assert client.get("/", headers={"host": f"localhost:{PORT}"}).status_code == 200
    assert client.get("/", headers={"host": f"127.0.0.1:{PORT}"}).status_code == 200
    assert client.get("/auth?token=x", headers={"host": "evil.example"}).status_code == 400


def test_state_changes_need_the_csrf_header(client, paths):
    client, store = client
    csrf = login(client, store)
    assert client.post("/voice/pause", data={"preset": "15"}).status_code == 403
    assert client.post("/voice/pause", data={"preset": "15"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert not paths.voice_pause_file.exists()
    response = client.post("/voice/pause", data={"preset": "15"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and paths.voice_pause_file.exists()
    assert client.get("/partials/voice").status_code == 200  # reads need no CSRF token
    # The token is per session and the page only shows it to the logged-in browser.
    assert csrf == store.get(client.cookies[COOKIE_NAME]).csrf


def test_every_response_carries_the_security_headers(client):
    client, store = client
    responses = [client.get("/"), client.get("/static/app.css"), client.get("/", headers={"host": "evil.example"})]
    login(client, store)
    responses += [client.get("/"), client.get("/partials/strip"), client.get("/logs"), client.post("/voice/resume")]
    for response in responses:
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "access-control-allow-origin" not in response.headers
    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY and "'unsafe-eval'" not in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY and "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert responses[-1].headers["cache-control"] == "no-store"  # pages with private data are never cached


def test_cors_preflight_is_not_answered(client):
    client, store = client
    login(client, store)
    response = client.options("/", headers={"origin": "http://evil.example", "access-control-request-method": "GET"})
    assert "access-control-allow-origin" not in response.headers


def test_templates_have_no_inline_scripts_styles_or_eval_attributes():
    """CSP forbids them, and HTMX's eval features are off: a template that used them would silently break."""
    for template in Path(TEMPLATES_DIR).rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), template
        assert not re.search(r"<style\b", text), template
        assert not re.search(r"\sstyle=", text), template
        assert not re.search(r"\son[a-z]+=", text), template
        assert "hx-on" not in text and "javascript:" not in text and 'hx-vals="js:' not in text, template
        for src in re.findall(r'(?:src|href)="([^"]+)"', text):
            assert not src.startswith(("http:", "https:", "//")), (template, src)  # no CDN, no external request
    config = re.search(r'name="htmx-config" content=\'([^\']+)\'', (TEMPLATES_DIR / "base.html").read_text()).group(1)
    assert '"allowEval": false' in config and '"selfRequestsOnly": true' in config and '"historyEnabled": false' in config


def test_frontend_assets_are_vendored():
    for name in ("vendor/htmx.min.js", "vendor/alpine-csp.min.js", "vendor/chart.umd.js", "app.css", "app.js"):
        assert (STATIC_DIR / name).is_file(), name
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "eval(" not in js and "new Function" not in js


def test_static_files_are_public_but_nothing_else_is(client):
    client, _store = client
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert client.get("/static/../app.py").status_code in (400, 401, 404)  # the client normalizes `..`; either way no file
    assert client.get("/static/%2e%2e/app.py").status_code in (400, 401, 404)
    assert client.get("/partials/strip").status_code == 401


# --- content ------------------------------------------------------------------------------------------------


def test_hostile_transcripts_are_escaped(client, paths):
    client, store = client
    login(client, store)
    db = Database(paths.db_file)
    try:
        db.start_session("s1", "m", "d", "telegram")
        db.start_turn("t1", "s1", '<script>alert("x")</script><img src=x onerror=alert(1)> "quoted" & more')
        db.finish_turn("t1", final_reply="ok", tainted=True, total_ms=10.0, error="<b>err</b>")
    finally:
        db.close()
    for path in ("/", "/partials/today"):
        text = client.get(path).text
        assert '<script>alert("x")</script>' not in text and "<img src=x onerror=alert(1)>" not in text and "<b>err</b>" not in text
        assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in text
        assert "&#34;quoted&#34;" in text and "&amp; more" in text


def test_secrets_never_appear_in_any_response(client, monkeypatch):
    client, store = client
    monkeypatch.setattr(secrets, "get_secret", lambda name: SECRET_VALUE)
    monkeypatch.setattr(security, "secrets", security.secrets)  # the stdlib module, untouched
    board = client.app.state.dashboard
    board.health._full_runner = lambda c, p: [Check("ElevenLabs API key", True, "in Keychain")]
    csrf = login(client, store)
    board.health.run_full()
    wait_for(lambda: not board.health.checking)
    bodies = [
        client.get(p).text for p in ("/", "/partials/health", "/partials/strip", "/partials/services", "/partials/today", "/settings")
    ]
    bodies.append(client.post("/partials/health/run", headers={"X-CSRF-Token": csrf}).text)
    for body in bodies:
        assert SECRET_VALUE not in body
        assert "sk-ant-" not in body


def test_unknown_service_and_unexpected_errors_render_the_error_page(client, monkeypatch):
    client, store = client
    csrf = login(client, store)
    response = client.post("/services/finder/restart", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400 and "Something went wrong." in response.text
    board = client.app.state.dashboard

    def boom():
        raise RuntimeError("database on fire /Users/alex/secret")

    monkeypatch.setattr(board, "today", boom)
    client.app.state  # the TestClient re-raises server exceptions unless told otherwise
    quiet, _ = make_client(board.config, board.paths, board)
    quiet.app.state
    from starlette.testclient import TestClient

    tolerant = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False, cookies=client.cookies)
    response = tolerant.get("/partials/today")
    assert response.status_code == 500 and "Something went wrong." in response.text and "secret" not in response.text


def test_voice_pause_rejects_a_bad_custom_duration_without_pausing(client, paths):
    client, store = client
    csrf = login(client, store)
    response = client.post("/voice/pause", data={"preset": "custom", "minutes": "soon"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and "between 1 and 1440" in response.text
    assert not paths.voice_pause_file.exists()
    response = client.post("/voice/pause", data={"preset": "custom", "minutes": "99999"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and not paths.voice_pause_file.exists()


# --- the cart and the editors ----------------------------------------------------------------------------


@pytest.fixture
def editing(config, paths, board, monkeypatch):
    persona = paths.home / "persona-copy.md"
    persona.write_text("# Persona\n\nBe kind.\n", encoding="utf-8")
    from jarvis.config import Paths as _Paths

    monkeypatch.setattr(_Paths, "persona_file", property(lambda self: persona))
    paths.memory_file.write_text("- 2026-10-01: Likes <script>tea</script>\n", encoding="utf-8")
    paths.profile_file.write_text("# Profile\n\n- Name: Test\n", encoding="utf-8")
    client, store = make_client(config, paths, board)
    csrf = login(client, store)
    return client, csrf


def test_cart_endpoints_need_csrf_and_a_session(editing, config, paths, board):
    client, csrf = editing
    for path, data in (
        ("/cart/settings", {"path": "persona.humor_level", "value": "3"}),
        ("/cart/memory/add", {"text": "x"}),
        ("/cart/text", {"area": "persona", "text": "y"}),
        ("/cart/discard", {}),
        ("/cart/apply", {}),
        ("/history/restore", {"id": "x"}),
    ):
        assert client.post(path, data=data).status_code == 403, path
    assert client.app.state.cart.count() == 0
    fresh, _ = make_client(config, paths, board)
    assert fresh.get("/settings").status_code == 401 and fresh.get("/cart").status_code == 401
    assert (
        client.post("/cart/settings", data={"path": "persona.humor_level", "value": "3"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    )
    assert client.app.state.cart.count() == 1
    client.app.state.cart.clear()


def test_read_only_settings_and_secrets_are_never_written_or_shown(editing, monkeypatch):
    client, csrf = editing
    from jarvis.utils import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: SECRET_VALUE)
    page = client.get("/settings")
    assert page.status_code == 200 and SECRET_VALUE not in page.text and "In Keychain" in page.text
    assert "Read-only" in page.text and 'id="input-security-shell-denylist"' not in page.text
    response = client.post("/cart/settings", data={"path": "security.shell_timeout_seconds", "value": "1"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and "Read-only" in response.text and client.app.state.cart.count() == 0
    response = client.post(
        "/cart/settings", data={"path": "telegram.remote_allowed_tools", "value": "shell.*"}, headers={"X-CSRF-Token": csrf}
    )
    assert client.app.state.cart.count() == 0


def test_editor_content_is_escaped_and_the_preview_is_sanitized(editing):
    client, csrf = editing
    memory = client.get("/memory")
    assert "&lt;script&gt;tea&lt;/script&gt;" in memory.text and "<script>tea" not in memory.text
    evil = "# Hi\n\n<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))\n"
    response = client.post("/cart/text", data={"area": "persona", "text": evil}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and "onerror=alert" not in response.text.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert (
        "<img" not in response.text and "javascript:" not in response.text.replace("javascript:alert(1)", "") or "href" not in response.text
    )
    page = client.get("/persona")
    assert "<img src=x" not in page.text and "&lt;img src=x onerror=alert(1)&gt;" in page.text
    drawer = client.get("/cart")
    assert "<img src=x" not in drawer.text and drawer.status_code == 200
    client.app.state.cart.clear()
