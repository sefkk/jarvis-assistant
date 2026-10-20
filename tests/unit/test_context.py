import anthropic
import pytest
from anthropic.types import ToolUseBlock

from jarvis import context
from jarvis.assistant import Assistant
from jarvis.context import History, Turn, compact, supports_forced_tool_choice
from jarvis.tools.base import ToolContext
from jarvis.tools.registry import ToolRegistry
from tests.conftest import FakeClient, RecordingSink, api_error, make_message, text_message, thinking_block


def summary_message(summary="Earlier the user asked about tea and the time.", open_items=None):
    return make_message(
        [ToolUseBlock(type="tool_use", id="tu_sum", name="write_summary", input={"summary": summary, "open_items": open_items or []})],
        stop_reason="tool_use",
    )


def test_build_system_layout():
    system = context.build_system("P", "R", None, "")
    assert [b["text"].split("\n")[0] for b in system] == ["<persona>", "<rules>", "<user_profile>", "<memory>"]
    assert "cache_control" not in system[0] and sum("cache_control" in b for b in system) == 3
    assert context.NO_PROFILE_TEXT in system[2]["text"] and "(empty)" in system[3]["text"]


def test_persona_marker_expands_to_the_configured_humor_level():
    persona = "Who you are.\n\n2. Then the quip. [[HUMOR]]\n3. Keep it short."
    for level in range(4):
        text = context.build_system(persona, "R", None, "", humor_level=level)[0]["text"]
        assert "[[HUMOR]]" not in text and f"Humor level {level}" in text
        assert text.index("Then the quip.") < text.index(f"Humor level {level}") < text.index("Keep it short.")
    assert len({context.humor_text(level) for level in range(4)}) == 4
    assert context.humor_text(9) == context.humor_text(2)  # unknown level: the default
    # a persona file without the marker still gets the level, appended
    appended = context.render_persona("No marker here.", 0)
    assert appended.startswith("No marker here.") and appended.endswith(context.humor_text(0))
    assert "Humor level 0" in context.build_system("No marker here.", "R", None, "", humor_level=0)[0]["text"]


@pytest.mark.parametrize("language", ["en", "tr"])
def test_shipped_persona_has_the_marker_and_the_examples(language):
    from pathlib import Path

    persona = (Path(__file__).resolve().parents[2] / "src" / "jarvis" / "prompts" / f"persona.{language}.md").read_text(encoding="utf-8")
    assert persona.count("[[HUMOR]]") == 1 and "[[TEAM]]" in persona
    assert persona.count("User: ") >= 6 and persona.count("Jarvis: ") >= 6  # the tone examples in the language
    rendered = context.render_persona(persona, 2, team="Galatasaray")
    assert "Humor level 2 (playful)" in rendered and "[[HUMOR]]" not in rendered
    assert "Galatasaray" in rendered and "[[TEAM]]" not in rendered
    assert persona.index("[[HUMOR]]") < persona.index("User: ")  # the level sits with the instructions, before the examples


def test_prompts_follow_the_main_language_and_a_user_persona_wins(paths):
    persona, rules = context.load_prompts(paths, "tr")
    assert "Şemsiyeni al" in persona and "Trust boundaries:" in rules and '"Günaydın" only in the morning' in rules
    persona, rules = context.load_prompts(paths, "en")
    assert "Take an umbrella" in persona and '"Good morning" only in the morning' in rules and "Günaydın" not in rules
    paths.persona_file.write_text("You are my butler. [[HUMOR]]", encoding="utf-8")
    assert context.load_prompts(paths, "tr")[0] == "You are my butler. [[HUMOR]]"


def test_history_strip_thinking_keeps_tool_pairs():
    h = History()
    h.add(
        Turn(
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t", "signature": "s"},
                        {"type": "tool_use", "id": "a", "name": "x", "input": {}},
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "r"}]},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "only", "signature": "s"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            ]
        )
    )
    assert h.strip_thinking() == 2
    msgs = h.as_messages()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[1]["content"] == [{"type": "tool_use", "id": "a", "name": "x", "input": {}}]
    assert h.strip_thinking() == 0


def test_supports_forced_tool_choice():
    assert supports_forced_tool_choice("claude-haiku-5-5")
    assert not supports_forced_tool_choice("claude-sonnet-5-5") and not supports_forced_tool_choice("claude-opus-5-5")


def make_assistant(config, paths, outcomes):
    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    return Assistant(config, paths, client=FakeClient(outcomes), registry=registry)


def fill_history(assistant, n, tainted_turns=()):
    for i in range(n):
        assistant.handle(f"message {i}", RecordingSink())
    for i in tainted_turns:
        assistant.history.turns[i].tainted = True


def test_compaction_triggers_with_small_threshold(config, paths, logs):
    config.models.main_model = "claude-haiku-5-5"  # a family that accepts a forced tool_choice; auto is tested below
    config.context.compact_after_tokens = 100
    config.context.keep_recent_turns = 2
    # five normal turns; only the fifth reply reports a prompt over the threshold, then the summary call, then the reply
    outcomes = [text_message(f"r{i}", input_tokens=10) for i in range(4)] + [
        text_message("r4", input_tokens=500),
        summary_message(),
        text_message("after"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    fill_history(assistant, 5, tainted_turns=(1,))
    assert len(assistant.history) == 5
    sink = RecordingSink()
    result = assistant.handle("sixth", sink)
    assert result.compacted and "Önceki konuşma özetleniyor..." in sink.notices  # Turkish test config
    assert len(assistant.history) == 4  # summary + 2 recent + the new turn
    summary_turn = assistant.history.turns[0]
    assert summary_turn.is_summary and summary_turn.tainted  # a tainted turn was summarized
    text = summary_turn.messages[0]["content"][0]["text"]
    assert text.startswith('<untrusted source="summary">') and "[Summary of 3 earlier turns]" in text
    assert "tea and the time" in text
    assert assistant.history.turns[1].messages[0]["content"] == "message 3"  # recent turns kept verbatim

    calls = assistant.client.calls
    summary_call = calls[5]
    assert summary_call["tool_choice"] == {"type": "tool", "name": "write_summary"}
    assert summary_call["tools"][0]["name"] == "write_summary" and summary_call["tools"][0]["strict"] is True
    assert summary_call["messages"][-1]["content"] == context.SUMMARY_REQUEST
    assert summary_call["messages"][0]["content"] == "message 0" and len(summary_call["messages"]) == 7
    assert "summary" in summary_call["system"].lower()
    # the real reply call sees the summary followed by the kept turns
    final_call = calls[6]
    assert final_call["messages"][0]["content"][0]["text"] == text
    assert final_call["messages"][-1]["content"] == "sixth"
    stages = {r["stage"] for r in assistant.db.conn.execute("SELECT stage FROM timings").fetchall()}
    assert "compaction" in stages
    assistant.close()


def test_compaction_keeps_working_when_summary_call_fails(config, paths, logs):
    config.context.compact_after_tokens = 100
    config.context.keep_recent_turns = 1
    outcomes = [
        text_message("r", input_tokens=10),
        text_message("r", input_tokens=10),
        text_message("r", input_tokens=500),
        api_error(anthropic.BadRequestError, 400),
        text_message("still fine"),
    ]
    assistant = make_assistant(config, paths, outcomes)
    fill_history(assistant, 3)
    result = assistant.handle("next", RecordingSink())
    assert not result.compacted and result.reply == "still fine" and result.error is None
    assert len(assistant.history) == 4  # untouched plus the new turn
    assistant.close()


def test_compaction_uses_auto_tool_choice_on_models_without_forcing(config, paths, logs):
    config.models.main_model = "claude-sonnet-5-5"
    config.context.compact_after_tokens = 100
    config.context.keep_recent_turns = 1
    outcomes = [text_message("r", input_tokens=10), text_message("r", input_tokens=500), summary_message(), text_message("ok")]
    assistant = make_assistant(config, paths, outcomes)
    fill_history(assistant, 2)
    assistant.handle("x", RecordingSink())
    assert assistant.client.calls[2]["tool_choice"] == {"type": "auto"}
    assistant.close()


def test_compact_strips_thinking_from_old_and_kept_turns(config, paths, logs):
    config.context.compact_after_tokens = 100
    config.context.keep_recent_turns = 1
    config.models.main_thinking = True
    small = make_message([thinking_block("deep", "sig"), *text_message("r").content], input_tokens=10)
    big = make_message([thinking_block("deep", "sig"), *text_message("r").content], input_tokens=500)
    outcomes = [small, big, summary_message(), text_message("ok")]
    assistant = make_assistant(config, paths, outcomes)
    fill_history(assistant, 2)
    assistant.handle("x", RecordingSink())
    calls = assistant.client.calls
    for call in calls[2:]:
        blocks = [b for m in call["messages"] if m["role"] == "assistant" and isinstance(m["content"], list) for b in m["content"]]
        assert all(b["type"] != "thinking" for b in blocks)
    assistant.close()


def test_compact_direct_with_nothing_to_do(config, db):
    h = History()
    h.add(Turn(messages=[{"role": "user", "content": "a"}]))
    assert compact(h, brain=None, keep_recent_turns=6, turn_id="t") is None


def test_voice_channel_note_asks_for_speakable_forms():
    """In voice mode the model writes times, percentages and dates the way they are said."""
    from jarvis.context import CHANNEL_NOTES

    note = CHANNEL_NOTES["voice"]
    for form in ("saat 19:00", "yüzde 75", "no tables", "'1,5'", "17 Ekim"):
        assert form in note


def test_rules_ask_for_direct_web_searches_and_the_answer_after_the_last_tool_call():
    """The 2026-10-19 briefs: `web_search` wrapped in `params` (rejected), then run from code (crashed), and
    every retry narrated into the message. The rules name both, and the brief's new sections."""
    from pathlib import Path

    import jarvis

    rules = (Path(jarvis.__file__).parent / "prompts" / "rules.md").read_text(encoding="utf-8")
    assert "never wrap it in another object (such as `params`)" in rules
    assert "never write code to run a search" in rules
    assert "The whole answer goes into the text after your last tool call" in rules
    assert "never narrate tool errors or retries" in rules
    assert '"Mail today" section' in rules and 'Weather marked "(home)"' in rules and "a morning greeting only in the morning" in rules
