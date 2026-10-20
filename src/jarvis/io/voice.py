"""Voice interfaces.

`SpeechToText` (mlx-whisper) turns audio into text. `VoiceOutputSink` streams a reply
sentence by sentence into `TextToSpeech` (macOS `say`, `voice/tts.py`) and handles confirmations
with an on-screen dialog plus an explicit spoken phrase (`voice/confirm.py`). `VoiceInputSource`
yields one utterance per `read()` from the voice service (`voice/service.py`), which owns the
microphone, the wake word, the hotkey and the state machine. The core (`assistant.py`, `brain.py`,
tools, security) is unchanged apart from the input channel passed to the system prompt.

Heavy dependencies live in the optional `voice` extra and are imported lazily, so this module
imports without them; `SpeechToText.transcribe` then raises `VoiceUnavailable`.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from jarvis.exceptions import JarvisError
from jarvis.i18n import LANGUAGES, resolve, t
from jarvis.io.base import ConfirmOutcome, ConfirmRequest, InputSource, OutputSink
from jarvis.observability import logging_setup
from jarvis.voice import confirm as vconfirm
from jarvis.voice.hotkey import HotkeyListener  # noqa: F401
from jarvis.voice.tts import SentenceSplitter, TextToSpeech, details_phrase, list_phrase, make_speakable, table_phrase
from jarvis.voice.wakeword import WakeWordDetector as WakeWordListener  # noqa: F401 - the older names

log = logging.getLogger("jarvis.voice")

VOICE_UNAVAILABLE = "voice messages need the voice extra (`uv tool install --editable '.[voice]' --python 3.12`) and `brew install ffmpeg`"
DEFAULT_WHISPER_MODEL = "mlx-community/whisper-small-mlx"
MIN_TRANSCRIPT_LETTERS = 2
LISTEN_SLICE_S = 5.0
SPEECH_WAIT_MAX_S = 90.0  # the question (and the reply sentences before it) must have ended before we listen
SPEECH_WAIT_POLL_S = 0.1
DIALOG_WATCH_POLL_S = 0.05
FEEDBACK_WAIT_S = 10.0
MAX_BULLETS_SPOKEN = 3
NOTIFICATION_MAX_CHARS = 500  # `display notification` truncates anyway


class VoiceUnavailable(JarvisError):
    """mlx-whisper, ffmpeg or another voice dependency is not installed."""


def spoken_error(language: str | None = None) -> str:
    """The generic spoken sentence when a turn fails."""
    return t("voice.speech.error", language)


def spoken_choice(language: str | None = None) -> str:
    """Spoken before a choice dialog opens."""
    return t("voice.speech.choice", language)


# --- speech to text ---------------------------------------------------------------------------------


def _normalize_for_blocklist(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip()


def clean_transcript(text: str, blocklist: list[str] | None = None, *, after_question: bool = False) -> str | None:
    """None for an empty or near-empty transcript or a known Whisper silence hallucination.

    `after_question`: Jarvis's last reply asked something, so a one-letter answer ("E", "H") is kept:
    a short reply to a question is never dropped as noise. The hallucination blocklist still applies."""
    text = (text or "").strip()
    min_letters = 1 if after_question else MIN_TRANSCRIPT_LETTERS
    if sum(ch.isalnum() for ch in text) < min_letters:
        return None
    if is_repetition_loop(text):
        return None
    normalized = _normalize_for_blocklist(text)
    for entry in blocklist or ():
        if normalized == _normalize_for_blocklist(entry):
            return None
    return text


APPLE_LOCALES = {"tr": "tr-TR", "en": "en-US"}
APPLE_TIMEOUT_S = 30.0  # one recording; a pending permission prompt must not hang the turn
REPEAT_RUN = 4  # the same word this many times in a row is a decoder loop, not speech


def is_repetition_loop(text: str) -> bool:
    """Whisper's failure mode on silence with a prompt: one word or phrase over and over."""
    words = [w.casefold() for w in re.findall(r"\w+", text or "")]
    if len(words) < REPEAT_RUN:
        return False
    run = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        if run >= REPEAT_RUN:
            return True
    return len(words) >= 12 and len(set(words)) / len(words) < 0.3


def is_prompt_echo(text: str, prompt: str | None) -> bool:
    """The transcript is (mostly) the vocabulary list Whisper was prompted with: nothing was said."""
    if not prompt or not text or text.count(",") < 2:
        return False
    terms = {t.strip(" .").casefold() for t in prompt.split(",") if t.strip(" .")}
    pieces = [piece.strip(" .!?").casefold() for piece in text.split(",") if piece.strip(" .!?")]
    return bool(pieces) and sum(piece in terms for piece in pieces) / len(pieces) >= 0.8


class SpeechToText:
    """Speech to text for the voice service, Telegram voice notes and `debug-record`.

    `engine` ("apple", "whisper", "elevenlabs", "hybrid"; `voice.stt_engine`) picks the recognizer.
    Local mlx-whisper is always there as the fallback: any error of the chosen engine (helper missing,
    permission denied, network, quota) falls back to Whisper for that recording, so speech never gets
    lost. Apple and ElevenLabs need a fixed language; with "auto" they are skipped for Whisper.

    Whisper: `transcribe(bytes)` accepts any container ffmpeg can read (Telegram sends OGG/Opus): the
    bytes are decoded to 16 kHz mono WAV with ffmpeg (argv, never a shell) in a temp dir that is deleted
    afterwards. `transcribe_array` takes 16 kHz float32 samples straight from the microphone. `language`
    ("tr", "en") is passed to Whisper, so its per-utterance language detection never runs
    (`voice.stt_language`); with `language=None` ("auto") `languages` restricts detection: a result
    outside the list is transcribed again with the first entry forced. `model_by_language`
    swaps the model for a fixed language (`voice.stt_model_by_language`). `warm_up` loads the models
    once so the service never pays the load at request time.

    `vocabulary` (a callable returning `voice.vocabulary.Vocabulary`) gives every engine its hints;
    `corrections` (`voice.corrections.CorrectionStore`) fixes learned mishearings in every transcript.
    """

    def __init__(
        self,
        model: str = DEFAULT_WHISPER_MODEL,
        *,
        ffmpeg: str = "ffmpeg",
        languages: list[str] | None = None,
        blocklist: list[str] | None = None,
        language: str | None = None,
        model_by_language: dict[str, str] | None = None,
        engine: str = "whisper",
        primary: Any = None,
        vocabulary: Callable[[], Any] | None = None,
        corrections: Any = None,
    ) -> None:
        self.base_model = model
        self.ffmpeg = ffmpeg
        self.languages = [lang.lower() for lang in languages] if languages else None
        self.blocklist = list(blocklist or [])
        language = (language or "").strip().lower()
        self.language = None if language in ("", "auto") else language
        self.model_by_language = {k.lower(): v for k, v in (model_by_language or {}).items()}
        self.engine = engine
        self.primary = primary  # an engine from voice.stt_engines; None means Whisper
        self.vocabulary = vocabulary
        self.corrections = corrections
        self.warmed_up = False
        self.last_engine: str | None = None  # what produced the last transcript ("apple", "whisper (fallback)", ...)
        self.last_confidence: float | None = None
        self.last_error: str | None = None  # the primary engine's last failure
        self.fallbacks = 0
        self._prompt: str | None = None

    @classmethod
    def from_config(
        cls,
        voice_config: Any,
        paths: Any = None,
        *,
        db: Any = None,
        config: Any = None,
        vocabulary: Callable[[], Any] | None = None,
        corrections: Any = None,
    ) -> "SpeechToText":
        """The engine as `[voice]` configures it (the service, `debug-record` and Telegram voice notes).
        With `paths` the Apple helper, the vocabulary and the corrections are wired in."""
        v = voice_config
        language = v.stt_language
        engine = getattr(v, "stt_engine", "whisper")
        primary = None
        if paths is not None:
            if corrections is None:
                from jarvis.voice.corrections import CorrectionStore  # noqa: PLC0415

                corrections = CorrectionStore(paths.corrections_file)
            if vocabulary is None and config is not None:
                from jarvis.voice import vocabulary as vocab  # noqa: PLC0415

                store = corrections
                cache = vocab.VocabularyCache(
                    lambda: vocab.build(config, paths, db=db, corrections=store.meant_terms()), watch=paths.vocabulary_file
                )
                vocabulary = cache.get
            primary = build_primary(engine, v, paths, language)
        stt = cls(
            v.stt_model,
            languages=list(v.stt_languages),
            blocklist=list(v.stt_hallucination_blocklist),
            language=language,
            model_by_language=dict(v.stt_model_by_language),
            engine=engine,
            primary=primary,
            vocabulary=vocabulary,
            corrections=corrections,
        )
        stt.vocabulary_cache = getattr(vocabulary, "__self__", None)  # the VocabularyCache, to refresh after a correction
        return stt

    @property
    def model(self) -> str:
        """The Whisper model in use (the engine itself, or the fallback): the one mapped to the fixed
        language, else `stt_model`."""
        if self.language and self.language in self.model_by_language:
            return self.model_by_language[self.language]
        return self.base_model

    def describe(self) -> str:
        lang = f"language fixed to {self.language}" if self.language else f"language auto-detected within {self.languages or 'any'}"
        if self.primary is None:
            return f"{self.model}, {lang}"
        return f"{self.engine} ({getattr(self.primary, 'name', self.engine)}), {lang}; fallback {self.model}"

    @staticmethod
    def available(ffmpeg: str = "ffmpeg") -> bool:
        return importlib.util.find_spec("mlx_whisper") is not None and shutil.which(ffmpeg) is not None

    @staticmethod
    def model_cached(model: str = DEFAULT_WHISPER_MODEL) -> bool:
        """Whether the Hugging Face cache already holds the model (first use downloads it)."""
        cache = Path.home() / ".cache" / "huggingface" / "hub" / ("models--" + model.replace("/", "--")) / "snapshots"
        return cache.is_dir() and any(cache.glob("*/config.json"))

    def _load_whisper(self):
        try:
            import mlx_whisper  # noqa: PLC0415 - heavy optional dependency, imported on first use
        except ImportError:
            raise VoiceUnavailable(VOICE_UNAVAILABLE) from None
        return mlx_whisper

    def warm_up(self) -> float:
        """Load Whisper (the engine or the fallback) by transcribing one second of silence, and check the
        chosen engine once (the Apple helper is compiled and asked for permission here, not mid-turn)."""
        import numpy as np  # noqa: PLC0415

        whisper = self._load_whisper()
        start = time.perf_counter()
        whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=self.model)
        self._hints()  # the first vocabulary build (calendar, history) happens here, not in the first turn
        if self.primary is not None and hasattr(self.primary, "check"):
            try:
                self.primary.check()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("speech engine %s is not ready, Whisper will be used: %s", self.engine, exc)
        self.warmed_up = True
        return (time.perf_counter() - start) * 1000.0

    def _decode(self, src: Path, dst: Path, sample_rate: int) -> None:
        if shutil.which(self.ffmpeg) is None:
            raise VoiceUnavailable(VOICE_UNAVAILABLE)
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(sample_rate), str(dst)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        except FileNotFoundError:
            raise VoiceUnavailable(VOICE_UNAVAILABLE) from None
        if proc.returncode != 0:
            raise JarvisError(f"ffmpeg could not decode the audio: {(proc.stderr or '').strip()[:300]}")

    def _hints(self) -> Any:
        if self.vocabulary is None:
            return None
        try:
            return self.vocabulary()
        except Exception as exc:
            log.warning("vocabulary unavailable: %s", exc)
            return None

    def _run(self, audio: Any, **options: Any) -> tuple[str, str | None]:
        whisper = self._load_whisper()
        if self._prompt and "initial_prompt" not in options:
            options["initial_prompt"] = self._prompt
        if self.language:  # fixed: one pass, no detection
            result = whisper.transcribe(audio, path_or_hf_repo=self.model, language=self.language, **options)
            return (str(result.get("text", "")).strip() if isinstance(result, dict) else ""), self.language
        result = whisper.transcribe(audio, path_or_hf_repo=self.model, **options)
        text = str(result.get("text", "")).strip() if isinstance(result, dict) else ""
        language = result.get("language") if isinstance(result, dict) else None
        language = str(language).lower() if language else None
        if self.languages and language not in self.languages:
            fallback = self.languages[0]
            log.info("whisper detected %r, outside %s; transcribing again as %s", language, self.languages, fallback)
            result = whisper.transcribe(audio, path_or_hf_repo=self.model, language=fallback, **options)
            text = str(result.get("text", "")).strip() if isinstance(result, dict) else ""
            language = fallback
        return text, language

    def transcribe(self, audio: bytes, sample_rate: int = 16000) -> tuple[str, str | None]:
        """Any audio container (Telegram's OGG/Opus): decoded with ffmpeg, then like `transcribe_array`."""
        if not audio:
            return "", None
        self._load_whisper()  # the fallback must exist: a clear "install the voice extra" before any decoding
        with tempfile.TemporaryDirectory(prefix="jarvis-voice-") as tmp:
            src = Path(tmp) / "input.audio"
            wav = Path(tmp) / "input.wav"
            src.write_bytes(audio)
            self._decode(src, wav, sample_rate)
            if self.primary is None:  # Whisper reads the file itself
                vocab = self._hints()
                self._prompt = vocab.whisper_prompt() if vocab is not None else None
                text, language = self._run(str(wav))
                self.last_engine, self.last_confidence = "whisper", None
                return self._postprocess(text), language
            from jarvis.voice.audio import read_wav_int16  # noqa: PLC0415

            return self.transcribe_array(read_wav_int16(wav), sample_rate)

    def transcribe_array(self, audio: Any, sample_rate: int = 16000) -> tuple[str, str | None]:
        """16 kHz mono float32 samples (int16 is converted). The chosen engine first, Whisper when it fails."""
        import numpy as np  # noqa: PLC0415

        if audio is None or len(audio) == 0:
            return "", None
        if sample_rate != 16000:
            raise ValueError("transcribe_array expects 16 kHz audio")
        samples = np.asarray(audio)
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32) / 32768.0 if samples.dtype == np.int16 else samples.astype(np.float32)
        vocab = self._hints()
        self._prompt = vocab.whisper_prompt() if vocab is not None else None
        self.last_confidence = None
        text, language = None, None
        if self.primary is not None:
            set_hints(self.primary, vocab)
            try:
                result = self.primary.transcribe_array(samples)
                text, language = result.text, result.language or self.language
                self.last_engine = result.routed_to or result.engine
                self.last_confidence = result.confidence
                self.last_error = None
            except Exception as exc:
                self.fallbacks += 1
                self.last_error = str(exc)
                log.warning("speech engine %s failed, using Whisper for this recording: %s", self.engine, exc)
        if text is None:
            text, language = self._run(samples)
            self.last_engine = "whisper" if self.primary is None else "whisper (fallback)"
        return self._postprocess(text), language

    def _postprocess(self, text: str) -> str:
        """Drop a vocabulary-prompt echo, then apply the learned corrections."""
        if is_prompt_echo(text, self._prompt):
            log.info("transcript dropped: Whisper repeated its vocabulary prompt")
            return ""
        if self.corrections is not None and text:
            fixed = self.corrections.apply(text)
            if fixed != text:
                log.info("transcript corrected by data/corrections.md")
            return fixed
        return text

    def clean(self, text: str, *, after_question: bool = False) -> str | None:
        return clean_transcript(text, self.blocklist, after_question=after_question)


def set_hints(engine: Any, vocab: Any) -> None:
    """Give an engine (or both halves of a hybrid) the current vocabulary in its own form."""
    if vocab is None:
        return
    for target in (engine, getattr(engine, "local_engine", None), getattr(engine, "cloud_engine", None)):
        if target is None:
            continue
        if hasattr(target, "keyterms"):
            target.keyterms = vocab.for_keyterms()
        elif hasattr(target, "hints"):
            target.hints = vocab.for_apple()


def build_primary(engine: str, v: Any, paths: Any, language: str | None) -> Any:
    """The chosen recognizer, or None for Whisper (also when the engine cannot work in this language)."""
    from jarvis.voice import stt_engines as se  # noqa: PLC0415

    lang = (language or "").lower()
    if engine == "whisper":
        return None
    if lang not in APPLE_LOCALES:
        log.warning("voice.stt_engine %r needs voice.stt_language tr or en (not %r); Whisper is used", engine, language)
        return None
    apple = se.AppleSpeech(paths.bin_dir, locale=APPLE_LOCALES[lang], timeout_s=APPLE_TIMEOUT_S)
    if engine == "apple":
        return apple
    from jarvis.voice.elevenlabs import get_api_key  # noqa: PLC0415

    key = get_api_key()
    if not key:
        log.warning("voice.stt_engine %r needs the ElevenLabs key (`jarvis voice set-elevenlabs-key`); Apple is used", engine)
        return apple
    cloud = se.ElevenLabsStt(key, language=lang)
    if engine == "elevenlabs":
        return cloud
    return se.HybridStt(apple, cloud, threshold=float(getattr(v, "stt_hybrid_threshold", 0.6)))


# --- input source --------------------------------------------------------------------------------------


class VoiceInputSource(InputSource):
    """One utterance per `read()`: the service waits for the wake word or the hotkey, records,
    transcribes and filters; `read()` returns the text, or None when the service is stopping."""

    def __init__(self, next_utterance: Callable[[], str | None]) -> None:
        self._next = next_utterance

    def read(self) -> str | None:
        return self._next()


# --- output sink ----------------------------------------------------------------------------------------


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def spawn_daemon_thread(target: Callable[[], None]) -> threading.Thread:
    """The default dialog watcher runner: a daemon thread (the service passes this)."""
    thread = threading.Thread(target=target, name="jarvis-voice-confirm-dialog", daemon=True)
    thread.start()
    return thread


class VoiceOutputSink(OutputSink):
    """Streams the reply into `TextToSpeech` sentence by sentence and keeps unspeakable parts
    (URLs, code, paths, tables, long lists) for one macOS notification at the end of the turn.

    Confirmations: one spoken sentence plus the on-screen dialog with the actual
    arguments. Low-risk actions may be approved by an explicit spoken phrase heard through
    `listen`; `run_shell`, `write_file`, deletes and every tainted-turn confirmation accept only
    the dialog button. Silence, anything else said, Escape and the timeout deny.

    ElevenLabs: the short system phrases are spoken with `cacheable=True` so the cloud
    engine can serve them from disk; reply sentences and confirmation questions never are. Once the
    Brain reports the turn as tainted (`taint_changed`) and `use_cloud_for_tainted` is false, the
    rest of the turn is spoken with `local_only=True` (macOS `say`), so email or web content never
    reaches ElevenLabs.
    """

    def __init__(
        self,
        tts: TextToSpeech,
        *,
        confirm_phrases: list[str],
        confirm_timeout_s: float = 20.0,
        listen: Callable[..., str | None] | None = None,
        notify: Callable[[str, str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        on_attention: Callable[[str], None] | None = None,
        on_attention_done: Callable[[str], None] | None = None,
        language: str | None = None,
        dialog_starter: Callable[..., vconfirm.DialogHandle] = vconfirm.start_dialog,
        choice_dialog: Callable[..., str] = vconfirm.ask_choice_dialog,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        use_cloud_for_tainted: bool = True,
        spawn_thread: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self.tts = tts
        self.use_cloud_for_tainted = use_cloud_for_tainted
        self.matcher = vconfirm.ConfirmPhraseMatcher(confirm_phrases)
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.listen = listen
        # `listen(timeout_s, should_stop=...)`: the service stops recording as soon as the dialog
        # decided. Older one-argument listeners (tests) are called without it.
        self._listen_takes_should_stop = bool(listen) and _accepts_kwarg(listen, "should_stop")
        # The dialog watcher thread (a click settles the confirmation while the microphone is
        # busy). None: no thread, the loop polls the dialog between listen slices (tests).
        self._spawn_thread = spawn_thread
        self._pending: vconfirm.PendingConfirmation | None = None
        self.notify = notify
        self.on_text = on_text  # the full reply text, for the foreground terminal
        self.on_event = on_event  # one-line events (notices, confirmations) for the foreground terminal
        # Jarvis is about to address the user before the reply (a confirmation question, a choice
        # dialog, an error sentence): the service stops the thinking sound on `on_attention(kind)`
        # and may arm it again on `on_attention_done(kind)` when the turn goes on working.
        self.on_attention = on_attention
        self.on_attention_done = on_attention_done
        self.language = resolve(language)
        self._dialog_starter = dialog_starter
        self._dialog_language = self.language
        self._choice_dialog = choice_dialog
        self._clock = clock
        self._sleep = sleep
        self.reset()

    # -- per turn state --

    def reset(self) -> None:
        self._splitter = SentenceSplitter()
        self._details: list[str] = []
        self._bullets: list[str] = []
        self._in_fence = False
        self._fence_lines: list[str] = []
        self._phrases_spoken: set[str] = set()
        self._reply = ""
        self.cancelled = False
        self.tainted = False
        self.confirmations: list[dict[str, Any]] = []

    def begin_turn(self, language: str | None = None) -> None:
        self.reset()
        if language in LANGUAGES:
            self.language = language
        self.tts.set_default_language(self.language)
        self.tts.begin_turn()

    def cancel(self) -> None:
        """Barge-in or stop: nothing more from this turn is spoken; a pending confirmation is over."""
        self.cancelled = True
        self.tts.stop()
        pending = self._pending
        if pending is not None and not pending.settled:
            pending.settle(vconfirm.Decision("deny", "superseded", "superseded"))

    @property
    def reply_text(self) -> str:
        return self._reply

    # -- speaking --

    @property
    def local_only(self) -> bool:
        """Keep this turn's speech away from the cloud TTS (tainted turn, cloud not allowed)."""
        return self.tainted and not self.use_cloud_for_tainted

    def _say(self, text: str, *, cacheable: bool = False) -> None:
        if text and not self.cancelled:
            self.tts.speak(text, cacheable=cacheable, local_only=self.local_only)

    def _say_once(self, phrase: str) -> None:
        """The constant system phrases: spoken once per turn, cacheable by the cloud engine."""
        if phrase not in self._phrases_spoken:
            self._phrases_spoken.add(phrase)
            self._say(phrase, cacheable=True)

    def _flush_bullets(self) -> None:
        if not self._bullets:
            return
        items, self._bullets = self._bullets, []
        if len(items) > MAX_BULLETS_SPOKEN:
            self._details.append("\n".join(items))
            self._say_once(list_phrase(self.language))
            return
        for item in items:
            self._speak_sentence(item, bullet=True)

    @staticmethod
    def _is_bullet(line: str) -> bool:
        stripped = line.lstrip()
        if stripped[:2] in ("- ", "* ", "• "):
            return True
        head = stripped.split(" ", 1)[0]
        return head[:-1].isdigit() and head[-1:] in (".", ")") if head else False

    def _speak_sentence(self, sentence: str, *, bullet: bool = False) -> None:
        speakable = make_speakable(sentence, self.language)
        self._details.extend(speakable.details)
        text = speakable.text.strip()
        if not text:
            return
        if text in (table_phrase(self.language), list_phrase(self.language)):
            self._say_once(text)
            return
        self._say(text)

    def _handle_sentence(self, sentence: str) -> None:
        stripped = sentence.strip()
        if stripped.startswith("```"):
            if self._in_fence:
                self._details.append("\n".join(self._fence_lines))
                self._fence_lines = []
                self._in_fence = False
            else:
                self._in_fence = True
                self._flush_bullets()
            return
        if self._in_fence:
            self._fence_lines.append(sentence)
            return
        if self._is_bullet(stripped):
            self._bullets.append(stripped)
            return
        self._flush_bullets()
        self._speak_sentence(stripped)

    # -- OutputSink --

    def write_delta(self, text: str) -> None:
        if not text:
            return
        self._reply += text
        if self.cancelled:
            return
        for sentence in self._splitter.feed(text):
            self._handle_sentence(sentence)

    def finish(self) -> None:
        rest = self._splitter.flush()
        if rest and not self.cancelled:
            self._handle_sentence(rest)
        if self._in_fence and self._fence_lines:
            self._details.append("\n".join(self._fence_lines))
            self._fence_lines, self._in_fence = [], False
        if not self.cancelled:
            self._flush_bullets()
        if self.on_text and self._reply.strip():
            self.on_text(self._reply.strip())
        if self._details and not self.cancelled:
            body = "\n".join(d for d in self._details if d.strip())[:NOTIFICATION_MAX_CHARS]
            if self.notify is not None:
                try:
                    self.notify("Jarvis", body)
                except Exception as exc:  # a failed notification must not break the turn
                    log.warning("details notification failed: %s", exc)
            self._say_once(details_phrase(self.language))
        self._details = []

    def notice(self, text: str) -> None:
        if self.on_event:
            self.on_event(text)
        log.info("notice: %s", text)
        if text.startswith(("Error", "Hata")) and not self.cancelled:
            self._say(spoken_error(self.language), cacheable=True)

    def _attention(self, kind: str, done: bool = False) -> None:
        hook = self.on_attention_done if done else self.on_attention
        if hook is None:
            return
        try:
            hook(kind)
        except Exception:  # a sound hook must never break a confirmation or an error report
            log.exception("attention hook failed")

    def error(self, text: str, *, spoken: str | None = None) -> None:
        """The turn failed: the one-line reason goes to the foreground log; one short sentence
        (the category's, or the generic one) is spoken so the user is not left in silence. The
        thinking sound is stopped first, so the sentence never plays over it."""
        self._attention("error")
        if self.on_event:
            self.on_event(text)
        log.info("error: %s", text)
        if not self.cancelled:
            self._say(spoken or spoken_error(self.language), cacheable=True)

    def taint_changed(self, sources: list[str]) -> None:
        self.tainted = True
        log.info(
            "turn tainted (%s): %s",
            ", ".join(sources),
            "cloud TTS allowed" if self.use_cloud_for_tainted else "the rest of the turn is spoken by say",
        )

    def debug(self, text: str) -> None:
        log.debug("debug: %s", text)

    def confirm(self, request: ConfirmRequest) -> bool:
        return self.confirm_detailed(request).approved

    # -- confirmations: one decision, the dialog and the microphone racing fairly --

    def _check_dialog(self, handle: vconfirm.DialogHandle, pending: vconfirm.PendingConfirmation) -> bool:
        """Settle from the dialog if it has answered. True when the confirmation is settled."""
        if pending.settled:
            return True
        result = handle.poll()
        if result is None:
            return False
        decision, reason = vconfirm.dialog_outcome(result, self._dialog_language)
        if decision == "cancelled":  # we closed it ourselves: the other path decided
            return pending.settled
        how = "timeout" if decision == "timeout" else "dialog"
        pending.settle(vconfirm.Decision(decision, how, reason, heard=result))
        return True

    def _wait_for_question(self, handle: vconfirm.DialogHandle, pending: vconfirm.PendingConfirmation) -> float:
        """Block until the question (and the reply sentences queued before it) has been played, or
        the dialog decided meanwhile (then the rest of the question is cut). Returns the wait in ms."""
        started = self._clock()
        deadline = started + SPEECH_WAIT_MAX_S
        while not self.tts.wait(timeout=SPEECH_WAIT_POLL_S):
            if self._check_dialog(handle, pending) or self.cancelled:
                self.tts.stop()
                break
            if self._clock() > deadline:
                log.warning("the confirmation question is still playing after %.0f s; listening anyway", SPEECH_WAIT_MAX_S)
                break
        return (self._clock() - started) * 1000.0

    def _listen_slice(self, seconds: float, pending: vconfirm.PendingConfirmation) -> str | None:
        assert self.listen is not None
        if self._listen_takes_should_stop:
            return self.listen(seconds, should_stop=lambda: pending.settled)
        return self.listen(seconds)

    def _speak_and_wait(self, text: str, *, cacheable: bool) -> None:
        self._say(text, cacheable=cacheable)
        self.tts.wait(timeout=FEEDBACK_WAIT_S)

    def confirm_detailed(self, request: ConfirmRequest) -> ConfirmOutcome:
        started = self._clock()
        voice_block = vconfirm.dialog_only_reason(request)
        voice_allowed = self.listen is not None and voice_block is None
        language = self.language
        self._dialog_language = language
        question = vconfirm.spoken_question(request, voice_allowed=voice_allowed, language=language)
        self._attention("confirm")  # the thinking sound stops before the question is spoken
        if self.on_event:
            self.on_event(f"confirm {request.tool}: {'voice or dialog' if voice_allowed else 'dialog only'}")

        previous, self._pending = self._pending, None
        if previous is not None and not previous.settled:
            previous.settle(vconfirm.Decision("deny", "superseded", "superseded"))
        pending = vconfirm.PendingConfirmation()
        self._pending = pending
        verdicts: list[str] = []
        heard_last: str | None = None
        reasked = False
        question_ms: float | None = None
        listen_started: float | None = None
        watcher_stop = threading.Event()
        handle: vconfirm.DialogHandle | None = None
        try:
            # 1. The dialog opens first, so a click counts from the first second; its own timeout
            # is generous, the real deadline is enforced here once listening has started.
            text = vconfirm.dialog_text(request, voice_allowed=voice_allowed, language=language)
            timeout_s = self.confirm_timeout_s + SPEECH_WAIT_MAX_S
            # The real starter opens the dialog in the turn's language; simpler starters (tests) take no language.
            if _accepts_kwarg(self._dialog_starter, "language"):
                handle = self._dialog_starter(text, timeout_s=timeout_s, language=language)
            else:
                handle = self._dialog_starter(text, timeout_s=timeout_s)
            dialog = handle
            pending.on_settled.append(lambda d: dialog.cancel() if d.how != "dialog" else None)
            if self._spawn_thread is not None:

                def watch() -> None:
                    while not watcher_stop.is_set() and not pending.settled:
                        try:
                            if self._check_dialog(dialog, pending):
                                return
                        except Exception:
                            log.exception("dialog watcher failed")
                            return
                        time.sleep(DIALOG_WATCH_POLL_S)

                self._spawn_thread(watch)
            # 2. The question is spoken to the end before the microphone opens (Jarvis must not
            # hear itself). The question quotes the arguments: never cached.
            self.tts.speak(question, local_only=self.local_only)
            question_ms = self._wait_for_question(handle, pending)
            # 3. The answer: the deadline starts only now.
            if not pending.settled:
                listen_started = self._clock()
                deadline = listen_started + self.confirm_timeout_s
            while not pending.settled:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._check_dialog(handle, pending)  # a click that is already there beats the timeout
                    if not pending.settled:
                        pending.settle(vconfirm.Decision("timeout", "timeout", voice_block or "timeout"))
                    break
                if self.cancelled:
                    pending.settle(vconfirm.Decision("deny", "superseded", "superseded"))
                    break
                if self._check_dialog(handle, pending):
                    break
                if not voice_allowed:
                    self._sleep(0.2)
                    continue
                heard = self._listen_slice(min(LISTEN_SLICE_S, remaining), pending)
                if self._check_dialog(handle, pending):
                    break  # a click that arrived while we listened wins over whatever was heard
                if not heard:
                    continue
                heard_last = heard
                verdict = self.matcher.classify(heard)
                verdicts.append(f"{verdict.kind}:{verdict.normalized}")
                log.info("confirmation answer %r -> %s (normalized %r)", heard, verdict.kind, verdict.normalized)
                if verdict.kind == "confirm":
                    pending.settle(vconfirm.Decision("confirm", "voice_phrase", None, heard=heard))
                elif verdict.kind == "deny":
                    pending.settle(vconfirm.Decision("deny", "voice_other", "negation", heard=heard))
                elif verdict.kind == "vague" and not reasked:
                    reasked = True  # "evet" alone: ask once more, then the clock restarts
                    if self.on_event:
                        self.on_event(f"confirm {request.tool}: only {heard!r}; asking for the phrase once more")
                    self._speak_and_wait(vconfirm.reask_message(language), cacheable=True)
                    if not pending.settled:
                        deadline = self._clock() + self.confirm_timeout_s
                elif verdict.kind == "vague":
                    pending.settle(vconfirm.Decision("deny", "voice_other", "vague", heard=heard))
                else:
                    pending.settle(vconfirm.Decision("deny", "voice_other", "phrase_mismatch", heard=heard))
        except Exception:
            log.exception("confirmation failed")
            pending.settle(vconfirm.Decision("deny", "error", "error"))
        finally:
            watcher_stop.set()
            if handle is not None:
                handle.cancel()
            if self._pending is pending:
                self._pending = None

        final = pending.decision or vconfirm.Decision("deny", "error", "error")
        approved = final.decision == "confirm"
        elapsed_ms = (self._clock() - started) * 1000.0
        heard = final.heard if final.how.startswith("voice") else heard_last
        message = None if approved else vconfirm.deny_message(final.reason or "error", language, heard)
        record = {
            "tool": request.tool,
            "tainted": request.tainted,
            "voice_allowed": voice_allowed,
            "decision": final.decision,
            "how": final.how,
            "reason": final.reason,
            "heard": heard,
        }
        self.confirmations.append(record)
        logging_setup.audit(
            {
                "tool": "voice",
                "decision": f"confirm_{final.decision}",
                "how": final.how,
                "reason": final.reason,
                "for_tool": request.tool,
                "tainted": request.tainted,
                "voice_allowed": voice_allowed,
                "heard": heard,
                "heard_chars": len(heard or ""),
                "verdicts": verdicts,
                "dialog_raw": handle.result() if handle is not None else None,
                "elapsed_ms": round(elapsed_ms, 1),
                "question_ms": None if question_ms is None else round(question_ms, 1),
                "listen_after_ms": None if listen_started is None else round((listen_started - started) * 1000.0, 1),
                "reasked": reasked,
                "user_message": message,
            }
        )
        if self.on_event:
            self.on_event(f"confirm {request.tool}: {final.decision} ({final.how}" + (f", {final.reason}" if final.reason else "") + ")")
        # The user hears the decision and, when denied, why, before the model says anything.
        if approved:
            self._speak_and_wait(vconfirm.confirmed_message(language), cacheable=True)
        elif message and final.reason != "superseded":
            self._speak_and_wait(message, cacheable=final.reason != "phrase_mismatch")
        self._attention("confirm", done=True)  # the turn goes on working: the thinking sound may return
        return ConfirmOutcome(
            approved=approved,
            how=final.how,
            reason=None if approved else (final.reason or "error"),
            heard=heard,
            user_message=message,
            elapsed_ms=elapsed_ms,
        )

    def ask_choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        self._attention("choice")
        self.tts.speak(spoken_choice(self.language), cacheable=True, local_only=self.local_only)
        self.tts.wait(timeout=10.0)
        answer = self._choice_dialog(question, options, default, timeout_s=self.confirm_timeout_s)
        logging_setup.audit({"tool": "voice", "decision": "choice", "answer": answer, "question": question[:80]})
        if self.on_event:
            self.on_event(f"choice: {answer}")
        self._attention("choice", done=True)
        return answer
