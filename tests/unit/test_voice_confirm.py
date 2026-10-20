"""Voice confirmations: phrase matching, which tools are dialog-only, the dialog texts and the
osascript handles (fake processes; no dialog is ever shown)."""

from __future__ import annotations

import subprocess

import pytest

from jarvis.io.base import ConfirmRequest
from jarvis.voice import confirm as vc


def req(tool="google_create_event", tainted=False, **kw) -> ConfirmRequest:
    return ConfirmRequest(
        tool=tool, args=kw.pop("args", {"title": "Diş", "start": "2026-10-15 15:00"}), risk="CONFIRM", tainted=tainted, **kw
    )


# --- phrases --------------------------------------------------------------------------------------


def test_normalize_phrase_folds_case_and_punctuation_in_both_languages():
    assert vc.normalize_phrase("  ONAYLIYORUM!  ") == vc.normalize_phrase("onaylıyorum") == "onayliyorum"
    assert vc.normalize_phrase("Evet, onaylıyorum.") == "evet onayliyorum"
    assert vc.normalize_phrase("İstanbul") == "istanbul"
    assert vc.normalize_phrase("CONFIRM") == "confirm"  # the English I stays an i


def test_matcher_needs_the_phrase_itself():
    m = vc.ConfirmPhraseMatcher(["onaylıyorum", "evet onaylıyorum", "confirm", "yes confirm"])
    assert m.matches("Onaylıyorum.")
    assert m.matches("evet onaylıyorum")
    assert m.matches("Yes, confirm!")
    assert m.matches("confirm it")  # the phrase plus a short tail
    for spoken in ("evet", "tamam", "olur", "onayla", "onaylıyorum sanırım", "yes", "", "   "):
        assert not m.matches(spoken), spoken


def test_matcher_rejects_empty_config():
    with pytest.raises(ValueError):
        vc.ConfirmPhraseMatcher(["", "  "])


# --- dialog-only rules -----------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["run_shell", "write_file", "google_delete_event", "apple_delete_event", "delete_workout", "forget"])
def test_dialog_only_tools(tool):
    assert vc.dialog_only(req(tool))


def test_tainted_turn_is_dialog_only_for_every_tool():
    assert vc.dialog_only(req("google_create_event", tainted=True))
    assert not vc.dialog_only(req("google_create_event"))
    assert not vc.dialog_only(req("gmail_create_draft"))


# --- texts --------------------------------------------------------------------------------------------


def test_dialog_text_shows_actual_arguments_and_warnings():
    r = req("run_shell", args={"command": "ls -la ~"}, command_preview="ls -la ~", tainted=True, taint_warning="untrusted: gmail")
    text = vc.dialog_text(r, voice_allowed=False, language="tr")
    assert "run_shell" in text and '"command": "ls -la ~"' in text and "Komut:\nls -la ~" in text
    assert "untrusted: gmail" in text and "Onayla düğmesi gerekir" in text
    text2 = vc.dialog_text(req(), voice_allowed=True, language="tr")
    assert "onaylıyorum" in text2 and "Başka her şey ret" in text2


def test_dialog_text_is_capped_but_never_silently_cut():
    r = req(args={"body": "x" * 10000})
    text = vc.dialog_text(r, voice_allowed=True, language="tr")
    assert len(text) <= vc.DIALOG_TEXT_MAX_CHARS and "kısaltıldı" in text
    english = vc.dialog_text(r, voice_allowed=True, language="en")
    assert len(english) <= vc.DIALOG_TEXT_MAX_CHARS and "shortened" in english


def test_dialog_text_and_question_in_english():
    r = req("run_shell", args={"command": "ls"}, command_preview="ls", tainted=True)
    text = vc.dialog_text(r, voice_allowed=False, language="en")
    assert text.startswith("Tool: run_shell (risk: CONFIRM)") and "Command:\nls" in text and "Arguments:\n" in text
    assert "untrusted content" in text and "the Confirm button is required" in text
    assert 'say "confirm"' in vc.dialog_text(req(), voice_allowed=True, language="en")
    assert vc.spoken_question(req(), voice_allowed=True, language="en").endswith("Say 'confirm' to approve.")
    assert "Confirm button" in vc.spoken_question(r, voice_allowed=False, language="en")
    assert vc.deny_message("phrase_mismatch", "en", heard="make coffee") == (
        'I heard "make coffee"; to approve you need to say "confirm". I did not do it.'
    )
    assert vc.reask_message("en") == 'A plain "yes" is not enough; say "confirm".' and vc.confirmed_message("en") == "Confirmed."


def test_spoken_question_mentions_tool_and_how_to_answer():
    assert "onaylıyorum" in vc.spoken_question(req(), voice_allowed=True, language="tr")  # asks for the phrase, not for a yes
    q = vc.spoken_question(req("run_shell", args={"command": "ls"}), voice_allowed=False, language="tr")
    assert "run shell" in q and "Onayla düğmesine" in q


# --- processes -----------------------------------------------------------------------------------------


class FakeProc:
    def __init__(self, output: str | None, *, exit_after_polls: int = 0):
        self.output = output
        self.polls = 0
        self.exit_after_polls = exit_after_polls
        self.killed = False

    def poll(self):
        self.polls += 1
        if self.killed:
            return -9
        return 0 if self.polls > self.exit_after_polls else None

    def communicate(self):
        return (self.output or ""), ""

    def kill(self):
        self.killed = True


def test_start_dialog_uses_argv_and_polls(monkeypatch):
    calls = []

    def popen(argv, **kw):
        calls.append((argv, kw))
        return FakeProc("Onayla\n", exit_after_polls=1)

    handle = vc.start_dialog('hello "$(rm -rf ~)" `x`', timeout_s=20, language="tr", popen=popen)
    argv, kw = calls[0]
    assert argv[:3] == ["osascript", "-e", vc.DIALOG_SCRIPT] and argv[3] == "--"
    assert argv[4] == 'hello "$(rm -rf ~)" `x`' and argv[5:] == ["Onayla", "Reddet", "20"]
    assert kw["stdin"] is subprocess.DEVNULL and "shell" not in kw
    assert kw["encoding"] == "utf-8"  # Turkish button labels and text survive a launchd agent's locale
    assert handle.poll() is None  # still open
    assert handle.poll() == "Onayla" and handle.result() == "Onayla"
    vc.start_dialog("x", timeout_s=20, language="en", popen=popen)
    assert calls[1][0][5:] == ["Confirm", "Reject", "20"]


def test_dialog_handle_cancel_kills_open_dialog():
    proc = FakeProc("Onayla", exit_after_polls=99)
    handle = vc.DialogHandle(proc)
    assert handle.poll() is None
    handle.cancel()
    assert proc.killed and handle.result() == "cancelled"
    assert vc.dialog_outcome(handle.result()) == ("cancelled", "superseded")  # closed by us, never a user decision


def test_dialog_decision_mapping():
    assert vc.dialog_decision("Onayla", "tr") == "confirm"
    assert vc.dialog_decision("Reddet", "tr") == "deny"
    assert vc.dialog_decision("Confirm", "en") == "confirm" and vc.dialog_decision("Reject", "en") == "deny"
    assert vc.dialog_decision("Confirm", "tr") == "deny" and vc.dialog_decision("Onayla", "en") == "deny"  # the dialog's own labels only
    assert vc.dialog_decision("timeout") == "timeout"
    assert vc.dialog_decision("") == "deny" and vc.dialog_decision(None) == "deny"
    assert vc.dialog_decision("onayla") == "deny"  # exact label only


def test_choice_dialog_maps_label_to_key_and_defaults():
    options = [("always", "always allow"), ("once", "only this time"), ("never", "never allow it (saved)")]

    def run(argv, **kw):
        assert argv[:3] == ["osascript", "-e", vc.CHOICE_SCRIPT] and argv[3] == "--"
        assert argv[5] == "20" and argv[6] == "never allow it (saved)"  # default label
        assert argv[7:] == ["always allow", "only this time", "never allow it (saved)"]
        return subprocess.CompletedProcess(argv, 0, stdout="only this time\n", stderr="")

    assert vc.ask_choice_dialog("q", options, "never", timeout_s=20, run=run) == "once"

    def timeout(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="timeout\n", stderr="")

    assert vc.ask_choice_dialog("q", options, "never", timeout_s=20, run=timeout) == "never"

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 25)

    assert vc.ask_choice_dialog("q", options, "never", timeout_s=20, run=boom) == "never"
