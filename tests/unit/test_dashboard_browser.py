"""The dashboard in a headless browser: a smoke test. The real server runs on a free port inside this process
(so the test fixtures still keep every Mac integration offline), Playwright drives the installed Google Chrome
through its `chrome` channel, opens every page and the cart drawer, and fails on any console error, blocked
request or CSP violation. Skips when Playwright or Chrome is not available."""

from __future__ import annotations

import re
import threading
import time

import pytest

import jarvis.services as services
from jarvis.config import Paths
from jarvis.dashboard import server as dashboard_server
from tests.unit.test_dashboard import fake_reports

playwright_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")

PAGES = ("/", "/analytics", "/logs", "/audit", "/settings", "/memory", "/persona", "/persona?tab=profile", "/history")


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # no Chrome on this Mac
            pytest.skip(f"Google Chrome is not available: {str(exc).splitlines()[0]}")
        yield instance
        instance.close()


@pytest.fixture
def live_server(config, paths: Paths, monkeypatch):
    """The dashboard on 127.0.0.1:<free port>, served by uvicorn in a thread; yields the one-time link."""
    import uvicorn

    monkeypatch.setattr(services, "service_reports", lambda: fake_reports())
    monkeypatch.setattr(services, "process_started_at", lambda pid: None)
    persona = paths.home / "persona-copy.md"
    persona.write_text("# Persona\n\nBe kind.\n", encoding="utf-8")
    monkeypatch.setattr(Paths, "persona_file", property(lambda self: persona))
    paths.memory_file.write_text("- 2026-10-01: Likes tea\n", encoding="utf-8")
    # a little traffic from today, so at least the cost, model and channel charts have something to draw
    from datetime import datetime

    from jarvis.observability.db import Database
    from tests.unit.test_analytics import _insert_turn

    db = Database(paths.db_file)
    now = datetime.now().astimezone()
    comp = {
        "persona": 100,
        "rules": 300,
        "profile": 50,
        "memory": 150,
        "tool_definitions": 400,
        "conversation_history": 200,
        "tool_results": 100,
        "user_input": 20,
        "thinking": 30,
        "prompt_tokens": 1320,
        "exact": True,
    }
    _insert_turn(
        db,
        turn_id="b1",
        session="bs1",
        channel="cli",
        at=now,
        user_input="hello",
        total_ms=900,
        api=[("ok", 320, 1000, 0, 100, 0.01, None, False, comp, False)],
        stages={"api_call": 800, "ttft": 300},
    )
    db.close()
    paths.profile_file.write_text("# Profile\n\n- Name: Test\n", encoding="utf-8")
    holder: dict = {}
    lines: list[str] = []

    def serve(app, sock, port):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="on"))
        holder["server"] = server
        server.run(sockets=[sock])

    thread = threading.Thread(
        target=dashboard_server.run_dashboard,
        kwargs=dict(config=config, paths=paths, open_browser=False, port=0, out=lines.append, serve=serve),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 15
    link = None
    while time.monotonic() < deadline and link is None:
        for line in lines:
            match = re.search(r"http://127\.0\.0\.1:\d+/auth\?token=[A-Za-z0-9_-]+", line)
            if match:
                link = match.group(0)
        time.sleep(0.05)
    assert link, lines
    while "server" not in holder or not getattr(holder["server"], "started", False):
        time.sleep(0.05)
        assert time.monotonic() < deadline + 10
    yield link
    holder["server"].should_exit = True
    thread.join(10)


def test_every_page_and_the_cart_drawer_work_in_a_real_browser(browser, live_server):
    problems: list[str] = []
    page = browser.new_page()
    page.on("console", lambda msg: problems.append(f"console {msg.type}: {msg.text}") if msg.type in ("error",) else None)
    page.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
    page.on("requestfailed", lambda req: problems.append(f"request failed: {req.url}"))
    page.on("response", lambda res: problems.append(f"{res.status} {res.url}") if res.status >= 400 else None)

    page.goto(live_server)
    page.wait_for_load_state("networkidle")
    assert page.url.endswith("/") and "token=" not in page.url  # the token became a cookie and left the address bar
    assert page.title() == "Overview · Jarvis Dashboard"
    assert page.locator(".strip").is_visible() and page.locator("#health").is_visible()
    base = page.url.rstrip("/")

    for path in PAGES:
        page.goto(base + path)
        page.wait_for_load_state("networkidle")
        assert page.locator("h1.page-title").is_visible(), path
        assert page.locator("#save-btn").count() == 1, path
    # Analytics: Chart.js is loaded, every chart either drew or shows its empty note, and nothing overlaps.
    page.goto(base + "/analytics")
    page.wait_for_load_state("networkidle")
    page.wait_for_function("typeof Chart !== 'undefined' && document.querySelectorAll('.chart-loading').length === 0")
    canvases = page.locator("canvas[data-chart]")
    assert canvases.count() >= 10
    states = page.evaluate("""() => Array.from(document.querySelectorAll('canvas[data-chart]')).map(c => ({
        name: c.dataset.chart, drawn: !!Chart.getChart(c), hidden: c.hidden,
        emptyShown: !c.parentElement.querySelector('.chart-empty').hidden }))""")
    for state in states:
        assert state["drawn"] != state["emptyShown"], state  # exactly one of: a chart, or the "No data" note
    drawn = {s["name"] for s in states if s["drawn"]}
    assert {"daily_cost", "cost_by_provider", "model_usage", "composition", "channels"} <= drawn, states
    assert page.locator(".info[data-tip]").count() >= 10

    # Settings: move the humor slider -> Save (1) -> the drawer lists the change -> remove it with x.
    page.goto(base + "/settings")
    page.wait_for_load_state("networkidle")
    slider = page.locator("#input-persona-humor_level")
    slider.fill("3") if False else slider.evaluate("el => { el.value = '3'; el.dispatchEvent(new Event('change', { bubbles: true })); }")
    page.wait_for_selector("#field-persona-humor_level .badge:has-text('Changed')")
    save = page.locator("#save-btn")
    assert save.is_enabled() and save.inner_text().strip() == "Save (1)"
    save.click()
    page.wait_for_selector(".drawer h2:has-text('Changes (1)')")
    assert page.locator(".drawer").is_visible() and page.locator(".cart-item").count() == 1
    assert "Restarts: voice service, telegram service" in page.locator(".cart-item").inner_text()
    page.locator(".cart-item .icon-btn").click()
    page.wait_for_selector(".drawer:has-text('No changes staged.')")
    page.wait_for_function("document.getElementById('save-btn').disabled === true")
    # The section list scrolls on its own: from the top of a short window its last link can be reached without
    # scrolling the page.
    page.set_viewport_size({"width": 1280, "height": 560})
    page.evaluate("window.scrollTo(0, 0)")
    reachable = page.evaluate("""() => { const a = document.querySelector('.section-nav a[href="#section-secrets"]'); const nav = a.parentElement;
        nav.scrollTop = nav.scrollHeight; const r = a.getBoundingClientRect();
        return { overflow: nav.scrollHeight > nav.clientHeight, page: window.scrollY, top: r.top, bottom: r.bottom, h: window.innerHeight }; }""")
    assert reachable["overflow"] and reachable["page"] == 0 and 0 <= reachable["top"] and reachable["bottom"] <= reachable["h"], reachable
    page.set_viewport_size({"width": 1280, "height": 720})

    # The lock: the clock is shortened in the page; the banner comes one minute before, the lock at the end, Continue keeps
    # the page, the scroll position and the text typed into the editor.
    page.goto(base + "/persona")
    page.wait_for_load_state("networkidle")
    page.locator("textarea.editor-text").fill("# Persona\n\nTyped while testing.\n")
    page.evaluate("window.scrollTo(0, 400)")
    scrolled = page.evaluate("window.scrollY")  # the page may be shorter than 400 px; whatever it is must survive the lock
    assert scrolled > 0
    page.evaluate("window.jarvisDashboard.lock.seconds = 63; window.jarvisDashboard.lock.lastActivity = Date.now() - 1000")
    page.wait_for_selector("#expire-banner:not([hidden])", timeout=8000)
    assert "Your session will expire in 1 minute." in page.locator("#expire-banner").inner_text()
    page.locator("[data-stay]").click()
    assert page.locator("#expire-banner").is_hidden()
    page.evaluate("window.jarvisDashboard.lock.seconds = 2; window.jarvisDashboard.lock.lastActivity = Date.now() - 1000")
    page.wait_for_selector("#lock-panel:not([hidden])", timeout=8000)
    assert page.locator("body").evaluate("b => b.classList.contains('locked')")
    assert page.locator("#lock-panel").inner_text().startswith("Session locked")
    page.evaluate("window.jarvisDashboard.lock.seconds = 180")
    page.locator("[data-unlock]").click()
    page.wait_for_selector("#lock-panel", state="hidden", timeout=8000)
    assert not page.locator("body").evaluate("b => b.classList.contains('locked')")
    assert page.url.endswith("/persona") and page.evaluate("window.scrollY") == scrolled
    assert page.locator("textarea.editor-text").input_value().startswith("# Persona\n\nTyped while testing.")
    # a full page load while the server says locked shows only the panel; Continue reloads the real page
    page.evaluate(
        "fetch('/session/activity', {method: 'POST', headers: {'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content}})"
    )
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    assert page.locator(".strip").is_visible()

    # Dark mode follows the system.
    light = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.emulate_media(color_scheme="dark")
    dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert light != dark
    # The sidebar toggle pins a theme over the system one, keeps it on the next page, and System hands it back.
    page.locator('[data-theme-choice="light"]').click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == light
    page.goto(base + "/settings")
    page.wait_for_load_state("networkidle")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert page.locator('[data-theme-choice="light"]').get_attribute("aria-pressed") == "true"
    page.locator('[data-theme-choice="system"]').click()
    assert not page.evaluate("document.documentElement.hasAttribute('data-theme')")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == dark

    assert problems == [], problems  # no console error, failed request or 4xx/5xx while using the dashboard

    # The expired page after the session is gone (its 401 is the point, so it is not counted above).
    page.context.clear_cookies()
    page.goto(base + "/settings")
    assert "Your session expired" in page.content()
