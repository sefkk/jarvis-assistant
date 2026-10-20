"""`jarvis voice debug-record`: one utterance recorded the way the voice service records it.

The audio that reaches the STT engine is written as a 16 kHz mono 16-bit WAV, so the user can listen
to exactly what Whisper hears, next to its levels (peak, RMS, clipping), the VAD's speech boundaries,
the input device, and the transcript the engine produced. Nothing here touches the API or the
assistant; the microphone, VAD and STT objects are injectable for tests.
"""

from __future__ import annotations

import logging
import math
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from jarvis.config import Config, Paths
from jarvis.i18n import t
from jarvis.voice import audio as vaudio
from jarvis.voice.audio import SAMPLE_RATE, Microphone
from jarvis.voice.vad import EndOfSpeechDetector, Recording, SileroVad, record_until_silence

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("jarvis.voice.debug")

MIC_READ_TIMEOUT_S = 0.5  # the service's value
CLIP_LEVEL = 32000  # int16 samples at or above this count as clipped (the converter saturates a bit below full scale)
QUIET_RMS_DBFS = -35.0  # below this the speech is probably too quiet for the model
LOUD_PEAK_DBFS = -1.0  # above this the microphone is probably clipping


@dataclass
class AudioStats:
    sample_rate: int
    samples: int
    duration_ms: float
    peak_dbfs: float
    rms_dbfs: float
    clipped_samples: int

    @property
    def clipped_pct(self) -> float:
        return 100.0 * self.clipped_samples / self.samples if self.samples else 0.0

    def verdicts(self) -> list[str]:
        notes: list[str] = []
        if self.samples == 0:
            return ["no audio"]
        if self.clipped_pct >= 0.1 or self.peak_dbfs > LOUD_PEAK_DBFS:
            notes.append("clipping: the input is too hot (lower the input volume in System Settings > Sound)")
        if self.rms_dbfs < QUIET_RMS_DBFS:
            notes.append("quiet: the speech is low; move closer to the microphone or raise the input volume")
        return notes or ["levels look fine"]


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -math.inf


def audio_stats(audio: "np.ndarray", sample_rate: int = SAMPLE_RATE) -> AudioStats:
    """Peak, RMS (dBFS) and clipped-sample count of an int16 array."""
    import numpy as np  # noqa: PLC0415

    data = np.asarray(audio)
    if data.size == 0:
        return AudioStats(sample_rate, 0, 0.0, -math.inf, -math.inf, 0)
    scaled = data.astype(np.float64) / 32768.0
    peak = float(np.max(np.abs(scaled)))
    rms = math.sqrt(float(np.mean(np.square(scaled))))
    clipped = int(np.count_nonzero(np.abs(data.astype(np.int32)) >= CLIP_LEVEL))
    return AudioStats(sample_rate, int(data.size), vaudio.duration_ms(int(data.size), sample_rate), _dbfs(peak), _dbfs(rms), clipped)


def speech_stats(recording: Recording) -> AudioStats | None:
    """Levels of the part the VAD marked as speech (None when it never started)."""
    if recording.speech_start_ms is None:
        return None
    start = int(recording.speech_start_ms * SAMPLE_RATE / 1000)
    end = int(recording.speech_end_ms * SAMPLE_RATE / 1000) if recording.speech_end_ms is not None else len(recording.audio)
    return audio_stats(recording.audio[start:end])


def input_device_description() -> str:
    """The default input device as PortAudio sees it; never raises."""
    try:
        import sounddevice as sd  # noqa: PLC0415

        device = sd.query_devices(kind="input")
        api = sd.query_hostapis(device["hostapi"])["name"]
        return f"{device['name']} ({api}), {device['max_input_channels']} input channel(s), native {device['default_samplerate']:.0f} Hz"
    except Exception as exc:  # no sounddevice, no device
        return f"unknown ({exc})"


@dataclass
class DebugRecordResult:
    wav_path: Path
    recording: Recording
    stats: AudioStats
    speech: AudioStats | None
    text: str
    cleaned: str | None
    language: str | None
    stt_ms: float


def debug_record(
    config: Config,
    paths: Paths,  # noqa: ARG001 - kept for symmetry with the other CLI entry points (future: data/ paths)
    *,
    out_dir: Path | None = None,
    out: Callable[[str], None] = print,
    mic: Any = None,
    vad: SileroVad | None = None,
    stt: Any = None,
    clock: Callable[[], float] = time.monotonic,
    play_sound: Callable[[str], Any] = vaudio.play_sound,
) -> DebugRecordResult:
    """Record one utterance with the service's VAD settings, save it, transcribe it, print the facts."""
    from jarvis.io.voice import SpeechToText  # noqa: PLC0415

    v = config.voice
    if stt is None:
        stt = SpeechToText.from_config(v, paths, config=config)
    if vad is None:
        vad = SileroVad()
    if mic is None:
        mic = Microphone()
    out(t("voice.debug.input_device", device=input_device_description()))
    out(t("voice.debug.stream", rate=SAMPLE_RATE, frame=vaudio.WAKE_FRAME_SAMPLES))
    out(t("voice.debug.vad", threshold=v.vad_threshold, silence=v.vad_silence_ms, max=v.max_record_s, timeout=v.no_speech_timeout_s))
    out(t("voice.debug.stt", engine=stt.describe()))
    if not getattr(stt, "warmed_up", False):
        ms = stt.warm_up()
        out(
            t("voice.debug.stt_ready", ms=f"{ms:.0f}")
            + (t("voice.debug.stt_not_ready", error=stt.last_error) if getattr(stt, "last_error", None) else "")
        )
    vad.load()
    mic.start()
    try:
        mic.flush()
        vad.reset()
        detector = EndOfSpeechDetector(
            vad,
            threshold=v.vad_threshold,
            silence_ms=v.vad_silence_ms,
            max_record_s=v.max_record_s,
            no_speech_timeout_s=v.no_speech_timeout_s,
        )
        play_sound(v.listen_sound)
        out(t("voice.debug.speak_now"))

        def frames():
            while True:
                yield mic.read(timeout=MIC_READ_TIMEOUT_S)

        recording = record_until_silence(frames(), detector, idle_timeout_s=float(v.no_speech_timeout_s), clock=clock)
    finally:
        mic.stop()

    stats = audio_stats(recording.audio)
    speech = speech_stats(recording)
    folder = Path(out_dir) if out_dir is not None else Path(tempfile.mkdtemp(prefix="jarvis-stt-"))
    wav_path = folder / f"debug-{datetime.now().strftime('%Y%m%d-%H%M%S')}.wav"
    vaudio.write_wav_int16(wav_path, recording.audio)  # byte for byte what transcribe_array gets (as float32 / 32768)

    out("")
    out(t("voice.debug.saved", path=wav_path))
    out(t("voice.debug.recording", ms=f"{stats.duration_ms:.0f}", samples=stats.samples, rate=stats.sample_rate, reason=recording.reason))
    if recording.speech_start_ms is not None:
        end = (
            t("voice.debug.speech_end", ms=f"{recording.speech_end_ms:.0f}")
            if recording.speech_end_ms is not None
            else t("voice.debug.speech_to_end")
        )
        out(
            t(
                "voice.debug.speech",
                start=f"{recording.speech_start_ms:.0f}",
                end=end,
                tail=f"{(stats.duration_ms - (recording.speech_end_ms or stats.duration_ms)):.0f}",
            )
        )
    else:
        out(t("voice.debug.speech_never"))
    out(
        t(
            "voice.debug.levels_file",
            peak=f"{stats.peak_dbfs:.1f}",
            rms=f"{stats.rms_dbfs:.1f}",
            clipped=stats.clipped_samples,
            pct=f"{stats.clipped_pct:.2f}",
        )
    )
    if speech is not None:
        out(t("voice.debug.levels_speech", peak=f"{speech.peak_dbfs:.1f}", rms=f"{speech.rms_dbfs:.1f}"))
    if speech is None:
        out(t("voice.debug.verdict_silence"))
    else:
        out(t("voice.debug.verdict", verdicts="; ".join(speech.verdicts())))

    text, language, stt_ms, cleaned = "", None, 0.0, None
    if recording.speech_started and len(recording.audio):
        t0 = time.perf_counter()
        text, language = stt.transcribe_array(vaudio.int16_to_float32(recording.audio))
        stt_ms = (time.perf_counter() - t0) * 1000.0
        cleaned = stt.clean(text)
        out("")
        engine = getattr(stt, "last_engine", None)
        confidence = getattr(stt, "last_confidence", None)
        confidence_text = t("voice.debug.confidence", value=f"{confidence:.2f}") if confidence is not None else None
        detail = ", ".join(x for x in (language, engine, confidence_text, f"{stt_ms:.0f} ms") if x)
        out(t("voice.debug.transcript", detail=detail, text=repr(text)))
        if getattr(stt, "last_error", None) and engine and "fallback" in engine:
            out(t("voice.debug.fallback", error=stt.last_error))
        if cleaned is None:
            out(t("voice.debug.filtered"))
    else:
        out(t("voice.debug.skipped"))
    return DebugRecordResult(wav_path, recording, stats, speech, text, cleaned, language, stt_ms)


def run_debug_record(config: Config, paths: Paths, *, out_dir: str | None = None, out: Callable[[str], None] = print) -> int:
    """`jarvis voice debug-record [--out DIR]`."""
    from jarvis.voice.service import VoiceService  # noqa: PLC0415

    missing = VoiceService.missing_dependencies()
    if missing:
        out(t("voice.service.missing_packages", packages=", ".join(missing)))
        return 1
    try:
        result = debug_record(config, paths, out_dir=Path(out_dir).expanduser() if out_dir else None, out=out)
    except KeyboardInterrupt:
        out(t("voice.service.cancelled"))
        return 130
    out("")
    out(t("voice.debug.listen_with", path=result.wav_path))
    return 0
