"""data/memory.md: notes Jarvis keeps about the user.

Plain text, one fact per line, prefixed with the date. The user may edit the file by hand at
any time, so it is re-read whenever it is needed and never cached in memory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.security import sanitize
from jarvis.utils.text import fold
from jarvis.utils.timeutil import today_local

MAX_ENTRY_CHARS = 300
MAX_BYTES = 8 * 1024
WARN_FRACTION = 0.85

_ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}): (.*)$")
_TAG_RE = re.compile(r"<\s*/?\s*(memory|untrusted|user_profile|persona|rules|system)\b", re.IGNORECASE)


@dataclass
class WriteOutcome:
    entry: str
    size_bytes: int
    near_cap: bool
    status: str = "saved"  # saved | duplicate (nothing written) | replaced (old lines removed first)
    removed: list[str] = field(default_factory=list)  # the lines a `replaces` write took out

    @property
    def written(self) -> bool:
        return self.status in ("saved", "replaced")

    @property
    def fact(self) -> str:
        """The entry without its date prefix."""
        m = _ENTRY_RE.match(self.entry)
        return m.group(2) if m else self.entry

    def note(self) -> str:
        if self.near_cap:
            return f" Memory is at {self.size_bytes} of {MAX_BYTES} bytes; suggest the user prune it soon."
        return ""


@dataclass(frozen=True)
class Entry:
    date: str  # YYYY-MM-DD, or "" for a hand-written line without one
    text: str
    line: str


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
DUPLICATE_OVERLAP = 0.8  # word-set Jaccard at or above this: the same fact said again


def normalize_fact(text: str) -> str:
    text = fold(text)
    return " ".join(_WORD_RE.findall(text))


def fact_words(text: str) -> set[str]:
    return set(normalize_fact(text).split())


def related(new: str, old: str) -> bool:
    """The two lines are about the same thing: they share a content word (four letters or more, a Turkish
    suffix allowed: "salonu" matches "salonuna"). A `replaces` that names an unrelated line is ignored, so
    a wrong guess by the model cannot delete a note about something else."""
    wa = [w for w in fact_words(new) if len(w) >= 4]
    wb = [w for w in fact_words(old) if len(w) >= 4]
    return any(a == b or a.startswith(b) or b.startswith(a) for a in wa for b in wb)


def looks_duplicate(new: str, old: str) -> bool:
    """The same fact in the same or nearly the same words (not a contradiction: a different day or
    place changes the words, so the overlap drops and the caller should use `replaces`)."""
    a, b = normalize_fact(new), normalize_fact(old)
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = set(a.split()), set(b.split())
    if wa <= wb or wb <= wa:  # one says everything the other says (and a word or two more)
        return min(len(wa), len(wb)) >= 3
    return len(wa & wb) / len(wa | wb) >= DUPLICATE_OVERLAP


def sanitize_entry(fact: str) -> str:
    """One clean line: normalized, no line breaks, no fake sections, no tags, length capped."""
    text = sanitize.for_model(fact)
    text = " ".join(text.split())  # collapses newlines and runs of whitespace
    text = text.replace("<", "‹").replace(">", "›")
    if text.startswith("#"):
        text = "\\" + text
    if not text:
        raise ToolValidationError("the fact is empty")
    if len(text) > MAX_ENTRY_CHARS:
        text = text[: MAX_ENTRY_CHARS - 1].rstrip() + "…"
    return text


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    # --- reading --------------------------------------------------------------------------

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def lines(self) -> list[str]:
        return [line for line in self.read().splitlines() if line.strip()]

    def entries(self) -> list[Entry]:
        """Every non-empty line, with its date when it has the `- YYYY-MM-DD: fact` form."""
        out = []
        for line in self.lines():
            m = _ENTRY_RE.match(line)
            out.append(Entry(m.group(1), m.group(2), line) if m else Entry("", line, line))
        return out

    def find_duplicate(self, fact: str) -> Entry | None:
        clean = sanitize_entry(fact)
        for entry in self.entries():
            if looks_duplicate(clean, entry.text):
                return entry
        return None

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def for_prompt(self) -> str:
        """Memory text for the <memory> section, with tag-like text neutralized (the file is hand-editable)."""
        text = sanitize.for_model(self.read()).strip()
        return _TAG_RE.sub(lambda m: m.group(0).replace("<", "‹"), text)

    # --- writing --------------------------------------------------------------------------

    def remember(self, fact: str, *, replaces: str | None = None) -> WriteOutcome:
        """Append one dated line. A fact already in memory (same or nearly the same words) is not
        written twice (`status = "duplicate"`). `replaces` names a few distinctive words of an older
        line the new fact supersedes (a new day, a new address): those lines are removed first, so
        the old and the new version never sit next to each other (`status = "replaced"`)."""
        entry = f"- {today_local()}: {sanitize_entry(fact)}"
        removed: list[str] = []
        needle = " ".join(sanitize.for_model(replaces or "").split()).lower()
        kept: list[str] = []
        clean = sanitize_entry(fact)
        for line in self.read().splitlines():
            if needle and needle in line.lower() and related(clean, _text_of(line)):
                removed.append(line)
            else:
                kept.append(line)
        duplicate = next((line for line in kept if line.strip() and looks_duplicate(sanitize_entry(fact), _text_of(line))), None)
        if duplicate is not None and not removed:
            return WriteOutcome(entry=duplicate, size_bytes=self.size_bytes(), near_cap=False, status="duplicate")
        if duplicate is not None:
            kept = [line for line in kept if line != duplicate]  # the replacement wins over an older copy
            removed.append(duplicate)
        current = "\n".join(kept).rstrip("\n")
        current = current + "\n" if current else ""
        new_size = len(current.encode("utf-8")) + len((entry + "\n").encode("utf-8"))
        if new_size > MAX_BYTES:
            raise ToolExecutionError(
                f"memory is full ({self.size_bytes()} of {MAX_BYTES} bytes). Nothing was saved; "
                "ask the user to prune memory.md or use forget."
            )
        self._write(current + entry + "\n")
        return WriteOutcome(
            entry=entry,
            size_bytes=new_size,
            near_cap=new_size >= MAX_BYTES * WARN_FRACTION,
            status="replaced" if removed else "saved",
            removed=removed,
        )

    def forget(self, text: str) -> int:
        """Remove every line containing `text` (case-insensitive). Returns the number removed."""
        needle = " ".join(sanitize.for_model(text).split()).lower()
        if not needle:
            raise ToolValidationError("the text to forget is empty")
        kept: list[str] = []
        removed = 0
        for line in self.read().splitlines():
            if needle in line.lower():
                removed += 1
            else:
                kept.append(line)
        if removed:
            self._write("\n".join(kept).rstrip("\n") + ("\n" if kept else ""))
        return removed

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)


def _text_of(line: str) -> str:
    m = _ENTRY_RE.match(line)
    return m.group(2) if m else line
