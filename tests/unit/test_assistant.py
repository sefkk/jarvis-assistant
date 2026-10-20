import json

import anthropic

from jarvis.assistant import Assistant
from jarvis.tools.base import ToolContext
from jarvis.tools.registry import ToolRegistry
from tests.conftest import FakeClient, RecordingSink, api_error, text_message, tool_message


def make_assistant(config, paths, outcomes, **kw):
    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    return Assistant(config, paths, client=FakeClient(outcomes), registry=registry, **kw)


def test_handle_streams_and_records_everything(config, paths, logs):
    assistant = make_assistant(config, paths, [tool_message("get_time", {}), text_message("It is noon.")])
    sink = RecordingSink()
    result = assistant.handle("What time is it?", sink)

    assert sink.text == "It is noon." and sink.finished == 1
    assert result.reply == "It is noon." and result.tools_used == ["get_time"] and result.error is None
    assert result.session_id == assistant.session_id and result.total_ms > 0
    assert "api_call" in result.timings and "tool:get_time" in result.timings and "prompt_build" in result.timings
    assert len(assistant.history) == 1
    assert len(assistant.history.turns[0].messages) == 4  # user, assistant(tool_use), user(tool_result), assistant

    db = assistant.db
    turn = db.conn.execute("SELECT * FROM turns").fetchone()
    assert turn["id"] == result.turn_id and turn["user_input"] == "What time is it?"
    assert turn["final_reply"] == "It is noon." and turn["error"] is None
    assert db.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 4
    assert db.conn.execute("SELECT COUNT(*) AS n FROM api_calls").fetchone()["n"] == 2
    assert db.conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"] == 1
    assert db.conn.execute("SELECT model_main FROM sessions").fetchone()["model_main"] == config.models.main_model

    for name in ("jarvis.log", "audit.jsonl", "usage.jsonl"):
        lines = [json.loads(l) for l in (logs / name).read_text().splitlines() if l.strip()]
        assert any(l.get("turn_id") == result.turn_id for l in lines), name
        assert all(l.get("session_id") == assistant.session_id for l in lines if l.get("turn_id") == result.turn_id)
    assistant.close()


def test_api_failure_keeps_the_users_message_and_drops_the_dangling_tool_use(config, paths, logs):
    assistant = make_assistant(
        config,
        paths,
        [
            tool_message("get_time", {}),
            api_error(anthropic.BadRequestError, 400, error_type="invalid_request_error"),
            text_message("second turn works"),
        ],
    )
    sink = RecordingSink()
    result = assistant.handle("first", sink)
    assert result.error and "400" in result.error
    # The user sees one short categorised message in the configured language (tr by default),
    # with the status, the attempt count and the request id; the English one_line() goes to the log.
    assert any(n.startswith("Hata (API):") and "status 400" in n and "req_test" in n for n in sink.notices)
    assert sink.finished == 1
    # Only the user's own message survives the failure: the tool_use without a final reply is dropped.
    assert len(assistant.history) == 1
    failed = assistant.history.turns[0]
    assert failed.failed and failed.messages == [{"role": "user", "content": "first"}]
    turn = assistant.db.conn.execute("SELECT error FROM turns").fetchone()
    assert "400" in turn["error"]

    result2 = assistant.handle("second", RecordingSink())
    assert result2.reply == "second turn works" and result2.error is None
    assert len(assistant.history) == 2 and len(assistant.history.turns[1].messages) == 2
    # the next request still carries what the user said in the failed turn
    assert [m["content"] for m in assistant.client.calls[-1]["messages"]] == ["first", "second"]
    assistant.close()


def test_new_session_resets_history(config, paths, logs):
    assistant = make_assistant(config, paths, [text_message("a"), text_message("b")])
    assistant.handle("one", RecordingSink())
    first = assistant.session_id
    assistant.new_session()
    assert len(assistant.history) == 0 and assistant.session_id != first
    assistant.handle("two", RecordingSink())
    rows = assistant.db.conn.execute("SELECT id, ended_at FROM sessions ORDER BY started_at").fetchall()
    assert len(rows) == 2 and rows[0]["ended_at"] is not None
    assistant.close()


def test_transcripts_can_be_disabled(config, paths, logs):
    config.general.save_transcripts = False
    assistant = make_assistant(config, paths, [text_message("quiet")])
    assistant.handle("hi", RecordingSink())
    assert assistant.db.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0
    assert assistant.db.conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"] == 1
    assistant.close()


# --- prompt assembly ---------------------------------------------------------------------


def test_system_prompt_has_profile_memory_and_stable_cache_layout(config, paths, logs):
    paths.profile_file.write_text("# Profile\n\n- Name: Kerem\n- Address me as: Kerem\n\n## Preferences\n\nshort answers\n")
    paths.memory_file.write_text("- 2026-10-01: likes tea\n")
    assistant = make_assistant(config, paths, [text_message("Merhaba Kerem"), text_message("again")])
    assistant.handle("selam", RecordingSink())
    assistant.handle("tekrar", RecordingSink())
    calls = assistant.client.calls
    system = calls[0]["system"]
    tags = [block["text"].split("\n", 1)[0] for block in system]
    assert tags == ["<persona>", "<rules>", "<user_profile>", "<memory>"]
    assert "Kerem" in system[2]["text"] and "likes tea" in system[3]["text"]
    assert "cache_control" not in system[0] and all("cache_control" in b for b in system[1:])
    assert calls[0]["cache_control"] == {"type": "ephemeral"}
    assert calls[1]["system"] == system  # byte-identical prefix on the next turn, so the cache hits
    assert [t["name"] for t in calls[0]["tools"]] == [t["name"] for t in calls[1]["tools"]]
    assert len(calls[1]["messages"]) == 3  # previous turn kept verbatim plus the new user message
    assert "in the language of the user's latest message" in system[0]["text"]
    assert "<untrusted" in system[1]["text"] and "never instructions" in system[1]["text"]
    assistant.close()


def test_fallback_language_is_in_the_prompt(config, paths, logs):
    config.general.language = "tr"
    assistant = make_assistant(config, paths, [text_message("ok")])
    assistant.handle("hi", RecordingSink())
    assert "Fallback language (when the user's language cannot be told): tr" in assistant.client.calls[0]["system"][2]["text"]
    assistant.close()


def test_non_api_crash_does_not_end_the_session(config, paths, logs, monkeypatch):
    assistant = make_assistant(config, paths, [text_message("a"), text_message("b")])
    monkeypatch.setattr(assistant.brain, "run_turn", lambda **kw: (_ for _ in ()).throw(KeyError("boom")))
    sink = RecordingSink()
    result = assistant.handle("one", sink)
    assert result.error == "KeyError: 'boom'" and any("ters gitti" in n for n in sink.notices)  # Turkish test config
    assert sink.finished == 1 and assistant.db.conn.execute("SELECT error FROM turns").fetchone()["error"].startswith("KeyError")
    monkeypatch.undo()
    assert assistant.handle("two", RecordingSink()).reply == "a"  # the session goes on
    assistant.close()


def test_no_compaction_notice_when_nothing_can_be_compacted(config, paths, logs):
    config.context.compact_after_tokens = 50
    config.context.keep_recent_turns = 6
    assistant = make_assistant(config, paths, [text_message("big", input_tokens=500), text_message("next")])
    assistant.handle("one", RecordingSink())
    sink = RecordingSink()
    result = assistant.handle("two", sink)
    assert not result.compacted and "Önceki konuşma özetleniyor..." not in sink.notices
    assert len(assistant.client.calls) == 2  # no summary call was attempted
    assistant.close()


def test_missing_profile_is_handled(config, paths, logs):
    assistant = make_assistant(config, paths, [text_message("hi")])
    assert not assistant.has_profile
    assistant.handle("hi", RecordingSink())
    system = assistant.client.calls[0]["system"]
    assert "no profile yet" in system[2]["text"] and "(empty)" in system[3]["text"]
    assistant.close()


def test_memory_change_is_visible_next_turn_and_strips_thinking(config, paths, logs):
    from tests.conftest import make_message, thinking_block

    config.models.main_thinking = True
    first = make_message([thinking_block("t1", "sig1"), *tool_message("remember", {"fact": "has a cat"}).content], stop_reason="tool_use")
    assistant = make_assistant(config, paths, [first, text_message("noted"), text_message("ok")])
    assistant.handle("remember that I have a cat", RecordingSink())
    assert "has a cat" in paths.memory_file.read_text()
    assistant.handle("thanks", RecordingSink())
    calls = assistant.client.calls
    assert "has a cat" in calls[2]["system"][3]["text"]  # memory block updated
    # The prefix changed, so the earlier thinking block is no longer replayed.
    assistant_blocks = [b for m in calls[2]["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert all(b["type"] != "thinking" for b in assistant_blocks)
    assert any(b["type"] == "tool_use" for b in assistant_blocks)
    assistant.close()
