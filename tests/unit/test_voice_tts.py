"""Text to speech pieces: sentence splitting, speakable cleaning, language routing and
the `say` queue. No sound is ever produced: the engine gets a fake runner."""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from jarvis import i18n
from jarvis.voice import tts
from jarvis.voice.tts import (
    SentenceSplitter,
    TextToSpeech,
    details_phrase,
    detect_language,
    list_phrase,
    list_voices,
    make_speakable,
    pick_voice,
    split_sentences,
    table_phrase,
    voice_quality,
)

# --- sentence splitter -----------------------------------------------------------------------------


def feed_all(deltas):
    splitter = SentenceSplitter()
    out = []
    for d in deltas:
        out.extend(splitter.feed(d))
    return out, splitter.flush()


def test_streaming_split_keeps_numbers_times_and_abbreviations():
    out, rest = feed_all(["Yarın 15:00'te diş randevun var. ", "Sonra 7.", "1 saniye koştun. Dr. Ahmet aradı.\nBitti"])
    assert out == ["Yarın 15:00'te diş randevun var.", "Sonra 7.1 saniye koştun.", "Dr. Ahmet aradı."]
    assert rest == "Bitti"


@pytest.mark.parametrize(
    "text",
    [
        "Toplam 3.5 km koştun ve bitti",
        "Fiyatı 1.000 TL oldu",
        "Sürüm v0.1 çıktı ve çalışıyor",
        "Saat 15:00 gibi gel",
        "Kalem, defter vb. al",
        "Use a tool, e.g. the agenda",
        "Virgüllü 3,5 saniye",
    ],
)
def test_no_split_inside_numbers_and_abbreviations(text):
    assert split_sentences(text) == [text]


NUMERIC_PHRASES = [
    "Maç saat 19.00'da başlıyor ve bitiyor",
    "Maç 19:00'da başlıyor",
    "Batarya %75 dolu kaldı",
    "Battery is at 75% now",
    "Sürüm 1.14 yayınlandı dün",
    "Bugün 17.10.2026 ve sıcaklık 21°C",
    "Toplantı 3'te, uçuş 08:30'da kalkıyor",
    "Fiyat 1,14 TL ve 1.500.000 lira",
    "Hız 10.5 km/s ve 3.14 sabit",
    "Version v2.0.1 shipped at 14:30:15",
    "It is 1.5 km, about 19.00 euros",
]


@pytest.mark.parametrize("text", NUMERIC_PHRASES)
def test_numbers_are_never_split_whole_or_streamed(text):
    """Times, decimals, dates, percentages, versions and suffixed numbers stay in one chunk, whether the
    text arrives at once or one character at a time (the ElevenLabs sentences are built from deltas)."""
    assert split_sentences(text) == [text]
    out, rest = feed_all(list(text))
    assert out == [] and rest == text


def test_streaming_ordinal_waits_for_the_next_word():
    """ "1. " alone used to be cut before "maddeyi" arrived: a number before a period waits for the next word."""
    out, rest = feed_all(list("1. maddeyi oku. 2. madde önemli. Bitti"))
    assert out == ["1. maddeyi oku.", "2. madde önemli."] and rest == "Bitti"
    assert split_sentences("Toplam 3. Sonra gel.") == ["Toplam 3.", "Sonra gel."]


def test_no_chunk_ends_inside_a_number_when_streamed():
    text = "Yarın 15:00'te, 17.10.2026 günü, %75 ihtimalle 1,5 saat sürer. Sonra 19.00'da maç var. Bitti."
    out, rest = feed_all(list(text))
    chunks = out + ([rest] if rest else [])
    assert " ".join(chunks) == text
    for chunk in chunks:
        assert not chunk.endswith(("19.", "15:", "17.10.", "1,"))
        assert chunk.count("'") == text.count("'") // 2 or "'" not in chunk or chunk.count("'") == 1


def test_question_and_exclamation_runs_and_ellipsis():
    assert split_sentences("Gerçekten mi?! Evet... Tamam.") == ["Gerçekten mi?!", "Evet...", "Tamam."]
    assert split_sentences('"Geldi." Sonra gitti.') == ['"Geldi."', "Sonra gitti."]


def test_initials_do_not_split():
    assert split_sentences("A. Smith geldi. Sonra gitti.") == ["A. Smith geldi.", "Sonra gitti."]


def test_newline_ends_a_sentence_and_blank_lines_are_skipped():
    out, rest = feed_all(["ilk satır\n\nikinci satır"])
    assert out == ["ilk satır"] and rest == "ikinci satır"


def test_trailing_period_waits_for_the_next_delta():
    s = SentenceSplitter()
    assert s.feed("Bitti.") == []
    assert s.pending == "Bitti."
    assert s.feed(" Devam") == ["Bitti."]
    assert s.flush() == "Devam"


def test_max_buffer_forces_a_cut_at_whitespace():
    s = SentenceSplitter(max_buffer_chars=40)
    words = " ".join(["kelime"] * 12)  # 83 chars, no punctuation
    out = s.feed(words)
    assert out and all(len(x) <= 40 for x in out)
    assert "".join(out).replace(" ", "") + s.flush().replace(" ", "") == words.replace(" ", "")


def test_split_sentences_convenience_and_empty():
    assert split_sentences("") == []
    assert split_sentences("Merhaba") == ["Merhaba"]


# --- speakable -----------------------------------------------------------------------------------------


def test_say_engine_speaks_the_normalized_form():
    """`say` gets words, not digits (voice/normalize.py runs in `speak()` for every engine)."""
    runner = FakeRunner(duration=0.01)
    engine = TextToSpeech(runner=runner, voices=VOICES)
    engine.start()
    try:
        engine.speak("Toplantı 19.00'da, %75 doluyuz.")
        engine.speak("The meeting is at 14:30, 75% full.")
        assert engine.wait(timeout=2)
    finally:
        engine.close()
    assert [p.argv[-1] for p in runner.processes] == [
        "Toplantı saat on dokuzda, yüzde yetmiş beş doluyuz.",
        "The meeting is at two thirty PM, seventy-five percent full.",
    ]


def test_make_speakable_strips_markdown_and_emoji():
    s = make_speakable("# Başlık\n**Kalın** ve *eğik* metin 🎉 bitti ✅\n> alıntı")
    assert s.text == "Başlık Kalın ve eğik metin bitti alıntı"
    assert s.details == [] and not s.had_details


def test_make_speakable_pulls_out_urls_code_and_links():
    s = make_speakable("Bak: https://example.com/a?b=1 ve [dokümana](https://docs.example.com) göz at. `uv sync` çalıştır, `tamam` de.")
    assert "https://example.com/a?b=1" in s.details and "https://docs.example.com" in s.details
    assert "uv sync" in s.details
    assert "http" not in s.text and "dokümana" in s.text and "tamam" in s.text
    assert s.had_details


def test_make_speakable_code_block_and_paths():
    s = make_speakable("Şunu çalıştır:\n```\nls -la /tmp\n```\nDosya /Users/alex/Documents/not.txt içinde, config.toml dosyasına bak.")
    assert "ls -la /tmp" in s.details
    assert "/Users/alex/Documents/not.txt" in s.details
    assert "/Users" not in s.text and "config.toml" in s.text and "```" not in s.text


def test_make_speakable_table_phrase():
    s = make_speakable("Puan durumu:\n| Takım | Puan |\n|---|---|\n| FB | 30 |\nBu kadar.", "tr")
    assert s.text == "Puan durumu: Tabloyu bildirime bıraktım. Bu kadar."
    assert any("| FB | 30 |" in d for d in s.details)


def test_make_speakable_short_list_spoken_long_list_to_details():
    short = make_speakable("Planın:\n- süt al\n- ekmek al\n- yumurta al")
    assert short.text == "Planın: süt al. ekmek al. yumurta al." and short.details == []
    long = make_speakable("Planın:\n1. a\n2. b\n3. c\n4. d\n5. e", "tr")
    assert long.text == "Planın: Listeyi bildirime bıraktım."
    assert long.details and "- d" in long.details[0]


def test_phrase_constants_exist():
    assert details_phrase("tr").endswith(".") and table_phrase("tr") and list_phrase("tr")


def test_make_speakable_phrases_follow_the_language():
    table = "Standings:\n| Team | Pts |\n|---|---|\n| FB | 30 |\nThat's it."
    assert make_speakable(table, "en").text == "Standings: I left the table in a notification. That's it."
    assert make_speakable("Plan:\n1. a\n2. b\n3. c\n4. d", "en").text == "Plan: I left the list in a notification."
    assert details_phrase("en") == "I left the details in a notification."
    i18n.set_language("tr")
    assert make_speakable(table).text.endswith("Tabloyu bildirime bıraktım. That's it.")  # default: the main language


def test_make_speakable_all_details_gives_empty_text():
    s = make_speakable("https://example.com")
    assert s.text == "" and s.details == ["https://example.com"]


# --- language -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Yarın saat 15:00'te randevun var.", "tr"),
        ("Bugun hava guzel ve sicak.", "tr"),  # no Turkish letters, Turkish words
        ("Your meeting is at 3 pm today.", "en"),
        ("Tamam, the meeting is at 3.", "en"),
        ("Sahin geldi", "tr"),  # nothing recognizable -> default
        ("", "tr"),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


def test_detect_language_default_when_unknown():
    assert detect_language("12345", default="en") == "en"


# --- voices --------------------------------------------------------------------------------------------------

SAY_LIST = """\
Albert              en_US    # Hello! My name is Albert.
Eddy (İngilizce (ABD)) en_US    # Hello! My name is Eddy.
Samantha            en_US    # Hello! My name is Samantha.
Yelda               tr_TR    # Merhaba, benim adım Yelda.
Yelda (Enhanced)    tr_TR    # Merhaba, benim adım Yelda.
"""


def fake_run(stdout: str, code: int = 0):
    def run(argv, **kw):
        assert argv == ["say", "-v", "?"] and "shell" not in kw
        return subprocess.CompletedProcess(argv, code, stdout, "")

    return run


def test_list_voices_parses_say_output():
    voices = list_voices(run=fake_run(SAY_LIST))
    assert voices["Yelda"] == "tr_TR" and voices["Yelda (Enhanced)"] == "tr_TR"
    assert voices["Samantha"] == "en_US" and voices["Eddy (İngilizce (ABD))"] == "en_US"


def test_list_voices_never_raises():
    def boom(argv, **kw):
        raise FileNotFoundError("say")

    assert list_voices(run=boom) == {}
    assert list_voices(run=fake_run("", 1)) == {}


def test_pick_voice_prefers_premium_then_enhanced_then_plain():
    voices = list_voices(run=fake_run(SAY_LIST))
    assert pick_voice("Yelda", voices) == "Yelda (Enhanced)"
    assert pick_voice("Samantha", voices) == "Samantha"
    assert pick_voice("Yelda", {**voices, "Yelda (Premium)": "tr_TR"}) == "Yelda (Premium)"
    assert pick_voice("Yelda (Enhanced)", {"Yelda": "tr_TR"}) == "Yelda"
    assert pick_voice("Nobody", voices) is None
    assert pick_voice("", voices) is None


def test_voice_quality():
    voices = list_voices(run=fake_run(SAY_LIST))
    assert voice_quality("Yelda", voices) == "enhanced"
    assert voice_quality("Samantha", voices) == "standard"
    assert voice_quality("Nobody", voices) == "missing"
    assert voice_quality("Yelda", {"Yelda (Premium)": "tr_TR"}) == "premium"


# --- engine --------------------------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, argv, duration: float = 0.05) -> None:
        self.argv = argv
        self.returncode = None
        self.killed = False
        self.stderr = None
        self._done = threading.Event()
        self._timer = threading.Timer(duration, self._finish)
        self._timer.start()

    def _finish(self) -> None:
        if self.returncode is None:
            self.returncode = 0
        self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._timer.cancel()
        self._done.set()


class FakeRunner:
    def __init__(self, duration: float = 0.05) -> None:
        self.duration = duration
        self.processes: list[FakeProcess] = []

    def __call__(self, argv):
        assert isinstance(argv, list)
        proc = FakeProcess(argv, self.duration)
        self.processes.append(proc)
        return proc


VOICES = {"Yelda": "tr_TR", "Samantha": "en_US"}


@pytest.fixture
def engine():
    runner = FakeRunner()
    tts_engine = TextToSpeech(runner=runner, voices=VOICES)
    tts_engine.start()
    yield tts_engine, runner
    tts_engine.close()


def test_speaks_in_order_with_the_voice_per_language(engine):
    tts_engine, runner = engine
    tts_engine.speak("Yarın saat üçte randevun var.")
    tts_engine.speak("The meeting is at three today.")
    assert tts_engine.wait(timeout=2)
    argvs = [p.argv for p in runner.processes]
    assert argvs[0][:5] == ["say", "-v", "Yelda", "-r", "190"] and argvs[0][-2:] == ["--", "Yarın saat üçte randevun var."]
    assert argvs[1][:5] == ["say", "-v", "Samantha", "-r", "190"] and argvs[1][-1] == "The meeting is at three today."
    assert not tts_engine.is_speaking


def test_argv_for_single_text_element_and_missing_voice():
    tts_engine = TextToSpeech(voices={"Samantha": "en_US"}, runner=FakeRunner())
    tts_engine.start()
    try:
        assert tts_engine.argv_for("a b c", "en") == ["say", "-v", "Samantha", "-r", "190", "--", "a b c"]
        assert tts_engine.voice_for("tr") is None
        assert tts_engine.argv_for("merhaba dünya", "tr") == ["say", "-r", "190", "--", "merhaba dünya"]
    finally:
        tts_engine.close()


def test_stop_kills_current_and_clears_queue_then_speaks_again(engine):
    tts_engine, runner = engine
    runner.duration = 5.0
    tts_engine.speak("birinci cümle uzun")
    tts_engine.speak("ikinci cümle")
    tts_engine.speak("üçüncü cümle")
    deadline = time.time() + 2
    while not runner.processes and time.time() < deadline:
        time.sleep(0.005)
    assert runner.processes and tts_engine.is_speaking
    before = tts_engine.generation
    tts_engine.stop()
    assert tts_engine.generation == before + 1
    assert runner.processes[0].killed
    assert tts_engine.wait(timeout=2)
    assert len(runner.processes) == 1  # the queued sentences were dropped
    runner.duration = 0.02
    tts_engine.speak("yeniden")
    assert tts_engine.wait(timeout=2)
    assert runner.processes[-1].argv[-1] == "yeniden"


def test_first_audio_fires_once_per_turn(engine):
    tts_engine, runner = engine
    fired = []
    tts_engine.on_first_audio = lambda: fired.append(time.monotonic())
    tts_engine.begin_turn()
    tts_engine.speak("bir")
    tts_engine.speak("iki")
    assert tts_engine.wait(timeout=2) and len(fired) == 1
    tts_engine.speak("üç")
    assert tts_engine.wait(timeout=2) and len(fired) == 1
    tts_engine.begin_turn()
    tts_engine.speak("dört")
    assert tts_engine.wait(timeout=2) and len(fired) == 2


def test_on_sentence_hook_and_empty_text_ignored(engine):
    tts_engine, runner = engine
    seen = []
    tts_engine.on_sentence = lambda text, voice: seen.append((text, voice))
    tts_engine.speak("   ")
    tts_engine.speak("Merhaba dünya.")
    assert tts_engine.wait(timeout=2)
    assert seen == [("Merhaba dünya.", "Yelda")] and len(runner.processes) == 1


def test_speak_stream_and_wait_timeout(engine):
    tts_engine, runner = engine
    runner.duration = 0.3
    tts_engine.speak_stream(["bir.", "iki."])
    assert tts_engine.wait(timeout=0.01) is False
    assert tts_engine.wait(timeout=3) is True
    assert [p.argv[-1] for p in runner.processes] == ["bir.", "iki."]


def test_close_joins_worker():
    runner = FakeRunner()
    tts_engine = TextToSpeech(runner=runner, voices=VOICES)
    tts_engine.start()
    tts_engine.start()  # idempotent
    worker = tts_engine._worker
    tts_engine.close()
    assert worker is not None and not worker.is_alive()


def test_default_language_routing(engine):
    tts_engine, runner = engine
    tts_engine.set_default_language("en")
    tts_engine.speak("12345")  # nothing to detect -> default
    assert tts_engine.wait(timeout=2)
    assert runner.processes[0].argv[2] == "Samantha"


def test_runner_failure_does_not_kill_worker(engine):
    tts_engine, runner = engine
    calls = []

    def flaky(argv):
        calls.append(argv)
        if len(calls) == 1:
            raise OSError("no say")
        return FakeProcess(argv, 0.01)

    tts_engine.runner = flaky
    tts_engine.speak("bir")
    tts_engine.speak("iki")
    assert tts_engine.wait(timeout=2) and len(calls) == 2


def test_default_runner_uses_popen_without_shell(monkeypatch):
    captured = {}

    class P:
        returncode = 0
        stderr = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return P()

    monkeypatch.setattr(tts.subprocess, "Popen", fake_popen)
    proc = tts._default_runner(["say", "--", "x"])
    assert isinstance(proc, P) and captured["argv"] == ["say", "--", "x"]
    assert "shell" not in captured["kw"] and captured["kw"]["stdin"] is subprocess.DEVNULL
