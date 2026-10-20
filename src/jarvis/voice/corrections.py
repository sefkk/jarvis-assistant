"""Learning from spoken corrections: "hayır, Sezen Aksu dedim" after "Sezen Aksa'dan çal".

`detect(text)` finds the corrected words in a correction sentence (Turkish and English forms:
"hayır X dedim", "X dedim X", "X demek istedim", "no, I said X", "I meant X"). `learn(previous, text)`
aligns them with the most similar span of the previous transcript; that span is what the recognizer
misheard. The pair is stored in `data/corrections.md` as `- misheard → meant` and `apply(text)` replaces
the misheard words in every later transcript (whole words, case-insensitive). The corrected words also
join the speech vocabulary. The user may edit or delete lines in the file; it is re-read when it changes.

Pure code, no model call: when nothing lines up well enough nothing is learned, and the correction
sentence still reaches the model, which answers it from the conversation as before.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis.i18n import t
from jarvis.utils.text import fold as fold_case

log = logging.getLogger("jarvis.voice.corrections")

ARROW = "→"
MIN_SIMILARITY = 0.5  # the misheard span must look like the meant words at least this much
MAX_WORDS = 5


def file_header(language: str | None = None) -> str:
    """The comment block a new `data/corrections.md` starts with (default: the main language)."""
    return t("voice.files.corrections", language, arrow=ARROW)


_PATTERNS = [
    # "hayır, Sezen Aksu dedim" / "yok Sezen Aksu diyorum" / "hayır Sezen Aksu demiştim"
    re.compile(
        r"^(?:hayır|hayir|yok|değil|degil|yanlış|yanlis)\b[\s,.!]*(?P<x>.+?)\s+(?:dedim|demiştim|diyorum|dedimki|demek istedim|demek istiyorum)\b",
        re.IGNORECASE,
    ),
    # "Sezen Aksu dedim, Sezen Aksu" / "Sezen Aksu demek istedim"
    re.compile(r"^(?P<x>.+?)\s+(?:dedim|demiştim|demek istedim)\b", re.IGNORECASE),
    # "no, I said Sezen Aksu" / "I meant Sezen Aksu" / "no, Sezen Aksu I said"
    re.compile(r"^(?:no[\s,.!]+)?(?:i said|i meant|i mean)\s+(?P<x>.+?)$", re.IGNORECASE),
]


def fold(text: str) -> str:
    """Case- and accent-insensitive form for comparing (every i-like letter is "i", then accents dropped)."""
    text = unicodedata.normalize("NFKD", fold_case(text))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _words(text: str) -> list[str]:
    return re.findall(r"[\w’']+", text)


def _strip_suffix_apostrophe(word: str) -> str:
    return re.split(r"[’']", word, maxsplit=1)[0]


def detect(text: str) -> str | None:
    """The words the user says they meant, or None when the sentence is not a correction."""
    text = " ".join((text or "").strip().split())
    for pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            meant = match.group("x").strip(" ,.!?\"'")
            meant = re.sub(r"^(?:hayır|hayir|yok|no)\b[\s,]*", "", meant, flags=re.IGNORECASE).strip()
            if meant and 1 <= len(_words(meant)) <= MAX_WORDS:
                return meant
    return None


@dataclass
class Correction:
    heard: str
    meant: str
    similarity: float


def align(previous: str, meant: str) -> Correction | None:
    """The span of `previous` (as many words as `meant`, give or take one) most similar to `meant`."""
    words = _words(previous)
    target = [_strip_suffix_apostrophe(w) for w in _words(meant)]
    if not words or not target:
        return None
    best: Correction | None = None
    meant_folded = fold(" ".join(target))
    for size in {max(1, len(target) - 1), len(target), len(target) + 1}:
        for start in range(0, max(0, len(words) - size) + 1):
            span = [_strip_suffix_apostrophe(w) for w in words[start : start + size]]
            if not span:
                continue
            ratio = difflib.SequenceMatcher(None, fold(" ".join(span)), meant_folded).ratio()
            if best is None or ratio > best.similarity:
                best = Correction(" ".join(span), " ".join(target), ratio)
    if best is None or best.similarity < MIN_SIMILARITY or fold(best.heard) == fold(best.meant):
        return None
    return best


class CorrectionStore:
    """`data/corrections.md`: read when it changed on disk, appended by `learn`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pairs: list[tuple[str, str]] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._pairs, self._mtime = [], None
            return
        if mtime == self._mtime:
            return
        pairs = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = re.sub(r"<!--.*?-->", "", line).strip()
            if line.startswith("#"):
                continue
            if line.startswith(("- ", "* ")):
                line = line[2:]
            if ARROW in line:
                heard, meant = (part.strip() for part in line.split(ARROW, 1))
                if heard and meant and fold(heard) != fold(meant):
                    pairs.append((heard, meant))
        pairs.sort(key=lambda p: -len(p[0]))  # longer phrases first
        self._pairs, self._mtime = pairs, mtime

    def pairs(self) -> list[tuple[str, str]]:
        with self._lock:
            self._load()
            return list(self._pairs)

    def meant_terms(self) -> list[str]:
        return [meant for _heard, meant in self.pairs()]

    def apply(self, text: str) -> str:
        """Replace every learned misheard phrase (whole words, any case, Turkish suffixes kept)."""
        if not text:
            return text
        for heard, meant in self.pairs():
            pattern = re.compile(
                r"(?<![\w])" + r"\s+".join(re.escape(w) for w in heard.split()) + r"(?![^\W\d_])(?=[’'\s.,!?;:]|$)", re.IGNORECASE
            )
            text = pattern.sub(meant, text)
        return text

    def learn(self, previous: str | None, text: str) -> Correction | None:
        """Store the correction when `text` is one and lines up with `previous`; None otherwise."""
        if not previous:
            return None
        meant = detect(text)
        if meant is None:
            return None
        correction = align(previous, meant)
        if correction is None:
            log.info("correction not learned: %r did not line up with the previous transcript", meant)
            return None
        with self._lock:
            self._load()
            if any(fold(h) == fold(correction.heard) and fold(m) == fold(correction.meant) for h, m in self._pairs):
                return correction
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as fh:
                if new_file:
                    fh.write(file_header())
                fh.write(f"- {correction.heard} {ARROW} {correction.meant}  <!-- {datetime.now().strftime('%Y-%m-%d')} -->\n")
            self._mtime = None
        log.info("correction learned: %r -> %r (similarity %.2f)", correction.heard, correction.meant, correction.similarity)
        return correction
