"""VoiceOutputSink: sentence streaming into a fake TTS, details to a notification, confirmations
with a fake dialog and a fake listener. Nothing speaks, no dialog is shown."""

from __future__ import annotations

from jarvis.io.base import ConfirmRequest
from jarvis.io.voice import VoiceOutputSink, spoken_error
from jarvis.voice import confirm as vc
from jarvis.voice.tts import details_phrase, list_phrase


class FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []
        self.calls: list[dict] = []  # text + the ElevenLabs routing flags (cacheable, local_only)
        self.stopped = 0
        self.default_language = "tr"
        self.turns = 0

    def speak(self, text, language=None, *, cacheable=False, local_only=False):
        self.spoken.append(text)
        self.calls.append({"text": text, "cacheable": cacheable, "local_only": local_only})

    def speak_stream(self, sentences):
        for s in sentences:
            self.speak(s)

    def stop(self):
        self.stopped += 1

    def wait(self, timeout=None):
        return True

    def begin_turn(self):
        self.turns += 1

    def set_default_language(self, language):
        self.default_language = language


class FakeDialog:
    """Scripted dialog: `result_after` polls, then `result`."""

    def __init__(self, result="timeout", result_after=10_000):
        self.result_value = result
        self.result_after = result_after
        self.polls = 0
        self.cancelled = False
        self.text = ""

    def poll(self):
        self.polls += 1
        return self.result_value if self.polls > self.result_after else None

    def result(self):
        return self.poll()

    def cancel(self):
        self.cancelled = True


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_sink(tts=None, *, dialog=None, listen=None, notify=None, clock=None, **kw):
    tts = tts or FakeTTS()
    clock = clock or FakeClock()
    started = []

    def starter(text, *, timeout_s):
        d = dialog or FakeDialog()
        d.text = text
        started.append(d)
        return d

    sink = VoiceOutputSink(
        tts,
        confirm_phrases=["onaylıyorum", "evet onaylıyorum", "confirm", "yes confirm"],
        confirm_timeout_s=20,
        listen=listen,
        notify=notify,
        dialog_starter=starter,
        clock=clock,
        sleep=lambda s: clock.advance(s),
        **kw,
    )
    sink.begin_turn("tr")
    return sink, tts, started


# --- streaming ----------------------------------------------------------------------------------------


def test_sentences_are_spoken_as_they_complete():
    sink, tts, _ = make_sink()
    sink.write_delta("Yarın 15:00'te diş randevun var. ")
    assert tts.spoken == ["Yarın 15:00'te diş randevun var."]
    sink.write_delta("Sonra 7.")
    assert len(tts.spoken) == 1  # "7." is not a boundary yet
    sink.write_delta("1 saniye koştun. Başka")
    assert tts.spoken[1] == "Sonra 7.1 saniye koştun."
    sink.finish()
    assert tts.spoken[-1] == "Başka" and sink.reply_text.endswith("Başka")


def test_markdown_urls_and_code_go_to_notification_not_speech():
    notes = []
    sink, tts, _ = make_sink(notify=lambda title, body: notes.append((title, body)))
    sink.write_delta("**Bak:** https://example.com/a?b=1 adresine gir.\n```\nrm -rf /tmp/x\n```\nDosya `~/Desktop/notlar.txt` içinde.\n")
    sink.finish()
    spoken = " ".join(tts.spoken)
    assert "https://" not in spoken and "rm -rf" not in spoken and "**" not in spoken and "~/Desktop" not in spoken
    assert "Bak" in spoken
    assert details_phrase("tr") in tts.spoken and tts.spoken.count(details_phrase("tr")) == 1
    assert len(notes) == 1 and "https://example.com/a?b=1" in notes[0][1] and "rm -rf /tmp/x" in notes[0][1]


def test_short_lists_spoken_long_lists_notified():
    notes = []
    sink, tts, _ = make_sink(notify=lambda t, b: notes.append(b))
    sink.write_delta("Planın:\n- kahve\n- koşu\n- toplantı\n")
    sink.finish()
    assert "kahve" in " ".join(tts.spoken) and list_phrase("tr") not in tts.spoken and not notes
    sink2, tts2, _ = make_sink(notify=lambda t, b: notes.append(b))
    sink2.write_delta("Planın:\n- a\n- b\n- c\n- d\n- e\n")
    sink2.finish()
    assert list_phrase("tr") in tts2.spoken and tts2.spoken.count(list_phrase("tr")) == 1
    assert not any(" a" == s or s == "a" for s in tts2.spoken)
    assert notes and "- d" in notes[-1]


def test_cancel_mutes_rest_of_reply():
    sink, tts, _ = make_sink()
    sink.write_delta("Birinci cümle. ")
    sink.cancel()
    sink.write_delta("İkinci cümle. ")
    sink.finish()
    assert tts.spoken == ["Birinci cümle."] and tts.stopped == 1 and sink.cancelled


def test_notice_speaks_only_errors_and_debug_is_silent():
    events = []
    sink, tts, _ = make_sink(on_event=events.append)
    sink.notice("• get_weather")
    sink.notice("Compacting earlier conversation...")
    sink.debug("thinking...")
    assert tts.spoken == [] and events == ["• get_weather", "Compacting earlier conversation..."]
    sink.notice("Error: api_error: boom")
    assert tts.spoken == [spoken_error("tr")]


def test_begin_turn_sets_language_and_resets_state():
    sink, tts, _ = make_sink()
    sink.write_delta("Hello there. ")
    sink.begin_turn("en")
    assert tts.default_language == "en" and sink.reply_text == "" and tts.turns == 2


# --- confirmations ----------------------------------------------------------------------------------------


def req(tool="google_create_event", **kw):
    return ConfirmRequest(tool=tool, args={"title": "Diş", "start": "2026-10-15 15:00"}, risk="CONFIRM", **kw)


def test_confirm_by_dialog_button(logs):
    dialog = FakeDialog("Onayla", result_after=1)
    sink, tts, started = make_sink(dialog=dialog, listen=lambda t: None)
    assert sink.confirm(req()) is True
    assert "Onaylıyor musun" in tts.spoken[0]
    assert '"title": "Diş"' in started[0].text and dialog.cancelled
    assert sink.confirmations[-1]["how"] == "dialog"


def test_confirm_by_spoken_phrase(logs):
    heard = iter([None, "Onaylıyorum."])
    clock = FakeClock()

    def listen(timeout):
        clock.advance(2)
        return next(heard)

    sink, tts, started = make_sink(listen=listen, clock=clock)
    assert sink.confirm(req()) is True
    assert sink.confirmations[-1] == {
        "tool": "google_create_event",
        "tainted": False,
        "voice_allowed": True,
        "decision": "confirm",
        "how": "voice_phrase",
        "reason": None,
        "heard": "Onaylıyorum.",
    }
    assert started[0].cancelled  # the dialog is closed once the voice answered


def test_other_words_deny_and_silence_times_out(logs):
    clock = FakeClock()

    def listen_tamam(timeout):
        clock.advance(2)
        return "tamam"

    sink, tts, _ = make_sink(listen=listen_tamam, clock=clock)
    # "tamam" alone: asked once more ("onaylıyorum de"), still only "tamam": denied for that reason
    assert sink.confirm(req()) is False and sink.confirmations[-1]["how"] == "voice_other"
    assert sink.confirmations[-1]["reason"] == "vague" and vc.reask_message("tr") in tts.spoken

    def silence(timeout):
        clock.advance(timeout)
        return None

    sink2, _, started = make_sink(listen=silence, clock=clock)
    assert sink2.confirm(req()) is False and sink2.confirmations[-1]["how"] == "timeout"
    assert started[0].cancelled


def test_ask_choice_uses_dialog_and_speaks_hint(logs):
    sink, tts, _ = make_sink(choice_dialog=lambda q, o, d, timeout_s: "once")
    answer = sink.ask_choice("Allow?", [("always", "a"), ("once", "b"), ("never", "c")], "never")
    assert answer == "once" and tts.spoken and "seçim" in tts.spoken[0]


# --- English turns ----------------------------------------------------------------------------------------


def test_english_turn_speaks_english_phrases_and_opens_an_english_dialog(logs):
    languages: list[str] = []
    dialog = FakeDialog("Confirm", result_after=1)

    def starter(text, *, timeout_s, language=None):
        languages.append(language)
        dialog.text = text
        return dialog

    sink = VoiceOutputSink(
        FakeTTS(),
        confirm_phrases=["onaylıyorum", "confirm"],
        listen=lambda t: None,
        dialog_starter=starter,
        clock=FakeClock(),
        sleep=lambda s: None,
    )
    sink.begin_turn("en")
    assert sink.confirm(req()) is True  # "Confirm" is the English dialog's own label
    assert languages == ["en"] and dialog.text.startswith("Tool: google_create_event") and "Arguments:" in dialog.text
    assert sink.tts.spoken[0].startswith("Confirmation needed: google create event") and sink.tts.spoken[-1] == "Confirmed."
    sink.write_delta("Plan:\n- a\n- b\n- c\n- d\n- e\nSee https://example.com for more.")
    sink.finish()
    assert "I left the list in a notification." in sink.tts.spoken and sink.tts.spoken[-1] == details_phrase("en")
    sink.notice("Error: boom")
    assert sink.tts.spoken[-1] == spoken_error("en") == "Something went wrong; the details are in the log."


def test_english_turn_understands_turkish_negations_and_english_vague_words(logs):
    clock = FakeClock()
    answers = iter(["yes", "yes", "hayır"])

    def listen(timeout):
        clock.advance(2)
        return next(answers)

    sink, tts, _ = make_sink(listen=listen, clock=clock)
    sink.begin_turn("en")
    assert sink.confirm(req()) is False and sink.confirmations[-1]["reason"] == "vague"
    assert vc.reask_message("en") in tts.spoken and tts.spoken[-1] == vc.deny_message("vague", "en")
    sink.begin_turn("en")
    assert sink.confirm(req()) is False and sink.confirmations[-1]["reason"] == "negation"  # "hayır" in an English turn
