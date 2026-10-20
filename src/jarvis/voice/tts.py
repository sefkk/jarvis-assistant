"""Text to speech for the voice service: sentence streaming into macOS `say`.

Three independent pieces:
- `SentenceSplitter` turns a streamed reply into complete sentences as soon as they end, so
  speech starts before the model has finished.
- `make_speakable` strips what should not be read aloud (markdown, emoji, code, URLs, paths,
  tables, long lists) and hands those parts back as `details` for a notification.
- `TextToSpeech` runs one `say` process at a time from a queue on a worker thread. The text is
  always ONE argv element after `--` (never a shell, never interpolated); `stop()` is barge-in.

Voices are chosen per sentence by `detect_language` (Turkish letters and common words), and every
sentence is rewritten in its spoken form by `voice/normalize.py` (numbers, times, dates, units and
symbols as words) before it is queued; the same happens in `ElevenLabsTTS.speak`.

This `say` engine is the fallback behind `voice/elevenlabs.py` (`ElevenLabsTTS`
has the same interface); `voice.tts_engine` selects the main one.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

from jarvis.i18n import t
from jarvis.voice.normalize import normalize_for_speech

log = logging.getLogger("jarvis.voice.tts")


def table_phrase(language: str | None = None) -> str:
    """Spoken in place of a table, which goes to the notification."""
    return t("voice.speech.table", language)


def list_phrase(language: str | None = None) -> str:
    """Spoken in place of a long list, which goes to the notification."""
    return t("voice.speech.list", language)


def details_phrase(language: str | None = None) -> str:
    """Spoken once at the end of a turn whose URLs, code or paths went to the notification."""
    return t("voice.speech.details", language)


# --- sentence splitting -----------------------------------------------------------------------------

_TERMINATORS = ".!?…"
_CLOSERS = "\"'”’»)]}"
ABBREVIATIONS = {
    # Turkish
    "vb",
    "vs",
    "örn",
    "sn",
    "dr",
    "prof",
    "no",
    "bkz",
    "yy",
    "hz",
    "doç",
    "av",
    "mah",
    "cad",
    "sok",
    "tel",
    # English
    "mr",
    "mrs",
    "ms",
    "e.g",
    "i.e",
    "etc",
    "st",
    "jr",
    "sr",
    "inc",
    "ltd",
    "fig",
    "approx",
}


def _last_word(text: str) -> str:
    """The word right before the end of `text` (letters, digits and dots), lowercased."""
    match = re.search(r"([\w.]+)$", text, re.UNICODE)
    return match.group(1).lower() if match else ""


def _is_boundary(buffer: str, end: int) -> bool:
    """Is `buffer[end - 1]` (a terminator) the end of a sentence given the characters after it?

    `end` points just past the terminator run and any closing quotes/brackets. A boundary needs
    whitespace after it; the caller guarantees at least one character follows (or a flush).
    """
    i = end
    # step back over closers to the punctuation itself
    while i > 0 and buffer[i - 1] in _CLOSERS:
        i -= 1
    punct_end = i
    while i > 0 and buffer[i - 1] in _TERMINATORS:
        i -= 1
    punct = buffer[i:punct_end]
    before = buffer[:i]
    if end < len(buffer) and not buffer[end].isspace():
        return False  # "7.1", "15:00" ("." or no terminator), "v0.1", "1.000": no whitespace follows
    if punct == ".":
        word = _last_word(before)
        if not word:
            return False
        if word.rstrip(".") in ABBREVIATIONS or word in ABBREVIATIONS:
            return False
        if len(word) == 1 and word.isalpha() and before[-1:].isupper():
            return False  # "A. Smith"
        if re.fullmatch(r"\d+(?:[.,]\d+)*", word):
            # "1.000 TL" would need the next token to decide; a plain number before a period that
            # is followed by whitespace is most often an ordinal in Turkish ("3. kat") or a
            # trailing number in a sentence. Treat as a boundary only if the next non-space
            # character starts a new sentence (uppercase or a quote), else keep going.
            rest = buffer[end:].lstrip()
            if not rest:
                return False  # streaming: "1. " alone cannot be decided yet; wait for the next word (or the flush)
            if not (rest[0].isupper() or rest[0] in _CLOSERS or rest[0] in "\"'“‘«("):
                return False
    return True


class SentenceSplitter:
    """Streaming sentence splitter for a reply arriving in deltas."""

    def __init__(self, *, max_buffer_chars: int = 400) -> None:
        self.max_buffer_chars = max_buffer_chars
        self._buffer = ""

    @property
    def pending(self) -> str:
        return self._buffer

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        while True:
            sentence = self._pop_sentence()
            if sentence is None:
                break
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        rest, self._buffer = self._buffer.strip(), ""
        return rest

    def _pop_sentence(self) -> str | None:
        buf = self._buffer
        if not buf:
            return None
        # A line break always ends a sentence.
        nl = buf.find("\n")
        # Scan for a terminator that is followed by something (the next character decides).
        i = 0
        n = len(buf)
        while i < n:
            ch = buf[i]
            if nl != -1 and i >= nl:
                break
            if ch in _TERMINATORS:
                j = i + 1
                while j < n and buf[j] in _TERMINATORS:
                    j += 1
                while j < n and buf[j] in _CLOSERS:
                    j += 1
                if j >= n:
                    break  # the next delta decides (e.g. "7." then "1")
                if _is_boundary(buf, j):
                    self._buffer = buf[j:]
                    return buf[:j].strip()
                i = j
                continue
            i += 1
        if nl != -1:
            self._buffer = buf[nl + 1 :]
            return buf[:nl].strip()
        if len(buf) > self.max_buffer_chars:
            cut = buf.rfind(" ", 0, self.max_buffer_chars)
            if cut <= 0:
                cut = self.max_buffer_chars
            self._buffer = buf[cut:]
            return buf[:cut].strip()
        return None


def split_sentences(text: str) -> list[str]:
    splitter = SentenceSplitter()
    sentences = splitter.feed(text)
    rest = splitter.flush()
    if rest:
        sentences.append(rest)
    return sentences


# --- speakable text ---------------------------------------------------------------------------------


@dataclass
class Speakable:
    text: str
    details: list[str] = field(default_factory=list)
    had_details: bool = False


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/[\w.\-~]+(?:/[\w.\-~]*)+")
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f600-\U0001f64f\U0001f680-\U0001f6ff"
    "\U0001f1e6-\U0001f1ff\U0000fe0f\U0000200d\U000023e9-\U000023fa\U00002b50\U00002b55\U0000203c\U00002049]"
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")


def _looks_like_code(snippet: str) -> bool:
    """Inline code that is a command, path or expression rather than a plain word to read out."""
    if any(c in snippet for c in "/()=_-$|<>{}[];:\\"):
        return True
    # Several tokens in backticks ("uv sync", "git status") read like a command, not a word.
    return len(snippet.split()) > 1 and snippet.isascii()


def make_speakable(text: str, language: str | None = None) -> Speakable:
    """Remove what should not be read aloud; return it as `details` for a notification. A table or a long
    list is replaced by a short sentence in `language` (default: the main language)."""
    details: list[str] = []

    def take(match: re.Match, *, strip_fence: bool = False) -> str:
        value = match.group(0)
        if strip_fence:
            value = value.strip("`").strip()
        if value:
            details.append(value)
        return " "

    text = _FENCE_RE.sub(lambda m: take(m, strip_fence=True), text)
    text = _LINK_RE.sub(lambda m: details.append(m.group(2)) or m.group(1), text)
    text = _URL_RE.sub(take, text)

    def inline(match: re.Match) -> str:
        inner = match.group(1).strip()
        if _looks_like_code(inner):
            details.append(inner)
            return " "
        return inner

    text = _INLINE_CODE_RE.sub(inline, text)
    text = _PATH_RE.sub(take, text)
    text = _EMOJI_RE.sub("", text)

    spoken_lines: list[str] = []
    table_lines: list[str] = []
    list_items: list[str] = []
    table_seen = False

    def close_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        if len(list_items) >= 4:
            details.append("\n".join(f"- {item}" for item in list_items))
            spoken_lines.append(list_phrase(language))
        else:
            spoken_lines.extend(item if item.endswith(tuple(_TERMINATORS)) else item + "." for item in list_items)
        list_items = []

    def close_table() -> None:
        nonlocal table_lines, table_seen
        if table_lines:
            details.append("\n".join(table_lines))
            table_lines = []
            if not table_seen:
                spoken_lines.append(table_phrase(language))
                table_seen = True

    for raw in text.splitlines():
        line = raw.rstrip()
        if "|" in line and line.strip().startswith("|") or ("|" in line and line.count("|") >= 2):
            close_list()
            table_lines.append(line.strip())
            continue
        close_table()
        if _HR_RE.match(line):
            continue
        line = _HEADER_RE.sub("", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = _EMPHASIS_RE.sub(r"\2", line)
        line = line.replace("**", "").replace("__", "")
        item = _LIST_ITEM_RE.match(line)
        if item:
            list_items.append(item.group(1).strip())
            continue
        close_list()
        if line.strip():
            spoken_lines.append(line.strip())
    close_list()
    close_table()

    spoken = " ".join(spoken_lines)
    spoken = re.sub(r"\s+([.,!?;:])", r"\1", spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    return Speakable(text=spoken, details=details, had_details=bool(details))


# --- language -------------------------------------------------------------------------------------------

_TURKISH_LETTERS = set("çğıöşüİÇĞÖŞÜ")
_TURKISH_WORDS = {
    "ve",
    "bir",
    "bu",
    "için",
    "ile",
    "var",
    "yok",
    "evet",
    "hayır",
    "bugün",
    "yarın",
    "saat",
    "ne",
    "mi",
    "mı",
    "da",
    "de",
    "çok",
    "tamam",
    "şu",
    "o",
    "ben",
    "sen",
    "biz",
    "ama",
    "değil",
    "olan",
    "olarak",
    "gibi",
    "kadar",
    "sonra",
    "önce",
    "daha",
}
_ENGLISH_WORDS = {
    "the",
    "and",
    "is",
    "are",
    "you",
    "your",
    "to",
    "of",
    "in",
    "on",
    "at",
    "for",
    "with",
    "it",
    "this",
    "that",
    "have",
    "has",
    "will",
    "can",
    "today",
    "tomorrow",
    "not",
    "be",
    "i",
    "we",
    "a",
    "an",
}


def detect_language(sentence: str, default: str = "tr") -> str:
    if any(ch in _TURKISH_LETTERS for ch in sentence):
        return "tr"
    words = re.findall(r"[^\W\d_]+", sentence.lower(), re.UNICODE)
    tr = sum(w in _TURKISH_WORDS for w in words)
    en = sum(w in _ENGLISH_WORDS for w in words)
    if en > tr and en >= 1:
        return "en"
    if tr > en:
        return "tr"
    return default


# --- voices ------------------------------------------------------------------------------------------------

# "Yelda               tr_TR    # Merhaba..." and "Eddy (İngilizce (ABD)) en_US    # Hello..." (one space)
_VOICE_LINE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<locale>[a-z]{2,3}[_-][A-Za-z0-9_-]+)\s*(?:#.*)?$")


def list_voices(run=subprocess.run) -> dict[str, str]:
    """name -> locale from `say -v ?`. Empty on any failure; never raises."""
    try:
        proc = run(["say", "-v", "?"], capture_output=True, text=True, timeout=15, check=False)
    except Exception as exc:  # say missing, timeout: the doctor reports it
        log.warning("could not list say voices: %s", exc)
        return {}
    voices: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        match = _VOICE_LINE_RE.match(line.rstrip())
        if match:
            voices[match.group("name").strip()] = match.group("locale")
    if not voices:
        log.warning("`say -v ?` returned no voices (exit %s)", getattr(proc, "returncode", "?"))
    return voices


def pick_voice(preferred: str, voices: dict[str, str]) -> str | None:
    """The best installed variant of `preferred`: Premium, then Enhanced, then the plain name."""
    preferred = preferred.strip()
    if not preferred:
        return None
    base = re.sub(r"\s*\((?:Premium|Enhanced)\)\s*$", "", preferred)
    for candidate in (f"{base} (Premium)", f"{base} (Enhanced)", preferred, base):
        if candidate in voices:
            return candidate
    return None


def voice_quality(name: str, voices: dict[str, str]) -> str:
    chosen = pick_voice(name, voices)
    if chosen is None:
        return "missing"
    if chosen.endswith("(Premium)"):
        return "premium"
    if chosen.endswith("(Enhanced)"):
        return "enhanced"
    return "standard"


# --- the engine --------------------------------------------------------------------------------------------


def _default_runner(argv: list[str]):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


class TextToSpeech:
    """macOS `say`, one process at a time, fed from a queue by a worker thread. Interruptible."""

    def __init__(
        self,
        *,
        voice_tr: str = "Yelda",
        voice_en: str = "Samantha",
        rate: int = 190,
        default_language: str = "tr",
        runner: Callable[[list[str]], object] | None = None,
        voices: dict[str, str] | None = None,
        on_first_audio: Callable[[], None] | None = None,
        on_sentence: Callable[[str, str], None] | None = None,
    ) -> None:
        self.preferred = {"tr": voice_tr, "en": voice_en}
        self.rate = int(rate)
        self.default_language = default_language
        self.runner = runner or _default_runner
        self._voices_arg = voices
        self.on_first_audio = on_first_audio
        self.on_sentence = on_sentence
        self._resolved: dict[str, str | None] = {}
        self._queue: deque[tuple[str, str, int]] = deque()
        self._cond = threading.Condition()
        self._current = None
        self._generation = 0
        self._first_audio_pending = True
        self._closing = False
        self._worker: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> None:
        if self._worker is not None:
            return
        voices = self._voices_arg if self._voices_arg is not None else list_voices()
        for language, preferred in self.preferred.items():
            chosen = pick_voice(preferred, voices) if voices else None
            if chosen is None:
                if voices:
                    log.warning("say voice %r for %s is not installed; using the system default voice", preferred, language)
                else:
                    log.warning("no voice list available; using the system default voice for %s", language)
            self._resolved[language] = chosen
        self._closing = False
        self._worker = threading.Thread(target=self._run, name="jarvis-tts", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self.stop()
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None

    def begin_turn(self) -> None:
        with self._cond:
            self._first_audio_pending = True

    # -- speaking --

    def speak(self, text: str, language: str | None = None, *, cacheable: bool = False, local_only: bool = False) -> None:
        """Queue one sentence. `cacheable` and `local_only` exist for the shared engine signature
        (`ElevenLabsTTS` caches system phrases and keeps tainted turns local); `say`
        is always local and never caches, so it ignores both."""
        del cacheable, local_only
        text = text.strip()
        if not text:
            return
        language = language or detect_language(text, self.default_language)
        text = normalize_for_speech(text, language)  # digits and symbols as words (voice/normalize.py)
        if not text:
            return
        with self._cond:
            self._queue.append((text, language, self._generation))
            self._cond.notify_all()

    def speak_stream(self, sentences: Iterable[str]) -> None:
        for sentence in sentences:
            self.speak(sentence)

    def stop(self) -> None:
        """Barge-in: clear the queue and kill the current `say`. The worker keeps running."""
        with self._cond:
            self._generation += 1
            self._queue.clear()
            proc = self._current
            if proc is not None:
                try:
                    proc.kill()
                except Exception as exc:  # already gone
                    log.debug("kill failed: %s", exc)
            self._cond.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._queue or self._current is not None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(timeout=0.01 if remaining is None else min(0.01, remaining))
            return True

    @property
    def is_speaking(self) -> bool:
        with self._cond:
            return bool(self._queue) or self._current is not None

    @property
    def generation(self) -> int:
        return self._generation

    def set_default_language(self, language: str) -> None:
        self.default_language = language

    def voice_for(self, language: str) -> str | None:
        return self._resolved.get(language, self._resolved.get(self.default_language))

    def argv_for(self, text: str, language: str) -> list[str]:
        argv = ["say"]
        voice = self.voice_for(language)
        if voice:
            argv += ["-v", voice]
        argv += ["-r", str(self.rate), "--", text]
        return argv

    # -- worker --

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closing:
                    self._cond.wait(timeout=0.5)
                if self._closing and not self._queue:
                    return
                text, language, generation = self._queue.popleft()
                if generation != self._generation:
                    self._cond.notify_all()
                    continue
                argv = self.argv_for(text, language)
                voice = self.voice_for(language) or ""
                if self.on_sentence is not None:
                    try:
                        self.on_sentence(text, voice)
                    except Exception:  # a printing hook must not stop speech
                        log.exception("on_sentence hook failed")
                try:
                    proc = self.runner(argv)
                except Exception as exc:
                    log.error("could not start say: %s", exc)
                    self._cond.notify_all()
                    continue
                self._current = proc
                fire_first = self._first_audio_pending
                self._first_audio_pending = False
            if fire_first and self.on_first_audio is not None:
                try:
                    self.on_first_audio()
                except Exception:
                    log.exception("on_first_audio hook failed")
            self._wait_process(proc)
            with self._cond:
                self._current = None
                self._cond.notify_all()

    @staticmethod
    def _wait_process(proc) -> None:
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
            except Exception as exc:  # a fake or a process that vanished
                log.debug("wait failed: %s", exc)
                break
        code = getattr(proc, "returncode", 0)
        if code not in (0, None, -9):  # -9: killed by stop()
            err = ""
            stderr = getattr(proc, "stderr", None)
            if stderr is not None:
                try:
                    err = (stderr.read() or "").strip()[:300]
                except Exception:
                    err = ""
            log.warning("say exited with %s%s", code, f": {err}" if err else "")
