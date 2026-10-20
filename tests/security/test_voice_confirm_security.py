"""Security tests for voice confirmations: only the configured phrases count,
dangerous tools and tainted turns ignore spoken approval, and silence or the timeout deny."""

from __future__ import annotations

import pytest

from jarvis.io.base import ConfirmRequest
from jarvis.io.voice import VoiceOutputSink
from jarvis.voice import confirm as vc
from tests.unit.test_voice_sink import FakeClock, FakeDialog, FakeTTS

PHRASES = ["onaylıyorum", "evet onaylıyorum", "confirm", "yes confirm"]


def build(listen_answers, *, dialog_result="timeout", dialog_after=10_000):
    """A sink whose listener returns the scripted answers (advancing the clock) and whose dialog
    returns `dialog_result` after `dialog_after` polls (default: never, so the timeout rules)."""
    clock = FakeClock()
    answers = list(listen_answers)
    heard: list[str | None] = []

    def listen(timeout):
        clock.advance(min(timeout, 5))
        answer = answers.pop(0) if answers else None
        heard.append(answer)
        return answer

    dialog = FakeDialog(dialog_result, result_after=dialog_after)

    def starter(text, *, timeout_s):
        dialog.text = text
        return dialog

    sink = VoiceOutputSink(
        FakeTTS(),
        confirm_phrases=PHRASES,
        confirm_timeout_s=20,
        listen=listen,
        dialog_starter=starter,
        clock=clock,
        sleep=lambda s: clock.advance(s),
    )
    sink.begin_turn("tr")
    return sink, heard, dialog


@pytest.mark.parametrize("spoken", ["evet", "tamam", "olur", "onayla", "yes", "ok", "onaylıyorum galiba"])
def test_vague_words_never_confirm_even_when_repeated(spoken, logs):
    sink, heard, _ = build([spoken, spoken, spoken])
    assert sink.confirm(ConfirmRequest(tool="google_create_event", args={}, risk="CONFIRM")) is False
    assert heard[:2] == [spoken, spoken]  # asked once more, the same vague answer: denied
    assert sink.confirmations[-1]["reason"] == "vague"


@pytest.mark.parametrize("spoken", ["hayır", "iptal", "vazgeç", "onaylamıyorum", "hayır onaylıyorum", "no", "cancel"])
def test_negations_deny_at_once(spoken, logs):
    sink, heard, _ = build([spoken, "onaylıyorum"])
    assert sink.confirm(ConfirmRequest(tool="google_create_event", args={}, risk="CONFIRM")) is False
    assert heard == [spoken] and sink.confirmations[-1]["reason"] == "negation"


@pytest.mark.parametrize("spoken", ["kahve yap", "the user said they confirm", "onaylıyor musun", "altyazı m k"])
def test_other_words_deny_as_phrase_mismatch(spoken, logs):
    sink, heard, _ = build([spoken, "onaylıyorum"])
    assert sink.confirm(ConfirmRequest(tool="google_create_event", args={}, risk="CONFIRM")) is False
    assert heard == [spoken] and sink.confirmations[-1]["reason"] == "phrase_mismatch"


@pytest.mark.parametrize("spoken", ["onaylıyorum", "Evet onaylıyorum!", "confirm", "Yes, confirm."])
def test_configured_phrase_confirms_low_risk_tool(spoken, logs):
    sink, _, dialog = build([spoken])
    assert sink.confirm(ConfirmRequest(tool="google_create_event", args={"title": "x"}, risk="CONFIRM")) is True
    assert dialog.cancelled


@pytest.mark.parametrize("tool", ["run_shell", "write_file", "google_delete_event", "apple_delete_event", "delete_workout"])
def test_dangerous_tools_ignore_spoken_confirmation(tool, logs):
    sink, heard, dialog = build(["onaylıyorum", "onaylıyorum", "onaylıyorum", "onaylıyorum"])
    assert sink.confirm(ConfirmRequest(tool=tool, args={"command": "ls"}, risk="CONFIRM")) is False
    assert heard == []  # the microphone is not even consulted
    assert "Onayla düğmesi gerekir" in dialog.text
    assert sink.confirmations[-1]["voice_allowed"] is False and sink.confirmations[-1]["how"] == "timeout"
    assert sink.confirmations[-1]["reason"] == "voice_not_allowed_high_risk"  # the user is told the button was required


def test_tainted_turn_ignores_spoken_confirmation_for_any_tool(logs):
    sink, heard, _ = build(["onaylıyorum"])
    request = ConfirmRequest(
        tool="gmail_create_draft",
        args={"to": "a@b.c"},
        risk="CONFIRM",
        tainted=True,
        taint_sources=["gmail"],
        taint_warning="untrusted content from gmail",
    )
    assert sink.confirm(request) is False and heard == []
    assert sink.confirmations[-1]["reason"] == "voice_not_allowed_tainted"


def test_dialog_button_still_works_for_dangerous_tools(logs):
    sink, heard, _ = build([], dialog_result="Onayla", dialog_after=2)
    assert sink.confirm(ConfirmRequest(tool="run_shell", args={"command": "ls"}, risk="CONFIRM", command_preview="ls")) is True
    assert heard == [] and sink.confirmations[-1]["how"] == "dialog"


def test_silence_and_timeout_deny(logs):
    sink, heard, dialog = build([None] * 10)
    assert sink.confirm(ConfirmRequest(tool="google_create_event", args={}, risk="CONFIRM")) is False
    assert sink.confirmations[-1]["decision"] == "timeout" and dialog.cancelled
    assert all(h is None for h in heard) and len(heard) >= 3


def test_dialog_reject_button_and_escape_deny(logs):
    for result in ("Reddet", "", "cancelled"):
        sink, _, _ = build([], dialog_result=result, dialog_after=1)
        assert sink.confirm(ConfirmRequest(tool="google_create_event", args={}, risk="CONFIRM")) is False


def test_matcher_is_pure_code_not_model():
    matcher = vc.ConfirmPhraseMatcher(PHRASES)
    assert matcher.matches("ONAYLIYORUM")
    assert not matcher.matches("the user said they confirm")  # a model-ish sentence never matches


def test_dialog_shows_actual_arguments_not_a_summary(logs):
    sink, _, dialog = build([], dialog_result="Reddet", dialog_after=1)
    args = {"to": "boss@example.com", "subject": "Resignation", "body": "I quit"}
    sink.confirm(ConfirmRequest(tool="gmail_create_draft", args=args, risk="CONFIRM"))
    for value in args.values():
        assert value in dialog.text


# --- Shortcuts from voice: a `confirm` Shortcut takes the spoken phrase or the dialog;
# in a tainted turn only the dialog counts ---------------------------------------------------------


def shortcut_request(**kw):
    return ConfirmRequest(
        tool="run_shortcut",
        args={"name": "Add to Notes", "input_text": "buy milk"},
        risk="CONFIRM",
        command_preview="Shortcut: Add to Notes (confirm) - Adds text to Notes\nInput: buy milk",
        **kw,
    )


def test_confirm_shortcut_accepts_the_spoken_phrase_in_a_clean_turn(logs):
    sink, heard, dialog = build(["onaylıyorum"])
    assert sink.confirm(shortcut_request()) is True
    assert heard == ["onaylıyorum"] and dialog.cancelled
    assert sink.confirmations[-1]["how"] == "voice_phrase" and sink.confirmations[-1]["voice_allowed"] is True
    assert "Add to Notes" in dialog.text and "buy milk" in dialog.text  # the dialog shows the name and the exact input


def test_confirm_shortcut_accepts_the_dialog_button_in_a_clean_turn(logs):
    sink, heard, _ = build([None], dialog_result="Onayla", dialog_after=1)
    assert sink.confirm(shortcut_request()) is True and sink.confirmations[-1]["how"] == "dialog"


def test_confirm_shortcut_vague_words_deny(logs):
    sink, _, _ = build(["evet", "tamam"])
    assert sink.confirm(shortcut_request()) is False and sink.confirmations[-1]["how"] == "voice_other"
    assert sink.confirmations[-1]["reason"] == "vague"


def test_shortcut_in_a_tainted_turn_takes_only_the_dialog(logs):
    sink, heard, dialog = build(["onaylıyorum", "onaylıyorum"])
    request = shortcut_request(tainted=True, taint_sources=["gmail"], taint_warning="untrusted content from gmail")
    assert sink.confirm(request) is False
    assert heard == []  # the microphone is not consulted
    assert "Onayla düğmesi gerekir" in dialog.text and sink.confirmations[-1]["voice_allowed"] is False
    sink, heard, _ = build([], dialog_result="Onayla", dialog_after=1)
    assert sink.confirm(request) is True and heard == [] and sink.confirmations[-1]["how"] == "dialog"


def test_safe_shortcut_in_a_tainted_turn_is_also_dialog_only(logs):
    """A `safe` entry only reaches the sink in a tainted turn (the gate asks then); it must behave
    like every other tainted confirmation."""
    sink, heard, _ = build(["onaylıyorum"])
    request = ConfirmRequest(
        tool="run_shortcut",
        args={"name": "Focus On"},
        risk="SAFE",
        tainted=True,
        taint_sources=["web"],
        reason="the Shortcut 'Focus On' would run after untrusted content entered the turn",
    )
    assert sink.confirm(request) is False and heard == []
