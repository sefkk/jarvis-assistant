import subprocess
from datetime import datetime

import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.tools.base import RiskLevel
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.system import GetTime, OpenApp


def test_registry_discovers_the_core_tools(monkeypatch):
    import jarvis.tools.football as football_tools

    monkeypatch.setattr(football_tools, "api_key_configured", lambda: True)  # football is optional
    registry = ToolRegistry.discover()
    assert registry.names() == sorted(
        [
            "get_time",
            "open_app",
            "quit_app",
            "set_volume",
            "search_files",
            "read_file",
            "write_file",
            "run_shell",
            "remember",
            "forget",
            "list_memory",
            "agenda",
            "apple_list_events",
            "apple_create_event",
            "apple_delete_event",
            "list_reminders",
            "create_reminder",
            "complete_reminder",
            "google_list_events",
            "google_create_event",
            "google_update_event",
            "google_delete_event",
            "gmail_search_threads",
            "gmail_read_thread",
            "gmail_create_draft",
            "play",
            "pause",
            "next",
            "previous",
            "now_playing",
            "search_and_play",
            "get_weather",
            "web_search",
            "fetch_page",
            "git_status",
            "open_project",
            "list_repos",
            "delegate",
            "capture_screen",
            "get_clipboard",
            "set_clipboard",
            "log_training",
            "training_summary",
            "list_workouts",
            "delete_workout",
            "set_timer",
            "list_timers",
            "cancel_timer",
            "start_pomodoro",
            "stop_pomodoro",
            "set_alarm",
            "list_alarms",
            "cancel_alarm",
            "snooze_alarm",
            "stop_alarm",
            "team_fixtures",
            "team_results",
            "match_details",
            "league_standings",
            "daily_brief",
            "set_brief_schedule",
            "find_spreadsheet",
            "list_sheets",
            "read_range",
            "query_sheet",
            "append_rows",
            "update_range",
            "create_spreadsheet",
            "search_places",
            "place_details",
            "travel_time",
            "geocode",
            "open_in_maps",
            "list_shortcuts",
            "run_shortcut",
            "pause_listening",
            "am_i_home",
            "who_is_home",
            "is_home",
            "presence_history",
            "list_network_devices",
            "internet_test",
            "internet_history",
            "mac_status",
            "lock_screen",
            "sleep_mac",
            "schedule_sleep",
            "cancel_scheduled_sleep",
        ]
    )
    assert registry.qualified_name("get_time") == "system.get_time"
    assert registry.qualified_name("run_shell") == "shell.run_shell"
    schemas = {s["name"]: s for s in registry.api_tools()}

    def has_title_key(node):
        # Pydantic's generated "title": "<str>" keys must be stripped. A field *named* title shows up as
        # "title": {<schema>} under properties (a dict value) and inside "required" (a list), which is fine.
        if isinstance(node, dict):
            return any(k == "title" and isinstance(v, str) for k, v in node.items()) or any(has_title_key(v) for v in node.values())
        return isinstance(node, list) and any(has_title_key(v) for v in node)

    assert not any(has_title_key(s["input_schema"]) for s in schemas.values() if "input_schema" in s)
    # a field literally named `title` must survive the cleanup (events, reminders)
    for name in ("google_create_event", "google_update_event", "apple_create_event", "create_reminder"):
        props = schemas[name]["input_schema"]["properties"]
        assert "title" in props and "string" in str(props["title"]), name
    assert schemas["web_search"]["type"].startswith("web_search_") and "input_schema" not in schemas["web_search"]
    assert schemas["open_app"]["input_schema"]["required"] == ["app"]
    assert schemas["open_app"]["input_schema"]["properties"]["app"]["type"] == "string"
    assert registry.get("run_shell").risk.value == "CONFIRM" and registry.get("write_file").risk.value == "CONFIRM"
    assert registry.get("read_file").taints and not registry.get("search_files").taints


def test_registry_rejects_duplicates_and_filters_by_risk():
    registry = ToolRegistry()
    registry.register(GetTime())
    with pytest.raises(ValueError):
        registry.register(GetTime())
    assert registry.subset(max_risk=RiskLevel.SAFE).names() == ["get_time"]


def test_get_time_returns_current_year():
    result = GetTime().run(GetTime().parse_args({}))
    assert str(datetime.now().year) in result.content and "Local time" in result.content
    assert not result.is_error


def test_parse_args_validation_error():
    with pytest.raises(ToolValidationError, match="app"):
        OpenApp().parse_args({})
    with pytest.raises(ToolValidationError):
        OpenApp().parse_args({"app": "-a"})
    with pytest.raises(ToolValidationError):
        OpenApp().parse_args({"app": "../Applications/Evil"})
    with pytest.raises(ToolValidationError, match="object"):
        OpenApp().parse_args("Safari")


def test_open_app_uses_argv_not_shell(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = OpenApp()
    result = tool.run(tool.parse_args({"app": " Safari "}))
    assert result.content == "Opened Safari."
    assert calls[0][0] == ["open", "-a", "Safari"]
    assert "shell" not in calls[0][1] or calls[0][1]["shell"] is False


def test_open_app_failure_is_tool_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "Unable to find application"))
    tool = OpenApp()
    with pytest.raises(ToolExecutionError, match="Unable to find application"):
        tool.run(tool.parse_args({"app": "Nope"}))
