"""The voice state machine with fakes for every device: wake word -> listen -> transcribe -> think
-> speak -> idle, the no-speech and hallucination paths, hold-to-talk, barge-in, cancel from every
state, timings in the database, one session per day."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.voice import service as vs
from jarvis.voice.audio import WAKE_FRAME_SAMPLES
from jarvis.voice.cues import CuePlayer
from jarvis.voice.service import VoiceService, VoiceState
from jarvis.voice.vad import SileroVad
from jarvis.voice.wakeword import WakeWordDetector
from tests.unit.test_voice_cues import FakeProc
from tests.unit.test_voice_sink import FakeTTS

HEARD, READY = "voice_heard.m4a", "voice_ready.m4a"


class CueProcs:
    """The fake `afplay` behind the service's CuePlayer. With `auto_finish` every cue ends at once;
    a test that needs a cue to keep playing clears it and calls `finish_all()` later."""

    def __init__(self):
        self.procs: list[FakeProc] = []
        self.auto_finish = True

    def popen(self, argv, **kw):
        proc = FakeProc(argv)
        if self.auto_finish:
            proc.finish()
        self.procs.append(proc)
        return proc

    @property
    def played(self) -> list[str]:
        return [proc.argv[-1].rsplit("/", 1)[-1] for proc in self.procs]

    def finish_all(self):
        for proc in self.procs:
            proc.finish()

    def running(self) -> list[str]:
        return [proc.argv[-1].rsplit("/", 1)[-1] for proc in self.procs if proc.poll() is None]


FRAME = np.zeros(WAKE_FRAME_SAMPLES, dtype=np.int16)
LOUD = np.ones(WAKE_FRAME_SAMPLES, dtype=np.int16) * 3000


class FakeMic:
    """Frames are fed from the test (`push`) and read by the service's audio thread."""

    def __init__(self):
        self.frames: list = []
        self.lock = threading.Lock()
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def push(self, *frames):
        with self.lock:
            self.frames.extend(frames)

    def read(self, timeout=None):
        deadline = time.monotonic() + (timeout or 0.05)
        while True:
            with self.lock:
                if self.frames:
                    return self.frames.pop(0)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def flush(self):
        with self.lock:
            self.frames.clear()


class FakeWakeModel:
    """Fires on any frame whose first sample is 1 (the test's marker)."""

    def __init__(self):
        self.resets = 0

    def predict(self, frame):
        return {"hey_jarvis": 0.99 if frame[0] == 1 else 0.0}

    def reset(self):
        self.resets += 1


WAKE = np.zeros(WAKE_FRAME_SAMPLES, dtype=np.int16)
WAKE[0] = 1


def fake_vad_model(chunk):
    # speech when the chunk is loud
    return 0.95 if float(np.abs(chunk).mean()) > 0.01 else 0.0


class FakeSTT:
    model = "fake"
    warmed_up = True

    def __init__(self, text="yarın ne var", language="tr", blocklist=()):
        self.text, self.language, self.blocklist = text, language, list(blocklist)
        self.calls = 0

    def warm_up(self):
        return 0.0

    def transcribe_array(self, audio, sample_rate=16000):
        self.calls += 1
        self.audio = np.asarray(audio)
        return self.text, self.language

    def clean(self, text, *, after_question=False):
        from jarvis.io.voice import clean_transcript

        return clean_transcript(text, self.blocklist, after_question=after_question)


class FakeHotkey:
    def __init__(self):
        self.is_held = False
        self.started = 0

    def start(self):
        self.started += 1

    def stop(self):
        pass


class FakeAssistant:
    def __init__(self, reply="Yarın saat onda toplantın var.", db=None, delay=0.0):
        self.reply, self.db, self.delay = reply, db, delay
        self.handled: list[str] = []
        self.sessions = 1
        self.closed = False

    def handle(self, text, sink):
        self.handled.append(text)
        time.sleep(self.delay)
        sink.write_delta(self.reply)
        sink.finish()
        return SimpleNamespace(turn_id=f"t{len(self.handled)}", reply=self.reply)

    def new_session(self, *, resume=False):
        self.sessions += 1
        self.resumed = resume

    def close(self):
        self.closed = True


class WaitingTTS(FakeTTS):
    """`wait()` returns False until the test releases it, so SPEAKING is observable."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.release.set()
        self.first_audio = None

    def speak(self, text, language=None, **kw):
        super().speak(text, language, **kw)
        if self.on_first_audio and self.first_audio is None:
            self.first_audio = text
            self.on_first_audio()

    def wait(self, timeout=None):
        return self.release.wait(timeout=timeout)

    def start(self):
        pass

    def close(self):
        pass

    on_first_audio = None
    on_sentence = None


class TimingRecorder:
    """Stands in for the Database: SQLite connections cannot cross threads in the test."""

    def __init__(self):
        self.rows: list[tuple[str, str, float]] = []
        self.follow_ups: list[dict] = []

    def add_timing(self, turn_id, stage, ms):
        self.rows.append((turn_id, stage, ms))

    def add_follow_up(self, outcome, *, window_s, turn_id=None, speech_after_ms=None):
        self.follow_ups.append({"outcome": outcome, "window_s": window_s, "turn_id": turn_id, "speech_after_ms": speech_after_ms})

    def stages(self, turn_id):
        return {stage: ms for tid, stage, ms in self.rows if tid == turn_id}


@pytest.fixture
def parts(config, paths):
    db = TimingRecorder()
    config.general.use_voice = True
    config.voice.no_speech_timeout_s = 1
    config.voice.vad_silence_ms = 200
    config.voice.follow_up_mode = "off"  # the follow-up tests below turn it on explicitly
    config.voice.sounds.thinking_enabled = False  # test_voice_thinking.py covers the loop; here the cue list stays exact
    mic, hotkey, tts = FakeMic(), FakeHotkey(), WaitingTTS()
    wake_model = FakeWakeModel()
    wake = WakeWordDetector("hey_jarvis", threshold=0.5, cooldown_s=0.0, model=wake_model)
    vad = SileroVad(model=fake_vad_model)
    stt = FakeSTT()
    assistant = FakeAssistant(db=db)
    sounds: list[str] = []
    notes: list[tuple[str, str]] = []
    lines: list[str] = []
    cue_procs = CueProcs()
    paths.sounds_dir.mkdir(exist_ok=True)
    for name in (HEARD, READY):
        (paths.sounds_dir / name).write_bytes(b"m4a")
    cues = CuePlayer(paths.sounds_dir, config.voice.sounds, popen=cue_procs.popen, which=lambda n: "/usr/bin/afplay")
    service = VoiceService(
        config,
        paths,
        foreground=True,
        assistant_factory=lambda c, p: assistant,
        mic=mic,
        wake=wake,
        vad=vad,
        stt=stt,
        tts=tts,
        hotkey=hotkey,
        notify=lambda t, b: notes.append((t, b)),
        reporter=lines.append,
        play_sound=sounds.append,
        db=db,
        cues=cues,
    )
    yield SimpleNamespace(
        service=service,
        mic=mic,
        hotkey=hotkey,
        tts=tts,
        wake_model=wake_model,
        stt=stt,
        cues=cue_procs,
        assistant=assistant,
        sounds=sounds,
        notes=notes,
        lines=lines,
        db=db,
        config=config,
        paths=paths,
    )
    service.close()


def run_in_thread(service):
    t = threading.Thread(target=service.run, daemon=True)
    t.start()
    for _ in range(200):
        if service.state in (VoiceState.IDLE, VoiceState.PAUSED) and service._audio_thread is not None:
            break
        time.sleep(0.01)
    return t


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def speak_utterance(p, *, loud_frames=6, silence_frames=8):
    """Wake word, then speech, then silence long enough for the VAD to end the recording."""
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([LOUD] * loud_frames), *([FRAME] * silence_frames))


# --- the happy path -------------------------------------------------------------------------------------


def test_wake_word_turn_end_to_end(parts, logs):
    p = parts
    run_in_thread(p.service)
    assert p.service.state == VoiceState.IDLE and p.hotkey.started == 1
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and len(p.service.transitions) >= 5)
    states = [t[1] for t in p.service.transitions]
    assert states[:5] == [VoiceState.LISTENING, VoiceState.TRANSCRIBING, VoiceState.THINKING, VoiceState.SPEAKING, VoiceState.IDLE]
    assert p.service.transitions[0][0] == VoiceState.IDLE
    assert p.sounds[0] == p.config.voice.listen_sound
    assert p.tts.spoken == ["Yarın saat onda toplantın var."]
    assert any("Anladığım: yarın ne var" in line for line in p.lines)
    assert wait_until(lambda: "tts_total" in p.db.stages("t1"))
    stages = p.db.stages("t1")
    assert {"wake_detected", "vad_end", "stt", "first_audio", "tts_total"} <= set(stages)
    assert stages["first_audio"] >= 0 and stages["tts_total"] >= stages["first_audio"]
    assert p.wake_model.resets >= 1
    p.service.request_stop()
    assert wait_until(lambda: p.assistant.closed)


def test_no_speech_after_wake_word_returns_to_idle_without_api(parts):
    p = parts
    run_in_thread(p.service)
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([FRAME] * 40))  # 40 * 80 ms = 3.2 s of silence > no_speech_timeout 1 s
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and len(p.service.transitions) >= 2)
    assert p.service.transitions[-1][2] == "no speech"
    assert p.assistant.handled == [] and p.stt.calls == 0
    assert p.config.voice.no_speech_sound in p.sounds
    p.service.request_stop()


def test_hallucination_transcript_is_dropped(parts):
    p = parts
    p.stt.text, p.stt.blocklist = "Altyazı M.K.", ["Altyazı M.K."]
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.stt.calls == 1)
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "empty transcript" for t in p.service.transitions))
    assert p.assistant.handled == []
    p.service.request_stop()


def test_language_fallback_only_tr_en_set_on_sink(parts):
    p = parts
    p.stt.language = "en"
    p.stt.text = "what is on tomorrow"
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["what is on tomorrow"])
    assert p.tts.default_language == "en"
    p.service.request_stop()


# --- hotkey ----------------------------------------------------------------------------------------------


def test_hold_to_talk_records_until_release(parts):
    p = parts
    run_in_thread(p.service)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([LOUD] * 5))
    time.sleep(0.1)
    p.hotkey.is_held = False
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert p.sounds[0] == p.config.voice.listen_sound
    p.service.request_stop()


def test_hotkey_interrupts_speech_and_listens_again(parts):
    p = parts
    p.tts.release.clear()  # Jarvis "keeps speaking" until released
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.SPEAKING)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: p.tts.stopped >= 1)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING, timeout=5)  # barge-in -> straight to listening
    p.mic.push(*([LOUD] * 5))
    time.sleep(0.1)
    p.hotkey.is_held = False
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    assert any(t[2] == "interrupted" for t in p.service.transitions)
    p.service.request_stop()


def test_wake_word_is_ignored_during_the_post_speech_cooldown(parts):
    """`reset()` clears any suppression, so it must run before `suppress(post_speech_cooldown_s)`."""
    p = parts
    p.config.voice.post_speech_cooldown_s = 1.0
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and len(p.service.transitions) >= 5)
    p.mic.push(WAKE)
    time.sleep(0.3)
    assert p.service.state == VoiceState.IDLE and len(p.assistant.handled) == 1  # the speakers cannot wake Jarvis
    time.sleep(0.8)
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)  # after the cooldown it works again
    p.service.request_stop()


def test_wake_word_ignored_while_speaking_unless_barge_in_enabled(parts):
    p = parts
    p.tts.release.clear()
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.SPEAKING)
    p.mic.push(WAKE, WAKE)
    time.sleep(0.2)
    assert p.service.state == VoiceState.SPEAKING and p.tts.stopped == 0
    p.tts.release.set()
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "turn done" for t in p.service.transitions))
    p.service.request_stop()


def test_wake_word_barge_in_when_enabled(parts):
    p = parts
    p.config.voice.wake_word_barge_in = True
    p.tts.release.clear()
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.SPEAKING)
    p.service.wake.reset()  # the fixture's post-speech suppression does not apply yet; make sure nothing lingers
    p.mic.push(WAKE)
    assert wait_until(lambda: p.tts.stopped >= 1)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.service.request_stop()


# --- cancel from every state -----------------------------------------------------------------------------


def test_cancel_while_listening(parts):
    p = parts
    run_in_thread(p.service)
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.service.cancel("test")
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "cancelled" for t in p.service.transitions))
    assert p.assistant.handled == []
    p.service.request_stop()


def test_cancel_while_thinking_mutes_reply(parts):
    p = parts
    p.assistant.delay = 0.3
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.THINKING)
    p.service.cancel("test")
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "interrupted" for t in p.service.transitions))
    assert p.tts.spoken == []  # the reply arrived after the cancel and was muted
    p.service.request_stop()


def test_cancel_while_speaking(parts):
    p = parts
    p.tts.release.clear()
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.SPEAKING)
    p.service.cancel("test")
    p.tts.release.set()
    assert wait_until(lambda: p.service.state == VoiceState.IDLE)
    assert p.tts.stopped >= 1
    p.service.request_stop()


def test_cancel_in_idle_is_harmless_and_stop_ends_run(parts):
    p = parts
    t = run_in_thread(p.service)
    p.service.cancel("idle")
    assert p.service.state == VoiceState.IDLE
    p.service.request_stop()
    t.join(timeout=5)
    assert not t.is_alive() and p.service.state == VoiceState.STOPPED and p.mic.stopped >= 1
    assert p.service._assistant is None  # never created: no turn happened


# --- sessions and misc ---------------------------------------------------------------------------------------


def test_one_session_per_day(parts, monkeypatch):
    p = parts
    p.service._ensure_assistant()
    assert p.assistant.sessions == 1
    p.service._session_date = "2000-01-01"
    p.service._ensure_assistant()
    assert p.assistant.sessions == 2


def test_turn_timings_compute():
    t = vs.TurnTimings(trigger_at=10.0, record_start=10.2, speech_end=12.0, stt_ms=250.0, first_audio=13.0, tts_done=15.0)
    stages = t.compute()
    assert stages == pytest.approx({"wake_detected": 200.0, "vad_end": 1800.0, "stt": 250.0, "first_audio": 1000.0, "tts_total": 3000.0})
    assert vs.TurnTimings().compute() == {}


def test_preflight_requires_use_voice(config):
    config.general.use_voice = False
    problems = vs.preflight(config)
    assert any("use_voice" in p for p in problems)
    config.general.use_voice = True
    assert not any("use_voice" in p for p in vs.preflight(config))


def test_missing_dependencies_reports_names(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "pynput" else real(name))
    assert VoiceService.missing_dependencies() == ["pynput"]


def test_run_service_refuses_when_disabled(config, paths):
    config.general.use_voice = False
    out = []
    assert vs.run_service(config, paths, out=out.append) == 1
    assert any("use_voice" in line for line in out)


# --- pause / resume: the microphone is released, the file is the truth, every way in and out ----------------


def _pause_file(p):
    return p.service.paths.voice_pause_file


def test_pause_phrase_releases_the_microphone_without_api_call(parts):
    p = parts
    p.stt.text = "Sesi kapat."
    run_in_thread(p.service)
    mic_stops = p.mic.stopped
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    assert p.assistant.handled == [] and p.service.paused
    assert p.mic.stopped == mic_stops + 1  # the audio stream is closed: the orange dot disappears
    assert _pause_file(p).is_file()
    assert p.tts.spoken == ["Tamam, dinlemeyi durduruyorum."] and p.tts.calls[-1]["cacheable"] is True
    assert p.notes[-1] == ("Jarvis", "🔇 Jarvis dinlemiyor")
    assert p.service.transitions[-1][2] == "paused (voice)"
    # The wake word cannot trigger while paused: no state change, no transcription, no API call.
    p.stt.text = "yarın ne var"
    p.mic.push(WAKE, *([LOUD] * 6), *([FRAME] * 8))
    time.sleep(0.3)
    assert p.service.state == VoiceState.PAUSED and p.assistant.handled == [] and p.stt.calls == 1
    p.service.request_stop()


def test_pause_phrase_with_duration_sets_auto_resume(parts):
    from jarvis.voice import pause as vpause

    p = parts
    p.stt.text = "1 saat sesi kapat"
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    state = vpause.load_state(_pause_file(p))
    assert state.by == "voice" and 59 <= state.remaining_minutes() <= 60
    assert p.tts.spoken == ["Tamam, 1 saat dinlemiyorum."]
    assert p.notes[-1][1] == "🔇 Jarvis dinlemiyor (60 dk)"
    p.service.request_stop()


def test_english_utterance_gets_english_echo_acknowledgement_and_notification(parts):
    from jarvis.voice import pause as vpause

    p = parts
    p.stt.text, p.stt.language = "stop listening 1 hour", "en"
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    assert vpause.load_state(_pause_file(p)).by == "voice"
    assert any("🎙️ Heard: stop listening 1 hour  [en" in line for line in p.lines)
    assert p.tts.spoken == ["Okay, not listening for 1 hour."]
    assert p.notes[-1][1] == "🔇 Jarvis is not listening (60 min)"
    p.service.request_stop()


def test_starts_paused_with_the_microphone_closed_and_resumes_from_the_cli(parts):
    from jarvis.voice import pause as vpause

    p = parts
    vpause.pause(_pause_file(p), by="cli")  # survives a restart: the file is read at start
    run_in_thread(p.service)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED and p.service._audio_thread is not None)
    assert p.mic.started == 0  # never opened
    assert p.notes[-1][1] == "🔇 Jarvis dinlemiyor"
    p.mic.push(WAKE)
    time.sleep(0.2)
    assert p.service.state == VoiceState.PAUSED
    assert vpause.resume(_pause_file(p))  # `jarvis voice resume`
    assert wait_until(lambda: p.service.state == VoiceState.IDLE, timeout=3.0)  # polled on a quiet mic too
    assert p.mic.started == 1 and not p.service.paused
    assert p.notes[-1] == ("Jarvis", "🎙️ Jarvis tekrar dinliyor")
    assert p.service.transitions[-1] == (VoiceState.PAUSED, VoiceState.IDLE, "resumed (jarvis voice resume)")
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    p.service.request_stop()


def test_pause_from_cli_or_telegram_while_running_closes_the_mic(parts):
    from jarvis.voice import pause as vpause

    p = parts
    run_in_thread(p.service)
    assert p.mic.started == 1
    vpause.pause(_pause_file(p), by="telegram", minutes=30)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED, timeout=3.0)
    assert p.mic.stopped == 1 and p.notes[-1][1] == "🔇 Jarvis dinlemiyor (30 dk)"
    assert p.service.transitions[-1][2] == "paused (/mute)"
    p.service.request_stop()


def test_auto_resume_when_the_pause_time_is_over(parts):
    from datetime import timedelta

    from jarvis.utils.timeutil import now_utc
    from jarvis.voice import pause as vpause

    p = parts
    vpause.save_state(
        _pause_file(p),
        vpause.PauseState(paused=True, paused_at=vpause._iso(now_utc()), until=vpause._iso(now_utc() + timedelta(seconds=1.5)), by="cli"),
    )
    run_in_thread(p.service)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED and p.service._audio_thread is not None)
    assert p.service.state == VoiceState.PAUSED and p.mic.started == 0
    t0 = time.monotonic()
    assert wait_until(lambda: p.service.state == VoiceState.IDLE, timeout=5.0)
    assert 1.0 <= time.monotonic() - t0 <= 4.0  # ~1.5 s pause + up to one poll interval
    assert not _pause_file(p).exists() and p.mic.started == 1
    assert p.service.transitions[-1][2] == "resumed (pause time over)"
    assert p.notes[-1][1] == "🎙️ Jarvis tekrar dinliyor"
    p.service.request_stop()


def test_tool_pause_in_a_turn_takes_effect_right_after_the_reply(parts):
    """The model called `pause_listening`: the file appears mid-turn; the reply is spoken, then the mic is released."""
    from jarvis.voice import pause as vpause

    p = parts
    original = p.assistant.handle

    def handle(text, sink):
        vpause.pause(_pause_file(p), by="voice")
        return original(text, sink)

    p.assistant.handle = handle
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    assert p.tts.spoken == ["Yarın saat onda toplantın var."] and p.mic.stopped == 1
    assert p.service.transitions[-2][1] == VoiceState.SPEAKING
    p.service.request_stop()


@pytest.mark.parametrize("answer, resumes", [("yes", True), ("no", False), ("timeout", False)])
def test_hotkey_while_paused_asks_a_dialog(parts, answer, resumes):
    from jarvis.voice import pause as vpause

    p = parts
    vpause.pause(_pause_file(p), by="voice")
    asked: list[tuple] = []

    def dialog(question, options, default, *, timeout_s):
        asked.append((question, options, default, timeout_s))
        return answer if answer != "timeout" else default

    p.service._choice_dialog = dialog
    run_in_thread(p.service)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED and p.service._audio_thread is not None)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: len(asked) == 1)
    p.hotkey.is_held = False
    assert asked[0][0] == "Dinleme duraklatılmış. Dinlemeye devam edeyim mi?"
    assert asked[0][1] == [("no", "Hayır"), ("yes", "Evet")] and asked[0][2] == "no"
    if resumes:
        assert wait_until(lambda: p.service.state == VoiceState.IDLE and not p.service.paused)
        assert p.mic.started == 1 and not _pause_file(p).exists()
        assert p.service.transitions[-1][2] == "resumed (hotkey dialog)"
    else:
        time.sleep(0.3)
        assert p.service.state == VoiceState.PAUSED and _pause_file(p).is_file() and p.mic.started == 0
    assert p.assistant.handled == [] and p.stt.calls == 0  # the hold never became a recording
    p.service.request_stop()


def test_empty_pause_phrases_leave_it_to_the_tool(parts):
    from jarvis.voice import pause as vpause

    p = parts
    p.config.voice.pause_phrases = []
    assert vpause.match_pause_phrase("sesi kapat", list(p.config.voice.pause_phrases)) == (False, None)


# --- follow-up window: listening after a reply without the wake word ------------------------------------------


def _follow_up_setup(p, *, mode="always", window_s=1.0):
    p.config.voice.follow_up_mode = mode
    p.config.voice.follow_up_window_s = window_s
    run_in_thread(p.service)


def _wait_for_window(p, *, count=1):
    """The window is a LISTENING state entered without a trigger, after the reply was played to the end."""
    return wait_until(
        lambda: sum(1 for t in p.service.transitions if t[2] == "follow-up window") >= count and p.service.state == VoiceState.LISTENING
    )


def test_follow_up_window_opens_after_every_reply_and_chains(parts):
    p = parts
    _follow_up_setup(p)
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert _wait_for_window(p)
    assert p.cues.played.count(READY) == 1 and p.cues.played[-1] == READY
    # No wake word: just talk.
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    assert wait_until(lambda: len(p.db.follow_ups) == 1 and p.db.follow_ups[0]["turn_id"] == "t2")
    row = p.db.follow_ups[0]
    assert row["outcome"] == "turn" and row["window_s"] == 1.0 and row["speech_after_ms"] is not None and row["speech_after_ms"] < 500
    assert any("Anladığım: yarın ne var  [tr" in line and "(follow-up)" in line for line in p.lines)
    # ... and again after the follow-up's reply: the conversation continues until silence.
    assert _wait_for_window(p, count=2)
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: len(p.assistant.handled) == 3)
    assert wait_until(lambda: len(p.db.follow_ups) == 2 and p.db.follow_ups[1]["turn_id"] == "t3")
    assert p.sounds.count(p.config.voice.listen_sound) == 1  # only the first turn needed the wake word
    p.service.request_stop()


def test_follow_up_window_closes_silently_on_silence(parts):
    p = parts
    _follow_up_setup(p)
    speak_utterance(p)
    assert _wait_for_window(p)
    sounds_before, cues_before = len(p.sounds), len(p.cues.played)
    p.mic.push(*([FRAME] * 15))  # 1.2 s of silence > the 1 s window
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and p.service.transitions[-1][2] == "follow-up: silence")
    assert len(p.sounds) == sounds_before  # no "no speech" sound: the window closes quietly
    assert len(p.cues.played) == cues_before  # and no "heard" cue for an empty window
    assert p.db.follow_ups == [{"outcome": "silence", "window_s": 1.0, "turn_id": None, "speech_after_ms": None}]
    assert len(p.assistant.handled) == 1 and p.stt.calls == 1
    # Back in wake-word mode: speech without "Hey Jarvis" does nothing now.
    time.sleep(p.config.voice.post_speech_cooldown_s)
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    time.sleep(0.3)
    assert p.service.state == VoiceState.IDLE and len(p.assistant.handled) == 1
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.service.request_stop()


def test_follow_up_noise_is_dropped_without_a_turn(parts):
    p = parts
    p.stt.blocklist = ["Altyazı M.K."]
    _follow_up_setup(p)
    speak_utterance(p)
    assert _wait_for_window(p)
    p.stt.text = "Altyazı M.K."  # a cough, the TV: the transcript filters drop it
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and p.service.transitions[-1][2] == "follow-up: noise")
    assert p.db.follow_ups[0]["outcome"] == "noise" and p.db.follow_ups[0]["speech_after_ms"] is not None
    assert len(p.assistant.handled) == 1 and p.stt.calls == 2
    assert p.config.voice.no_speech_sound not in p.sounds
    p.service.request_stop()


def test_follow_up_only_after_questions_in_questions_only_mode(parts):
    p = parts
    _follow_up_setup(p, mode="questions_only")
    speak_utterance(p)  # the reply is a statement
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and len(p.service.transitions) >= 5)
    time.sleep(0.3)
    assert p.service.state == VoiceState.IDLE and READY not in p.cues.played
    assert not any(t[2] == "follow-up window" for t in p.service.transitions)
    p.assistant.reply = "Toplantıyı 15:00'e alayım mı?"
    time.sleep(p.config.voice.post_speech_cooldown_s)
    speak_utterance(p)
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    assert _wait_for_window(p)
    assert p.cues.played[-1] == READY
    p.service.request_stop()


@pytest.mark.parametrize(
    "reply, question",
    [
        ("Yarın ne yapalım?", True),
        ("Onaylıyor musun? 🙂", True),
        ('"Hazır mısın?"', True),
        ("Toplantı 10'da.", False),
        ("Tamam.", False),
        ("", False),
    ],
)
def test_is_question(reply, question):
    assert VoiceService._is_question(reply) is question


def test_follow_up_is_off_when_disabled(parts):
    p = parts
    _follow_up_setup(p, mode="off")
    p.assistant.reply = "Onaylıyor musun?"
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and len(p.service.transitions) >= 5)
    time.sleep(0.3)
    assert p.service.state == VoiceState.IDLE and READY not in p.cues.played
    assert p.db.follow_ups == []
    p.service.request_stop()


def test_follow_up_window_opens_only_after_playback_ends(parts):
    p = parts
    p.tts.release.clear()  # Jarvis "keeps speaking" until the test releases it
    _follow_up_setup(p)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.SPEAKING)
    time.sleep(0.4)
    assert p.service.state == VoiceState.SPEAKING and READY not in p.cues.played
    p.tts.release.set()  # playback ends now
    assert _wait_for_window(p)
    assert p.cues.played[-1] == READY
    p.service.request_stop()


def test_follow_up_speech_that_outlasts_the_window_is_fully_recorded(parts):
    p = parts
    _follow_up_setup(p, window_s=1.0)
    speak_utterance(p)
    assert _wait_for_window(p)
    p.mic.push(*([LOUD] * 30), *([FRAME] * 8))  # 2.4 s of speech, started inside the 1 s window
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    recorded = [line for line in p.lines if "recording:" in line][-1]
    ms = float(recorded.split("recording: ")[1].split(" ms")[0])
    assert ms >= 2400 and "end_of_speech" in recorded  # the window limits the start, not the length
    assert wait_until(lambda: p.db.follow_ups)  # recorded once the reply has been played, after `handled` grows
    assert p.db.follow_ups[0]["outcome"] == "turn"
    p.service.request_stop()


def test_no_follow_up_window_while_paused(parts):
    from jarvis.voice import pause as vpause

    p = parts
    original = p.assistant.handle

    def handle(text, sink):
        vpause.pause(_pause_file(p), by="voice")  # the model called `pause_listening` in this turn
        return original(text, sink)

    p.assistant.handle = handle
    _follow_up_setup(p)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    time.sleep(0.3)
    assert p.service.state == VoiceState.PAUSED and READY not in p.cues.played
    assert not any(t[2] == "follow-up window" for t in p.service.transitions) and p.db.follow_ups == []
    p.service.request_stop()


def test_pause_phrase_in_a_follow_up_pauses_immediately(parts):
    p = parts
    _follow_up_setup(p)
    speak_utterance(p)
    assert _wait_for_window(p)
    p.stt.text = "sesi kapat"
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    assert len(p.assistant.handled) == 1 and p.service.paused and _pause_file(p).is_file()
    assert p.db.follow_ups[0]["outcome"] == "pause"
    assert p.notes[-1] == ("Jarvis", "🔇 Jarvis dinlemiyor")
    p.service.request_stop()


def test_hotkey_during_the_follow_up_window_takes_over(parts):
    p = parts
    _follow_up_setup(p, window_s=5.0)
    speak_utterance(p)
    assert _wait_for_window(p)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: any(t[2] == "follow-up: cancelled" for t in p.service.transitions))
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING and p.service._listening_kind == "hotkey")
    p.mic.push(*([LOUD] * 5))
    time.sleep(0.1)
    p.hotkey.is_held = False
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    assert p.db.follow_ups[0]["outcome"] == "cancelled"
    p.service.request_stop()


# --- cue sounds: "heard" after the recording, "ready" before the follow-up window ------------------------------


def test_heard_cue_plays_after_the_recording_and_is_stopped_by_the_first_audio(parts):
    p = parts
    p.cues.auto_finish = False  # the cue "keeps playing" until something stops it
    seen: dict = {}
    original = p.stt.transcribe_array

    def transcribe(audio, sample_rate=16000):
        seen["playing_during_stt"] = p.cues.running()
        seen["state"] = p.service.state
        return original(audio, sample_rate)

    p.stt.transcribe_array = transcribe
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    # Non-blocking: afplay was started before the transcription, which ran while it played.
    assert seen["playing_during_stt"] == [HEARD] and seen["state"] == VoiceState.TRANSCRIBING
    assert p.cues.played == [HEARD]
    # The reply's first audio stopped it, so cue and reply never overlap.
    assert wait_until(lambda: p.cues.procs[0].killed)
    assert any("cue heard stopped (first audio)" in line for line in p.lines)
    assert p.cues.procs[0].argv[:3] == ["/usr/bin/afplay", "-v", "1"]
    p.service.request_stop()


def test_heard_cue_plays_on_hotkey_release(parts):
    p = parts
    run_in_thread(p.service)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([LOUD] * 5))
    time.sleep(0.1)
    assert p.cues.played == []  # nothing while the key is still held
    p.hotkey.is_held = False
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert p.cues.played == [HEARD]
    p.service.request_stop()


def test_no_heard_cue_for_empty_or_cancelled_recordings(parts):
    p = parts
    run_in_thread(p.service)
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([FRAME] * 40))  # silence only: no speech
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and p.service.transitions[-1][2] == "no speech")
    assert p.cues.played == [] and p.config.voice.no_speech_sound in p.sounds
    time.sleep(p.config.voice.post_speech_cooldown_s)
    p.mic.push(WAKE)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING and p.service._listening_kind == "wake")
    p.service.cancel("test")  # cancelled before anything was said
    assert wait_until(lambda: p.service.state == VoiceState.IDLE)
    assert p.cues.played == []
    p.service.request_stop()


def test_heard_cue_is_cut_when_the_transcript_is_dropped_as_noise(parts):
    p = parts
    p.cues.auto_finish = False
    p.stt.text, p.stt.blocklist = "Altyazı M.K.", ["Altyazı M.K."]
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: any(t[2] == "empty transcript" for t in p.service.transitions))
    assert p.cues.played == [HEARD] and p.cues.procs[0].killed  # cut the moment the filters dropped it
    assert p.assistant.handled == [] and p.config.voice.no_speech_sound in p.sounds
    p.service.request_stop()


def test_ready_cue_blocks_the_follow_up_window_until_it_has_finished(parts):
    p = parts
    _follow_up_setup(p, window_s=1.0)
    p.cues.auto_finish = False
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert wait_until(lambda: READY in p.cues.running())
    time.sleep(0.3)
    # Still no window: the cue is playing and the microphone is not recording.
    assert not any(t[2] == "follow-up window" for t in p.service.transitions)
    assert p.service.state == VoiceState.IDLE and not p.service._capturing
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))  # speech during the cue is not a follow-up (and no "heard" cue)
    time.sleep(0.3)
    assert len(p.assistant.handled) == 1 and p.cues.played.count(HEARD) == 1
    released_at = time.monotonic()
    p.cues.finish_all()
    assert _wait_for_window(p)
    assert p.service._timings.trigger_at >= released_at - 0.05  # the window's seconds start after the cue
    assert any("follow-up window: 1 s" in line for line in p.lines)
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    assert wait_until(lambda: p.db.follow_ups)
    assert p.db.follow_ups[0]["outcome"] == "turn"
    p.service.request_stop()


def test_ready_cue_is_killed_by_the_hotkey_and_no_window_opens(parts):
    p = parts
    _follow_up_setup(p, window_s=5.0)
    p.cues.auto_finish = False
    speak_utterance(p)
    assert wait_until(lambda: READY in p.cues.running())
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: p.cues.procs[-1].killed)
    assert wait_until(lambda: any("follow-up window skipped" in line for line in p.lines))
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING and p.service._listening_kind == "hotkey")
    assert not any(t[2] == "follow-up window" for t in p.service.transitions) and p.db.follow_ups == []
    p.mic.push(*([LOUD] * 5))
    time.sleep(0.1)
    p.hotkey.is_held = False
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    p.service.request_stop()


def test_barge_in_stops_the_heard_cue(parts):
    p = parts
    p.cues.auto_finish = False
    p.assistant.delay = 1.0  # a slow model: the "heard" cue is still playing while THINKING
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.THINKING and p.cues.running() == [HEARD])
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert p.cues.procs[0].killed  # immediately, on the hotkey thread
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING and p.service._listening_kind == "hotkey", timeout=5)
    p.hotkey.is_held = False
    p.service.request_stop()


def test_barge_in_while_speaking_stops_cues_too(parts):
    p = parts
    p.cues.auto_finish = False
    p.tts.release.clear()
    run_in_thread(p.service)
    p.service.cues.play("heard")  # whatever is playing when speech is interrupted
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.SPEAKING)
    p.service.cues.play("ready")
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    assert wait_until(lambda: all(proc.killed for proc in p.cues.procs))
    p.hotkey.is_held = False
    p.tts.release.set()
    p.service.request_stop()


def test_no_cues_while_paused(parts):
    from jarvis.voice import pause as vpause

    p = parts
    _follow_up_setup(p)
    # The heard cue checks the pause flag itself.
    p.service._paused = True
    p.service._play_heard_cue()
    assert p.cues.played == []
    p.service._paused = False
    # A pause that lands during the turn: no ready cue, no window.
    original = p.assistant.handle

    def handle(text, sink):
        vpause.pause(_pause_file(p), by="voice")
        return original(text, sink)

    p.assistant.handle = handle
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED)
    time.sleep(0.3)
    assert p.cues.played == [HEARD]  # recorded before the pause arrived; never the ready cue
    p.service.request_stop()


def test_disabled_cues_play_nothing_in_the_service(parts):
    p = parts
    p.config.voice.sounds.heard_enabled = False
    p.config.voice.sounds.ready_enabled = False
    _follow_up_setup(p)
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert _wait_for_window(p)  # the window still opens, just without the cue
    assert p.cues.played == [] and p.config.voice.listen_sound in p.sounds
    p.service.request_stop()


def test_missing_cue_files_fall_back_to_system_sounds_without_crashing(parts, tmp_path):
    p = parts
    for name in (HEARD, READY):
        (p.paths.sounds_dir / name).unlink()
    bottle, purr = tmp_path / "Bottle.aiff", tmp_path / "Purr.aiff"
    bottle.write_bytes(b"x")
    purr.write_bytes(b"x")
    p.service.cues._fallbacks = {"heard": str(bottle), "ready": str(purr)}
    _follow_up_setup(p)
    assert any("cue heard:" in line and "fallback" in line for line in p.lines)
    speak_utterance(p)
    assert _wait_for_window(p)
    assert p.cues.played == ["Bottle.aiff", "Purr.aiff"]
    p.service.request_stop()


# --- microphone watchdog: a stream that stops delivering frames is reopened ---------------------------------


def test_silent_microphone_is_reopened_and_the_wake_word_works_again(parts):
    p = parts
    p.service.mic_stall_s = 0.3
    run_in_thread(p.service)
    started_before = p.mic.started
    assert wait_until(lambda: p.mic.stopped >= 1 and p.mic.started == started_before + 1, timeout=3.0)
    assert p.service.mic_restarts == 1
    assert any("reopening the input stream" in line for line in p.lines)
    # one reopen per MIC_RESTART_MIN_GAP_S, no spinning on a dead device
    time.sleep(0.8)
    assert p.service.mic_restarts == 1
    # the stream is back: a wake word is still heard
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and len(p.service.transitions) >= 5)


def test_no_microphone_reopen_while_paused(parts):
    p = parts
    from jarvis.voice import pause as vpause

    p.service.mic_stall_s = 0.3
    vpause.pause(_pause_file(p), by="cli")
    run_in_thread(p.service)
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED and p.service._audio_thread is not None)
    time.sleep(1.0)
    assert p.service.mic_restarts == 0 and p.mic.started == 0


# --- short answers to a question Jarvis asked (in-session memory, part 1) ----------------------------------


def test_one_letter_answer_after_a_question_is_not_dropped_as_noise(parts):
    """Jarvis asked something; "E" (a clipped "evet") is a full answer, not noise, and reaches the model as is."""
    p = parts
    p.assistant.reply = "Takvime ekleyeyim mi?"
    _follow_up_setup(p)
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    assert _wait_for_window(p)
    assert p.service._last_reply_was_question
    p.stt.text = "E"
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var", "E"])
    assert p.db.follow_ups[0]["outcome"] == "turn"
    p.service.request_stop()


def test_plain_evet_after_a_model_question_is_a_normal_turn_not_a_confirmation(parts):
    p = parts
    p.assistant.reply = "Spor saatini 20:00 olarak ekleyeyim mi?"
    _follow_up_setup(p)
    speak_utterance(p)
    assert _wait_for_window(p)
    p.stt.text = "Evet."
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var", "Evet."])
    assert p.service._sink.confirmations == []  # the phrase matcher never saw it: no confirmation was pending
    p.service.request_stop()


def test_one_letter_transcript_is_still_noise_when_nothing_was_asked(parts):
    p = parts
    p.assistant.reply = "Yarın saat onda toplantın var."
    _follow_up_setup(p)
    speak_utterance(p)
    assert _wait_for_window(p)
    assert not p.service._last_reply_was_question
    p.stt.text = "E"
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and p.service.transitions[-1][2] == "follow-up: noise")
    assert len(p.assistant.handled) == 1
    p.service.request_stop()


def test_hallucination_is_dropped_even_after_a_question(parts):
    p = parts
    p.assistant.reply = "Takvime ekleyeyim mi?"
    p.stt.blocklist = ["Thank you."]
    _follow_up_setup(p)
    speak_utterance(p)
    assert _wait_for_window(p)
    p.stt.text = "Thank you."
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and p.service.transitions[-1][2] == "follow-up: noise")
    assert len(p.assistant.handled) == 1
    p.service.request_stop()


def test_daily_rollover_resumes_the_conversation(parts):
    p = parts
    p.service._ensure_assistant()
    p.service._session_date = "2000-01-01"
    p.service._ensure_assistant()
    assert p.assistant.sessions == 2 and p.assistant.resumed is True


def test_second_voice_service_refuses_to_start_while_one_holds_the_lock(config, paths, monkeypatch):
    config.general.use_voice = True
    monkeypatch.setattr(vs, "preflight", lambda c: [])
    monkeypatch.setattr(vs.vaudio, "microphone_permission", lambda: "granted")
    held = vs.acquire_service_lock(paths)
    assert held is not None and paths.voice_lock_file.read_text().strip().isdigit()
    out: list[str] = []
    built = []
    monkeypatch.setattr(vs, "VoiceService", lambda *a, **kw: built.append(1))
    assert vs.run_service(config, paths, out=out.append) == 1
    assert built == [] and any("zaten çalışıyor" in line for line in out)  # the test config is Turkish
    vs.release_service_lock(held)
    assert vs.acquire_service_lock(paths) is not None  # free again once released


# --- pre-roll, the wake phrase, learned corrections (STT step 3) ----------------------------------------------

EARLY = np.ones(WAKE_FRAME_SAMPLES, dtype=np.int16) * 2000  # speech right after "Hey Jarvis", before recording starts


def test_preroll_keeps_the_words_right_after_the_wake_word(parts):
    p = parts
    run_in_thread(p.service)
    p.mic.push(WAKE, EARLY, EARLY, EARLY)  # the user goes on talking at once
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: p.stt.calls == 1)
    first = p.stt.audio[:WAKE_FRAME_SAMPLES]
    assert np.allclose(first, 2000 / 32768.0)  # the recording starts with the early speech
    assert int(np.isclose(p.stt.audio, 2000 / 32768.0).sum()) == 3 * WAKE_FRAME_SAMPLES
    assert not np.any(p.stt.audio[: 3 * WAKE_FRAME_SAMPLES] == 1 / 32768.0)  # never the wake word frame itself
    assert any("pre-roll" in line for line in p.lines)
    p.service.request_stop()


def test_preroll_is_off_with_zero_and_never_used_for_answers(config, paths):
    config.voice.preroll_ms = 0
    service = VoiceService(config, paths, stt=FakeSTT(), vad=SileroVad(model=fake_vad_model), cues=None)
    assert service._preroll.maxlen == 0
    config.voice.preroll_ms = 1000
    service = VoiceService(config, paths, stt=FakeSTT(), vad=SileroVad(model=fake_vad_model))
    assert service._preroll.maxlen == 13  # 1000 ms of 80 ms frames, rounded up
    service.mark_preroll()
    service._preroll.extend([EARLY, EARLY])
    recording = service._record(mode="vad", no_speech_timeout_s=0.2)  # a confirmation answer starts clean
    assert recording.audio.size == 0 and len(service._preroll) == 0
    service.mark_preroll()
    service._preroll.extend([EARLY])
    service._preroll_marked_at -= 60  # a stale mark (the trigger never became a recording)
    assert service._record(mode="wake", no_speech_timeout_s=0.2).audio.size == 0


def test_wake_phrase_is_stripped_from_a_wake_turn(parts):
    p = parts
    p.stt.text = "Hey Jarvis, yarın ne var"
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.assistant.handled == ["yarın ne var"])
    p.service.request_stop()


def test_strip_wake_phrase():
    from jarvis.voice.service import strip_wake_phrase

    assert strip_wake_phrase("Hey Jarvis, saat kaç?") == "saat kaç?"
    assert strip_wake_phrase("jarvis yarın ne var") == "yarın ne var"
    assert strip_wake_phrase("Jarvis") == ""
    assert strip_wake_phrase("yarın Jarvis gelsin") == "yarın Jarvis gelsin"


def test_a_spoken_correction_is_learned_for_next_time(parts):
    from jarvis.voice.corrections import CorrectionStore

    p = parts
    p.config.voice.post_speech_cooldown_s = 0.0  # the second "Hey Jarvis" comes right after the reply
    p.stt.corrections = CorrectionStore(p.paths.corrections_file)
    p.stt.text = "Spotify'da Sezen Aksa'dan çal"
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: len(p.assistant.handled) == 1 and p.service.state == VoiceState.IDLE)
    p.stt.text = "Hayır, Sezen Aksu dedim"
    speak_utterance(p)
    assert wait_until(lambda: len(p.assistant.handled) == 2)
    assert p.assistant.handled[1] == "Hayır, Sezen Aksu dedim"  # the model still gets the correction
    assert "- Sezen Aksa → Sezen Aksu" in p.paths.corrections_file.read_text(encoding="utf-8")
    assert any("learned:" in line for line in p.lines)
    p.service.request_stop()


def test_follow_up_window_opens_when_the_chime_ends(parts):
    p = parts
    p.config.voice.follow_up_mode = "always"
    p.service.cues.measure_audible = lambda path: 0.0
    calls = []
    original = p.service.cues.play_blocking
    p.service.cues.play_blocking = lambda cue, **kw: calls.append((cue, kw.get("until_silent"))) or original(cue, **kw)
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: ("ready", True) in calls)
    p.service.request_stop()
