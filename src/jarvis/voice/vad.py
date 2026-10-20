"""End of speech with Silero VAD and the two recording modes.

`EndOfSpeechDetector` holds the endpointing logic over speech probabilities and counts time in
samples fed, so tests drive it with scripted probabilities and no clock. `record_until_silence`
(wake word mode) and `record_while` (push-to-talk, no VAD) pull frames from any iterable, the
microphone in production and arrays in tests.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal

from jarvis.exceptions import JarvisError
from jarvis.voice.audio import SAMPLE_RATE, VAD_CHUNK_SAMPLES, duration_ms, int16_to_float32

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("jarvis.voice.vad")

RecordingReason = Literal["end_of_speech", "max_length", "no_speech", "stopped"]


@dataclass
class Recording:
    audio: "np.ndarray"  # int16, everything recorded (push-to-talk: the whole hold)
    reason: RecordingReason
    speech_started: bool
    duration_ms: float  # audio length
    speech_end_ms: float | None  # where the VAD decided speech ended (None if never)
    speech_start_ms: float | None = None  # where the VAD first saw speech (None if never)


class SileroVad:
    """Speech probability per 512-sample float32 chunk. `model` is injectable for tests."""

    def __init__(self, *, model: Callable[[Any], float] | None = None) -> None:
        self._model = model
        self._raw: Any = None  # the silero object, for reset_states()

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch  # noqa: PLC0415
            from silero_vad import load_silero_vad  # noqa: PLC0415
        except ImportError:
            from jarvis.io.voice import VOICE_UNAVAILABLE, VoiceUnavailable  # noqa: PLC0415

            raise VoiceUnavailable(f"{VOICE_UNAVAILABLE} (silero-vad is not installed)") from None
        started = time.perf_counter()
        try:
            raw = load_silero_vad(onnx=True)
        except Exception as exc:
            raise JarvisError(f"could not load Silero VAD: {exc}") from exc
        self._raw = raw

        def prob(chunk) -> float:
            import numpy as np  # noqa: PLC0415

            return float(raw(torch.from_numpy(np.ascontiguousarray(chunk, dtype=np.float32)), SAMPLE_RATE).item())

        self._model = prob
        log.info("Silero VAD loaded in %.0f ms", (time.perf_counter() - started) * 1000)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def speech_prob(self, chunk_float32) -> float:
        if self._model is None:
            raise JarvisError("Silero VAD not loaded; call load() first")
        return float(self._model(chunk_float32))

    def reset(self) -> None:
        target = self._raw if self._raw is not None else self._model
        reset = getattr(target, "reset_states", None)
        if reset is not None:
            try:
                reset()
            except Exception as exc:
                log.debug("VAD reset failed: %s", exc)


class EndOfSpeechDetector:
    """Pure endpointing logic: time is counted in samples fed, so tests are deterministic.

    Speech starts when a chunk's probability reaches `threshold`. After that, probabilities below
    `threshold - 0.15` for at least `silence_ms` end the utterance. Without speech for
    `no_speech_timeout_s` the recording is dropped; `max_record_s` caps everything.
    """

    def __init__(
        self,
        vad: SileroVad,
        *,
        threshold: float = 0.5,
        silence_ms: int = 700,
        max_record_s: float = 30,
        no_speech_timeout_s: float = 5,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self.vad = vad
        self.threshold = float(threshold)
        self.silence_ms = int(silence_ms)
        self.max_record_s = float(max_record_s)
        self.no_speech_timeout_s = float(no_speech_timeout_s)
        self.sample_rate = sample_rate
        self._frames: list = []
        self._pending: Any = None  # samples not yet handed to the VAD (fewer than a chunk)
        self._fed = 0  # samples appended
        self._vad_samples = 0  # samples the VAD has seen
        self._speech_started = False
        self._silence_since: int | None = None  # VAD sample position where the current silence began
        self._speech_end: int | None = None
        self._speech_start: int | None = None
        self.vad.reset()

    @property
    def audio(self) -> "np.ndarray":
        import numpy as np  # noqa: PLC0415

        return np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.int16)

    @property
    def speech_started(self) -> bool:
        return self._speech_started

    @property
    def fed_ms(self) -> float:
        return duration_ms(self._fed, self.sample_rate)

    def feed(self, frame) -> RecordingReason | None:
        import numpy as np  # noqa: PLC0415

        frame = np.ascontiguousarray(frame, dtype=np.int16)
        self._frames.append(frame)
        self._fed += len(frame)
        data = frame if self._pending is None else np.concatenate([self._pending, frame])
        offset = 0
        while offset + VAD_CHUNK_SAMPLES <= len(data):
            chunk = data[offset : offset + VAD_CHUNK_SAMPLES]
            offset += VAD_CHUNK_SAMPLES
            self._vad_samples += VAD_CHUNK_SAMPLES
            decision = self._judge(self.vad.speech_prob(int16_to_float32(chunk)))
            if decision is not None:
                self._pending = None
                return decision
        self._pending = data[offset:] if offset < len(data) else None
        if self.fed_ms >= self.max_record_s * 1000:
            return "max_length"
        if not self._speech_started and self.fed_ms >= self.no_speech_timeout_s * 1000:
            return "no_speech"
        return None

    def _judge(self, prob: float) -> RecordingReason | None:
        if prob >= self.threshold:
            if not self._speech_started:
                self._speech_started = True
                self._speech_start = max(0, self._vad_samples - VAD_CHUNK_SAMPLES)
                log.debug("speech started at %.0f ms", duration_ms(self._vad_samples, self.sample_rate))
            self._silence_since = None
            return None
        if self._speech_started and prob < self.threshold - 0.15:
            if self._silence_since is None:
                self._silence_since = self._vad_samples - VAD_CHUNK_SAMPLES
            silent_ms = duration_ms(self._vad_samples - self._silence_since, self.sample_rate)
            if silent_ms >= self.silence_ms:
                self._speech_end = self._silence_since
                return "end_of_speech"
        return None

    def result(self, reason: RecordingReason) -> Recording:
        audio = self.audio
        if reason == "end_of_speech" and self._speech_end is None:
            self._speech_end = self._vad_samples
        return Recording(
            audio=audio,
            reason=reason,
            speech_started=self._speech_started,
            duration_ms=duration_ms(len(audio), self.sample_rate),
            speech_end_ms=duration_ms(self._speech_end, self.sample_rate) if self._speech_end is not None else None,
            speech_start_ms=duration_ms(self._speech_start, self.sample_rate) if self._speech_start is not None else None,
        )


def record_until_silence(
    frames: "Iterable[np.ndarray | None]",
    detector: EndOfSpeechDetector,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    idle_timeout_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
) -> Recording:
    """Wake word mode: pull frames until the VAD ends the utterance. A None frame means the
    microphone yielded nothing; after `idle_timeout_s` of that the recording ends."""
    idle_since: float | None = None
    for frame in frames:
        if should_stop():
            return detector.result("stopped")
        if frame is None:
            now = clock()
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= idle_timeout_s:
                log.warning("microphone produced no audio for %.1f s", idle_timeout_s)
                return detector.result("stopped" if detector.speech_started else "no_speech")
            continue
        idle_since = None
        reason = detector.feed(frame)
        if reason is not None:
            return detector.result(reason)
    return detector.result("stopped" if detector.speech_started else "no_speech")


def record_while(
    frames: "Iterable[np.ndarray | None]",
    *,
    still_held: Callable[[], bool],
    max_record_s: float,
    sample_rate: int = SAMPLE_RATE,
) -> Recording:
    """Push-to-talk: no VAD, collect while the key is held (checked before every frame)."""
    import numpy as np  # noqa: PLC0415

    collected: list = []
    total = 0
    reason: RecordingReason = "end_of_speech"
    limit = int(max_record_s * sample_rate)
    for frame in frames:
        if not still_held():
            break
        if frame is None:
            continue
        frame = np.ascontiguousarray(frame, dtype=np.int16)
        collected.append(frame)
        total += len(frame)
        if total >= limit:
            reason = "max_length"
            break
    audio = np.concatenate(collected) if collected else np.zeros(0, dtype=np.int16)
    return Recording(
        audio=audio,
        reason=reason,
        speech_started=total > 0,
        duration_ms=duration_ms(total, sample_rate),
        speech_end_ms=duration_ms(total, sample_rate) if total > 0 else None,
    )


def rms_dbfs(audio) -> float:
    """Loudness in dBFS for the foreground display (-inf for silence)."""
    import numpy as np  # noqa: PLC0415

    data = np.asarray(audio, dtype=np.float64)
    if data.size == 0:
        return -math.inf
    rms = math.sqrt(float(np.mean(np.square(data / 32768.0))))
    return 20 * math.log10(rms) if rms > 0 else -math.inf
