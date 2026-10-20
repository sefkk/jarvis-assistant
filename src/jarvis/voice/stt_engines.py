"""Speech-to-text engines behind one interface (the STT comparison, later `voice.stt_engine`).

Every engine takes 16 kHz mono float32 samples and returns an `SttResult` with the text, the language,
a confidence in [0, 1] (engine-specific: Whisper's mean segment log-probability, Apple's segment
confidence, Scribe's word log-probabilities), the time it took and an estimated cost in USD.

- `WhisperMlx`: mlx-whisper, any model on the Hugging Face hub or a local folder, with an optional
  `initial_prompt` of vocabulary hints. Local, free.
- `AppleSpeech`: the macOS Speech framework (`SFSpeechRecognizer`, on-device) through a small Swift helper
  compiled once from `apple_speech.swift` into data/bin/. Local, free. The newer `SpeechAnalyzer` API of
  macOS 26 has no Turkish, so the helper uses the classic recognizer with `requiresOnDeviceRecognition`.
- `ElevenLabsStt`: Scribe v2 over `POST /v1/speech-to-text`, with `keyterms` as hints. Cloud, paid, the
  audio leaves the Mac.
- `HybridStt`: a local engine first; the cloud engine only when the local confidence is below a threshold.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from jarvis.exceptions import JarvisError
from jarvis.i18n import current_language
from jarvis.voice.audio import SAMPLE_RATE, write_wav_int16

log = logging.getLogger("jarvis.voice.stt_engines")

SCRIBE_MODEL = "scribe_v2"
SCRIBE_USD_PER_HOUR = 0.22  # elevenlabs.io/pricing/api, October 2026
SCRIBE_KEYTERMS_USD_PER_HOUR = 0.05  # keyterm prompting surcharge
SCRIBE_TIMEOUT_S = 60.0
HELPER_NAME = "jarvis-speech"
SWIFT_SOURCE = Path(__file__).with_name("apple_speech.swift")
LOCALES = {"tr": "tr-TR", "en": "en-US"}  # the Apple recognizer locale for each `voice.stt_language`
MAIN_LANGUAGE = "main"  # default `language`: the main language (`general.language`) when none is given


def locale_for(language: str | None) -> str:
    """The Apple locale for a language ("tr" -> "tr-TR"); the main language's when unknown."""
    return LOCALES.get((language or "").lower(), LOCALES.get(current_language(), "en-US"))


def _resolve_language(language: str | None) -> str | None:
    return current_language() if language == MAIN_LANGUAGE else language


@dataclass
class SttResult:
    text: str
    language: str | None
    confidence: float | None  # 0..1, None when the engine gives none
    ms: float
    engine: str
    cost_usd: float = 0.0
    routed_to: str | None = None  # hybrid: which engine produced the text


class SttEngine(Protocol):
    name: str
    local: bool
    paid: bool

    def transcribe_array(self, audio: Any) -> SttResult: ...


def _float32(audio: Any) -> Any:
    import numpy as np  # noqa: PLC0415

    samples = np.asarray(audio)
    if samples.dtype == np.int16:
        return samples.astype(np.float32) / 32768.0
    return samples.astype(np.float32)


def _int16(audio: Any) -> Any:
    import numpy as np  # noqa: PLC0415

    samples = np.asarray(audio)
    if samples.dtype == np.int16:
        return samples
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)


def audio_seconds(audio: Any) -> float:
    return len(audio) / SAMPLE_RATE


# --- Whisper (mlx) ----------------------------------------------------------------------------------------------


class WhisperMlx:
    """mlx-whisper with a fixed language and optional vocabulary hints (`initial_prompt`)."""

    local = True
    paid = False

    def __init__(
        self,
        model: str,
        *,
        language: str | None = MAIN_LANGUAGE,
        initial_prompt: str | None = None,
        name: str | None = None,
        loader: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        self.language = _resolve_language(language)  # None: Whisper detects the language
        self.initial_prompt = initial_prompt or None
        self.name = name or model.split("/")[-1]
        self._loader = loader

    def _whisper(self):
        if self._loader is not None:
            return self._loader()
        try:
            import mlx_whisper  # noqa: PLC0415
        except ImportError:
            from jarvis.io.voice import VOICE_UNAVAILABLE, VoiceUnavailable  # noqa: PLC0415

            raise VoiceUnavailable(VOICE_UNAVAILABLE) from None
        return mlx_whisper

    def warm_up(self) -> float:
        import numpy as np  # noqa: PLC0415

        t0 = time.perf_counter()
        self._whisper().transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=self.model, **self._options())
        return (time.perf_counter() - t0) * 1000.0

    def _options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.language:
            options["language"] = self.language
        if self.initial_prompt:
            options["initial_prompt"] = self.initial_prompt
        return options

    def transcribe_array(self, audio: Any) -> SttResult:
        t0 = time.perf_counter()
        result = self._whisper().transcribe(_float32(audio), path_or_hf_repo=self.model, **self._options())
        ms = (time.perf_counter() - t0) * 1000.0
        text = str(result.get("text", "")).strip() if isinstance(result, dict) else ""
        language = result.get("language") if isinstance(result, dict) else None
        return SttResult(text, str(language).lower() if language else self.language, whisper_confidence(result), ms, self.name)


def whisper_confidence(result: Any) -> float | None:
    """exp(mean segment avg_logprob): 1.0 is certain, below ~0.5 the decoder was guessing."""
    segments = result.get("segments") if isinstance(result, dict) else None
    if not segments:
        return None
    probs = [float(s.get("avg_logprob")) for s in segments if isinstance(s, dict) and s.get("avg_logprob") is not None]
    if not probs:
        return None
    return max(0.0, min(1.0, math.exp(sum(probs) / len(probs))))


# --- Apple Speech -------------------------------------------------------------------------------------------------


# Embedded in the helper binary: macOS needs a usage description before it asks for Speech Recognition,
# and the bundle id names the permission in System Settings.
INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.jarvis.speech-helper</string>
  <key>CFBundleName</key><string>Jarvis Speech</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Jarvis turns what you say into text on this Mac.</string>
</dict>
</plist>
"""


def helper_path(bin_dir: Path, source: Path = SWIFT_SOURCE) -> Path:
    digest = hashlib.sha256(source.read_bytes() + INFO_PLIST.encode()).hexdigest()[:12]
    return bin_dir / f"{HELPER_NAME}-{digest}"


def ensure_helper(bin_dir: Path, *, source: Path = SWIFT_SOURCE, run: Callable[..., Any] = subprocess.run) -> Path:
    """Compile the Swift helper once per source version (needs the Xcode command line tools)."""
    target = helper_path(bin_dir, source)
    if target.is_file():
        return target
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        raise JarvisError("swiftc not found: install the Xcode command line tools (`xcode-select --install`) for Apple speech recognition")
    bin_dir.mkdir(parents=True, exist_ok=True)
    plist = bin_dir / "jarvis-speech-Info.plist"
    plist.write_text(INFO_PLIST, encoding="utf-8")
    t0 = time.perf_counter()
    argv = [
        swiftc,
        "-O",
        "-parse-as-library",
        str(source),
        "-o",
        str(target),
        "-Xlinker",
        "-sectcreate",
        "-Xlinker",
        "__TEXT",
        "-Xlinker",
        "__info_plist",
        "-Xlinker",
        str(plist),
    ]
    proc = run(argv, capture_output=True, text=True, timeout=600, check=False)
    for old in bin_dir.glob(f"{HELPER_NAME}-*"):  # earlier versions of the helper
        if old != target and old.suffix != ".plist":
            old.unlink(missing_ok=True)
    if proc.returncode != 0 or not target.is_file():
        raise JarvisError(f"could not compile the Apple speech helper: {(proc.stderr or '').strip()[:400]}")
    log.info("Apple speech helper compiled in %.0f s: %s", time.perf_counter() - t0, target)
    return target


class AppleSpeech:
    """On-device `SFSpeechRecognizer` for one locale; `hints` become `contextualStrings`."""

    local = True
    paid = False

    def __init__(
        self,
        bin_dir: Path,
        *,
        locale: str | None = None,
        hints: list[str] | None = None,
        name: str | None = None,
        run: Callable[..., Any] = subprocess.run,
        helper: Path | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.bin_dir = bin_dir
        self.locale = locale or locale_for(current_language())
        self.hints = [h for h in (hints or []) if h.strip()]
        self.name = name or "apple"
        self._run = run
        self._helper = helper
        self.timeout_s = float(timeout_s)

    def helper(self) -> Path:
        if self._helper is None:
            self._helper = ensure_helper(self.bin_dir)
        return self._helper

    def check(self) -> None:
        """Compile the helper and run it once on half a second of silence: raises when the permission is
        missing or the locale has no on-device model."""
        import numpy as np  # noqa: PLC0415

        self.transcribe_array(np.zeros(SAMPLE_RATE // 2, dtype=np.int16))

    def transcribe_array(self, audio: Any) -> SttResult:
        helper = self.helper()
        with tempfile.TemporaryDirectory(prefix="jarvis-apple-stt-") as tmp:
            wav = Path(tmp) / "input.wav"
            write_wav_int16(wav, _int16(audio))
            t0 = time.perf_counter()
            try:
                proc = self._run(
                    [str(helper), self.locale, str(wav), *[h for h in self.hints if h.strip()]],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise JarvisError(
                    f"Apple speech recognition timed out after {self.timeout_s:.0f} s (a pending Speech Recognition permission prompt?)"
                ) from None
            ms = (time.perf_counter() - t0) * 1000.0
        line = (proc.stdout or "").strip().splitlines()
        try:
            data = json.loads(line[-1]) if line else {}
        except json.JSONDecodeError:
            data = {}
        stderr = (proc.stderr or "").strip()
        if "no speech detected" in stderr.lower():  # silence or noise: an empty transcript, like Whisper's
            return SttResult("", self.locale.split("-")[0].lower(), None, ms, self.name)
        if proc.returncode != 0 or "error" in data or not line:
            detail = " ".join(part for part in (data.get("error"), stderr[:300]) if part) or f"exit {proc.returncode}"
            raise JarvisError(f"Apple speech recognition failed: {detail}")
        confidence = data.get("confidence")
        return SttResult(
            str(data.get("text", "")).strip(),
            self.locale.split("-")[0].lower(),
            float(confidence) if confidence is not None else None,
            ms,
            self.name,
        )


# --- ElevenLabs Scribe ----------------------------------------------------------------------------------------------


class ElevenLabsStt:
    """Scribe v2 (`POST /v1/speech-to-text`). `keyterms` are the vocabulary hints (a 20 % surcharge)."""

    local = False
    paid = True

    def __init__(
        self,
        api_key: str,
        *,
        language: str | None = MAIN_LANGUAGE,
        keyterms: list[str] | None = None,
        model: str = SCRIBE_MODEL,
        name: str | None = None,
        transport: Any = None,
    ) -> None:
        self.api_key = api_key
        self.language = _resolve_language(language)  # None: Scribe detects the language
        self.keyterms = [k.strip() for k in (keyterms or []) if k.strip()][:1000]
        self.model = model
        self.name = name or "elevenlabs"
        self._transport = transport

    @property
    def usd_per_minute(self) -> float:
        return (SCRIBE_USD_PER_HOUR + (SCRIBE_KEYTERMS_USD_PER_HOUR if self.keyterms else 0.0)) / 60.0

    def transcribe_array(self, audio: Any) -> SttResult:
        import httpx  # noqa: PLC0415

        from jarvis.voice.elevenlabs import API_BASE, _error_from_response, _safe_error  # noqa: PLC0415

        with tempfile.TemporaryDirectory(prefix="jarvis-scribe-") as tmp:
            wav = Path(tmp) / "input.wav"
            write_wav_int16(wav, _int16(audio))
            data: dict[str, Any] = {"model_id": self.model}
            if self.language:
                data["language_code"] = self.language
            if self.keyterms:
                data["keyterms"] = self.keyterms
            t0 = time.perf_counter()
            try:
                with httpx.Client(
                    base_url=API_BASE,
                    headers={"xi-api-key": self.api_key},
                    timeout=httpx.Timeout(SCRIBE_TIMEOUT_S, connect=5.0),
                    transport=self._transport,
                ) as client:
                    with wav.open("rb") as fh:
                        response = client.post("/v1/speech-to-text", data=data, files={"file": ("input.wav", fh, "audio/wav")})
            except httpx.HTTPError as exc:
                raise JarvisError(f"ElevenLabs speech to text failed: {_safe_error(exc)}") from None
            ms = (time.perf_counter() - t0) * 1000.0
        if response.status_code != 200:
            raise JarvisError(f"ElevenLabs speech to text failed: {_error_from_response(response)}")
        body = response.json()
        text = re.sub(r"\s*[\[(][^\])]{1,40}[\])]\s*", " ", str(body.get("text", ""))).strip()  # "[tık sesi]": an audio event, not speech
        language = body.get("language_code")
        words = [w for w in body.get("words", []) if isinstance(w, dict) and w.get("type", "word") == "word"]
        logprobs = [float(w["logprob"]) for w in words if w.get("logprob") is not None]
        confidence = max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs)))) if logprobs else None
        cost = audio_seconds(audio) / 60.0 * self.usd_per_minute
        return SttResult(text, _iso1(language), confidence, ms, self.name, cost_usd=cost)


_ISO3_TO_1 = {"tur": "tr", "eng": "en", "deu": "de", "fra": "fr", "spa": "es", "ita": "it", "rus": "ru", "ara": "ar"}


def _iso1(code: Any) -> str | None:
    if not code:
        return None
    code = str(code).lower()
    return _ISO3_TO_1.get(code, code[:2])


# --- hybrid -----------------------------------------------------------------------------------------------------------


class HybridStt:
    """`local` first; when its confidence is below `threshold` (or missing) the `cloud` engine decides.
    A cloud failure keeps the local text, so nothing is lost offline."""

    local = False
    paid = True

    def __init__(self, local: Any, cloud: Any, *, threshold: float = 0.6, name: str = "hybrid") -> None:
        self.local_engine = local
        self.cloud_engine = cloud
        self.threshold = float(threshold)
        self.name = name

    def check(self) -> None:
        check = getattr(self.local_engine, "check", None)
        if check is not None:
            check()

    def transcribe_array(self, audio: Any) -> SttResult:
        first = self.local_engine.transcribe_array(audio)
        if first.confidence is not None and first.confidence >= self.threshold and first.text:
            return SttResult(first.text, first.language, first.confidence, first.ms, self.name, routed_to=first.engine)
        try:
            second = self.cloud_engine.transcribe_array(audio)
        except Exception as exc:
            log.warning("hybrid STT: cloud engine failed, keeping the local text: %s", exc)
            return SttResult(first.text, first.language, first.confidence, first.ms, self.name, routed_to=first.engine)
        return SttResult(
            second.text,
            second.language,
            second.confidence,
            first.ms + second.ms,
            self.name,
            cost_usd=second.cost_usd,
            routed_to=second.engine,
        )


# --- registry ---------------------------------------------------------------------------------------------------------


@dataclass
class EngineSpec:
    key: str
    label: str
    build: Callable[[], Any]
    local: bool
    paid: bool
    usd_per_minute: float = 0.0
    requires_model: str | None = None  # a Hugging Face model that must be cached (or downloaded)
    notes: list[str] = field(default_factory=list)
