"""In-session memory (part 1): a question Jarvis asked and the user's short answer stay together.

The transcripts showed three ways the answer got lost: a failed turn (API timeout) dropped the user's
message from the history, every interface and every restart starts an empty history, and compaction
could in principle summarize the turn that holds the open question. These tests pin the fixes."""

from __future__ import annotations

from datetime import timedelta

import anthropic
import httpx2

from jarvis import context
from jarvis.assistant import Assistant
from jarvis.context import History, Turn, carry_over_turn, compact, ends_with_question
from jarvis.tools.base import ToolContext
from jarvis.tools.registry import ToolRegistry
from jarvis.utils.timeutil import now_iso, now_utc
from tests.conftest import FakeClient, RecordingSink, text_message, tool_message
from tests.unit.test_context import summary_message


def make_assistant(config, paths, outcomes, **kw):
    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    return Assistant(config, paths, client=FakeClient(outcomes), registry=registry, **kw)


QUESTION = "Takvime 20:00 olarak ekleyeyim mi?"


# --- CLI: the question and the answer reach the model together ---------------------------------------


def test_cli_short_answer_continues_the_request_without_a_confirmation(config, paths, logs):
    assistant = make_assistant(config, paths, [text_message(QUESTION), tool_message("get_time", {}), text_message("Ekledim.")])
    assistant.handle("spor saatimi takvime ekle", RecordingSink())
    sink = RecordingSink()
    result = assistant.handle("evet", sink)
    assert result.reply == "Ekledim." and sink.confirms == []  # "evet" went to the model, not to a confirmation prompt
    request = assistant.client.calls[1]["messages"]
    assert request[0]["content"] == "spor saatimi takvime ekle"
    assert request[1]["content"][0]["text"] == QUESTION
    assert request[2]["content"] == "evet"
    assistant.close()


def test_cli_answer_lost_to_a_timeout_is_still_seen_by_the_next_turn(config, paths, logs):
    """The bug as recorded on 2026-10-16: Jarvis asked, the user said "Evet.", the API timed out, and the
    next utterance ("Hey Jarvis") got "Buradayım, ne lazım?" because the "Evet." had vanished."""
    config.loop.api_max_attempts = 1
    assistant = make_assistant(
        config,
        paths,
        [
            text_message(QUESTION),
            anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
            text_message("Tamam, 20:00 olarak ekliyorum."),
        ],
    )
    assistant.handle("spor saatimi takvime ekle", RecordingSink())
    failed = assistant.handle("Evet.", RecordingSink())
    assert failed.error and "APITimeoutError" in failed.error
    assert assistant.history.turns[-1].failed and assistant.history.turns[-1].messages == [{"role": "user", "content": "Evet."}]
    result = assistant.handle("Hey Jarvis", RecordingSink())
    assert result.error is None
    contents = [m["content"] if isinstance(m["content"], str) else m["content"][0]["text"] for m in assistant.client.calls[-1]["messages"]]
    assert contents == ["spor saatimi takvime ekle", QUESTION, "Evet.", "Hey Jarvis"]
    assistant.close()


def test_empty_failed_message_is_not_kept(config, paths, logs):
    assistant = make_assistant(config, paths, [])
    assistant._keep_failed_turn("   ", None)
    assert len(assistant.history) == 0
    assistant.close()


# --- compaction -------------------------------------------------------------------------------------


def test_ends_with_question():
    assert ends_with_question("Ekleyeyim mi?") and ends_with_question('Ekleyeyim mi?" 🙂') and ends_with_question("mi? )")
    assert not ends_with_question("Ekledim.") and not ends_with_question("") and not ends_with_question("soru mu? evet.")


def user_turn(text, reply):
    return Turn(messages=[{"role": "user", "content": text}, {"role": "assistant", "content": [{"type": "text", "text": reply}]}])


def test_turn_reply_text_and_question():
    assert user_turn("a", QUESTION).asks_question() and not user_turn("a", "Tamam.").asks_question()
    assert Turn(messages=[{"role": "user", "content": "x"}], failed=True).reply_text() == ""


def test_compaction_never_summarizes_the_turn_holding_the_open_question(config, paths, logs):
    """keep_recent_turns = 1 would keep only the answer turn; the question turn before it stays verbatim too."""
    config.context.compact_after_tokens = 100
    config.context.keep_recent_turns = 1
    outcomes = [
        text_message("r0", input_tokens=10),
        text_message("r1", input_tokens=10),
        text_message(QUESTION, input_tokens=10),
        text_message("Ekledim.", input_tokens=500),
        summary_message(),
        text_message("after"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    for text in ("m0", "m1", "spor saatimi ekle", "evet"):
        assistant.handle(text, RecordingSink())
    result = assistant.handle("sonra?", RecordingSink())
    assert result.compacted
    kept = [t.messages[0]["content"] for t in assistant.history.turns if not t.is_summary]
    assert kept == ["spor saatimi ekle", "evet", "sonra?"]  # the question turn, its answer and the new turn
    assert "[Summary of 2 earlier turns]" in assistant.history.turns[0].messages[0]["content"][0]["text"]
    assistant.close()


def test_compaction_keeps_at_least_one_turn_even_when_configured_zero(config, db):
    h = History()
    for i in range(3):
        h.add(user_turn(f"m{i}", "ok" if i < 2 else QUESTION))
    calls = []

    class FakeBrain:
        model = "claude-haiku-5-5"
        max_output_tokens = 1024

        def stream_message(self, **kw):
            calls.append(kw)
            from types import SimpleNamespace

            return SimpleNamespace(message=summary_message())

    result = compact(h, FakeBrain(), keep_recent_turns=0, turn_id="t")
    assert result is not None and result.summarized_turns == 2
    assert len(h.turns) == 2 and h.turns[1].asks_question()  # summary + the question turn verbatim
    assert len(calls[0]["messages"]) == 5  # two old turns and the summary request


def test_compaction_measures_the_conversation_not_the_system_prompt(config, paths, logs):
    """The real prompt is ~28k tokens before a word is said (rules, tools). A constant prompt size means
    nothing was added to the conversation, so no compaction call is made on every turn."""
    config.context.compact_after_tokens = 1000
    config.context.keep_recent_turns = 2
    outcomes = [text_message(f"r{i}", input_tokens=28000) for i in range(8)]
    assistant = make_assistant(config, paths, outcomes)
    for i in range(8):
        result = assistant.handle(f"m{i}", RecordingSink())
        assert not result.compacted
    assert assistant.conversation_tokens == 0 and len(assistant.client.calls) == 8
    assistant.close()


def test_compaction_still_triggers_when_the_conversation_grows(config, paths, logs):
    config.context.compact_after_tokens = 1000
    config.context.keep_recent_turns = 1
    outcomes = [
        text_message("r0", input_tokens=28000),
        text_message("r1", input_tokens=28500),
        text_message("r2", input_tokens=29500),
        summary_message(),
        text_message("r3"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    for i in range(3):
        assistant.handle(f"m{i}", RecordingSink())
    assert assistant.conversation_tokens == 1500
    assert assistant.handle("m3", RecordingSink()).compacted
    assistant.close()


# --- carry-over across a restart or a channel switch ---------------------------------------------


def test_new_assistant_within_the_window_sees_the_open_question(config, paths, logs):
    first = make_assistant(config, paths, [text_message(QUESTION)], channel="voice")
    first.handle("spor saatimi takvime ekle", RecordingSink())
    first.close()  # the voice service restarted (or the user switched to the terminal)

    second = make_assistant(config, paths, [text_message("Tamam, ekliyorum.")], channel="cli")
    assert len(second.history) == 1 and second.history.turns[0].is_summary and not second.history.turns[0].tainted
    second.handle("evet", RecordingSink())
    messages = second.client.calls[0]["messages"]
    carried = messages[0]["content"][0]["text"]
    assert carried.startswith(context.CARRY_OVER_PREFIX) and "on voice" in carried and "continued on cli" in carried
    assert "user: spor saatimi takvime ekle" in carried and f"assistant: {QUESTION}" in carried
    assert messages[1]["content"] == "evet"
    row = second.db.conn.execute("SELECT channel FROM sessions WHERE id = ?", (second.session_id,)).fetchone()
    assert row["channel"] == "cli"
    second.close()


def test_no_carry_over_outside_the_window_or_when_disabled(config, paths, logs):
    first = make_assistant(config, paths, [text_message(QUESTION)])
    first.handle("ekle", RecordingSink())
    old = (now_utc() - timedelta(minutes=config.context.resume_after_min + 5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    first.db.conn.execute("UPDATE turns SET started_at = ?", (old,))
    first.close()
    assert len(make_assistant(config, paths, []).history) == 0

    first = make_assistant(config, paths, [text_message(QUESTION)])
    first.handle("ekle", RecordingSink())
    first.close()
    config.context.resume_after_min = 0
    assert len(make_assistant(config, paths, []).history) == 0


def test_explicit_new_session_does_not_resume_but_the_rollover_does(config, paths, logs):
    assistant = make_assistant(config, paths, [text_message(QUESTION), text_message("ok")])
    assistant.handle("ekle", RecordingSink())
    assistant.new_session()  # Telegram /new: the user wants a clean slate
    assert len(assistant.history) == 0
    assistant.new_session(resume=True)  # the midnight rollover
    assert len(assistant.history) == 1 and QUESTION in assistant.history.turns[0].messages[0]["content"][0]["text"]
    assistant.close()


def test_carry_over_of_a_tainted_turn_is_wrapped_as_untrusted():
    rows = [
        {
            "user_input": "maili özetle",
            "final_reply": "Mailde şu var... Yanıtlayayım mı?",
            "started_at": now_iso(),
            "channel": "telegram",
            "tainted": 1,
            "error": None,
        },
        {
            "user_input": "hmm",
            "final_reply": None,
            "started_at": now_iso(),
            "channel": "telegram",
            "tainted": 0,
            "error": "APITimeoutError",
        },
    ]
    turn = carry_over_turn(rows, now_iso=now_iso(), channel="cli")
    assert turn.tainted and turn.is_summary
    text = turn.messages[0]["content"][0]["text"]
    assert text.startswith('<untrusted source="previous_session">') and "(no reply: the request failed)" in text
    assert carry_over_turn([], now_iso=now_iso(), channel="cli") is None
    assert carry_over_turn([{"user_input": "  ", "final_reply": "x", "started_at": now_iso()}], now_iso=now_iso(), channel="cli") is None


def test_carry_over_is_capped(config):
    rows = [
        {"user_input": "u" * 5000, "final_reply": "r" * 5000, "started_at": now_iso(), "channel": "cli", "tainted": 0, "error": None}
        for _ in range(4)
    ]
    text = carry_over_turn(rows, now_iso=now_iso(), channel="cli").messages[0]["content"][0]["text"]
    assert len(text) <= context.CARRY_OVER_MAX_CHARS + 400 and text.startswith(context.CARRY_OVER_PREFIX)


def test_recent_turns_query_excludes_the_current_session(db):
    db.start_session("s1", "m", "d", "voice")
    db.start_turn("t1", "s1", "ekle")
    db.finish_turn("t1", final_reply=QUESTION, tainted=False, total_ms=1, error=None)
    db.start_session("s2", "m", "d", "cli")
    db.start_turn("t2", "s2", "evet")
    rows = db.recent_turns(since_iso="2000-01-01T00:00:00.000Z", limit=5, exclude_session="s2")
    assert [r["user_input"] for r in rows] == ["ekle"] and rows[0]["channel"] == "voice"
    assert db.recent_turns(since_iso="2999-01-01T00:00:00.000Z", limit=5) == []
