"""The dashboard's Analytics queries on a synthetic database, the filters, and the page itself."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.dashboard import analytics
from jarvis.dashboard.analytics import Filters
from jarvis.dashboard.queries import connect_readonly
from jarvis.dashboard.ui_text import UI
from jarvis.observability.db import Database
from tests.unit.test_dashboard import login, make_client, quiet_services  # noqa: F401 - fixture

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone(timedelta(hours=3)))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _insert_turn(
    db,
    *,
    turn_id,
    session,
    channel,
    at,
    user_input,
    total_ms,
    tools=(),
    tainted=False,
    error=None,
    api=(),
    stages=None,
    model="claude-haiku-5-5",
):
    c = db.conn
    if c.execute("SELECT 1 FROM sessions WHERE id = ?", (session,)).fetchone() is None:
        c.execute(
            "INSERT INTO sessions (id, started_at, model_main, model_delegate, channel) VALUES (?, ?, 'm', 'd', ?)",
            (session, iso(at), channel),
        )
    c.execute(
        "INSERT INTO turns (id, session_id, started_at, user_input, final_reply, tainted, total_ms, error) VALUES (?, ?, ?, ?, 'ok', ?, ?, ?)",
        (turn_id, session, iso(at), user_input, int(tainted), total_ms, error),
    )
    for tool, decision, risk, ms in tools:
        c.execute(
            "INSERT INTO tool_calls (turn_id, tool, args_json, risk, decision, duration_ms, error, created_at) VALUES (?, ?, '{}', ?, ?, ?, ?, ?)",
            (turn_id, tool, risk, decision, ms, "boom" if decision == "error" else None, iso(at)),
        )
    for status, inp, cr, cw, out, cost, category, final, comp, is_delegate in api:
        c.execute(
            "INSERT INTO api_calls (turn_id, model, attempt, status, duration_ms, ttft_ms, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, cost_usd, is_delegate, created_at, error_category, final_failure, composition_json)"
            " VALUES (?, ?, 1, ?, 500, 200, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                model if not is_delegate else "claude-sonnet-5-5",
                status,
                inp,
                out,
                cr,
                cw,
                cost,
                int(is_delegate),
                iso(at),
                category,
                int(final),
                json.dumps(comp) if comp else None,
            ),
        )
    for stage, ms in (stages or {}).items():
        c.execute("INSERT INTO timings (turn_id, stage, duration_ms) VALUES (?, ?, ?)", (turn_id, stage, ms))
    db.add_turn_summary(turn_id)


@pytest.fixture
def seeded(paths):
    """Three days of synthetic traffic: a CLI day with a retried call, a voice day with tools, one old turn."""
    db = Database(paths.db_file)
    day0 = NOW.replace(hour=9, minute=0)
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
        turn_id="t1",
        session="s1",
        channel="cli",
        at=day0,
        user_input="first question",
        total_ms=1000,
        api=[("error:429", 0, 0, 0, 0, 0, "rate_limit", False, None, False), ("ok", 320, 1000, 0, 100, 0.01, None, False, comp, False)],
        stages={"api_call": 800, "ttft": 300},
    )
    _insert_turn(
        db,
        turn_id="t2",
        session="s1",
        channel="cli",
        at=day0 + timedelta(minutes=5),
        user_input="<b>second</b>",
        total_ms=3000,
        tools=[("run_shell", "confirmed", "CONFIRM", 50), ("fetch_page", "error", "SAFE", 20), ("remember", "denied", "SAFE", 1)],
        api=[("ok", 500, 500, 0, 200, 0.02, None, False, comp, False), ("ok", 100, 0, 0, 50, 0.05, None, False, None, True)],
        stages={"api_call": 2000, "ttft": 400, "tool:run_shell": 50, "tool:fetch_page": 20},
        tainted=True,
    )
    day1 = day0 - timedelta(days=1)
    _insert_turn(
        db,
        turn_id="v1",
        session="s2",
        channel="voice",
        at=day1,
        user_input="saat kaç",
        total_ms=2500,
        api=[("ok", 200, 800, 0, 40, 0.004, None, False, comp, False)],
        stages={"stt": 400, "api_call": 900, "ttft": 300, "first_audio": 1200},
    )
    _insert_turn(
        db,
        turn_id="v2",
        session="s2",
        channel="voice",
        at=day1 + timedelta(seconds=30),
        user_input="saat kaç",
        total_ms=2000,
        api=[("ok", 200, 800, 0, 40, 0.004, None, False, comp, False)],
        stages={"first_audio": 900},
    )
    _insert_turn(
        db,
        turn_id="v3",
        session="s2",
        channel="voice",
        at=day1 + timedelta(seconds=60),
        user_input="hayır yarın",
        total_ms=2000,
        api=[("error:APITimeoutError:read", 0, 0, 0, 0, 0, "timeout", True, None, False)],
        error="timeout",
    )
    _insert_turn(
        db,
        turn_id="old",
        session="s3",
        channel="telegram",
        at=NOW - timedelta(days=40),
        user_input="old",
        total_ms=100,
        api=[("ok", 10, 0, 0, 5, 1.0, None, False, None, False)],
    )
    for kind, detail, when in (
        ("trigger", "wake", day1),
        ("trigger", "hotkey", day1),
        ("transcribed", "tr", day1),
        ("transcript_dropped", None, day1),
    ):
        db.conn.execute("INSERT INTO voice_events (created_at, kind, detail) VALUES (?, ?, ?)", (iso(when), kind, detail))
    for outcome in ("turn", "silence", "noise"):
        db.conn.execute("INSERT INTO voice_follow_ups (created_at, outcome, window_s) VALUES (?, ?, 6)", (iso(day1), outcome))
    db.conn.execute(
        "INSERT INTO external_calls (service, operation, units, cached, status, created_at) VALUES ('elevenlabs', 'v3', 2000, 0, 'ok', ?)",
        (iso(day1),),
    )
    db.conn.execute(
        "INSERT INTO external_calls (service, operation, units, cached, status, created_at) VALUES ('elevenlabs', 'v3', 500, 1, 'ok', ?)",
        (iso(day1),),
    )
    db.conn.execute(
        "INSERT INTO external_calls (service, operation, units, cached, status, created_at) VALUES ('maps', 'places', 1, 0, 'ok', ?)",
        (iso(day0),),
    )
    db.conn.execute(
        "INSERT INTO external_calls (service, operation, units, cached, status, created_at) VALUES ('maps', 'places', 1, 0, 'REQUEST_DENIED', ?)",
        (iso(day0),),
    )
    db.close()
    conn = connect_readonly(paths.db_file)
    yield conn
    conn.close()


def filters(**kw) -> Filters:
    base = {"range_key": "7d", "channel": None, "model": None, "start": None, "end": None, "now": NOW}
    base.update(kw)
    return Filters.from_query(**base)


# --- filters ------------------------------------------------------------------------------------------------


def test_filters_ranges_and_query_string():
    week = filters()
    assert week.range_key == "7d" and week.days()[0] == "2026-10-12" and week.days()[-1] == "2026-10-18" and len(week.days()) == 7
    assert filters(range_key="today").days() == ["2026-10-18"]
    assert len(filters(range_key="30d").days()) == 30
    custom = filters(range_key="custom", start="2026-10-01", end="2026-10-03", channel="voice", model="claude-haiku-5-5")
    assert custom.days() == ["2026-10-01", "2026-10-02", "2026-10-03"] and custom.channel == "voice"
    assert custom.query_string(sort="cost") == "range=custom&start=2026-10-01&end=2026-10-03&channel=voice&model=claude-haiku-5-5&sort=cost"
    assert filters(range_key="bogus").range_key == "7d" and filters(channel="nope").channel is None
    bad = filters(range_key="custom", start="2026-10-05", end="2026-10-01")
    assert bad.days() == ["2026-10-05"]  # an inverted range becomes one day
    assert week.start_iso == "2026-10-11T21:00:00.000Z"  # local midnight (UTC+3) as the stored UTC text


# --- datasets ----------------------------------------------------------------------------------------------


def test_daily_cost_and_providers(seeded, config):
    f = filters()
    cost = analytics.daily_cost(seeded, f, config)
    assert cost["days"][-1] == "2026-10-18" and cost["budget"] == config.budget.daily_budget_usd
    assert cost["anthropic"][-1] == pytest.approx(0.08) and cost["anthropic"][-2] == pytest.approx(0.008)
    assert cost["elevenlabs"][-2] == pytest.approx(2.0 * config.budget.elevenlabs_usd_per_1k_chars)  # cached characters do not count
    assert cost["maps"][-1] == pytest.approx(config.budget.maps_usd_per_call)  # the denied request costs nothing
    providers = analytics.cost_by_provider(seeded, f, config)
    assert providers["Anthropic"] == pytest.approx(0.088) and providers["Google Maps"] == pytest.approx(0.005)
    assert analytics.cost_by_provider(seeded, filters(channel="voice"), config)["Anthropic"] == pytest.approx(0.008)
    assert analytics.daily_cost(seeded, filters(range_key="today"), config)["anthropic"] == [pytest.approx(0.08)]


def test_monthly_total_and_projection(seeded, config):
    m = analytics.monthly(seeded, config, NOW)
    assert m["month"] == "October 2026" and m["days_in_month"] == 31
    assert m["total"] == pytest.approx(0.088 + 0.44 + 0.005)  # the 40-day-old turn is in September
    assert m["projected"] == pytest.approx(m["total"] / (17 + 15 / 24) * 31)


def test_model_usage(seeded):
    rows = analytics.model_usage(seeded, filters())
    by_label = {r["label"]: r for r in rows}
    assert by_label["claude-haiku-5-5"]["calls"] == 4 and by_label["claude-haiku-5-5"]["cost"] == pytest.approx(0.038)
    assert by_label["claude-sonnet-5-5 (delegate)"]["role"] == "delegate" and by_label["claude-sonnet-5-5 (delegate)"]["tokens"] == 150
    assert by_label["claude-haiku-5-5"]["tokens"] == (320 + 1000 + 100) + (500 + 500 + 200) + 2 * (200 + 800 + 40)
    assert analytics.model_usage(seeded, filters(model="claude-sonnet-5-5"))[0]["calls"] == 1


def test_composition_totals_and_series(seeded):
    c = analytics.composition(seeded, filters())
    assert c["calls"] == 4 and c["exact_calls"] == 4
    assert c["parts"]["rules"] == 1200 and c["parts"]["thinking"] == 120 and c["labels"]["memory"] == UI.PART_MEMORY
    assert c["series"]["memory"][-1] == 150 and c["series"]["memory"][-2] == 150 and c["series"]["memory"][0] == 0
    assert analytics.composition(seeded, filters(channel="voice"))["calls"] == 2


def test_cache_stats(seeded):
    c = analytics.cache_stats(seeded, filters())
    assert c["hit_rate"][-1] == pytest.approx(1500 / (320 + 1000 + 500 + 500 + 100) * 100, abs=0.1)
    assert c["saved_tokens"][-1] == 1500 and c["hit_rate"][0] is None
    assert c["total_saved"] == 1500 + 1600 and c["overall_hit_rate"] is not None


def test_latency_and_stage_breakdown(seeded):
    lat = analytics.latency(seeded, filters())
    assert (
        lat["turns"] == 5 and lat["p50"][-1] == 1000 and lat["p90"][-1] == 3000 and lat["p95"][-1] == 3000
    )  # nearest rank of [1000, 3000]
    assert lat["overall_p95"] == 3000 and lat["p95"][0] is None
    assert lat["stages"]["stt"][-2] == pytest.approx(400 / 3, abs=1) and lat["stages"]["first_audio"][-2] == 700
    assert lat["stages"]["tools"][-1] == 35 and lat["stages"]["ttft"][-1] == 350 and lat["stages"]["api"][-1] == 1050
    assert analytics.stage_breakdown({"api_call": 1000, "ttft": 1500}) == {
        "stt": 0.0,
        "ttft": 1000,
        "api": 0.0,
        "tools": 0.0,
        "first_audio": 0.0,
    }
    assert analytics.stage_breakdown({"delegate:tool:x": 5, "tool:y": 7})["tools"] == 12


def test_requests_table_sorting_paging_and_flags(seeded):
    table = analytics.requests(seeded, filters(), sort="latency", direction="desc")
    assert table["total"] == 5 and [r["turn_id"] for r in table["rows"]][:2] == ["t2", "v1"]
    t2 = table["rows"][0]
    assert t2["tools"] == ["run_shell", "fetch_page", "remember"] and t2["cost"] == pytest.approx(0.07)
    assert set(t2["flags"]) == {"tainted", "denied", "confirmed"} and t2["tokens"] == 500 + 500 + 200 + 100 + 50
    assert [s["key"] for s in t2["segments"]] == ["ttft", "api", "tools"] and sum(s["pct"] for s in t2["segments"]) <= 100.1
    assert "retried" in {f for r in table["rows"] if r["turn_id"] == "t1" for f in r["flags"]}
    assert "error" in {f for r in table["rows"] if r["turn_id"] == "v3" for f in r["flags"]}
    cheapest = analytics.requests(seeded, filters(), sort="cost", direction="asc")["rows"][0]
    assert cheapest["turn_id"] == "v3"
    paged = analytics.requests(seeded, filters(), page=2, page_size=2)
    assert paged["pages"] == 3 and paged["page"] == 2 and len(paged["rows"]) == 2
    assert analytics.requests(seeded, filters(), sort="DROP TABLE", direction="x")["sort"] == "time"
    assert analytics.requests(seeded, filters(channel="telegram"))["total"] == 0  # 40 days ago


def test_errors_tools_channels_confirmations(seeded):
    e = analytics.errors(seeded, filters())
    assert (
        e["by_type"][UI.ERR_RATE_LIMIT] == 1 and e["by_type"][UI.ERR_TIMEOUT] == 1 and e["by_type"][UI.ERR_AUTH] == 1
    )  # maps REQUEST_DENIED
    assert e["retries"] == 1 and e["final"] == 2
    top = {(t["provider"], t["error"]): t for t in e["top"]}
    assert top[("Anthropic", "error:429")]["example_turn"] == "t1" and top[("Google Maps", "REQUEST_DENIED")]["type"] == UI.ERR_AUTH
    assert e["failing_tools"] == [{"tool": "fetch_page", "calls": 1, "errors": 1, "rate": 100.0, "last_error": "boom"}]
    tools = {t["tool"]: t for t in analytics.tool_usage(seeded, filters())}
    assert tools["run_shell"]["calls"] == 1 and tools["remember"]["denied"] == 1 and tools["fetch_page"]["error_rate"] == 100.0
    assert analytics.channels(seeded, filters()) == {"CLI": 2, "Voice": 3}
    assert analytics.confirmations(seeded, filters()) == [
        {"reason": UI.CONFIRM_REASON_RISKY, "approved": 1, "denied": 0},
        {"reason": UI.CONFIRM_REASON_TAINTED, "approved": 0, "denied": 1},
    ]


def test_voice_metrics(seeded):
    v = analytics.voice(seeded, filters(), correction_words=["hayır", "no "])
    assert v["triggers"] == 2 and v["triggers_by_kind"] == {"wake": 1, "hotkey": 1}
    assert v["dropped"] == 1 and v["dropped_rate"] == 50.0
    assert v["follow_ups_opened"] == 3 and v["follow_ups_used"] == 1 and v["follow_up_rate"] == pytest.approx(33.3)
    assert v["voice_turns"] == 3 and v["repeats"] == 1 and v["corrections"] == 1  # "saat kaç" twice, then "hayır ..."
    assert v["first_audio_p50"] == 900 and v["first_audio_p90"] == 1200 and v["spoken_turns"] == 2


def test_chart_data_and_empty_data_have_the_same_shape(seeded, config):
    full = analytics.chart_data(seeded, filters(), config, NOW)
    empty = analytics.empty_chart_data(filters(), config)
    assert set(full) == set(empty)
    assert set(full["daily_cost"]) == set(empty["daily_cost"]) and set(full["latency"]) == set(empty["latency"])
    json.dumps(full)  # serializable
    assert analytics.models_available(seeded) == ["claude-haiku-5-5", "claude-sonnet-5-5"]


# --- the page --------------------------------------------------------------------------------------------


def test_analytics_page_renders_tables_and_escapes(config, paths, seeded, quiet_services):
    from jarvis.dashboard.state import Dashboard

    client, store = make_client(config, paths, Dashboard(config, paths, auto_full_check=False))
    login(client, store)
    assert client.get("/analytics/data").status_code == 200
    page = client.get("/analytics?range=30d&sort=latency&dir=desc")
    assert page.status_code == 200
    html = page.text
    assert "&lt;b&gt;second&lt;/b&gt;" in html and "<b>second</b>" not in html
    for text in (
        UI.CHART_DAILY_COST,
        UI.CHART_TOP_ERRORS,
        UI.CHART_TOOL_USAGE,
        UI.CHART_VOICE,
        UI.CHART_CONFIRMATIONS,
        UI.CONFIRM_REASON_RISKY,
        "run_shell",
        "error:429",
    ):
        assert text in html, text
    assert 'data-src="/analytics/data?range=30d"' in html
    data = client.get("/analytics/data?range=30d").json()
    assert data["channels"] == {"CLI": 2, "Voice": 3}
    partial = client.get("/analytics/requests?range=30d&sort=cost&dir=asc&page=1")
    assert partial.status_code == 200 and partial.text.count("<tr") == 6  # header + 5 rows
    assert client.get("/analytics?range=custom&start=2026-01-01&end=2026-01-02").status_code == 200  # empty range renders


def test_analytics_without_a_database_renders_empty_states(config, paths, quiet_services):
    from jarvis.dashboard.state import Dashboard

    paths.db_file.unlink(missing_ok=True)
    client, store = make_client(config, paths, Dashboard(config, paths, auto_full_check=False))
    login(client, store)
    page = client.get("/analytics")
    assert page.status_code == 200 and UI.NO_DATA in page.text
    data = client.get("/analytics/data").json()
    assert data["model_usage"] == [] and data["daily_cost"]["budget"] == config.budget.daily_budget_usd
