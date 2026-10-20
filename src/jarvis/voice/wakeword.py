"""Wake word detection with openWakeWord: "hey jarvis" on the CPU, audio stays local.

The pretrained `hey_jarvis` model runs through onnxruntime (tflite has no Python 3.12 wheel on
macOS). `WakeWordDetector` adds what the raw model lacks: a threshold, a cooldown so one phrase
cannot trigger twice, and a suppression window used while Jarvis itself is speaking.
"""

from __future__ import annotations

import importlib.util
import logging
import time
from pathlib import Path
from typing import Any, Callable

from jarvis.exceptions import JarvisError

log = logging.getLogger("jarvis.voice.wakeword")

DEFAULT_MODEL = "hey_jarvis"
MODEL_VERSION = "v0.1"
FEATURE_FILES = ("melspectrogram.onnx", "embedding_model.onnx")


def openwakeword_available() -> bool:
    return importlib.util.find_spec("openwakeword") is not None


def model_dir() -> Path:
    """openWakeWord's `resources/models` directory (where `download_models` writes)."""
    spec = importlib.util.find_spec("openwakeword")
    if spec is None or not spec.origin:
        raise JarvisError("openwakeword is not installed: reinstall with `uv tool install --editable '.[voice]' --python 3.12`")
    return Path(spec.origin).resolve().parent / "resources" / "models"


def _model_file(model_name: str) -> str:
    return f"{model_name}_{MODEL_VERSION}.onnx"


def models_present(model_name: str = DEFAULT_MODEL) -> bool:
    """The wake word's onnx file and the two feature models exist."""
    try:
        directory = model_dir()
    except JarvisError:
        return False
    return all((directory / name).is_file() for name in (_model_file(model_name), *FEATURE_FILES))


class WakeWordDetector:
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        threshold: float = 0.5,
        cooldown_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        model: Any = None,
    ) -> None:
        self.model_name = model_name
        self.threshold = float(threshold)
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self._model = model
        self._last_detection: float | None = None
        self._suppressed_until: float | None = None
        self._last_score = 0.0

    # --- loading ----------------------------------------------------------------------------

    def load(self) -> None:
        """Load (and if needed download) the model. Idempotent."""
        if self._model is not None:
            return
        try:
            import openwakeword.utils  # noqa: PLC0415
            from openwakeword.model import Model  # noqa: PLC0415
        except ImportError:
            from jarvis.io.voice import VOICE_UNAVAILABLE, VoiceUnavailable  # noqa: PLC0415

            raise VoiceUnavailable(f"{VOICE_UNAVAILABLE} (openwakeword is not installed)") from None
        if not models_present(self.model_name):
            log.info("downloading openWakeWord model %s", self.model_name)
            try:
                openwakeword.utils.download_models(model_names=[self.model_name])
            except Exception as exc:
                raise JarvisError(f"could not download the wake word model {self.model_name!r}: {exc}") from exc
            if not models_present(self.model_name):
                raise JarvisError(
                    f"wake word model {self.model_name!r} is unknown to openWakeWord or failed to download (looked in {model_dir()})"
                )
        started = time.perf_counter()
        try:
            self._model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
        except Exception as exc:
            raise JarvisError(f"could not load the wake word model {self.model_name!r}: {exc}") from exc
        log.info("wake word model %s loaded in %.0f ms", self.model_name, (time.perf_counter() - started) * 1000)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _require_model(self) -> Any:
        if self._model is None:
            raise JarvisError("wake word model not loaded; call load() first")
        return self._model

    # --- detection ----------------------------------------------------------------------------

    def score(self, frame) -> float:
        """Highest score over the loaded models for one 1280-sample int16 frame."""
        prediction = self._require_model().predict(frame)
        self._last_score = max((float(v) for v in prediction.values()), default=0.0)
        return self._last_score

    def process(self, frame) -> bool:
        """True exactly once per detection: above threshold, not suppressed, cooldown elapsed."""
        score = self.score(frame)
        if score < self.threshold:
            return False
        now = self.clock()
        if self._suppressed_until is not None and now < self._suppressed_until:
            return False
        if self._last_detection is not None and now - self._last_detection < self.cooldown_s:
            return False
        self._last_detection = now
        self._reset_model()
        log.info("wake word detected (score %.2f)", score)
        return True

    def reset(self) -> None:
        """Forget model state and any suppression (the last detection time stays for the cooldown)."""
        self._reset_model()
        self._suppressed_until = None

    def _reset_model(self) -> None:
        if self._model is None:
            return
        reset = getattr(self._model, "reset", None)
        if reset is not None:
            try:
                reset()
            except Exception as exc:
                log.debug("wake word model reset failed: %s", exc)

    def suppress(self, seconds: float) -> None:
        """Ignore detections until now + seconds (while Jarvis speaks and briefly afterwards)."""
        until = self.clock() + max(0.0, float(seconds))
        if self._suppressed_until is None or until > self._suppressed_until:
            self._suppressed_until = until

    @property
    def last_score(self) -> float:
        return self._last_score
