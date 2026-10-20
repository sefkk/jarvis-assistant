"""`jarvis voice debug-record` (voice/debug.py): levels, the saved WAV and the transcript, with fakes."""

from __future__ import annotations

import math

import numpy as np
import pytest

from jarvis.main import build_parser
from jarvis.voice import debug
from jarvis.voice.audio import SAMPLE_RATE, WAKE_FRAME_SAMPLES, read_wav_int16
from jarvis.voice.vad import SileroVad


def fake_vad_model(chunk):
    return 0.95 if float(np.abs(chunk).mean()) > 0.01 else 0.0


class FakeMic:
    def __init__(self, frames):
        self.frames = list(frames)
        self.started = self.stopped = False
        self.flushed = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def flush(self):
        self.flushed += 1

    def read(self, timeout=None):
        return self.frames.pop(0) if self.frames else None


class FakeSTT:
    model = "fake-whisper"
    warmed_up = False

    def __init__(self, text="selam", language="tr"):
        self.text, self.language, self.calls, self.received = text, language, 0, None

    def warm_up(self):
        self.warmed_up = True
        return 1.0

    def describe(self):
        return f"{self.model}, language fixed to {self.language}"

    def transcribe_array(self, audio, sample_rate=16000):
        self.calls += 1
        self.received = np.asarray(audio)
        return self.text, self.language

    def clean(self, text, *, after_question=False):
        from jarvis.io.voice import clean_transcript

        return clean_transcript(text, [], after_question=after_question)


def frames_of(audio: np.ndarray):
    return [audio[i : i + WAKE_FRAME_SAMPLES] for i in range(0, len(audio), WAKE_FRAME_SAMPLES)]


def speech_then_silence(speech_s=1.0, lead_s=0.4, tail_s=1.5, amplitude=6000, seed=1):
    rng = np.random.default_rng(seed)
    lead = np.zeros(int(lead_s * SAMPLE_RATE), dtype=np.int16)
    speech = (rng.standard_normal(int(speech_s * SAMPLE_RATE)) * amplitude).clip(-32768, 32767).astype(np.int16)
    tail = np.zeros(int(tail_s * SAMPLE_RATE), dtype=np.int16)
    return np.concatenate([lead, speech, tail])


# --- stats -------------------------------------------------------------------------------------------------------


def test_audio_stats_of_a_sine():
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    sine = (np.sin(2 * math.pi * 440 * t) * 16384).astype(np.int16)
    stats = debug.audio_stats(sine)
    assert stats.samples == SAMPLE_RATE and stats.duration_ms == 1000.0
    assert stats.peak_dbfs == pytest.approx(-6.02, abs=0.05)
    assert stats.rms_dbfs == pytest.approx(-9.03, abs=0.05)
    assert stats.clipped_samples == 0 and stats.verdicts() == ["levels look fine"]


def test_audio_stats_silence_and_clipping():
    silence = debug.audio_stats(np.zeros(1600, dtype=np.int16))
    assert silence.peak_dbfs == -math.inf and silence.rms_dbfs == -math.inf
    assert any("quiet" in v for v in silence.verdicts())
    assert debug.audio_stats(np.zeros(0, dtype=np.int16)).verdicts() == ["no audio"]
    square = np.where(np.arange(1600) % 2 == 0, 32767, -32768).astype(np.int16)
    hot = debug.audio_stats(square)
    assert hot.clipped_samples == 1600 and hot.clipped_pct == 100.0
    assert any("clipping" in v for v in hot.verdicts())


# --- the recording ------------------------------------------------------------------------------------------------


def test_debug_record_saves_what_the_engine_gets(config, paths, tmp_path):
    audio = speech_then_silence()
    mic, stt = FakeMic(frames_of(audio)), FakeSTT(text="yarın hava nasıl")
    lines: list[str] = []
    sounds: list[str] = []
    result = debug.debug_record(
        config,
        paths,
        out_dir=tmp_path / "rec",
        out=lines.append,
        mic=mic,
        vad=SileroVad(model=fake_vad_model),
        stt=stt,
        play_sound=sounds.append,
    )
    assert mic.started and mic.stopped and mic.flushed == 1
    assert sounds == [config.voice.listen_sound]
    assert stt.warmed_up and stt.calls == 1
    # The WAV holds the same samples the engine received (float32 = int16 / 32768).
    saved = read_wav_int16(result.wav_path)
    assert result.wav_path.parent == tmp_path / "rec" and result.wav_path.suffix == ".wav"
    np.testing.assert_array_equal(saved, result.recording.audio)
    np.testing.assert_allclose(stt.received, saved.astype(np.float32) / 32768.0)
    assert result.recording.reason == "end_of_speech" and result.recording.speech_started
    assert 300 <= result.recording.speech_start_ms <= 500  # the lead-in is kept, not cut
    assert result.text == "yarın hava nasıl" and result.cleaned == "yarın hava nasıl" and result.language == "tr"
    assert result.speech is not None and result.speech.rms_dbfs > result.stats.rms_dbfs
    text = "\n".join(lines)
    assert "Kaydedildi: " in text and "Metin [tr" in text and "yarın hava nasıl" in text  # the test config is Turkish
    assert "Seviyeler (konuşma kısmı)" in text and "Sonuç: " in text
    assert "Konuşmadan metne: fake-whisper, language fixed to tr" in text


def test_debug_record_without_speech_skips_the_engine(config, paths, tmp_path):
    silence = np.zeros(int(config.voice.no_speech_timeout_s * SAMPLE_RATE) + WAKE_FRAME_SAMPLES, dtype=np.int16)
    mic, stt = FakeMic(frames_of(silence)), FakeSTT()
    lines: list[str] = []
    result = debug.debug_record(
        config, paths, out_dir=tmp_path, out=lines.append, mic=mic, vad=SileroVad(model=fake_vad_model), stt=stt, play_sound=lambda _p: None
    )
    assert result.recording.reason == "no_speech" and stt.calls == 0 and result.text == ""
    assert result.speech is None and result.wav_path.is_file()
    assert any("hiç başlamadı" in line for line in lines) and any("atlandı" in line for line in lines)


def test_debug_record_marks_a_dropped_transcript(config, paths, tmp_path):
    mic, stt = FakeMic(frames_of(speech_then_silence())), FakeSTT(text="I")
    lines: list[str] = []
    result = debug.debug_record(
        config, paths, out_dir=tmp_path, out=lines.append, mic=mic, vad=SileroVad(model=fake_vad_model), stt=stt, play_sound=lambda _p: None
    )
    assert result.text == "I" and result.cleaned is None
    assert any(line.startswith("Elendi:") for line in lines)


def test_run_debug_record_needs_the_voice_extra(config, paths, monkeypatch):
    from jarvis.voice import service

    monkeypatch.setattr(service.VoiceService, "missing_dependencies", staticmethod(lambda: ["mlx-whisper"]))
    lines: list[str] = []
    assert debug.run_debug_record(config, paths, out=lines.append) == 1
    assert "mlx-whisper" in lines[0]


def test_run_debug_record_prints_the_listen_hint(config, paths, tmp_path, monkeypatch):
    from jarvis.voice import service

    monkeypatch.setattr(service.VoiceService, "missing_dependencies", staticmethod(lambda: []))
    fake = debug.DebugRecordResult(tmp_path / "x.wav", None, None, None, "", None, None, 0.0)  # type: ignore[arg-type]
    monkeypatch.setattr(debug, "debug_record", lambda *a, **k: fake)
    lines: list[str] = []
    assert debug.run_debug_record(config, paths, out_dir=str(tmp_path), out=lines.append) == 0
    assert lines[-1] == f"Dinlemek için: afplay {tmp_path / 'x.wav'}"


def test_debug_record_speaks_english_when_the_main_language_is_english(config, paths, tmp_path):
    from jarvis import i18n

    i18n.set_language("en")
    mic, stt = FakeMic(frames_of(speech_then_silence())), FakeSTT(text="what's the weather")
    lines: list[str] = []
    debug.debug_record(
        config, paths, out_dir=tmp_path, out=lines.append, mic=mic, vad=SileroVad(model=fake_vad_model), stt=stt, play_sound=lambda _p: None
    )
    text = "\n".join(lines)
    assert "Saved: " in text and "Transcript [tr" in text and "Levels (speech part)" in text and "Verdict: " in text
    assert "Speak now" in text


def test_voice_cli_parser_debug_record():
    args = build_parser().parse_args(["voice", "debug-record", "--out", "/tmp/x"])
    assert args.voice_command == "debug-record" and args.out == "/tmp/x"
    assert build_parser().parse_args(["voice", "debug-record"]).out is None
