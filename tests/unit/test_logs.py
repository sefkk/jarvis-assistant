"""The dashboard's Logs queries (sessions, FTS search, session and turn detail), the audit log reader
and filters, and the pages. Hostile content in transcripts, tool arguments, results and audit lines must come
out escaped (the security suite repeats the escaping checks against the real pages)."""

from __future__ import annotations

import json

import pytest

from jarvis.dashboard import audit as audit_mod
from jarvis.dashboard import logs
from jarvis.dashboard.analytics import Filters
from jarvis.dashboard.queries import connect_readonly
from jarvis.dashboard.ui_text import UI
from jarvis.observability.db import Database
from tests.unit.test_analytics import NOW, iso
from tests.unit.test_dashboard import login, make_client, quiet_services  # noqa: F401 - fixture

EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def seed(paths):
    db = Database(paths.db_file)
    at = NOW.replace(hour=10)
    db.conn.execute(
        "INSERT INTO sessions (id, started_at, ended_at, model_main, model_delegate, channel) VALUES ('s1', ?, ?, 'claude-haiku-5-5', 'claude-sonnet-5-5', 'voice')",
        (iso(at), iso(at.replace(hour=11))),
    )
    db.conn.execute(
        "INSERT INTO sessions (id, started_at, model_main, model_delegate, channel) VALUES ('s2', ?, 'claude-haiku-5-5', 'claude-sonnet-5-5', 'telegram')",
        (iso(at.replace(hour=12)),),
    )
    db.conn.execute(
        "INSERT INTO turns (id, session_id, started_at, user_input, final_reply, tainted, total_ms, raw_transcript) VALUES ('t1', 's1', ?, ?, ?, 1, 1800, ?)",
        (iso(at), "hava nasıl " + EVIL, "Hava güneşli. " + EVIL, "hava nasıl?"),
    )
    db.conn.execute(
        "INSERT INTO turns (id, session_id, started_at, user_input, final_reply, tainted, total_ms, error) VALUES ('t2', 's1', ?, 'saat kaç', NULL, 0, 300, 'timeout')",
        (iso(at.replace(minute=5)),),
    )
    db.conn.execute(
        "INSERT INTO turns (id, session_id, started_at, user_input, final_reply, tainted, total_ms) VALUES ('t3', 's2', ?, 'mesaj', 'tamam', 0, 100)",
        (iso(at.replace(hour=12)),),
    )
    # the FTS index is filled by triggers; t1's transcript
    db.add_message("t1", "user", "hava nasıl " + EVIL)
    db.add_message(
        "t1",
        "assistant",
        [
            {"type": "thinking", "thinking": "think " + EVIL, "signature": "s"},
            {"type": "tool_use", "id": "tu1", "name": "get_weather", "input": {"place": EVIL}},
        ],
    )
    db.add_message("t1", "user", [{"type": "tool_result", "tool_use_id": "tu1", "content": "22 °C " + EVIL + " x" * 700}])
    db.add_message("t1", "assistant", [{"type": "tool_use", "id": "tu2", "name": "remember", "input": {"fact": "likes sun"}}])
    db.add_message("t1", "user", [{"type": "tool_result", "tool_use_id": "tu2", "content": "denied by the user", "is_error": True}])
    db.add_message("t1", "assistant", [{"type": "text", "text": "Hava güneşli. " + EVIL}])
    db.add_tool_call("t1", tool="get_weather", args={"place": EVIL}, risk="SAFE", decision="ran", duration_ms=120, error=None)
    db.add_tool_call(
        "t1", tool="remember", args={"fact": "likes sun"}, risk="SAFE", decision="denied", duration_ms=1, error="denied: " + EVIL
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
        error_category="rate_limit",
        request_id="req_a",
    )
    db.add_api_call(
        "t1",
        model="claude-haiku-5-5",
        attempt=2,
        status="ok",
        duration_ms=900,
        ttft_ms=300,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=800,
        cache_write_tokens=0,
        cost_usd=0.002,
        request_id="req_b",
    )
    for stage, ms in (("stt", 400), ("api_call", 900), ("ttft", 300), ("tool:get_weather", 120), ("first_audio", 1100)):
        db.add_timing("t1", stage, ms)
    db.add_turn_summary("t1")
    db.add_turn_summary("t2")
    db.add_turn_summary("t3")
    db.close()
    audit = paths.logs_dir / "audit.jsonl"
    lines = [
        {
            "ts": iso(at),
            "session_id": "s1",
            "turn_id": "t1",
            "tool": "get_weather",
            "args": {"place": EVIL},
            "risk": "SAFE",
            "tainted": False,
            "taint_sources": [],
            "decision": "ran",
            "duration_ms": 120.0,
            "result_summary": {"length": 20, "head": "22 °C"},
        },
        {
            "ts": iso(at.replace(minute=1)),
            "session_id": "s1",
            "turn_id": "t1",
            "tool": "remember",
            "args": {"fact": "likes sun"},
            "risk": "SAFE",
            "tainted": True,
            "taint_sources": ["web:example.com"],
            "decision": "denied",
        },
        {"ts": iso(at.replace(hour=12)), "session_id": None, "turn_id": None, "tool": "voice", "decision": "trigger", "kind": "wake"},
        {
            "ts": iso(at.replace(hour=12, minute=2)),
            "session_id": "s2",
            "turn_id": "t3",
            "tool": "telegram",
            "decision": "ignored",
            "reason": "rate_limited",
        },
        "not json at all",
    ]
    audit.write_text("\n".join(json.dumps(l) if isinstance(l, dict) else l for l in lines) + "\n", encoding="utf-8")


@pytest.fixture
def seeded(paths):
    seed(paths)
    conn = connect_readonly(paths.db_file)
    yield conn
    conn.close()


def filters(**kw) -> Filters:
    base = {"range_key": "7d", "channel": None, "model": None, "start": None, "end": None, "now": NOW}
    base.update(kw)
    return Filters.from_query(**base)


# --- sessions and search ------------------------------------------------------------------------------------


def test_sessions_list(seeded):
    table = logs.sessions(seeded, filters())
    assert table["total"] == 2 and [s["id"] for s in table["rows"]] == ["s2", "s1"]
    s1 = table["rows"][1]
    assert s1["turns"] == 2 and s1["errors"] == 1 and s1["cost"] == pytest.approx(0.002) and s1["channel_label"] == "Voice"
    assert s1["first_input"].startswith("hava nasıl")
    assert logs.sessions(seeded, filters(channel="telegram"))["total"] == 1
    assert logs.sessions(seeded, filters(range_key="custom", start="2026-01-01", end="2026-01-02"))["total"] == 0


def test_fts_query_is_always_safe():
    assert logs.fts_query("hava nasıl") == '"hava"* ("nasıl"* OR "nasil"*)'
    assert logs.fts_query("Istanbul") == '("istanbul"* OR "ıstanbul"*)'
    assert logs.fts_query("iii") == '("iii"* OR "ıii"* OR "iıi"* OR "ııi"* OR "iiı"* OR "ıiı"* OR "iıı"* OR "ııı"*)'
    assert logs.fts_query("iiii") == '("iiii"* OR "ıııı"*)'
    assert logs.fts_query('a:b OR NOT "x') == '"a"* "b"* "OR"* "NOT"* "x"*'
    assert logs.fts_query("   ") is None and logs.fts_query("()*") is None
    assert len(logs.fts_query(" ".join(["w"] * 40)).split()) == 12


def test_search_matches_with_folded_diacritics_and_highlights_safely(seeded):
    results = logs.search(seeded, filters(), "nasil")
    assert results["total"] == 1 and results["rows"][0]["turn_id"] == "t1"
    html = str(results["rows"][0]["input"])
    assert "<mark>nasıl</mark>" in html and "<script>" not in html and "&lt;script&gt;" in html
    assert logs.search(seeded, filters(), "gunesli")["rows"][0]["reply"].startswith("Hava <mark>") or "<mark>güneşli</mark>" in str(
        logs.search(seeded, filters(), "gunesli")["rows"][0]["reply"]
    )
    assert logs.search(seeded, filters(), "saat")["total"] == 1
    assert logs.search(seeded, filters(channel="telegram"), "saat")["total"] == 0
    for weird in ('"unbalanced', "a:b OR NOT", "*", 'x" y', "NEAR(a b)"):
        logs.search(seeded, filters(), weird)  # never raises
    assert logs.search(seeded, filters(), "")["total"] == 0


def test_highlight_escapes_everything_but_the_markers():
    out = str(logs.highlight("\x02<b>\x03 & x"))
    assert out == "<mark>&lt;b&gt;</mark> &amp; x"


# --- session and turn detail -----------------------------------------------------------------------------------


def test_session_detail_and_export(seeded):
    s = logs.session_detail(seeded, "s1")
    assert s["total"] == 2 and s["channel_label"] == "Voice" and s["cost"] == pytest.approx(0.002) and s["errors"] == 1
    t1 = s["turns"][0]
    assert t1["tools"] == ["get_weather", "remember"] and t1["denied"] == 1 and t1["tainted"]
    assert s["turns"][1]["error"] == "timeout" and s["turns"][1]["reply"] == ""
    assert logs.session_detail(seeded, "nope") is None
    md = logs.export_markdown(seeded, "s1")
    assert md.startswith("# Jarvis session s1") and "get_weather" in md and "## Turn 2" in md
    assert logs.export_markdown(seeded, "nope") is None


def test_turn_detail_pairs_tool_uses_with_results_and_calls(seeded):
    d = logs.turn_detail(seeded, "t1", audit_entries=[{"taint_sources": ["web:example.com"], "decision": "denied"}])
    assert d.turn["channel_label"] == "Voice" and d.turn["raw_transcript"] == "hava nasıl?"
    kinds = [e.kind for e in d.events]
    assert kinds == ["user", "thinking", "tool", "tool", "text"]
    weather, remember = d.events[2].tool, d.events[3].tool
    assert (
        weather.name == "get_weather"
        and weather.decision == "ran"
        and weather.duration_ms == 120
        and json.loads(weather.args)["place"] == EVIL
    )
    assert weather.result.startswith("22 °C") and weather.result_long and weather.result_preview.endswith("…") and not weather.is_error
    assert (
        remember.decision == "denied"
        and remember.is_error
        and remember.result == "denied by the user"
        and remember.error.startswith("denied: ")
    )
    assert d.thinking_count == 1 and d.has_transcript
    assert [c["status"] for c in d.api_calls] == ["error:429", "ok"] and d.cost == pytest.approx(0.002)
    assert d.tokens == {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 800, "cache_write_tokens": 0}
    assert [t["stage"] for t in d.timeline] == ["stt", "api_call", "ttft", "tool:get_weather", "first_audio"]
    assert (
        d.timeline[0]["pct"] == pytest.approx(400 / 1800 * 100, abs=0.1)
        and d.timeline[0]["cls"] == "stt"
        and d.timeline[3]["cls"] == "tools"
    )
    assert [c.name for c in d.confirmations] == ["remember"] and d.taint_sources == ["web:example.com"]
    assert logs.turn_detail(seeded, "missing") is None


def test_turn_detail_without_saved_transcript_falls_back_to_the_turn_row(seeded):
    d = logs.turn_detail(seeded, "t2")
    assert not d.has_transcript and [e.kind for e in d.events] == ["user"] and d.events[0].text == "saat kaç"
    assert d.turn["error"] == "timeout" and d.timeline == [] and d.api_calls == []


def test_decision_labels():
    assert logs.decision_label("ran") == UI.DECISION_RAN and logs.decision_label("validation_error") == UI.DECISION_ERROR
    assert logs.decision_label("transcript_dropped") == "Transcript dropped" and logs.decision_label(None) == UI.DECISION_OTHER
    assert (
        logs.decision_class("blocked") == "failed"
        and logs.decision_class("denied") == "stopped"
        and logs.decision_class("trigger") == "muted"
    )


# --- audit ------------------------------------------------------------------------------------------------------


def test_audit_log_parses_filters_and_caches(paths, seeded):
    log = audit_mod.AuditLog(paths.logs_dir / "audit.jsonl")
    entries = log.entries()
    assert len(entries) == 4 and entries[0].tool == "telegram"  # newest first, the bad line skipped
    assert entries[-1].details == {
        "args": {"place": EVIL},
        "taint_sources": [],
        "duration_ms": 120.0,
        "result_summary": {"length": 20, "head": "22 °C"},
    }
    assert log.entries() is entries  # cached while the file is unchanged
    channels = audit_mod.session_channels(seeded)
    assert channels == {"s1": "voice", "s2": "telegram"}
    f = filters(range_key="30d")
    table = audit_mod.filter_entries(
        entries, start_iso=f.start_iso, end_iso=f.end_iso, tool=None, decision=None, tainted=None, channel=None, channels=channels
    )
    assert (
        table["total"] == 4 and table["rows"][1].channel == "voice" and table["rows"][2].channel_label == "Voice"
    )  # voice tool, no session
    denied = audit_mod.filter_entries(
        entries,
        start_iso=f.start_iso,
        end_iso=f.end_iso,
        tool=None,
        decision=UI.DECISION_DENIED,
        tainted=None,
        channel=None,
        channels=channels,
    )
    assert [e.tool for e in denied["rows"]] == ["remember"] and denied["rows"][0].decision_label == UI.DECISION_DENIED
    other = audit_mod.filter_entries(
        entries,
        start_iso=f.start_iso,
        end_iso=f.end_iso,
        tool=None,
        decision=UI.DECISION_OTHER,
        tainted=None,
        channel=None,
        channels=channels,
    )
    assert sorted(e.decision for e in other["rows"]) == ["ignored", "trigger"]
    tainted = audit_mod.filter_entries(
        entries, start_iso=f.start_iso, end_iso=f.end_iso, tool=None, decision=None, tainted="yes", channel=None, channels=channels
    )
    assert [e.tool for e in tainted["rows"]] == ["remember"]
    by_channel = audit_mod.filter_entries(
        entries, start_iso=f.start_iso, end_iso=f.end_iso, tool=None, decision=None, tainted=None, channel="telegram", channels=channels
    )
    assert [e.tool for e in by_channel["rows"]] == ["telegram"]
    assert log.for_turn("t1")[0]["tool"] == "get_weather" and log.tools() == ["get_weather", "remember", "telegram", "voice"]
    (paths.logs_dir / "audit.jsonl").write_text("", encoding="utf-8")
    assert log.entries() == []
    assert audit_mod.AuditLog(paths.logs_dir / "missing.jsonl").entries() == []


# --- the pages -----------------------------------------------------------------------------------------------------


def test_logs_pages_render_and_escape(config, paths, seeded, quiet_services):
    from jarvis.dashboard.state import Dashboard

    client, store = make_client(config, paths, Dashboard(config, paths, auto_full_check=False))
    login(client, store)
    index = client.get("/logs?range=30d")
    assert index.status_code == 200 and "s1" in index.text and "Voice" in index.text and EVIL not in index.text
    search = client.get("/logs?range=30d&q=hava")
    assert search.status_code == 200 and "<mark>hava</mark>" in search.text and EVIL not in search.text and "&lt;script&gt;" in search.text
    assert client.get("/logs?range=30d&q=%22unbalanced%20OR%20NOT").status_code == 200
    session = client.get("/logs/sessions/s1")
    assert (
        session.status_code == 200 and "get_weather" in session.text and EVIL not in session.text and "Export as Markdown" in session.text
    )
    export = client.get("/logs/sessions/s1/export.md")
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/markdown")
    assert export.headers["content-disposition"] == 'attachment; filename="jarvis-session-s1.md"'
    assert "<script>" in export.text  # markdown export is the raw transcript, served as a download, never rendered
    assert client.get("/logs/sessions/nope/export.md").status_code == 404
    turn = client.get("/logs/turns/t1")
    assert turn.status_code == 200
    html = turn.text
    assert (
        EVIL not in html and html.count("&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;") >= 5
    )  # input, thinking, args, result, reply, error
    for text in (
        UI.TURN_YOU_SAID,
        UI.TURN_HEARD,
        UI.TURN_REPLIED,
        UI.TURN_SHOW_THINKING,
        UI.TURN_TIMELINE,
        UI.TURN_API_CALLS,
        "req_b",
        "web:example.com",
        UI.TURN_SHOW_FULL,
    ):
        assert text in html, text
    assert client.get("/logs/turns/nope").status_code == 200 and UI.EMPTY in client.get("/logs/turns/nope").text
    audit = client.get("/audit?range=30d")
    assert audit.status_code == 200 and EVIL not in audit.text and "&lt;script&gt;" in audit.text and "get_weather" in audit.text
    assert client.get("/audit?range=30d&decision=Denied&tainted=yes&tool=remember&channel=voice").text.count("<tr") == 2
    assert client.get("/audit?range=30d&decision=Nonsense&tainted=maybe").status_code == 200


def test_logs_without_a_database(config, paths, quiet_services):
    from jarvis.dashboard.state import Dashboard

    paths.db_file.unlink(missing_ok=True)
    board = Dashboard(config, paths, auto_full_check=False)  # creates and migrates an empty database
    client, store = make_client(config, paths, board)
    login(client, store)
    assert client.get("/logs").status_code == 200 and client.get("/logs?q=x").status_code == 200
    assert client.get("/audit").status_code == 200 and UI.AUDIT_FILE_MISSING in client.get("/audit").text
