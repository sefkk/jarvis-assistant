"""Microphone input and small audio helpers.

Everything here works on 16 kHz mono int16 numpy arrays. The microphone runs a `sounddevice`
InputStream whose callback pushes fixed-size frames into a bounded queue; the voice service
pulls them from its own thread. `sounddevice`, `AVFoundation` and `afplay` are reached lazily so
this module imports without the `voice` extra.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from jarvis.exceptions import JarvisError

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("jarvis.voice.audio")

SAMPLE_RATE = 16000
WAKE_FRAME_SAMPLES = 1280  # 80 ms, what openWakeWord wants
VAD_CHUNK_SAMPLES = 512  # 32 ms, what Silero wants at 16 kHz

MIC_HELP = (
    "could not open the microphone. Allow the app that runs Jarvis (the terminal, or the Python "
    "executable under launchd) in System Settings > Privacy & Security > Microphone, then retry."
)


def _voice_unavailable(detail: str) -> Exception:
    from jarvis.io.voice import VOICE_UNAVAILABLE, VoiceUnavailable  # noqa: PLC0415 - avoids an import cycle

    return VoiceUnavailable(f"{VOICE_UNAVAILABLE} ({detail})")


class Microphone:
    """sounddevice InputStream -> bounded queue of int16 mono numpy frames of `frame_samples` each.

    `stream_factory(callback) -> stream` is injectable for tests; the stream must have `start()`
    and `stop()`/`close()`. The callback receives `(indata, frames, time_info, status)` as
    sounddevice sends it.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame_samples: int = WAKE_FRAME_SAMPLES,
        device: Any = None,
        max_queued_frames: int = 250,
        stream_factory: Callable[[Callable], Any] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.device = device
        self._queue: queue.Queue = queue.Queue(maxsize=max_queued_frames)
        self._stream_factory = stream_factory
        self._stream: Any = None
        self._dropped = 0
        self._running = False

    # --- the sounddevice callback (runs on PortAudio's thread) ---------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002 - sounddevice signature
        try:
            import numpy as np  # noqa: PLC0415

            if status:
                log.debug("microphone status: %s", status)
            data = np.asarray(indata)
            frame = data[:, 0] if data.ndim == 2 else data
            frame = np.ascontiguousarray(frame, dtype=np.int16).copy()
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                self._dropped += 1
                if self._dropped in (1, 100, 1000) or self._dropped % 10000 == 0:
                    log.warning("microphone queue full; %d frame(s) dropped so far", self._dropped)
        except Exception:  # the audio thread must never die
            log.exception("microphone callback failed")

    def _default_stream_factory(self, callback: Callable) -> Any:
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError:
            raise _voice_unavailable("sounddevice is not installed") from None
        try:
            return sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.frame_samples,
                device=self.device,
                callback=callback,
            )
        except Exception as exc:  # PortAudioError, ValueError for a bad device
            raise JarvisError(f"{MIC_HELP} ({type(exc).__name__}: {exc})") from exc

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        factory = self._stream_factory or self._default_stream_factory
        self._stream = factory(self._callback)
        try:
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise JarvisError(f"{MIC_HELP} ({type(exc).__name__}: {exc})") from exc
        self._running = True
        log.info("microphone started (%d Hz, %d-sample frames)", self.sample_rate, self.frame_samples)

    def stop(self) -> None:
        if self._stream is not None:
            for method in ("stop", "close"):
                fn = getattr(self._stream, method, None)
                if fn is not None:
                    try:
                        fn()
                    except Exception as exc:
                        log.debug("microphone %s failed: %s", method, exc)
            self._stream = None
        self._running = False
        self.flush()
        log.info("microphone stopped")

    def read(self, timeout: float | None = None) -> "np.ndarray | None":
        """Next frame (1-D int16 copy) or None on timeout / when the queue is empty and stopped."""
        try:
            if timeout is None:
                return self._queue.get_nowait() if not self._running else self._queue.get()
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    @property
    def dropped_frames(self) -> int:
        return self._dropped

    @property
    def running(self) -> bool:
        return self._running


# --- permission ---------------------------------------------------------------------------------

_AV_STATUS = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}


def microphone_permission() -> str:
    """Microphone permission of this process: never prompts, never raises."""
    try:
        import AVFoundation  # noqa: PLC0415

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return _AV_STATUS.get(int(status), "unknown")
    except Exception as exc:
        log.debug("microphone permission query failed: %s", exc)
        return "unknown"


# --- sounds and files ---------------------------------------------------------------------------


def play_sound(path: str, *, popen: Callable[..., Any] = subprocess.Popen) -> "subprocess.Popen | None":
    """Play a sound file with `afplay` (argv, non-blocking). None when it cannot be played."""
    if not path or not Path(path).expanduser().is_file():
        log.warning("sound file not found: %s", path)
        return None
    afplay = shutil.which("afplay")
    if afplay is None:
        log.warning("afplay not found; cannot play %s", path)
        return None
    try:
        return popen([afplay, str(Path(path).expanduser())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("afplay failed: %s", exc)
        return None


def read_wav_int16(path) -> "np.ndarray":
    """Load a mono 16 kHz 16-bit PCM WAV as int16. Anything else raises ValueError."""
    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        if (channels, width, rate) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"{path}: expected mono 16-bit {SAMPLE_RATE} Hz WAV, got {channels} channel(s), {width * 8}-bit, {rate} Hz")
        data = wav.readframes(wav.getnframes())
    return np.frombuffer(data, dtype=np.int16).copy()


def write_wav_int16(path, audio: "np.ndarray", sample_rate: int = SAMPLE_RATE) -> None:
    import numpy as np  # noqa: PLC0415

    audio = np.ascontiguousarray(audio, dtype=np.int16)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio.tobytes())


def iter_frames(audio: "np.ndarray", frame_samples: int = WAKE_FRAME_SAMPLES) -> "Iterator[np.ndarray]":
    """Fixed-size frames over an array; the last one is zero-padded."""
    import numpy as np  # noqa: PLC0415

    audio = np.asarray(audio)
    for start in range(0, len(audio), frame_samples):
        frame = audio[start : start + frame_samples]
        if len(frame) < frame_samples:
            frame = np.concatenate([frame, np.zeros(frame_samples - len(frame), dtype=audio.dtype)])
        yield frame


def int16_to_float32(audio: "np.ndarray") -> "np.ndarray":
    import numpy as np  # noqa: PLC0415

    return np.asarray(audio, dtype=np.float32) / 32768.0


def duration_ms(samples: int, sample_rate: int = SAMPLE_RATE) -> float:
    return samples * 1000.0 / sample_rate
