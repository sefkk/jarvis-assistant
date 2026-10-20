import itertools
import json

import anthropic
import pytest
from pydantic import BaseModel

from jarvis.brain import THINKING_MIN_OUTPUT_TOKENS, Brain, resolve_thinking_param
from jarvis.exceptions import JarvisAPIError
from jarvis.observability import logging_setup
from jarvis.observability.timing import TurnTimer
from jarvis.observability.usage import UsageTracker
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.system import GetTime
from tests.conftest import (
    FakeClient,
    RecordingSink,
    api_error,
    connection_error,
    make_message,
    text_message,
    thinking_block,
    tool_message,
)


class EchoInput(BaseModel):
    text: str


class EchoTool(Tool):
    name = "echo"
    description = "echo"
    input_model = EchoInput
    risk = RiskLevel.CONFIRM
    taints = True

    def run(self, args):
        return ToolResult(f"echo:{args.text}")


class CrashTool(Tool):
    name = "crash"
    description = "crash"

    def run(self, args):
        raise RuntimeError("kaboom")


class HugeTool(Tool):
    name = "huge"
    description = "huge"

    def run(self, args):
        return ToolResult("x" * 20000)


class PictureTool(Tool):
    name = "picture"
    description = "returns an image"
    taints = True
    taint_source = "screen"

    def run(self, args):
        from jarvis.tools.base import ImageAttachment

        return ToolResult("Screenshot 2x2", images=[ImageAttachment("image/png", "AAAA", 2, 2, "deadbeef")])


_counter = itertools.count()


def make_brain(config, db, outcomes, *, sleeps=None, **kw):
    registry = ToolRegistry()
    for tool in (GetTime(), EchoTool(), CrashTool(), HugeTool(), PictureTool()):
        registry.register(tool)
    client = FakeClient(outcomes)
    sleeps = sleeps if sleeps is not None else []
    brain = Brain(client, config, registry, db, UsageTracker(config, db), sleep=sleeps.append, rng=lambda: 0.0, **kw)
    n = next(_counter)
    brain.test_turn_id = f"t{n}"
    db.start_session(f"s{n}", "m", "d")
    db.start_turn(brain.test_turn_id, f"s{n}", "hi")
    logging_setup.set_session_id(f"s{n}")
    logging_setup.set_turn_id(brain.test_turn_id)
    return brain, client


def run(brain, messages, sink=None):
    sink = sink or RecordingSink()
    turn_id = brain.test_turn_id
    outcome = brain.run_turn(system="sys", messages=messages, sink=sink, turn_id=turn_id, timer=TurnTimer(turn_id, brain.db))
    return outcome, sink


def audit_lines(logs):
    return [json.loads(l) for l in (logs / "audit.jsonl").read_text().splitlines() if l.strip()]


# --- streaming and request shape ------------------------------------------------------------


def test_streams_text_and_records_usage(config, db, logs):
    config.models.main_model = "claude-haiku-5-5"  # pinned: the per-family spellings are tested below
    brain, client = make_brain(config, db, [text_message("Hello there", input_tokens=12, output_tokens=3)])
    messages = [{"role": "user", "content": "hi"}]
    outcome, sink = run(brain, messages)

    assert sink.text == "Hello there" and outcome.text == "Hello there"
    assert outcome.api_calls == 1 and outcome.stop_reason == "end_turn"
    assert outcome.usage.input_tokens == 12 and outcome.usage.output_tokens == 3
    assert messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "Hello there"}]}

    params = client.calls[0]
    assert params["model"] == config.models.main_model
    assert params["max_tokens"] == config.models.max_output_tokens
    assert params["thinking"] == {"type": "disabled"}  # Haiku 5.5 spelling of "off"
    assert params["output_config"] == {"effort": config.models.main_effort}
    assert [t["name"] for t in params["tools"]] == ["crash", "echo", "get_time", "huge", "picture"]

    row = db.conn.execute("SELECT * FROM api_calls").fetchone()
    assert row["status"] == "ok" and row["input_tokens"] == 12 and row["attempt"] == 1
    usage_line = json.loads((logs / "usage.jsonl").read_text().splitlines()[-1])
    assert usage_line["model"] == config.models.main_model and usage_line["turn_id"] == brain.test_turn_id


def test_thinking_on_uses_adaptive_and_preserves_blocks(config, db, logs):
    first = make_message([thinking_block("hmm", "sig1"), *tool_message("get_time", {}).content], stop_reason="tool_use")
    brain, client = make_brain(config, db, [first, text_message("done")], thinking=True, debug=True)
    messages = [{"role": "user", "content": "time?"}]
    outcome, sink = run(brain, messages)

    assert client.calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    # Thinking shares max_tokens with the reply, so the cap is raised above the configured 1024.
    assert client.calls[0]["max_tokens"] == THINKING_MIN_OUTPUT_TOKENS
    # The thinking block is sent back exactly as returned, before the tool_use.
    assistant_turn = client.calls[1]["messages"][1]
    assert assistant_turn["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "sig1"}
    assert assistant_turn["content"][1]["type"] == "tool_use"
    assert "hmm" in sink.debug_lines
    stored = db.conn.execute("SELECT content_json FROM messages WHERE role='assistant' ORDER BY id").fetchone()
    assert json.loads(stored["content_json"])[0]["thinking"] == "hmm"


@pytest.mark.parametrize(
    "model, thinking, expected",
    [
        ("claude-haiku-5-5", False, {"type": "disabled"}),
        ("claude-sonnet-5-5", False, {"type": "between_tools"}),
        ("claude-opus-5-5", False, {"type": "adaptive", "display": "summarized"}),  # cannot be turned off
        ("claude-haiku-4-5", False, {}),  # older models: omit the parameter
        ("claude-haiku-5-5", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-sonnet-5-5", True, {"type": "adaptive", "display": "summarized"}),
    ],
)
def test_thinking_off_is_spelled_per_model(model, thinking, expected):
    assert resolve_thinking_param(model, thinking) == expected


def test_brain_uses_model_specific_thinking_and_effort(config, db, logs):
    brain, client = make_brain(config, db, [text_message("ok")], model="claude-sonnet-5-5", thinking=False, effort="low")
    run(brain, [{"role": "user", "content": "hi"}])
    params = client.calls[0]
    assert params["thinking"] == {"type": "between_tools"} and params["output_config"] == {"effort": "low"}
    assert params["max_tokens"] == config.models.max_output_tokens  # no floor when thinking is off

    brain, client = make_brain(config, db, [text_message("ok")], model="claude-haiku-4-5", thinking=False)
    run(brain, [{"role": "user", "content": "hi"}])
    assert "thinking" not in client.calls[0]


def test_thinking_floor_respects_larger_configured_cap(config, db, logs):
    brain, client = make_brain(config, db, [text_message("ok")], thinking=True, max_output_tokens=8192)
    run(brain, [{"role": "user", "content": "hi"}])
    assert client.calls[0]["max_tokens"] == 8192


# --- tool loop -----------------------------------------------------------------------------


def test_tool_loop_executes_and_returns_result(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("get_time", {}, text="Checking."), text_message("It is noon.")])
    messages = [{"role": "user", "content": "time?"}]
    outcome, sink = run(brain, messages)

    assert sink.text == "Checking.It is noon."
    assert outcome.tools_used == ["get_time"] and outcome.api_calls == 2 and not outcome.tainted
    second_request = client.calls[1]["messages"]
    assert second_request[1]["role"] == "assistant" and second_request[1]["content"][1]["type"] == "tool_use"
    result_block = second_request[2]["content"][0]
    assert second_request[2]["role"] == "user"
    assert result_block["type"] == "tool_result" and result_block["tool_use_id"] == "tu_1"
    assert "Local time" in result_block["content"] and "is_error" not in result_block
    assert len(messages) == 4

    row = db.conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["tool"] == "get_time" and row["decision"] == "ran" and row["error"] is None
    audit = audit_lines(logs)[-1]
    assert audit["tool"] == "get_time" and audit["decision"] == "ran" and audit["tainted"] is False
    assert audit["result_summary"]["length"] > 0 and len(audit["result_summary"]["head"]) <= 300
    stages = {s for s, _ in brain.db.conn.execute("SELECT stage, duration_ms FROM timings").fetchall()}
    assert {"api_call", "tool:get_time"} <= stages


def test_reply_leaves_out_the_commentary_before_tool_calls_server_tools_included(config, db, logs):
    """The 2026-10-19 brief: status lines before daily_brief and before the server-side web searches were
    glued onto the reply ("Brief'i alıyorum.Football bölümü boş geldi..."). The reply is the text after the
    last tool block; the sink is told where every tool block starts."""
    from anthropic.types import ServerToolUseBlock, TextBlock, WebSearchToolResultBlock

    final = make_message(
        [
            TextBlock(type="text", text="Football bölümü boş, arıyorum."),
            ServerToolUseBlock(type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "Fenerbahçe maçı"}),
            WebSearchToolResultBlock(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=[]),
            TextBlock(type="text", text="Günaydın Kerem! "),
            TextBlock(type="text", text="Bugün maç yok."),
        ]
    )
    brain, client = make_brain(config, db, [tool_message("get_time", {}, text="Brief'i alıyorum."), final])
    outcome, sink = run(brain, [{"role": "user", "content": "özet"}])

    assert outcome.text == "Günaydın Kerem! Bugün maç yok."
    assert sink.text == "Brief'i alıyorum.Football bölümü boş, arıyorum.Günaydın Kerem! Bugün maç yok."  # the CLI still shows it live
    first, second = len("Brief'i alıyorum."), len("Brief'i alıyorum.Football bölümü boş, arıyorum.")
    assert sink.breaks[0] == first and set(sink.breaks[1:]) == {second}


def test_reply_keeps_a_long_answer_written_before_a_tool_call(config, db, logs):
    answer = "Son üç mailin hiçbiri acil değil. " * 8  # well over a status line
    brain, client = make_brain(config, db, [tool_message("get_time", {}, text=answer), text_message("Hatırlatıcıyı da ekledim.")])
    outcome, _ = run(brain, [{"role": "user", "content": "x"}])
    assert outcome.text == answer.strip() + "\n\nHatırlatıcıyı da ekledim."


def test_compose_reply_falls_back_to_the_commentary_when_nothing_followed():
    from jarvis.io.base import compose_reply

    assert compose_reply(["Bakıyorum.", ""]) == "Bakıyorum."
    assert compose_reply(["Bakıyorum.", "Hava güzel."]) == "Hava güzel."
    assert compose_reply(["Birinci paragraf.\n\nİkinci paragraf.", "Tamam."]) == "Birinci paragraf.\n\nİkinci paragraf.\n\nTamam."
    assert compose_reply([]) == ""


def test_unknown_tool_is_error_result(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("nope", {"x": 1}), text_message("ok")])
    outcome, _ = run(brain, [{"role": "user", "content": "go"}])
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "unknown tool" in result["content"]
    assert audit_lines(logs)[-1]["decision"] == "validation_error"


def test_bad_arguments_is_validation_error(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("echo", {}), text_message("ok")])
    outcome, _ = run(brain, [{"role": "user", "content": "go"}])
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "invalid arguments" in result["content"]
    assert db.conn.execute("SELECT decision FROM tool_calls").fetchone()["decision"] == "validation_error"


def test_crashing_tool_never_crashes_loop(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("crash", {}), text_message("sorry")])
    outcome, sink = run(brain, [{"role": "user", "content": "go"}])
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "kaboom" in result["content"]
    assert sink.text == "sorry"
    assert db.conn.execute("SELECT decision FROM tool_calls").fetchone()["decision"] == "error"


def test_confirm_tool_denied_and_approved(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("echo", {"text": "a"}), text_message("ok")])
    outcome, sink = run(brain, [{"role": "user", "content": "go"}], RecordingSink(confirm_answer=False))
    assert sink.confirms[0].tool == "echo" and sink.confirms[0].args == {"text": "a"}
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "not approved" in result["content"]
    assert audit_lines(logs)[-1]["decision"] == "denied"
    assert not outcome.tainted  # the tool never ran

    brain, client = make_brain(config, db, [tool_message("echo", {"text": "b"}), text_message("ok")])
    outcome, sink = run(brain, [{"role": "user", "content": "go"}], RecordingSink(confirm_answer=True))
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["content"] == '<untrusted source="tool">\necho:b\n</untrusted>' and "is_error" not in result
    assert audit_lines(logs)[-1]["decision"] == "confirmed"
    assert outcome.tainted and outcome.taint_sources == ["tool"]


def test_tool_result_is_truncated(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("huge", {}), text_message("ok")])
    run(brain, [{"role": "user", "content": "go"}])
    content = client.calls[1]["messages"][2]["content"][0]["content"]
    assert len(content) < 20000 and "[truncated" in content


def test_image_results_reach_the_model_but_not_the_transcript(config, db, logs):
    brain, client = make_brain(config, db, [tool_message("picture", {}), text_message("I see a square.")])
    outcome, _ = run(brain, [{"role": "user", "content": "look"}])
    result = client.calls[1]["messages"][2]["content"][0]
    assert result["type"] == "tool_result"
    blocks = result["content"]
    assert blocks[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert blocks[1]["type"] == "text" and blocks[1]["text"].startswith('<untrusted source="screen">')
    assert outcome.tainted and outcome.taint_sources == ["screen"]
    stored = json.loads(db.conn.execute("SELECT content_json FROM messages WHERE role='user' ORDER BY id DESC").fetchone()["content_json"])
    ref = stored[0]["content"][0]
    assert ref == {"type": "image_ref", "media_type": "image/png", "width": 2, "height": 2, "sha256": "deadbeef"}
    assert "AAAA" not in json.dumps(stored)

    config.screen.store_images = True
    brain, client = make_brain(config, db, [tool_message("picture", {}), text_message("ok")])
    run(brain, [{"role": "user", "content": "look"}])
    stored = json.loads(db.conn.execute("SELECT content_json FROM messages WHERE role='user' ORDER BY id DESC").fetchone()["content_json"])
    assert stored[0]["content"][0]["type"] == "image"


def test_iteration_cap_stops_loop(config, db, logs):
    cap = config.loop.max_tool_iterations
    brain, client = make_brain(config, db, [tool_message("get_time", {}, tool_id=f"tu_{i}") for i in range(cap + 2)])
    outcome, sink = run(brain, [{"role": "user", "content": "loop"}])
    assert outcome.hit_iteration_cap and outcome.api_calls == cap
    assert len(client.calls) == cap
    assert any("sınırına ulaşıldı" in n for n in sink.notices)  # Turkish test config


def test_iteration_cap_notice_in_english(config, db, logs):
    from jarvis import i18n

    i18n.set_language("en")
    cap = config.loop.max_tool_iterations
    brain, client = make_brain(config, db, [tool_message("get_time", {}, tool_id=f"tu_{i}") for i in range(cap + 2)])
    outcome, sink = run(brain, [{"role": "user", "content": "loop"}])
    assert f"Stopped: reached the limit of {cap} tool calls for this turn." in sink.notices


def test_max_tokens_with_tool_use_does_not_run_tool(config, db, logs):
    truncated = make_message(tool_message("get_time", {}).content, stop_reason="max_tokens")
    brain, client = make_brain(config, db, [truncated])
    messages = [{"role": "user", "content": "go"}]
    outcome, sink = run(brain, messages)
    assert outcome.tools_used == [] and len(client.calls) == 1
    assert any("kesildi" in n for n in sink.notices)
    # History stays valid: the pending tool_use got an error tool_result.
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": "not executed: the response was cut off at max_tokens",
        "is_error": True,
    }


# --- retries -------------------------------------------------------------------------------


def test_retries_rate_limit_then_succeeds(config, db, logs):
    sleeps = []
    brain, client = make_brain(
        config,
        db,
        [api_error(anthropic.RateLimitError, 429, error_type="rate_limit_error"), text_message("fine")],
        sleeps=sleeps,
    )
    outcome, sink = run(brain, [{"role": "user", "content": "hi"}])
    assert sink.text == "fine" and len(client.calls) == 2
    assert sleeps == [config.loop.api_backoff_base_seconds]  # rng fixed to 0
    assert any("yeniden deneniyor" in n for n in sink.notices)
    rows = db.conn.execute("SELECT attempt, status FROM api_calls ORDER BY id").fetchall()
    assert [(r["attempt"], r["status"]) for r in rows] == [(1, "error:429"), (2, "ok")]


class _FailMidStream(Exception):
    pass


def test_retry_after_partial_output_does_not_repeat_text(config, db, logs, monkeypatch):
    """The first attempt streams 'Hello wor' then dies; the retry streams the full reply."""
    from tests.conftest import FakeStream, connection_error

    full = text_message("Hello world, here it is.")
    partial_error = connection_error(anthropic.APIConnectionError)

    class PartialStream(FakeStream):
        def __iter__(self):
            from types import SimpleNamespace

            yield SimpleNamespace(type="text", text="Hello ", snapshot="Hello ")
            yield SimpleNamespace(type="text", text="wor", snapshot="Hello wor")
            raise partial_error

    brain, client = make_brain(config, db, [full, full])
    original = client.messages.stream
    state = {"n": 0}

    def stream(**params):
        state["n"] += 1
        if state["n"] == 1:
            client.messages.calls.append(params)
            return PartialStream(full)
        return original(**params)

    monkeypatch.setattr(client.messages, "stream", stream)
    outcome, sink = run(brain, [{"role": "user", "content": "hi"}])
    assert sink.text == "Hello world, here it is."  # not "Hello worHello world, ..."
    assert outcome.text == "Hello world, here it is."
    assert any("yeniden deneniyor" in n for n in sink.notices) and not any("baştan başladı" in n for n in sink.notices)


def test_retries_connection_errors_and_overload(config, db, logs):
    sleeps = []
    brain, client = make_brain(
        config,
        db,
        [
            connection_error(anthropic.APIConnectionError),
            api_error(anthropic.InternalServerError, 529, error_type="overloaded_error"),
            text_message("ok"),
        ],
        sleeps=sleeps,
    )
    outcome, sink = run(brain, [{"role": "user", "content": "hi"}])
    assert sink.text == "ok" and len(sleeps) == 2
    assert sleeps[1] == 2 * config.loop.api_backoff_base_seconds  # exponential


def test_gives_up_after_max_attempts(config, db, logs):
    sleeps = []
    errors = [api_error(anthropic.InternalServerError, 500, request_id="req_z") for _ in range(config.loop.api_max_attempts)]
    brain, client = make_brain(config, db, errors, sleeps=sleeps)
    with pytest.raises(JarvisAPIError) as exc:
        run(brain, [{"role": "user", "content": "hi"}])
    err = exc.value
    assert err.attempts == config.loop.api_max_attempts and err.status_code == 500
    assert err.error_type == "api_error" and err.request_id == "req_z"
    assert len(sleeps) == config.loop.api_max_attempts - 1
    assert db.conn.execute("SELECT COUNT(*) AS n FROM api_calls").fetchone()["n"] == config.loop.api_max_attempts


def test_non_retryable_error_raises_immediately(config, db, logs):
    sleeps = []
    brain, client = make_brain(config, db, [api_error(anthropic.BadRequestError, 400, error_type="invalid_request_error")], sleeps=sleeps)
    with pytest.raises(JarvisAPIError) as exc:
        run(brain, [{"role": "user", "content": "hi"}])
    assert exc.value.attempts == 1 and exc.value.status_code == 400 and sleeps == []
    assert exc.value.error_type == "invalid_request_error"


def test_auth_error_is_not_retried(config, db, logs):
    brain, client = make_brain(config, db, [api_error(anthropic.AuthenticationError, 401, error_type="authentication_error")])
    with pytest.raises(JarvisAPIError) as exc:
        run(brain, [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 401 and len(client.calls) == 1
