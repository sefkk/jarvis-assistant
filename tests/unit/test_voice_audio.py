"""Audio input for the voice service: microphone queue, wake word gating, end-of-speech logic.
No microphone, no speaker, no real models: everything is driven with fakes and arrays."""

from __future__ import annotations

import sys
import threading

import numpy as np
import pytest

from jarvis.exceptions import JarvisError
from jarvis.io.voice import VoiceUnavailable
from jarvis.voice import audio, vad, wakeword
from jarvis.voice.audio import SAMPLE_RATE, VAD_CHUNK_SAMPLES, WAKE_FRAME_SAMPLES, Microphone
from jarvis.voice.vad import EndOfSpeechDetector, Recording, SileroVad, record_until_silence, record_while
from jarvis.voice.wakeword import WakeWordDetector

# --- helpers -------------------------------------------------------------------------------------


def frame(value: int = 1000, n: int = WAKE_FRAME_SAMPLES) -> np.ndarray:
    return np.full(n, value, dtype=np.int16)


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeWakeModel:
    def __init__(self, scores) -> None:
        self.scores = list(scores)
        self.resets = 0
        self.frames = []

    def predict(self, frame):
        self.frames.append(frame)
        score = self.scores.pop(0) if self.scores else 0.0
        return {"hey_jarvis": np.float32(score)}

    def reset(self):
        self.resets += 1


class ScriptedVad:
    """Returns scripted probabilities per 512-sample chunk, then the last one forever."""

    def __init__(self, probs) -> None:
        self.probs = list(probs)
        self.chunks = []
        self.resets = 0

    def __call__(self, chunk) -> float:
        self.chunks.append(chunk)
        if len(self.probs) > 1:
            return self.probs.pop(0)
        return self.probs[0] if self.probs else 0.0

    def reset_states(self):
        self.resets += 1


def detector(probs, **kw) -> EndOfSpeechDetector:
    return EndOfSpeechDetector(SileroVad(model=ScriptedVad(probs)), **kw)


def chunks_for_ms(ms: float) -> int:
    return int(ms * SAMPLE_RATE / 1000 / VAD_CHUNK_SAMPLES)


def frames_forever(value: int = 1000):
    while True:
        yield frame(value)


# --- audio helpers --------------------------------------------------------------------------------


def test_iter_frames_pads_last_frame():
    data = np.arange(3000, dtype=np.int16)
    frames = list(audio.iter_frames(data, 1280))
    assert [len(f) for f in frames] == [1280, 1280, 1280]
    assert frames[2][:440].tolist() == list(range(2560, 3000)) and not frames[2][440:].any()
    assert list(audio.iter_frames(np.zeros(0, dtype=np.int16))) == []


def test_wav_round_trip_and_format_check(tmp_path):
    data = (np.sin(np.linspace(0, 100, 16000)) * 20000).astype(np.int16)
    path = tmp_path / "a.wav"
    audio.write_wav_int16(path, data)
    back = audio.read_wav_int16(path)
    assert back.dtype == np.int16 and np.array_equal(back, data)
    audio.write_wav_int16(tmp_path / "b.wav", data, sample_rate=8000)
    with pytest.raises(ValueError, match="8000 Hz"):
        audio.read_wav_int16(tmp_path / "b.wav")


def test_float_conversion_and_duration():
    f = audio.int16_to_float32(np.array([0, 16384, -32768], dtype=np.int16))
    assert f.dtype == np.float32 and f.tolist() == [0.0, 0.5, -1.0]
    assert audio.duration_ms(16000) == 1000.0 and audio.duration_ms(1280) == 80.0


# --- microphone -----------------------------------------------------------------------------------


class FakeStream:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def push(self, value: int, n: int = WAKE_FRAME_SAMPLES):
        indata = np.full((n, 1), value, dtype=np.int16)
        self.callback(indata, n, None, None)


def test_microphone_queue_bound_read_timeout_and_flush():
    streams = []

    def factory(callback):
        s = FakeStream(callback)
        streams.append(s)
        return s

    mic = Microphone(max_queued_frames=3, stream_factory=factory)
    assert mic.read(timeout=0.01) is None and not mic.running
    mic.start()
    stream = streams[0]
    assert mic.running and stream.started
    for v in (1, 2, 3, 4, 5):
        stream.push(v)
    assert mic.dropped_frames == 2
    first = mic.read(timeout=0.1)
    assert first.dtype == np.int16 and first.ndim == 1 and first[0] == 1 and len(first) == WAKE_FRAME_SAMPLES
    mic.flush()
    assert mic.read(timeout=0.01) is None
    stream.push(9)
    assert mic.read(timeout=0.1)[0] == 9
    mic.stop()
    assert not mic.running and stream.closed and not stream.started
    mic.start()  # restart works
    assert streams[1].started
    mic.stop()


def test_microphone_start_failure_mentions_permission():
    class BadStream:
        def start(self):
            raise RuntimeError("PortAudio -9986")

    mic = Microphone(stream_factory=lambda cb: BadStream())
    with pytest.raises(JarvisError, match="Microphone"):
        mic.start()
    assert not mic.running


def test_microphone_without_sounddevice(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(VoiceUnavailable):
        Microphone().start()


def test_microphone_permission_never_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "AVFoundation", None)
    assert audio.microphone_permission() == "unknown"


def test_play_sound_uses_afplay_argv(tmp_path, monkeypatch):
    sound = tmp_path / "Tink.aiff"
    sound.write_bytes(b"x")
    calls = []

    def popen(argv, **kw):
        calls.append(argv)
        return "proc"

    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/afplay")
    assert audio.play_sound(str(sound), popen=popen) == "proc"
    assert calls == [["/usr/bin/afplay", str(sound)]]
    assert audio.play_sound(str(tmp_path / "missing.aiff"), popen=popen) is None
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    assert audio.play_sound(str(sound), popen=popen) is None
    assert len(calls) == 1


# --- wake word ---------------------------------------------------------------------------------------


def test_wake_word_threshold_cooldown_and_reset():
    clock = FakeClock()
    model = FakeWakeModel([0.1, 0.4, 0.9, 0.95, 0.99, 0.2, 0.9])
    det = WakeWordDetector(threshold=0.5, cooldown_s=2.0, clock=clock, model=model)
    assert det.loaded
    det.load()  # idempotent with an injected model
    assert det.process(frame()) is False and det.process(frame()) is False
    assert det.last_score == pytest.approx(0.4)
    assert det.process(frame()) is True and model.resets == 1
    assert det.process(frame()) is False  # 0.95 inside the cooldown
    clock.advance(1.0)
    assert det.process(frame()) is False  # 0.99 still inside
    clock.advance(1.5)
    assert det.process(frame()) is False  # 0.2 below threshold
    assert det.process(frame()) is True and model.resets == 2


def test_wake_word_suppress_blocks_until_expiry():
    clock = FakeClock()
    model = FakeWakeModel([0.9, 0.9, 0.9])
    det = WakeWordDetector(threshold=0.5, cooldown_s=0.0, clock=clock, model=model)
    det.suppress(3.0)
    assert det.process(frame()) is False
    clock.advance(2.9)
    assert det.process(frame()) is False
    clock.advance(0.2)
    assert det.process(frame()) is True
    det.suppress(10.0)
    det.reset()  # reset clears the suppression
    model.scores = [0.9]
    assert det.process(frame()) is True


def test_wake_word_score_takes_the_max_model():
    class Multi:
        def predict(self, frame):
            return {"a": 0.2, "b": 0.7}

        def reset(self):
            pass

    det = WakeWordDetector(model=Multi())
    assert det.score(frame()) == pytest.approx(0.7)


def test_wake_word_requires_load():
    det = WakeWordDetector(model=None)
    assert not det.loaded
    with pytest.raises(JarvisError, match="not loaded"):
        det.score(frame())


def test_wake_word_without_openwakeword(monkeypatch):
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    monkeypatch.setitem(sys.modules, "openwakeword.model", None)
    monkeypatch.setitem(sys.modules, "openwakeword.utils", None)
    with pytest.raises(VoiceUnavailable):
        WakeWordDetector().load()


def test_models_present_checks_files(tmp_path, monkeypatch):
    monkeypatch.setattr(wakeword, "model_dir", lambda: tmp_path)
    assert wakeword.models_present() is False
    for name in ("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx"):
        (tmp_path / name).write_bytes(b"")
    assert wakeword.models_present() is True
    assert wakeword.models_present("alexa") is False


# --- end of speech -----------------------------------------------------------------------------------


def test_end_of_speech_after_silence():
    # 10 speech chunks, then silence; silence_ms=700 -> ~22 chunks of 32 ms
    det = detector([0.9] * 10 + [0.05], silence_ms=700)
    reason = None
    fed = 0
    while reason is None:
        reason = det.feed(frame())
        fed += 1
    assert reason == "end_of_speech" and det.speech_started
    rec = det.result(reason)
    assert isinstance(rec, Recording) and rec.reason == "end_of_speech"
    assert rec.duration_ms == pytest.approx(fed * 80)
    # speech ended when chunk 10 (0-based) began, i.e. after 10 chunks of 512 samples = 320 ms
    assert rec.speech_end_ms == pytest.approx(320, abs=1)
    assert rec.duration_ms >= 320 + 700 and rec.duration_ms < 320 + 700 + 160
    assert len(rec.audio) == fed * WAKE_FRAME_SAMPLES
    assert rec.speech_start_ms == 0  # speech from the first chunk


def test_speech_start_is_measured_after_leading_silence():
    """The follow-up window reports how long after it opened the user started talking."""
    det = detector([0.05] * 5 + [0.9] * 10 + [0.05], silence_ms=700, no_speech_timeout_s=60)
    reason = None
    while reason is None:
        reason = det.feed(frame())
    rec = det.result(reason)
    assert reason == "end_of_speech"
    assert rec.speech_start_ms == pytest.approx(5 * 32, abs=1)  # 5 silent chunks of 512 samples = 160 ms
    assert rec.speech_end_ms == pytest.approx(15 * 32, abs=1)
    silent = det = detector([0.05] * 100, no_speech_timeout_s=1)
    reason = None
    while reason is None:
        reason = silent.feed(frame())
    assert reason == "no_speech" and silent.result(reason).speech_start_ms is None


def test_no_speech_timeout_drops_the_recording():
    det = detector([0.01], no_speech_timeout_s=1.0)
    reason = None
    n = 0
    while reason is None:
        reason = det.feed(frame())
        n += 1
    assert reason == "no_speech" and not det.speech_started
    assert n * 80 == pytest.approx(1000, abs=80)
    assert det.result(reason).speech_end_ms is None


def test_max_length_caps_a_long_utterance():
    det = detector([0.9], max_record_s=2.0, no_speech_timeout_s=1.0)
    reason = None
    n = 0
    while reason is None:
        reason = det.feed(frame())
        n += 1
    assert reason == "max_length" and det.speech_started
    assert n * 80 == pytest.approx(2000, abs=80)


def test_short_dip_below_threshold_does_not_end_speech():
    # speech, 10 chunks of pause (~320 ms, below the 700 ms silence), speech again, then real silence
    det = detector([0.9] * 5 + [0.05] * 10 + [0.9] * 5 + [0.05], silence_ms=700)
    reason = None
    while reason is None:
        reason = det.feed(frame())
    rec = det.result(reason)
    assert rec.reason == "end_of_speech"
    assert rec.speech_end_ms == pytest.approx(20 * 32, abs=1)  # after the second speech block


def test_frames_are_rechunked_to_512_for_the_vad():
    scripted = ScriptedVad([0.0])
    det = EndOfSpeechDetector(SileroVad(model=scripted), no_speech_timeout_s=60)
    assert scripted.resets == 1  # the detector resets the VAD state when it starts
    det.feed(frame(n=1280))
    assert len(scripted.chunks) == 2
    det.feed(frame(n=1280))
    assert len(scripted.chunks) == 5  # 2560 samples = 5 chunks, 0 left over
    assert all(len(c) == 512 and c.dtype == np.float32 for c in scripted.chunks)
    assert det.fed_ms == 160.0


def test_hysteresis_uses_threshold_minus_015():
    # probabilities between threshold-0.15 and threshold neither start nor end speech
    det = detector([0.9] + [0.45] * 60, threshold=0.5, silence_ms=300)
    reason = None
    for _ in range(30):
        reason = det.feed(frame())
        if reason:
            break
    assert reason is None and det.speech_started


def test_record_until_silence_should_stop_and_none_frames():
    det = detector([0.9])
    stop = {"now": False}

    def frames():
        yield frame()
        stop["now"] = True
        yield frame()

    rec = record_until_silence(frames(), det, should_stop=lambda: stop["now"])
    assert rec.reason == "stopped" and rec.speech_started and len(rec.audio) == WAKE_FRAME_SAMPLES

    clock = FakeClock()

    def silent_mic():
        while True:
            clock.advance(1.0)
            yield None

    det2 = detector([0.0])
    rec2 = record_until_silence(silent_mic(), det2, idle_timeout_s=3.0, clock=clock)
    assert rec2.reason == "no_speech" and len(rec2.audio) == 0

    det3 = detector([0.9] * 2 + [0.0])
    rec3 = record_until_silence(iter([frame(), None, frame()]), det3)  # exhausted before the VAD decided
    assert rec3.reason == "stopped" and rec3.speech_started


def test_record_while_push_to_talk():
    held = {"n": 3}

    def still_held():
        held["n"] -= 1
        return held["n"] >= 0

    rec = record_while(frames_forever(7), still_held=still_held, max_record_s=30)
    assert rec.reason == "end_of_speech" and rec.speech_started
    assert len(rec.audio) == 3 * WAKE_FRAME_SAMPLES and rec.duration_ms == 240.0 and rec.speech_end_ms == 240.0

    rec2 = record_while(frames_forever(), still_held=lambda: True, max_record_s=1.0)
    assert rec2.reason == "max_length" and len(rec2.audio) >= SAMPLE_RATE

    rec3 = record_while(iter([None, None]), still_held=lambda: True, max_record_s=1.0)
    assert rec3.reason == "end_of_speech" and not rec3.speech_started and rec3.speech_end_ms is None


def test_rms_dbfs():
    assert vad.rms_dbfs(np.zeros(100, dtype=np.int16)) == -np.inf
    assert vad.rms_dbfs(np.full(100, 32767, dtype=np.int16)) == pytest.approx(0.0, abs=0.01)
    assert vad.rms_dbfs(np.full(100, 3276, dtype=np.int16)) == pytest.approx(-20.0, abs=0.1)


def test_silero_without_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "silero_vad", None)
    with pytest.raises(VoiceUnavailable):
        SileroVad().load()
    with pytest.raises(JarvisError, match="not loaded"):
        SileroVad().speech_prob(np.zeros(512, dtype=np.float32))


def test_silero_reset_reaches_the_model():
    scripted = ScriptedVad([0.0])
    v = SileroVad(model=scripted)
    v.reset()
    assert scripted.resets == 1 and v.loaded


# --- heavy modules absent -------------------------------------------------------------------------


def test_modules_import_without_the_voice_extra(monkeypatch):
    for name in ("sounddevice", "openwakeword", "silero_vad", "torch", "pynput", "AVFoundation", "Quartz"):
        monkeypatch.setitem(sys.modules, name, None)
    import importlib

    for mod in (audio, wakeword, vad):
        importlib.reload(mod)
    try:
        assert audio.microphone_permission() == "unknown"
        with pytest.raises(VoiceUnavailable):
            Microphone().start()
        with pytest.raises(VoiceUnavailable):
            SileroVad().load()
    finally:
        for name in ("sounddevice", "openwakeword", "silero_vad", "torch", "pynput", "AVFoundation", "Quartz"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        for mod in (audio, wakeword, vad):
            importlib.reload(mod)


def test_microphone_read_blocks_until_a_frame_arrives():
    streams = []
    mic = Microphone(stream_factory=lambda cb: streams.append(FakeStream(cb)) or streams[-1])
    mic.start()
    got = []

    def reader():
        got.append(mic.read(timeout=2.0))

    t = threading.Thread(target=reader)
    t.start()
    streams[0].push(5)
    t.join(timeout=3.0)
    assert got and got[0][0] == 5
    mic.stop()
