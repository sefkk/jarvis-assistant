from jarvis.observability import reports


def seed(db):
    db.start_session("s1", "claude-haiku-5-5", "claude-sonnet-5-5")
    db.start_turn("t1", "s1", "what time is it")
    db.add_message("t1", "user", "what time is it")
    db.add_message(
        "t1",
        "assistant",
        [{"type": "thinking", "thinking": "hmm", "signature": "s"}, {"type": "tool_use", "id": "a", "name": "get_time", "input": {}}],
    )
    db.add_message("t1", "user", [{"type": "tool_result", "tool_use_id": "a", "content": "Local time: noon"}])
    db.add_message("t1", "assistant", [{"type": "text", "text": "It is noon."}])
    db.add_tool_call("t1", tool="get_time", args={}, risk="SAFE", decision="ran", duration_ms=2, error=None)
    db.add_tool_call("t1", tool="run_shell", args={"command": "ls"}, risk="CONFIRM", decision="denied", duration_ms=1, error="denied")
    db.add_api_call(
        "t1",
        model="claude-haiku-5-5",
        attempt=1,
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
        "t1",
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
    db.add_api_call(
        "t1",
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
    db.add_timing("t1", "api_call", 300)
    db.add_timing("t1", "tool:get_time", 2)
    db.finish_turn("t1", final_reply="It is noon.", tainted=True, total_ms=350, error=None)
    db.end_session("s1")


def test_stats_report(db):
    seed(db)
    text = reports.stats_report(db, 7)
    assert "1 session(s), 1 turn(s), 0 failed, 1 tainted, average 350 ms" in text
    assert "api_call" in text and "tool:get_time" in text
    assert "run_shell" in text and "100%" in text  # the denied call counts as failed
    assert "get_time" in text and "0%" in text
    assert "No tool calls" not in text


def test_usage_report(db):
    seed(db)
    text = reports.usage_report(db, 7)
    assert "claude-haiku-5-5" in text and "claude-sonnet-5-5" in text
    assert "90%" in text  # haiku cache hit: 900 / (100 + 900)
    assert "$0.0110" in text and "1 failed call(s)" in text
    assert "Costs are 0" not in text
    assert reports.usage_report(db, 0).startswith("No API calls") or "Total" in reports.usage_report(db, 0)


def test_export_session(db):
    seed(db)
    md = reports.export_session(db, "s1")
    assert md.startswith("# Jarvis session s1")
    assert "## Turn 1" in md and "(tainted)" in md
    assert "**You:** what time is it" in md
    assert "> thinking: hmm" in md and "> tool call `get_time` {}" in md
    assert "> tool result:" in md and "> Local time: noon" in md
    assert "It is noon." in md and "_350 ms_" in md
    import pytest

    with pytest.raises(ValueError):
        reports.export_session(db, "nope")
