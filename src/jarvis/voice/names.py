"""Fuzzy name matching for spoken requests (Spotify and Maps): "Sezen Aksa" is close to "Sezen Aksu".

A speech recognizer may mishear a name. Tools that act on a name call these helpers when the request
was spoken (`Runtime.spoken`): `match_score` says how well a search result fits what was asked,
`suggest` finds a known name (the speech vocabulary: earlier searches, profile, corrections) that is
close to, but not exactly, a phrase of the request. When the fit is poor the tool does not act; it
hands the model one short question for the user ("Did you mean 'Sezen Aksu'?", in the user's language).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from jarvis.voice.corrections import fold

LOW_MATCH = 0.6  # a search result fitting the request less than this is not played or routed unasked
SUGGEST_MIN = 0.75  # a known name at least this similar to a phrase of the request is offered
MIN_NAME_CHARS = 4
TOKEN_MATCH = 0.8
# Words around a name in a spoken request that are not part of it.
FILLER = {
    "çal",
    "cal",
    "aç",
    "ac",
    "oynat",
    "dinle",
    "şarkı",
    "sarki",
    "şarkısı",
    "sarkisi",
    "şarkısını",
    "sarkisini",
    "albüm",
    "album",
    "albümü",
    "albumu",
    "playlist",
    "listesi",
    "çalma",
    "spotify",
    "spotifyda",
    "dan",
    "den",
    "tan",
    "ten",
    "nin",
    "nın",
    "nun",
    "nün",
    "in",
    "ın",
    "un",
    "ün",
    "by",
    "the",
    "song",
    "play",
    "from",
    "of",
    "ve",
    "and",
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", fold(re.sub(r"[’']\w*", "", text or ""))) if t not in FILLER]


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, fold(a), fold(b)).ratio()


def match_score(query: str, candidate: str) -> float:
    """How well `candidate` (a result's name and artist) fits `query`: the share of the query's words found
    (fuzzily) in the candidate, or the whole-string similarity when that is higher. 1.0 is a full fit."""
    q, c = _tokens(query), _tokens(candidate)
    if not q or not c:
        return 0.0
    found = sum(1 for word in q if max(difflib.SequenceMatcher(None, word, other).ratio() for other in c) >= TOKEN_MATCH)
    return max(found / len(q), similarity(" ".join(q), " ".join(c)))


@dataclass
class Suggestion:
    heard: str  # the phrase of the request
    term: str  # the known name it is probably meant to be
    score: float


def suggest(text: str, terms: Iterable[str], *, minimum: float = SUGGEST_MIN) -> Suggestion | None:
    """The known name closest to a phrase of `text` (same number of words), when it is similar but not the
    same. An exact or prefix match ("Taksim'e" for "Taksim") is no suggestion: the name was heard right."""
    words = re.findall(r"[\w’']+", text or "")
    if not words:
        return None
    folded_text = fold(text)
    best: Suggestion | None = None
    for term in terms:
        term_words = term.split()
        if len(term) < MIN_NAME_CHARS or len(term_words) > len(words):
            continue
        folded_term = fold(term)
        if re.search(r"(?<!\w)" + re.escape(folded_term), folded_text):
            return None  # the name itself is in the request
        n = len(term_words)
        for start in range(0, len(words) - n + 1):
            phrase = " ".join(re.split(r"[’']", w, maxsplit=1)[0] for w in words[start : start + n])
            score = difflib.SequenceMatcher(None, fold(phrase), folded_term).ratio()
            if score >= minimum and score < 1.0 and (best is None or score > best.score):
                best = Suggestion(phrase, term, score)
    return best


def spoken(ctx: Any) -> bool:
    """Whether this turn's request came from speech (the voice service or a Telegram voice note)."""
    runtime = getattr(ctx, "runtime", None) if ctx is not None else None
    return bool(runtime is not None and (getattr(runtime, "spoken", False) or getattr(runtime, "channel", "") == "voice"))


def known_names(ctx: Any) -> list[str]:
    """The speech vocabulary without the calendar (fast enough for every tool call). Never raises."""
    try:
        from jarvis.voice import vocabulary  # noqa: PLC0415
        from jarvis.voice.corrections import CorrectionStore  # noqa: PLC0415

        runtime = getattr(ctx, "runtime", None)
        db = getattr(runtime, "db", None)
        corrections = CorrectionStore(ctx.paths.corrections_file).meant_terms()
        return vocabulary.build(ctx.config, ctx.paths, db=db, calendar=False, corrections=corrections, write=False).terms
    except Exception:
        return []


def did_you_mean(term: str, *, retry: str) -> str:
    """The tool result that makes the model ask one short question instead of acting."""
    return (
        f"Not done yet: the request was spoken and the name may be misheard. Ask the user exactly one short "
        f"question in their language, e.g. \"Did you mean '{term}'?\", and wait. If they say yes, {retry}. "
        "If they say no, ask them to say or spell the name again."
    )
