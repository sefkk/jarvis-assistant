"""The analytics instrumentation. Prompt composition (API counts cached per content
version, local estimates scaled to the billed total), error categories with the final-failure flag, the turn
summary row, voice events, and the backfill that migration 10 runs on an older database."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import anthropic
import pytest

from jarvis.observability import composition
from jarvis.observability.composition import TokenCounter, compose, estimate_tokens, text_of
from jarvis.observability.db import MIGRATIONS, Database
from jarvis.observability.usage import classify_error, classify_status
from tests.conftest import api_error, connection_error


class CountingClient:
    """A fake Anthropic client whose count_tokens returns len(text) // 4 and records every call."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail
        self.messages = SimpleNamespace(count_tokens=self.count_tokens)

    def count_tokens(self, **params):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("count_tokens down")
        text = "".join(text_of(m["content"]) for m in params["messages"])
        tokens = len(text) // 4
        if params.get("tools"):
            tokens += 100 * len(params["tools"])
        return SimpleNamespace(input_tokens=tokens)


SYSTEM = [
    {"type": "text", "text": "<persona>\n" + "p" * 400 + "\n</persona>"},
    {"type": "text", "text": "<rules>\n" + "r" * 800 + "\n</rules>"},
    {"type": "text", "text": "<user_profile>\n" + "u" * 200 + "\n</user_profile>"},
    {"type": "text", "text": "<memory>\n" + "m" * 100 + "\n</memory>"},
]
TOOLS = [
    {"name": "get_time", "description": "time", "input_schema": {"type": "object", "properties": {}}},
    {"type": "web_search_20250305", "name": "web_search"},
]


def params_with(messages):
    return {"model": "claude-haiku-5-5", "system": SYSTEM, "tools": TOOLS, "messages": messages}


def test_token_counter_uses_the_api_once_per_content_version_and_caches_in_kv(db):
    client = CountingClient()
    counter = TokenCounter(client, db)
    tokens, exact = counter.count_text("claude-haiku-5-5", "x" * 400)
    assert (tokens, exact) == (100, True) and len(client.calls) == 1
    assert counter.count_text("claude-haiku-5-5", "x" * 400) == (100, True) and len(client.calls) == 1  # memory cache
    fresh = TokenCounter(CountingClient(), db)
    assert fresh.count_text("claude-haiku-5-5", "x" * 400) == (100, True) and fresh.api_calls == 0  # kv cache
    assert counter.count_text("claude-sonnet-5-5", "x" * 400)[1] is True and len(client.calls) == 2  # another tokenizer
    assert db.conn.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'tokcount:%'").fetchone()[0] == 2


def test_token_counter_counts_tools_as_the_difference_and_skips_server_tools(db):
    client = CountingClient()
    counter = TokenCounter(client, db)
    tokens, exact = counter.count_tools("claude-haiku-5-5", TOOLS)
    assert exact and tokens > 100  # 100 for the client tool from the fake, plus the estimate for web_search
    assert all("web_search" not in json.dumps(c.get("tools", [])) for c in client.calls)
    assert len(client.calls) == 2  # with and without tools


def test_token_counter_falls_back_to_estimates_when_the_api_fails(db):
    clock = {"t": 0.0}
    counter = TokenCounter(CountingClient(fail=True), db, clock=lambda: clock["t"])
    tokens, exact = counter.count_text("m", "y" * 350)
    assert exact is False and tokens == estimate_tokens("y" * 350) == 100
    assert counter.api_calls == 1
    counter.count_text("m", "z" * 10)
    assert counter.api_calls == 1  # blocked for a while after the failure
    clock["t"] += composition.API_RETRY_AFTER_S + 1
    counter.count_text("m", "z" * 10)
    assert counter.api_calls == 2
    assert TokenCounter(None, db).count_text("m", "abc")[1] is False  # no client at all


def test_compose_splits_the_billed_prompt_tokens_across_components(db):
    counter = TokenCounter(CountingClient(), db)
    messages = [
        {"role": "user", "content": "earlier question " * 10},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "get_time", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "result " * 50}]},
        {"role": "assistant", "content": [{"type": "text", "text": "answer " * 10}]},
        {"role": "user", "content": "what now?"},
    ]
    billed = 2000
    result = compose(
        params_with(messages), billed, [{"type": "thinking", "thinking": "t" * 70}, {"type": "text", "text": "ok"}], 40, counter
    )
    fixed = result["persona"] + result["rules"] + result["profile"] + result["memory"] + result["tool_definitions"]
    variable = result["conversation_history"] + result["tool_results"] + result["user_input"]
    assert fixed + variable == billed == result["prompt_tokens"]
    assert result["persona"] == 105 and result["rules"] == 204  # API counts (len // 4 incl. the tags)
    assert result["tool_results"] > result["user_input"] > 0 and result["conversation_history"] > 0
    assert result["thinking"] == 20 and result["exact"] is True  # 70 chars / 3.5, under the 40 output tokens


def test_compose_without_a_counter_estimates_everything_and_scales_down_when_needed():
    result = compose(params_with([{"role": "user", "content": "hi"}]), 100, [], 0, None)
    assert result["exact"] is False and result["prompt_tokens"] == 100
    assert sum(result[c] for c in composition.COMPONENTS) == 100
    assert result["user_input"] == 0  # the fixed blocks alone exceed 100 tokens, so they are shrunk to fit


def test_text_of_flattens_blocks():
    assert text_of("a") == "a"
    assert (
        text_of([{"type": "text", "text": "a"}, {"type": "tool_result", "tool_use_id": "x", "content": [{"type": "text", "text": "b"}]}])
        == "a\nb"
    )
    assert "get_time" in text_of({"type": "tool_use", "name": "get_time", "input": {"q": 1}})
    assert estimate_tokens(text_of({"type": "image", "source": {}})) == composition.IMAGE_TOKENS
    assert text_of(None) == "" and estimate_tokens("") == 0


# --- error categories --------------------------------------------------------------------------------------


def test_classify_error_and_status():
    assert classify_error(api_error(anthropic.RateLimitError, 429)) == "rate_limit"
    assert classify_error(api_error(anthropic.InternalServerError, 529, error_type="overloaded_error")) == "overloaded"
    assert classify_error(api_error(anthropic.AuthenticationError, 401)) == "auth"
    assert classify_error(api_error(anthropic.BadRequestError, 400)) == "validation"
    assert classify_error(api_error(anthropic.InternalServerError, 500)) == "other"
    assert classify_error(connection_error(anthropic.APIConnectionError)) == "network"
    assert classify_error(anthropic.APITimeoutError(request=connection_error(anthropic.APIConnectionError).request)) == "timeout"
    assert classify_error(api_error(anthropic.InternalServerError, 500), phase="read") == "timeout"
    assert classify_status("error:429") == "rate_limit" and classify_status("error:529") == "overloaded"
    assert classify_status("error:APITimeoutError:read") == "timeout" and classify_status("error:APIConnectionError") == "network"
    assert classify_status("error:401") == "auth" and classify_status("error:400") == "validation"
    assert classify_status("error:500") == "other" and classify_status("ok") is None and classify_status(None) is None


# --- the database: summaries, events, backfill ----------------------------------------------------------------


def seed_turn(db, turn_id="t1", session="s1", channel="voice"):
    db.start_session(session, "claude-haiku-5-5", "claude-sonnet-5-5", channel)
    db.start_turn(turn_id, session, "what time is it")
    db.add_tool_call(turn_id, tool="get_time", args={}, risk="SAFE", decision="ran", duration_ms=2, error=None)
    db.add_tool_call(turn_id, tool="run_shell", args={"command": "ls"}, risk="CONFIRM", decision="confirmed", duration_ms=5, error=None)
    db.add_tool_call(turn_id, tool="fetch_page", args={"url": "x"}, risk="SAFE", decision="error", duration_ms=1, error="boom")
    db.add_api_call(
        turn_id,
        model="claude-haiku-5-5",
        attempt=1,
        status="error:429",
        duration_ms=10,
        ttft_ms=None,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0,
    )
    db.add_api_call(
        turn_id,
        model="claude-haiku-5-5",
        attempt=2,
        status="ok",
        duration_ms=300,
        ttft_ms=100,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=900,
        cache_write_tokens=0,
        cost_usd=0.001,
    )
    db.add_api_call(
        turn_id,
        model="claude-sonnet-5-5",
        attempt=1,
        status="ok",
        duration_ms=800,
        ttft_ms=200,
        input_tokens=50,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=50,
        cost_usd=0.01,
        is_delegate=True,
    )
    db.add_timing(turn_id, "api_call", 300)
    db.add_timing(turn_id, "ttft", 100)
    db.add_timing(turn_id, "tool:get_time", 2)
    db.finish_turn(turn_id, final_reply="It is noon.", tainted=True, total_ms=350, error=None)


def test_turn_summary_aggregates_the_detail_tables(db):
    seed_turn(db)
    summary = db.add_turn_summary("t1")
    assert summary["channel"] == "voice" and summary["model"] == "claude-haiku-5-5"
    assert summary["tool_calls"] == 3 and summary["tool_errors"] == 1 and summary["confirmations"] == 1 and summary["denied"] == 0
    assert summary["api_calls"] == 3 and summary["api_errors"] == 1
    assert summary["input_tokens"] == 150 and summary["output_tokens"] == 220 and summary["cache_read_tokens"] == 900
    assert summary["cost_usd"] == pytest.approx(0.011) and summary["tainted"] == 1
    assert json.loads(summary["stages_json"]) == {"api_call": 300.0, "ttft": 100.0, "tool:get_time": 2.0}
    assert json.loads(summary["tools_json"]) == ["get_time", "run_shell", "fetch_page"]
    db.add_timing("t1", "first_audio", 900)
    assert json.loads(db.add_turn_summary("t1")["stages_json"])["first_audio"] == 900.0  # rewritten in place
    assert db.conn.execute("SELECT COUNT(*) FROM turn_summaries").fetchone()[0] == 1
    assert db.add_turn_summary("missing") is None


def test_api_call_rows_carry_category_request_id_and_composition(db):
    seed_turn(db)
    call_id = db.add_api_call(
        "t1",
        model="m",
        attempt=1,
        status="error:529",
        duration_ms=1,
        ttft_ms=None,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0,
        error_category="overloaded",
        request_id="req_1",
        final_failure=True,
    )
    row = db.conn.execute("SELECT * FROM api_calls WHERE id = ?", (call_id,)).fetchone()
    assert row["error_category"] == "overloaded" and row["request_id"] == "req_1" and row["final_failure"] == 1
    db.set_api_call_composition(call_id, {"persona": 10, "prompt_tokens": 10}, 7)
    row = db.conn.execute("SELECT composition_json, thinking_tokens FROM api_calls WHERE id = ?", (call_id,)).fetchone()
    assert json.loads(row["composition_json"])["persona"] == 10 and row["thinking_tokens"] == 7


def test_voice_events(db):
    db.add_voice_event("trigger", "wake")
    db.add_voice_event("transcript_dropped", None)
    rows = db.conn.execute("SELECT kind, detail FROM voice_events ORDER BY id").fetchall()
    assert [(r["kind"], r["detail"]) for r in rows] == [("trigger", "wake"), ("transcript_dropped", None)]


def test_migration_10_backfills_categories_and_summaries(paths):
    """An older database (schema 9) gets categories, final flags and a summary per turn when it is opened."""
    path = paths.db_file
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    for version, sql in MIGRATIONS:
        if version >= 10:
            break
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version VALUES (?, 'x')", (version,))
    conn.execute(
        "INSERT INTO sessions (id, started_at, model_main, model_delegate, channel) VALUES ('s1', '2026-10-01T08:00:00.000Z', 'm', 'd', 'telegram')"
    )
    conn.execute(
        "INSERT INTO turns (id, session_id, started_at, user_input, final_reply, tainted, total_ms) VALUES ('t1', 's1', '2026-10-01T08:00:01.000Z', 'q', 'a', 0, 500)"
    )
    conn.execute(
        "INSERT INTO turns (id, session_id, started_at, user_input, tainted, error) VALUES ('t2', 's1', '2026-10-01T08:05:00.000Z', 'q2', 0, 'timeout')"
    )
    api = "INSERT INTO api_calls (turn_id, model, attempt, status, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, ?)"
    conn.execute(api, ("t1", "m", 1, "error:429", "2026-10-01T08:00:02.000Z"))
    conn.execute(api, ("t1", "m", 2, "ok", "2026-10-01T08:00:03.000Z"))
    conn.execute(api, ("t2", "m", 1, "error:APITimeoutError:read", "2026-10-01T08:05:01.000Z"))
    conn.execute(api, ("t2", "m", 2, "error:APITimeoutError:read", "2026-10-01T08:05:02.000Z"))
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.schema_version() == 12  # 10, 11 and 12 are applied together
        rows = db.conn.execute(
            "SELECT turn_id, attempt, error_category, final_failure FROM api_calls WHERE status != 'ok' ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("t1", 1, "rate_limit", 0), ("t2", 1, "timeout", 0), ("t2", 2, "timeout", 1)]
        summaries = {r["turn_id"]: r for r in db.conn.execute("SELECT * FROM turn_summaries")}
        assert set(summaries) == {"t1", "t2"}
        assert summaries["t1"]["channel"] == "telegram" and summaries["t1"]["api_calls"] == 2 and summaries["t1"]["api_errors"] == 1
        assert summaries["t2"]["error"] == "timeout" and summaries["t2"]["api_errors"] == 2
        assert db.backfill_turn_summaries() == 0  # idempotent
    finally:
        db.close()


# --- end to end through the Brain --------------------------------------------------------------------------


def test_brain_records_categories_and_composition_for_every_call(config, db, logs):
    from tests.conftest import RecordingSink, text_message, tool_message
    from tests.unit.test_brain import make_brain

    brain, _client = make_brain(config, db, [tool_message("get_time", {}), text_message("It is noon.", input_tokens=900, output_tokens=12)])
    from jarvis.observability.timing import TurnTimer

    outcome = brain.run_turn(
        system=[{"type": "text", "text": "<persona>\nbe nice\n</persona>"}, {"type": "text", "text": "<memory>\nlikes tea\n</memory>"}],
        messages=[{"role": "user", "content": "what time is it"}],
        sink=RecordingSink(),
        turn_id=brain.test_turn_id,
        timer=TurnTimer(brain.test_turn_id, db),
    )
    assert outcome.text == "It is noon."
    rows = db.conn.execute(
        "SELECT status, composition_json, thinking_tokens FROM api_calls WHERE turn_id = ? ORDER BY id", (brain.test_turn_id,)
    ).fetchall()
    assert len(rows) == 2 and all(r["status"] == "ok" for r in rows)
    second = json.loads(rows[1]["composition_json"])
    assert second["prompt_tokens"] == 900 and second["exact"] is False  # the fake client has no count_tokens
    assert second["tool_results"] > 0 and second["conversation_history"] > 0 and second["persona"] > 0
    assert sum(second[c] for c in composition.COMPONENTS) == 900
    assert brain._pending_compositions == []


def test_brain_marks_the_final_failure(config, db, logs):
    from jarvis.exceptions import JarvisAPIError
    from jarvis.observability.timing import TurnTimer
    from tests.conftest import RecordingSink, api_error
    from tests.unit.test_brain import make_brain

    brain, _client = make_brain(config, db, [api_error(anthropic.RateLimitError, 429)] * config.loop.api_max_attempts)
    with pytest.raises(JarvisAPIError):
        brain.run_turn(
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            sink=RecordingSink(),
            turn_id=brain.test_turn_id,
            timer=TurnTimer(brain.test_turn_id, db),
        )
    rows = db.conn.execute(
        "SELECT error_category, final_failure, request_id FROM api_calls WHERE turn_id = ? ORDER BY id", (brain.test_turn_id,)
    ).fetchall()
    assert [r["error_category"] for r in rows] == ["rate_limit"] * config.loop.api_max_attempts
    assert [r["final_failure"] for r in rows] == [0] * (config.loop.api_max_attempts - 1) + [1]
    assert rows[0]["request_id"] == "req_test"
