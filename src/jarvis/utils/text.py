"""Matching what the user said or typed against word lists, the same way in English and Turkish.

Turkish has a dotted and a dotless i (İ/i, I/ı). Lowercasing with the Turkish rule turns the English "I" into
"ı" ("CONFIRM" -> "confırm"), and with the English rule turns "KAPAT" fine but "ILIK" into "ilik". `fold`
sidesteps both: every i-like letter becomes a plain "i", so "Iptal", "İPTAL", "ıptal" and "iptal" are equal,
and so are "CONFIRM" and "confirm". Fold both sides of a comparison.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable

_I_LIKE = str.maketrans({"İ": "i", "I": "i", "ı": "i"})


def fold(text: str) -> str:
    """Lowercase, NFC, with every i-like letter folded to "i". Keeps the other Turkish letters (ç, ğ, ö, ş, ü)."""
    return unicodedata.normalize("NFC", text or "").translate(_I_LIKE).casefold()


def fold_words(words: Iterable[str]) -> set[str]:
    return {fold(word) for word in words}
