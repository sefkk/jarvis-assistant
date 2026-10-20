import json

from jarvis.observability.db import Database


def test_schema_created_once(paths):
    db = Database(paths.db_file)
    assert db.schema_version() == 12
    tables = {r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "sessions",
        "turns",
        "messages",
        "tool_calls",
        "api_calls",
        "timings",
        "schema_version",
        "workouts",
        "workout_sets",
        "timers",
        "health_imports",
        "api_cache",
        "kv",
        "voice_follow_ups",
    } <= tables
    db.close()
    again = Database(paths.db_file)  # re-opening must not re-apply migrations
    assert again.conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 12
    again.close()


def test_transcript_store_is_redacted(db):
    db.start_session("s1", "main", "delegate")
    db.start_turn("t1", "s1", "my key is sk-ant-api03-abcdefghijklmnop and mail me at kerem@example.com")
    db.add_message("t1", "user", [{"type": "tool_result", "content": "token ya29.abc.def"}])
    db.add_tool_call(
        "t1",
        tool="write_file",
        args={"content": "ANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzz"},
        risk="CONFIRM",
        decision="confirmed",
        duration_ms=1,
        error=None,
    )
    db.finish_turn("t1", final_reply="sent to kerem@example.com", tainted=False, total_ms=1, error=None)
    rows = db.conn.execute("SELECT user_input, final_reply FROM turns").fetchone()
    assert "sk-ant-api03" not in rows["user_input"] and "kerem@example.com" not in rows["user_input"]
    assert rows["final_reply"] == "sent to ***@redacted"
    assert "ya29.abc" not in db.conn.execute("SELECT content_json FROM messages").fetchone()["content_json"]
    assert "sk-ant-api03" not in db.conn.execute("SELECT args_json FROM tool_calls").fetchone()["args_json"]


def test_writes_round_trip(db):
    db.start_session("s1", "main", "delegate")
    db.start_turn("t1", "s1", "what time is it")
    db.add_message("t1", "user", "what time is it")
    db.add_message("t1", "assistant", [{"type": "text", "text": "noon"}])
    db.add_tool_call("t1", tool="get_time", args={}, risk="SAFE", decision="ran", duration_ms=1.5, error=None)
    db.add_api_call(
        "t1",
        model="m",
        attempt=1,
        status="ok",
        duration_ms=100.0,
        ttft_ms=20.0,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.001,
    )
    db.add_timing("t1", "api_call", 100.0)
    db.finish_turn("t1", final_reply="noon", tainted=False, total_ms=120.0, error=None)
    db.end_session("s1")

    turn = db.conn.execute("SELECT * FROM turns WHERE id='t1'").fetchone()
    assert turn["final_reply"] == "noon" and turn["total_ms"] == 120.0 and turn["tainted"] == 0
    msgs = db.conn.execute("SELECT role, content_json FROM messages WHERE turn_id='t1' ORDER BY id").fetchall()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert json.loads(msgs[1]["content_json"])[0]["text"] == "noon"
    assert db.conn.execute("SELECT decision FROM tool_calls").fetchone()["decision"] == "ran"
    assert db.conn.execute("SELECT ttft_ms FROM api_calls").fetchone()["ttft_ms"] == 20.0
    assert db.conn.execute("SELECT ended_at FROM sessions").fetchone()["ended_at"] is not None
    assert db.spent_since("2000-01-01T00:00:00") == 0.001


def test_closed_database_reopens_on_next_use(paths):
    """The power tools close the connection before the Mac sleeps; the same process goes on after the
    wake, so the next query must reopen it instead of failing."""
    db = Database(paths.db_file)
    db.kv_set("k", "v")
    db.close()
    assert db.closed
    assert db.kv_get("k") == "v"  # reopened transparently
    assert not db.closed
    db.close()
    db.close()  # idempotent
