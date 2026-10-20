"""The confirmation fixes: the Turkish-aware answer classifier, dialog output
parsing, the one-decision arbiter (click vs voice timeout, voice vs dialog), timing (the microphone
opens only after the question has been played, the clock starts then), the deny-reason feedback,
the "evet" re-ask flow, and the tool result the model receives. Nothing speaks, no dialog is shown."""

from __future__ import annotations

import threading
import time

import pytest
from pydantic import BaseModel

from jarvis.exceptions import ToolDeniedError
from jarvis.io.base import DENY_REASONS, ConfirmOutcome, ConfirmRequest, OutputSink
from jarvis.io.voice import VoiceOutputSink, spawn_daemon_thread
from jarvis.security import confirm as gate_module
from jarvis.security.taint import TaintState
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.voice import confirm as vc
from tests.unit.test_voice_sink import FakeClock, FakeDialog, FakeTTS

PHRASES = ["onaylıyorum", "evet onaylıyorum", "confirm", "yes confirm"]


def req(tool="google_create_event", **kw) -> ConfirmRequest:
    return ConfirmRequest(tool=tool, args=kw.pop("args", {"title": "Diş", "start": "2026-10-15 15:00"}), risk="CONFIRM", **kw)


# --- 1. the phrase matcher table -------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, kind",
    [
        # punctuation, commas, capitals
        ("onaylıyorum", "confirm"),
        ("Onaylıyorum.", "confirm"),
        ("Evet, onaylıyorum.", "confirm"),
        ("EVET ONAYLIYORUM!", "confirm"),
        ("  onaylıyorum...  ", "confirm"),
        # Turkish I/İ and Whisper's ı/i confusion
        ("ONAYLIYORUM", "confirm"),
        ("Onayliyorum", "confirm"),
        ("ONAYLİYORUM", "confirm"),
        ("İstanbul'dan onaylıyorum", "confirm"),
        # extra words around the phrase (short answers)
        ("onaylıyorum tabii", "confirm"),
        ("evet evet onaylıyorum", "confirm"),
        ("tamam onaylıyorum Jarvis", "confirm"),
        ("Yes, confirm!", "confirm"),
        ("I confirm", "confirm"),
        ("confirm it", "confirm"),
        # too long: not a short answer any more
        ("the user said they confirm", "other"),
        ("bugün hava güzel olduğu için ben bunu onaylıyorum sanırım evet", "vague"),
        ("ben bu işi şimdi hemen burada onaylıyorum", "other"),
        # bare acknowledgements are vague
        ("evet", "vague"),
        ("Evet.", "vague"),
        ("tamam", "vague"),
        ("olur", "vague"),
        ("evet tamam", "vague"),
        ("ok", "vague"),
        ("yes", "vague"),
        ("onayla", "vague"),
        ("Evet, onayla.", "vague"),
        # hedges
        ("onaylıyorum galiba", "vague"),
        ("onaylıyorum sanırım", "vague"),
        ("I think I confirm", "vague"),
        # negations always deny, even next to the phrase
        ("hayır", "deny"),
        ("Hayır!", "deny"),
        ("iptal", "deny"),
        ("vazgeç", "deny"),
        ("vazgeçtim", "deny"),
        ("onaylamıyorum", "deny"),
        ("Onaylamıyorum.", "deny"),
        ("hayır onaylıyorum", "deny"),
        ("onaylıyorum hayır", "deny"),
        ("evet ama iptal", "deny"),
        ("no", "deny"),
        ("cancel", "deny"),
        ("don't", "deny"),
        # unrelated speech, Whisper noise
        ("kahve yap", "other"),
        ("onaylıyor musun", "other"),
        ("Altyazı M.K.", "other"),
        ("", "other"),
        ("   ", "other"),
    ],
)
def test_classifier_table(spoken, kind):
    m = vc.ConfirmPhraseMatcher(PHRASES)
    verdict = m.classify(spoken)
    assert verdict.kind == kind, (spoken, verdict)
    assert m.matches(spoken) is (kind == "confirm")


def test_normalize_folds_every_i():
    assert vc.normalize_phrase("ONAYLIYORUM") == vc.normalize_phrase("onaylıyorum") == "onayliyorum"
    assert vc.normalize_phrase("İPTAL") == "iptal"  # İ -> i
    assert vc.normalize_phrase("Evet,   onaylıyorum!!") == "evet onayliyorum"
    assert vc.normalize_phrase("I CONFIRM") == "i confirm"
    assert vc.fold_dotless("onaylıyorum") == "onayliyorum"


# --- 2. dialog output parsing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Onayla", ("confirm", None)),
        ("Onayla\n", ("confirm", None)),
        ("Reddet", ("deny", "dialog_reject")),
        ("cancel", ("deny", "dialog_cancel")),  # Escape / error -128
        ("timeout", ("timeout", "timeout")),  # gave up
        ("cancelled", ("cancelled", "superseded")),  # closed by Jarvis itself
        ("", ("deny", "dialog_parse_error")),
        (None, ("deny", "dialog_parse_error")),
        ("onayla", ("deny", "dialog_parse_error")),  # the exact label only
        ("Onayla\x00garbage", ("deny", "dialog_parse_error")),
        ("Onayl�", ("deny", "dialog_parse_error")),  # a mangled Turkish character never confirms
    ],
)
def test_dialog_outcome_parsing(raw, expected):
    assert vc.dialog_outcome(raw, "tr") == expected


class ExitedProc:
    """osascript already exited with a click, but nobody read the output yet."""

    def __init__(self, output: str):
        self.output = output
        self.killed = False

    def poll(self):
        return 0

    def communicate(self):
        return self.output, ""

    def kill(self):
        self.killed = True


def test_cancel_keeps_a_click_that_already_happened():
    handle = vc.DialogHandle(ExitedProc("Onayla\n"))
    handle.cancel()  # the voice path timed out and closes the dialog: the click must survive
    assert handle.result() == "Onayla" and vc.dialog_outcome(handle.result(), "tr") == ("confirm", None)


def test_dialog_script_maps_escape_to_cancel_not_to_the_deny_label():
    assert "on error number -128" in vc.DIALOG_SCRIPT and 'return "cancel"' in vc.DIALOG_SCRIPT
    assert 'return "timeout"' in vc.DIALOG_SCRIPT


# --- 3. one decision: the arbiter and the races ------------------------------------------------------------


def test_pending_confirmation_first_decision_wins_under_concurrency():
    pending = vc.PendingConfirmation()
    wins: list[int] = []
    barrier = threading.Barrier(12)

    def contend(i: int) -> None:
        barrier.wait()
        if pending.settle(vc.Decision("confirm" if i % 2 else "deny", "dialog" if i % 2 else "voice_other", None)):
            wins.append(i)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and pending.settled
    assert pending.settle(vc.Decision("timeout", "timeout", "timeout")) is False  # a late timeout changes nothing


class ClockDialog:
    """A dialog whose result appears at a given time on the fake clock (a real click), never by poll count."""

    def __init__(self, clock: FakeClock, result: str = "Onayla", at: float | None = None):
        self.clock, self.result_value, self.at = clock, result, at
        self.cancelled = False
        self.text = ""
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.at is not None and self.clock() >= self.at:
            return self.result_value
        return "cancelled" if self.cancelled else None

    def result(self):
        return self.poll()

    def cancel(self):
        self.cancelled = True


def build(listen_answers, *, clock=None, tts=None, dialog=None, spawn_thread=None, timeout=20, **kw):
    clock = clock or FakeClock()
    tts = tts or FakeTTS()
    answers = list(listen_answers)
    heard: list[str | None] = []
    listen_times: list[float] = []

    def listen(timeout, should_stop=None):
        listen_times.append(clock())
        clock.advance(min(timeout, 5))
        answer = answers.pop(0) if answers else None
        heard.append(answer)
        return answer

    dialog = dialog or FakeDialog()
    order: list[str] = []
    started = []

    def starter(text, *, timeout_s):
        dialog.text = text
        order.append("dialog")
        started.append(timeout_s)
        return dialog

    real_speak = tts.speak

    def speak(text, *a, **k):
        order.append("speak")
        return real_speak(text, *a, **k)

    tts.speak = speak
    sink = VoiceOutputSink(
        tts,
        confirm_phrases=PHRASES,
        confirm_timeout_s=timeout,
        listen=listen,
        dialog_starter=starter,
        clock=clock,
        sleep=lambda s: clock.advance(s),
        spawn_thread=spawn_thread,
        **kw,
    )
    sink.begin_turn("tr")
    sink._test = {
        "heard": heard,
        "listen_times": listen_times,
        "order": order,
        "dialog": dialog,
        "clock": clock,
        "tts": tts,
        "dialog_timeouts": started,
    }
    return sink


def test_click_at_the_deadline_beats_the_voice_timeout(logs):
    clock = FakeClock()
    dialog = ClockDialog(clock, "Onayla", at=clock() + 20)  # the click lands exactly when the voice window closes
    sink = build([None] * 10, clock=clock, dialog=dialog)
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "dialog"
    assert sink.confirmations[-1]["decision"] == "confirm"


def test_click_during_a_listen_slice_beats_whatever_was_heard(logs):
    clock = FakeClock()
    dialog = ClockDialog(clock, "Onayla", at=clock() + 3)  # pressed while the first 5 s slice records "kahve yap"
    sink = build(["kahve yap"], clock=clock, dialog=dialog)
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "dialog"


def test_reject_click_during_a_listen_slice_beats_a_spoken_phrase(logs):
    clock = FakeClock()
    dialog = ClockDialog(clock, "Reddet", at=clock() + 3)
    sink = build(["onaylıyorum"], clock=clock, dialog=dialog)
    outcome = sink.confirm_detailed(req())
    assert not outcome.approved and outcome.reason == "dialog_reject"


def test_voice_phrase_closes_the_dialog_and_a_late_click_is_ignored(logs):
    clock = FakeClock()
    dialog = ClockDialog(clock, "Reddet", at=clock() + 30)  # would arrive after the voice decided
    sink = build(["onaylıyorum"], clock=clock, dialog=dialog)
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "voice_phrase" and dialog.cancelled


def test_voice_timeout_closes_the_dialog(logs):
    sink = build([None] * 10)
    outcome = sink.confirm_detailed(req())
    assert not outcome.approved and outcome.reason == "timeout" and sink._test["dialog"].cancelled


def test_real_watcher_thread_stops_the_microphone_when_the_dialog_is_clicked(logs):
    """The production wiring: a daemon thread polls the dialog while `listen` blocks; a click settles
    the confirmation, `should_stop` ends the recording, and the voice path never decides."""

    class LiveDialog:
        def __init__(self):
            self.ready = threading.Event()
            self.cancelled = False
            self.text = ""

        def poll(self):
            return "Onayla" if self.ready.is_set() else None

        def result(self):
            return self.poll()

        def cancel(self):
            self.cancelled = True

    dialog = LiveDialog()
    stopped_by_should_stop = threading.Event()

    def listen(timeout, should_stop=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                stopped_by_should_stop.set()
                return None
            time.sleep(0.01)
        return "kahve yap"  # never reached: the click comes first

    sink = VoiceOutputSink(
        FakeTTS(),
        confirm_phrases=PHRASES,
        confirm_timeout_s=5,
        listen=listen,
        dialog_starter=lambda text, *, timeout_s: dialog,
        spawn_thread=spawn_daemon_thread,
    )
    sink.begin_turn("tr")
    threading.Timer(0.15, dialog.ready.set).start()
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "dialog"
    assert stopped_by_should_stop.is_set()


def test_barge_in_supersedes_a_pending_confirmation(logs):
    sink = build([])
    clock = sink._test["clock"]

    def listen(timeout, should_stop=None):
        clock.advance(1)
        sink.cancel()  # the hotkey / wake word interrupted the turn while we listened
        return None

    sink.listen = listen
    sink._listen_takes_should_stop = True
    outcome = sink.confirm_detailed(req())
    assert not outcome.approved and outcome.reason == "superseded"
    assert sink._test["dialog"].cancelled


def test_a_new_confirmation_supersedes_an_unsettled_one(logs):
    sink = build(["onaylıyorum"])
    stale = vc.PendingConfirmation()
    sink._pending = stale
    assert sink.confirm_detailed(req()).approved
    assert stale.settled and stale.decision.reason == "superseded"


# --- 4. timing ----------------------------------------------------------------------------------------


class BusyTTS(FakeTTS):
    """`wait` stays busy for `busy_s` of fake time after each speak (the question is long)."""

    def __init__(self, clock: FakeClock, busy_s: float):
        super().__init__()
        self.clock, self.busy_s = clock, busy_s
        self.busy_until = 0.0
        self.wait_calls: list[float] = []

    def speak(self, text, language=None, *, cacheable=False, local_only=False):
        super().speak(text, language, cacheable=cacheable, local_only=local_only)
        self.busy_until = max(self.busy_until, self.clock()) + self.busy_s

    def wait(self, timeout=None):
        self.wait_calls.append(self.clock())
        if self.clock() >= self.busy_until:
            return True
        self.clock.advance(min(timeout or 0.1, self.busy_until - self.clock()))
        return self.clock() >= self.busy_until


def test_listening_starts_only_after_the_question_ended_and_the_clock_starts_then(logs):
    clock = FakeClock()
    t0 = clock()
    tts = BusyTTS(clock, busy_s=35.0)  # longer than the 20 s confirm timeout (the old code gave up at 20 s)
    sink = build([None, None, "onaylıyorum"], clock=clock, tts=tts)
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "voice_phrase"
    listen_times = sink._test["listen_times"]
    assert listen_times[0] >= t0 + 35.0  # the microphone opened after the speech, not at 20 s
    assert listen_times[2] - listen_times[0] >= 10  # the window ran from the listen start: three slices fitted


def test_dialog_opens_before_the_question_and_a_click_cuts_the_question(logs):
    clock = FakeClock()
    tts = BusyTTS(clock, busy_s=30.0)
    dialog = ClockDialog(clock, "Onayla", at=clock() + 5)  # pressed while the question still plays
    sink = build(["kahve yap"], clock=clock, tts=tts, dialog=dialog)
    outcome = sink.confirm_detailed(req())
    assert sink._test["order"][:2] == ["dialog", "speak"]  # the dialog is on screen before Jarvis starts talking
    assert outcome.approved and outcome.how == "dialog"
    assert sink._test["heard"] == []  # never listened
    assert tts.stopped == 1  # the rest of the question is cut
    assert sink._test["dialog_timeouts"][0] > 20  # the dialog's own timeout covers the speech too


def test_thinking_sound_is_stopped_before_the_question_and_rearmed_after_the_feedback(logs):
    events: list[str] = []
    tts = FakeTTS()
    real_speak = tts.speak
    tts.speak = lambda text, *a, **k: (events.append(f"speak:{text[:12]}"), real_speak(text, *a, **k))
    sink = build(
        ["onaylıyorum"],
        tts=tts,
        on_attention=lambda kind: events.append(f"stop:{kind}"),
        on_attention_done=lambda kind: events.append(f"rearm:{kind}"),
    )
    sink.confirm_detailed(req())
    assert events[0] == "stop:confirm"
    assert events[-1] == "rearm:confirm" and events[-2].startswith("speak:Onaylandı")


# --- 5. every deny reason has a message, and it is spoken ------------------------------------------------


def test_every_deny_reason_has_a_message_in_both_languages():
    for reason in DENY_REASONS:
        for language in ("tr", "en"):
            text = vc.deny_message(reason, language, heard="kahve yap")
            assert text and "{heard}" not in text
    assert "kahve yap" in vc.deny_message("phrase_mismatch", "tr", heard="kahve yap")
    assert vc.deny_message("unknown_reason", "tr") == vc.deny_message("error", "tr")
    assert vc.deny_message("phrase_mismatch", "tr", heard="x" * 200).count("x") < 100  # long answers are cut


def spoken_after_question(sink) -> list[str]:
    return [t for t in sink._test["tts"].spoken if not t.startswith("Onay gerekiyor")]


def test_timeout_message(logs):
    sink = build([None] * 10)
    outcome = sink.confirm_detailed(req())
    assert outcome.reason == "timeout" and "Süre doldu" in outcome.user_message
    assert spoken_after_question(sink) == [outcome.user_message]


def test_phrase_mismatch_message_quotes_what_was_heard(logs):
    sink = build(["kahve yap"])
    outcome = sink.confirm_detailed(req())
    assert outcome.reason == "phrase_mismatch" and outcome.heard == "kahve yap"
    assert '"kahve yap"' in outcome.user_message and "onaylıyorum" in outcome.user_message
    assert spoken_after_question(sink) == [outcome.user_message]


def test_negation_message(logs):
    sink = build(["hayır"])
    outcome = sink.confirm_detailed(req())
    assert outcome.reason == "negation" and "reddettin" in outcome.user_message


def test_high_risk_tool_timeout_says_the_button_was_required(logs):
    sink = build(["onaylıyorum"] * 5)
    outcome = sink.confirm_detailed(req("run_shell", args={"command": "ls"}))
    assert outcome.reason == "voice_not_allowed_high_risk" and "Onayla düğmesi" in outcome.user_message
    assert sink._test["heard"] == []


def test_tainted_turn_timeout_says_why_voice_did_not_count(logs):
    sink = build(["onaylıyorum"] * 5)
    outcome = sink.confirm_detailed(req(tainted=True, taint_warning="untrusted: gmail"))
    assert outcome.reason == "voice_not_allowed_tainted" and "güvenilmeyen" in outcome.user_message


@pytest.mark.parametrize(
    "raw, reason, snippet",
    [
        ("Reddet", "dialog_reject", "Reddet"),
        ("cancel", "dialog_cancel", "kapattın"),
        ("�nayla", "dialog_parse_error", "okunamayan"),
    ],
)
def test_dialog_deny_messages(raw, reason, snippet, logs):
    sink = build([None] * 10, dialog=FakeDialog(raw, result_after=1))
    outcome = sink.confirm_detailed(req())
    assert outcome.reason == reason and snippet in outcome.user_message
    assert spoken_after_question(sink) == [outcome.user_message]


def test_error_in_the_listener_is_a_deny_with_reason_error(logs):
    sink = build([])

    def boom(timeout, should_stop=None):
        raise RuntimeError("microphone gone")

    sink.listen = boom
    sink._listen_takes_should_stop = True
    outcome = sink.confirm_detailed(req())
    assert not outcome.approved and outcome.reason == "error" and "hata" in outcome.user_message
    assert sink._test["dialog"].cancelled


def test_superseded_is_not_spoken(logs):
    sink = build([])
    clock = sink._test["clock"]

    def listen(timeout, should_stop=None):
        clock.advance(1)
        sink.cancel()
        return None

    sink.listen = listen
    sink._listen_takes_should_stop = True
    outcome = sink.confirm_detailed(req())
    assert outcome.reason == "superseded" and spoken_after_question(sink) == []


def test_confirmed_is_acknowledged_in_one_word(logs):
    sink = build(["Evet, onaylıyorum."])
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.user_message is None
    assert spoken_after_question(sink) == [vc.confirmed_message("tr")]


def test_audit_line_carries_the_evidence(logs):
    import json

    sink = build(["kahve yap"])
    sink.confirm_detailed(req())
    lines = [json.loads(l) for l in (logs / "audit.jsonl").read_text().splitlines() if l.strip()]
    line = [l for l in lines if l.get("decision") == "confirm_deny"][-1]
    assert line["reason"] == "phrase_mismatch" and line["heard"] == "kahve yap"
    assert line["verdicts"] == ["other:kahve yap"] and line["dialog_raw"] in (None, "cancelled")  # the fake dialog has no output
    assert line["elapsed_ms"] >= 0 and line["listen_after_ms"] is not None and line["user_message"]


# --- 6. the "evet" re-ask flow --------------------------------------------------------------------------


def test_evet_alone_is_asked_once_more_then_the_phrase_confirms(logs):
    sink = build(["evet", "onaylıyorum"])
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "voice_phrase"
    spoken = sink._test["tts"].spoken
    assert spoken.count(vc.reask_message("tr")) == 1
    assert sink._test["heard"] == ["evet", "onaylıyorum"]


def test_evet_twice_is_denied_as_vague(logs):
    sink = build(["evet", "tamam", "onaylıyorum"])
    outcome = sink.confirm_detailed(req())
    assert not outcome.approved and outcome.reason == "vague" and "Sadece" in outcome.user_message
    assert sink._test["heard"] == ["evet", "tamam"]  # the third answer was never asked for


def test_evet_then_negation_denies(logs):
    sink = build(["evet", "hayır"])
    outcome = sink.confirm_detailed(req())
    assert outcome.reason == "negation"


def test_reask_restarts_the_clock(logs):
    clock = FakeClock()
    sink = build([None, None, None, "evet", None, None, None, "onaylıyorum"], clock=clock)  # 15 s, evet, 15 s, phrase
    outcome = sink.confirm_detailed(req())
    assert outcome.approved  # without the restart the phrase would have come after the 20 s window


# --- 7. the tool result the model receives --------------------------------------------------------------


def test_model_text_states_decision_reason_and_answer():
    text = ConfirmOutcome(
        approved=False, how="voice_other", reason="phrase_mismatch", heard="kahve yap", user_message='"kahve yap" duydum'
    ).model_text("create_reminder")
    assert text.startswith("NOT RUN: create_reminder")
    assert "Reason: phrase_mismatch" in text and "'kahve yap'" in text and 'kahve yap" duydum' in text
    assert "do not say only 'not approved'" in text and "do not retry" in text
    assert "not approved" in text  # the old wording still appears for callers that look for it


class _Input(BaseModel):
    text: str = "x"


class _Tool(Tool):
    name = "echo"
    description = "echo"
    input_model = _Input
    risk = RiskLevel.CONFIRM

    def run(self, args):
        return ToolResult("ran")


class RecordingSink(OutputSink):
    def __init__(self, outcome: ConfirmOutcome):
        self.outcome = outcome

    def write_delta(self, text):
        pass

    def finish(self):
        pass

    def confirm(self, request):
        return self.outcome.approved

    def confirm_detailed(self, request):
        return self.outcome

    def notice(self, text):
        pass

    def debug(self, text):
        pass


def test_gate_raises_with_the_detailed_reason():
    tool = _Tool()
    outcome = ConfirmOutcome(approved=False, how="timeout", reason="timeout", user_message="Süre doldu")
    with pytest.raises(ToolDeniedError) as info:
        gate_module.gate(tool, _Input(), taint=TaintState(), sink=RecordingSink(outcome), user_text="")
    assert "Reason: timeout" in str(info.value) and "Süre doldu" in str(info.value)
    assert (
        gate_module.gate(tool, _Input(), taint=TaintState(), sink=RecordingSink(ConfirmOutcome(approved=True, how="dialog")), user_text="")
        == "confirmed"
    )


class PlainSink(RecordingSink):
    """A sink that only implements `confirm` (the base class wraps it)."""

    def confirm_detailed(self, request):
        return OutputSink.confirm_detailed(self, request)


def test_default_confirm_detailed_wraps_confirm():
    assert PlainSink(ConfirmOutcome(approved=False)).confirm_detailed(req()).reason == "declined"
    assert PlainSink(ConfirmOutcome(approved=True)).confirm_detailed(req()).approved
