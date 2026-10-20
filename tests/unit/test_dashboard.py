"""The dashboard's state (health cache, services, voice, today), the immediate
actions, the CLI entry and the server start. Nothing here touches launchctl, the network or the browser."""

from __future__ import annotations

import re
import socket
import threading
import time
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

import jarvis.services as services
from jarvis.config import Config, Paths
from jarvis.dashboard import server as dashboard_server
from jarvis.dashboard import state as dashboard_state
from jarvis.dashboard.app import create_app
from jarvis.dashboard.security import SessionStore
from jarvis.dashboard.state import Dashboard, HealthCache, format_duration, format_money, format_ms, parse_pause_minutes
from jarvis.dashboard.ui_text import UI, all_strings
from jarvis.exceptions import JarvisError
from jarvis.main import build_parser
from jarvis.observability.db import Database
from jarvis.observability.health import Check
from jarvis.voice import pause as voice_pause

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


def fake_reports(*, scheduler="running", telegram="stopped", voice="running"):
    def report(name, state):
        if state == "running":
            return services.ServiceReport(name, True, True, None, False, f"running (pid 4{len(name)}); runs /usr/local/bin/jarvis")
        if state == "stopped":
            return services.ServiceReport(name, True, False, None, False, "plist present but the agent is not loaded")
        if state == "missing":
            return services.ServiceReport(name, False, False, None, False, "not installed")
        return services.ServiceReport(name, False, False, None, False, "status unknown: RuntimeError: launchctl")

    return [report("scheduler", scheduler), report("telegram", telegram), report("voice", voice)]


@pytest.fixture
def quiet_services(monkeypatch):
    """No launchctl, no ps: the services module answers from these fakes."""
    monkeypatch.setattr(services, "service_reports", lambda: fake_reports())
    monkeypatch.setattr(services, "process_started_at", lambda pid: datetime.now().astimezone() - timedelta(hours=2, minutes=5))


@pytest.fixture
def board(config: Config, paths: Paths, quiet_services) -> Dashboard:
    return Dashboard(config, paths, auto_full_check=False)


def make_client(
    config: Config, paths: Paths, board: Dashboard, *, lock_after_s: float = 3600, clock=None
) -> tuple[TestClient, SessionStore]:
    store = SessionStore(lock_after_s=lock_after_s, **({"clock": clock} if clock else {}))
    app = create_app(config, paths, store=store, port=PORT, dashboard=board)
    return TestClient(app, base_url=BASE_URL), store


def login(client: TestClient, store: SessionStore) -> str:
    """Exchange the one-time token for the cookie and return the page's CSRF token."""
    response = client.get(f"/auth?token={store.token}", follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)


def wait_for(predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# --- CLI and server -----------------------------------------------------------------------------------------


def test_cli_has_the_dashboard_command():
    args = build_parser().parse_args(["dashboard", "--no-open", "--port", "9001"])
    assert args.command == "dashboard" and args.no_open is True and args.port == 9001
    args = build_parser().parse_args(["dashboard"])
    assert args.no_open is False and args.port is None


def test_server_binds_loopback_and_falls_back_to_a_free_port():
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]
    try:
        sock = dashboard_server.bind_socket(busy_port)
        try:
            host, port = sock.getsockname()
            assert host == "127.0.0.1" and port != busy_port and port > 0
        finally:
            sock.close()
    finally:
        busy.close()
    assert dashboard_server.HOST == "127.0.0.1"


def test_run_dashboard_prints_the_one_time_link_with_no_open(config, paths, monkeypatch, quiet_services):
    served: dict = {}
    lines: list[str] = []

    def fake_serve(app, sock, port):
        served["port"] = port
        served["app"] = app
        assert sock.getsockname()[0] == "127.0.0.1"

    monkeypatch.setattr(dashboard_server.webbrowser, "open", lambda url: pytest.fail("the browser must not open with --no-open"))
    assert dashboard_server.run_dashboard(config, paths, open_browser=False, port=0, out=lines.append, serve=fake_serve) == 0
    text = "\n".join(lines)
    match = re.search(r"http://127\.0\.0\.1:(\d+)/auth\?token=([A-Za-z0-9_-]{20,})", text)
    assert match and int(match.group(1)) == served["port"]
    assert "0.0.0.0" not in text


def test_run_dashboard_opens_the_browser_on_the_token_link(config, paths, monkeypatch, quiet_services):
    opened: list[str] = []
    monkeypatch.setattr(dashboard_server, "BROWSER_DELAY_S", 0.0)
    monkeypatch.setattr(dashboard_server.webbrowser, "open", lambda url: opened.append(url) or True)
    dashboard_server.run_dashboard(config, paths, open_browser=True, port=0, out=lambda text: None, serve=lambda app, sock, port: None)
    wait_for(lambda: opened)
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/auth\?token=[A-Za-z0-9_-]+", opened[0])


# --- services, voice, today ----------------------------------------------------------------------------------


def test_services_view_maps_launchd_state_to_the_ui_states(board, monkeypatch):
    monkeypatch.setattr(services, "service_reports", lambda: fake_reports(scheduler="running", telegram="missing", voice="error"))
    views = {v.name: v for v in board.services_view()}
    assert views["scheduler"].state == UI.STATE_RUNNING and views["scheduler"].label == UI.SERVICE_SCHEDULER
    assert views["scheduler"].uptime_s is not None and 2 * 3600 < views["scheduler"].uptime_s < 2.2 * 3600
    assert views["telegram"].state == UI.STATE_STOPPED and views["telegram"].detail == UI.NOT_INSTALLED and not views["telegram"].installed
    assert views["voice"].state == UI.STATE_ERROR
    board.restarting.add("scheduler")
    board._reports_at = None
    assert {v.name: v.state for v in board.services_view()}["scheduler"] == UI.STATE_RESTARTING


def test_services_view_shows_the_config_version_an_agent_runs(board, paths, monkeypatch):
    # No record yet: unknown. A record with the running pid: current, then outdated when config.toml changes.
    assert {v.name: v.config_note for v in board.services_view()}["scheduler"] == UI.CONFIG_UNKNOWN
    db = Database(paths.db_file)
    try:
        monkeypatch.setattr(services.os, "getpid", lambda: 49)  # fake_reports: "running (pid 49)" for scheduler
        record = services.record_start(db, "scheduler", paths)
    finally:
        db.close()
    view = {v.name: v for v in board.services_view()}["scheduler"]
    assert view.config_version == record["config_version"] and view.config_current is True and view.config_note == UI.CONFIG_CURRENT
    paths.config_file.write_text(paths.config_file.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    view = {v.name: v for v in board.services_view()}["scheduler"]
    assert view.config_current is False and view.config_note == UI.CONFIG_OUTDATED
    assert services.config_version(paths) != record["config_version"]


def test_voice_view_states(board, paths, monkeypatch):
    assert board.voice_view().state == UI.VOICE_LISTENING
    voice_pause.pause(paths.voice_pause_file, by="dashboard")
    assert board.voice_view().state == UI.VOICE_PAUSED
    voice_pause.pause(paths.voice_pause_file, by="dashboard", minutes=30)
    view = board.voice_view()
    assert view.paused and view.state.startswith("Paused until ") and re.fullmatch(r"Paused until \d\d:\d\d", view.state)
    monkeypatch.setattr(services, "service_reports", lambda: fake_reports(voice="stopped"))
    board._reports_at = None
    assert board.voice_view().state == UI.VOICE_OFF and board.voice_view().off


def test_voice_view_is_off_when_use_voice_is_false(config, paths, quiet_services):
    config.general.use_voice = False
    assert Dashboard(config, paths, auto_full_check=False).voice_view().state == UI.VOICE_OFF


def test_pause_and_resume_actions(board, paths):
    assert board.pause_voice(15).startswith("Paused for 15 min.")
    state = voice_pause.load_state(paths.voice_pause_file)
    assert state.paused and state.by == "dashboard" and state.remaining_minutes() in (14, 15)
    assert board.resume_voice() == UI.RESUMED
    assert not voice_pause.load_state(paths.voice_pause_file).paused
    assert board.resume_voice() == UI.WAS_NOT_PAUSED
    assert board.pause_voice(None) == UI.PAUSED_UNTIL_RESUMED


def test_parse_pause_minutes():
    assert parse_pause_minutes("15", None) == 15
    assert parse_pause_minutes("60", None) == 60
    assert parse_pause_minutes("custom", "45") == 45
    assert parse_pause_minutes(None, None) is None
    with pytest.raises(JarvisError):
        parse_pause_minutes("custom", "soon")
    with pytest.raises(JarvisError):
        parse_pause_minutes("custom", "")


def test_today_glance_reads_the_database_read_only(board, paths):
    assert board.today().turns == 0 and board.today().recent == []
    db = Database(paths.db_file)
    try:
        db.start_session("s1", "claude-haiku-5-5", "claude-sonnet-5-5", "voice")
        db.start_turn("t1", "s1", "what is on my agenda")
        db.finish_turn("t1", final_reply="nothing", tainted=False, total_ms=1200.0, error=None)
        db.add_api_call(
            "t1",
            model="claude-haiku-5-5",
            attempt=1,
            status="ok",
            duration_ms=900,
            ttft_ms=300,
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.012,
        )
        db.start_turn("t2", "s1", "second question")
        db.finish_turn("t2", final_reply=None, tainted=True, total_ms=800.0, error="boom")
    finally:
        db.close()
    glance = board.today()
    assert glance.turns == 2 and glance.errors == 1 and glance.cost_usd == pytest.approx(0.012)
    assert glance.avg_latency_ms == pytest.approx(1000.0)
    assert [t.id for t in glance.recent] == ["t2", "t1"]
    assert glance.recent[0].channel == "voice" and glance.recent[0].tainted and glance.recent[0].error == "boom"
    assert board.strip().spent_usd == pytest.approx(0.012)


@pytest.mark.parametrize("cost, red", [(0.5, False), (3.0, True), (4.2, True)])
def test_the_cost_turns_red_only_once_the_daily_budget_is_reached(config, paths, board, cost, red):
    """The budget is informational: Jarvis keeps answering, the dashboard only shows today's cost in red."""
    config.budget.daily_budget_usd = 3.0
    db = Database(paths.db_file)
    try:
        db.start_session("s1", "claude-haiku-5-5", "claude-sonnet-5-5", "cli")
        db.start_turn("t1", "s1", "hello")
        db.finish_turn("t1", final_reply="hi", tainted=False, total_ms=100.0, error=None)
        db.add_api_call(
            "t1",
            model="claude-haiku-5-5",
            attempt=1,
            status="ok",
            duration_ms=90,
            ttft_ms=30,
            input_tokens=10,
            output_tokens=2,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=cost,
        )
    finally:
        db.close()
    assert board.today().over_budget is red
    client, store = make_client(config, paths, board)
    login(client, store)
    assert ("over-budget" in client.get("/").text) is red  # the status strip
    today = client.get("/partials/today").text
    from jarvis.dashboard.ui_text import UI

    label = today.index(UI.TODAY_COST)
    cost_tile = today[today.rindex('<div class="kpi', 0, label) : label]
    assert ("kpi-bad" in cost_tile) is red


def test_no_budget_never_turns_red(config, paths, board):
    config.budget.daily_budget_usd = 0
    assert board.today().over_budget is False


def test_today_without_a_database_file_is_empty(config, paths, quiet_services):
    paths.db_file.unlink(missing_ok=True)
    glance = Dashboard(config, paths, auto_full_check=False).today()
    assert glance.turns == 0 and glance.cost_usd == 0.0 and glance.avg_latency_ms is None


# --- health cache --------------------------------------------------------------------------------------------


def test_health_cache_refreshes_quick_checks_on_the_interval_and_runs_full_checks_in_a_thread(config, paths):
    clock = {"t": 100.0}
    quick_calls = []
    release = threading.Event()

    def quick(c, p):
        quick_calls.append(clock["t"])
        return [Check("config.toml", True, "valid"), Check("today's budget", True, "0.1 of 3", warn=True)]

    def full(c, p):
        release.wait(5)
        return [Check("Spotify", False, "not running")]

    cache = HealthCache(config, paths, refresh_s=10, quick_runner=quick, full_runner=full, clock=lambda: clock["t"])
    view = cache.snapshot()
    assert [i.state for i in view.items] == [UI.HEALTH_OK, UI.HEALTH_WARNING] and not view.checking
    clock["t"] += 5
    cache.snapshot()
    assert len(quick_calls) == 1  # cached within the refresh window
    clock["t"] += 6
    cache.snapshot()
    assert len(quick_calls) == 2
    assert cache.run_full() is True
    assert cache.run_full() is False  # already running
    view = cache.snapshot()
    assert view.checking and view.items[-1].state == UI.HEALTH_CHECKING and view.items[-1].name == UI.HEALTH_INTEGRATIONS
    release.set()
    wait_for(lambda: not cache.checking)
    view = cache.snapshot()
    assert not view.checking and view.failed == 1 and view.warnings == 1
    assert [i.name for i in view.items] == ["config.toml", "today's budget", "Spotify"]
    assert view.last_check is not None


def test_dashboard_runs_the_full_check_once_at_start(config, paths, quiet_services, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_state.health, "integration_checks", lambda c, p: calls.append(1) or [])
    board = Dashboard(config, paths, auto_full_check=True)
    wait_for(lambda: not board.health.checking)
    assert calls == [1]


# --- quick actions -------------------------------------------------------------------------------------------


def test_restart_service_uses_the_services_module_and_marks_the_agent_restarting(board, paths, monkeypatch):
    gate = threading.Event()
    seen = []

    def fake_restart(name, p):
        seen.append((name, p))
        gate.wait(5)
        return paths.home / f"{name}.plist"

    monkeypatch.setattr(services, "restart_agent", fake_restart)
    result = board.restart_service("voice")
    assert result.state == "running"
    wait_for(lambda: "voice" in board.restarting)
    assert {v.name: v.state for v in board.services_view()}["voice"] == UI.STATE_RESTARTING
    gate.set()
    wait_for(lambda: board.actions["restart:voice"].state != "running")
    done = board.actions["restart:voice"]
    assert done.state == "done" and done.detail == "Voice restarted." and seen == [("voice", paths)]
    assert "voice" not in board.restarting
    with pytest.raises(JarvisError):
        board.restart_service("finder")


def test_failed_action_shows_the_reason(board, monkeypatch):
    def boom(name, p):
        raise JarvisError("the voice agent is not installed; run `jarvis voice install` first")

    monkeypatch.setattr(services, "restart_agent", boom)
    board.restart_service("voice")
    wait_for(lambda: board.actions["restart:voice"].state == "failed")
    assert "not installed" in board.actions["restart:voice"].detail


def test_send_test_message_goes_through_the_telegram_notifier(board, monkeypatch):
    import jarvis.interfaces.telegram_notify as notify_mod

    sent = []

    class FakeNotifier:
        def __init__(self, config, *, state_file=None):
            self.state_file = state_file

        def blocked_reason(self):
            return None

        def notify(self, title, message):
            sent.append((title, message))

    monkeypatch.setattr(notify_mod, "TelegramNotifier", FakeNotifier)
    board.send_test_message()
    wait_for(lambda: board.actions["telegram_test"].state != "running")
    assert board.actions["telegram_test"].state == "done" and sent and sent[0][0] == "Jarvis Dashboard"

    class Blocked(FakeNotifier):
        def blocked_reason(self):
            return "the kill switch (/stop) is active"

    monkeypatch.setattr(notify_mod, "TelegramNotifier", Blocked)
    board.send_test_message()
    wait_for(lambda: board.actions["telegram_test"].state == "failed")
    assert "kill switch" in board.actions["telegram_test"].detail


def test_send_brief_uses_the_scheduler_channel_choice_and_delivery(board, monkeypatch):
    import jarvis.scheduler.brief as brief_mod

    calls = []
    monkeypatch.setattr(brief_mod, "choose_channels", lambda cfg, probes: (["telegram"], "idle"))
    monkeypatch.setattr(brief_mod, "deliver", lambda config, paths, channels, **kw: calls.append(channels) or "text")
    board.send_brief()
    wait_for(lambda: board.actions["brief"].state != "running")
    assert calls == [["telegram"]] and board.actions["brief"].detail == "Sent via telegram"


# --- helpers and strings -------------------------------------------------------------------------------------


def test_format_helpers():
    assert format_duration(45) == "45 s" and format_duration(12 * 60) == "12 min"
    assert format_duration(3600 + 5 * 60) == "1 h 05 min" and format_duration(2 * 86400 + 3 * 3600) == "2 d 3 h"
    assert format_money(0.0123) == "$0.012" and format_money(3.0) == "$3.00" and format_money(None) == "–"
    assert format_ms(850) == "850 ms" and format_ms(1234) == "1.2 s" and format_ms(None) == "–"


def test_ui_strings_are_english_only():
    turkish = re.compile(r"[ğĞşŞıİçÇöÖüÜ]")
    for name, text in all_strings().items():
        assert not turkish.search(text), f"{name} looks Turkish: {text!r}"


def test_record_start_round_trip(paths, db, monkeypatch):
    record = services.record_start(db, "voice", paths)
    assert record["config_version"] == services.config_version(paths) and len(record["config_version"]) == 8
    assert services.recorded_start(db.conn, "voice") == record
    assert services.recorded_start(db.conn, "telegram") is None
