"""The lock-and-continue flow: polling never counts as activity, the session locks after `idle_lock_min`,
every data request answers 423 while locked, the unlock call still needs cookie + CSRF + Host, a one-click
unlock keeps the cart, Touch ID mode (mocked) succeeds and fails, and a second `jarvis dashboard` signs in to
the running server through the control file."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from jarvis.dashboard import server as dashboard_server
from jarvis.dashboard import touchid
from jarvis.dashboard.state import Dashboard
from jarvis.dashboard.ui_text import UI
from tests.unit.test_dashboard import login, make_client, quiet_services  # noqa: F401 - fixture

LOCK_S = 180.0


@pytest.fixture
def board(config, paths, quiet_services) -> Dashboard:
    return Dashboard(config, paths, auto_full_check=False)


@pytest.fixture
def clocked(config, paths, board):
    clock = {"t": 5000.0}
    client, store = make_client(config, paths, board, lock_after_s=LOCK_S, clock=lambda: clock["t"])
    csrf = login(client, store)
    return client, store, csrf, clock


def test_polling_does_not_reset_the_lock_clock_but_user_requests_do(clocked):
    client, store, csrf, clock = clocked
    session = next(iter(store._sessions.values()))
    start = session.last_activity
    clock["t"] += 100
    assert client.get("/partials/strip", headers={"HX-Request": "true"}).status_code == 200  # a poll
    assert client.get("/analytics/data", headers={"Accept": "application/json"}).status_code == 200  # a data fetch
    assert session.last_activity == start
    clock["t"] += 50
    assert client.get("/partials/health", headers={"HX-Request": "true", "X-Activity": "1"}).status_code == 200  # a click
    assert session.last_activity == clock["t"]
    clock["t"] += 100
    assert client.post("/session/activity", headers={"X-CSRF-Token": csrf}).json()["locked"] is False  # the activity ping
    assert session.last_activity == clock["t"]
    clock["t"] += 100
    assert client.get("/logs").status_code == 200  # a navigation
    assert session.last_activity == clock["t"]


def test_lock_at_three_minutes_and_423_for_everything_but_unlock(clocked):
    client, store, csrf, clock = clocked
    clock["t"] += LOCK_S + 1
    page = client.get("/settings")
    assert page.status_code == 423 and UI.SESSION_LOCKED in page.text and UI.SESSION_LOCKED_AFTER.format(n=3) in page.text
    assert UI.BTN_CONTINUE in page.text and 'name="csrf-token" content="' in page.text
    assert "Models" not in page.text  # no data on the locked page
    for method, path in (
        ("get", "/partials/strip"),
        ("get", "/analytics/data"),
        ("post", "/session/activity"),
        ("post", "/cart/discard"),
        ("post", "/voice/resume"),
        ("get", "/logs/sessions/x/export.md"),
    ):
        response = getattr(client, method)(path, headers={"X-CSRF-Token": csrf, "HX-Request": "true"})
        assert response.status_code == 423, path
    assert client.get("/partials/strip", headers={"HX-Request": "true"}).json() == {"locked": True}
    assert client.get("/static/app.css").status_code == 200  # the lock page still needs its stylesheet
    clock["t"] += 1000
    assert client.get("/").status_code == 423  # activity while locked does not unlock
    assert client.post("/unlock", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/unlock", headers={"X-CSRF-Token": csrf, "host": "evil.example:8765"}).status_code == 400
    assert client.post("/unlock", headers={"X-CSRF-Token": csrf}).json() == {"ok": True}
    assert client.get("/partials/strip", headers={"HX-Request": "true"}).status_code == 200
    assert client.post("/session/activity", headers={"X-CSRF-Token": csrf}).json()["seconds_left"] == LOCK_S


def test_one_click_unlock_keeps_the_cart(clocked, config):
    client, store, csrf, clock = clocked
    assert (
        client.post("/cart/settings", data={"path": "persona.humor_level", "value": "3"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    )
    assert client.app.state.cart.count() == 1
    clock["t"] += LOCK_S + 1
    assert client.get("/cart").status_code == 423
    assert client.post("/unlock", headers={"X-CSRF-Token": csrf}).json()["ok"] is True
    drawer = client.get("/cart")
    assert drawer.status_code == 200 and "Changes (1)" in drawer.text and client.app.state.cart.count() == 1
    client.app.state.cart.clear()


def test_touch_id_mode_success_and_failure(config, paths, quiet_services, monkeypatch):
    config.dashboard.unlock = "touch_id"
    clock = {"t": 100.0}
    board = Dashboard(config, paths, auto_full_check=False)
    client, store = make_client(config, paths, board, lock_after_s=LOCK_S, clock=lambda: clock["t"])
    csrf = login(client, store)
    assert UI.BTN_UNLOCK_TOUCH_ID in client.get("/").text
    clock["t"] += LOCK_S + 1
    assert client.get("/").status_code == 423
    calls = []
    monkeypatch.setattr(touchid, "authenticate", lambda *a, **k: calls.append(1) or (False, "Canceled by user"))
    response = client.post("/unlock", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json() == {"ok": False, "error": UI.UNLOCK_FAILED}
    assert client.get("/").status_code == 423 and calls == [1]
    monkeypatch.setattr(touchid, "authenticate", lambda *a, **k: (True, ""))
    assert client.post("/unlock", headers={"X-CSRF-Token": csrf}).json() == {"ok": True}
    assert client.get("/").status_code == 200


def test_touch_id_module_reports_availability_without_prompting():
    ok, why = touchid.available()
    assert isinstance(ok, bool) and isinstance(why, str)


def test_the_issue_endpoint_needs_the_control_token_and_never_a_browser_session(clocked):
    client, store, csrf, clock = clocked
    assert client.post("/auth/issue", headers={"X-CSRF-Token": csrf}).status_code == 403  # a session is not enough
    assert client.post("/auth/issue", headers={"X-Dashboard-Control": "nope"}).status_code == 403
    fresh, _ = make_client(client.app.state.dashboard.config, client.app.state.dashboard.paths, client.app.state.dashboard)
    response = (
        fresh.post("/auth/issue", headers={"X-Dashboard-Control": store.control_token})
        if False
        else client.post("/auth/issue", headers={"X-Dashboard-Control": store.control_token})
    )
    assert response.status_code == 200
    link = response.json()["url"]
    token = re.search(r"token=([A-Za-z0-9_-]+)$", link).group(1)
    other, _ = make_client(client.app.state.dashboard.config, client.app.state.dashboard.paths, client.app.state.dashboard)
    assert other.get(f"/auth?token={token}", follow_redirects=False).status_code == 401  # a different app instance: unknown token
    assert client.get(f"/auth?token={token}", follow_redirects=False).status_code == 303  # the issuing server accepts it once
    assert client.get(f"/auth?token={token}", follow_redirects=False).status_code == 401


def test_rerunning_the_command_signs_in_to_the_running_server(config, paths, quiet_services, monkeypatch):
    lines: list[str] = []
    served: dict = {}
    monkeypatch.setattr(dashboard_server, "BROWSER_DELAY_S", 0.0)
    monkeypatch.setattr(dashboard_server.webbrowser, "open", lambda url: pytest.fail("no browser with --no-open"))

    def serve(app, sock, port):  # "runs" until the test finishes: the control file is in place meanwhile
        served["port"] = port
        info = dashboard_server.running_instance(paths)
        assert info is not None and info["port"] == port and info["pid"] == __import__("os").getpid()
        assert (dashboard_server.control_file(paths).stat().st_mode & 0o777) == 0o600
        # the second run: it must not bind, only ask the running server for a link
        posted = []

        def fake_post(url, headers, timeout):
            posted.append((url, headers))
            assert headers["X-Dashboard-Control"] == info["control_token"]
            return SimpleNamespace(status_code=200, json=lambda: {"url": f"http://127.0.0.1:{port}/auth?token=fresh123"})

        second: list[str] = []
        assert (
            dashboard_server.run_dashboard(
                config, paths, open_browser=False, out=second.append, serve=lambda *a: pytest.fail("bound twice"), post=fake_post
            )
            == 0
        )
        assert posted[0][0] == f"http://127.0.0.1:{port}/auth/issue" and any("token=fresh123" in line for line in second)
        assert any("already running" in line for line in second)

    assert dashboard_server.run_dashboard(config, paths, open_browser=False, port=0, out=lines.append, serve=serve) == 0
    assert "port" in served and not dashboard_server.control_file(paths).exists()  # removed at shutdown
    # a stale control file (dead pid) is ignored and removed
    dashboard_server.control_file(paths).write_text(json.dumps({"port": 1, "pid": 999999, "control_token": "x"}), encoding="utf-8")
    assert dashboard_server.running_instance(paths) is None and not dashboard_server.control_file(paths).exists()
