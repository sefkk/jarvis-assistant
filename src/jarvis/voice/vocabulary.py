"""Personal vocabulary for speech recognition: names and places the engines should expect.

Two sources, merged in this order (the first ones win when the list is capped):

1. `data/vocabulary.md`, written by the user: one term per line (a leading "- " is fine, `#` starts a
   comment). Created with a short header on first use.
2. Terms collected by code (`collect_auto_terms`): Jarvis and the configured football team, Shortcut names, Sheets aliases,
   presence people, names from profile.md (the Name line and People / Family / Friends sections), the
   artists and tracks asked for on Spotify, places searched or routed to in Maps and calendar event
   locations from the tool history, upcoming and recent calendar locations, and the corrected words of
   `data/corrections.md`. They are written to `data/vocabulary.auto.md` so the user can see them.

Never collected: email content (no Gmail tool is read), secrets, email addresses, URLs, long numbers or
anything that looks like a token. Every term is 2-50 characters and at most 5 words.

The engines get the list in their own form: Apple `contextualStrings` (`for_apple`, the first 20),
Whisper `initial_prompt` (`whisper_prompt`, about 200 tokens) and ElevenLabs `keyterms` (`for_keyterms`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from jarvis.i18n import t

log = logging.getLogger("jarvis.voice.vocabulary")

SEED_TERMS = ["Jarvis"]  # after the configured football team (`football.team`)
MAX_TERM_CHARS = 50
MAX_TERM_WORDS = 5
APPLE_LIMIT = 20  # each contextual string costs Apple ~5 ms per recording (measured: 0 hints 290 ms, 50 hints 530 ms)
KEYTERMS_LIMIT = 1000
WHISPER_PROMPT_CHARS = 600  # ~200 tokens of Turkish; Whisper keeps the last 224 prompt tokens
HISTORY_DAYS = 120
HISTORY_LIMIT = 2000
CALENDAR_PAST_DAYS = 14
CALENDAR_AHEAD_DAYS = 60
REFRESH_S = 30 * 60

# Tool arguments that name a person, a piece of music or a place (tool -> argument keys).
HISTORY_ARGS: dict[str, tuple[str, ...]] = {
    "search_and_play": ("query",),
    "search_places": ("query", "near"),
    "travel_time": ("destination", "origin"),
    "open_in_maps": ("query", "destination"),
    "geocode": ("address",),
    "place_details": (),
    "apple_create_event": ("location",),
    "google_create_event": ("location",),
}
PEOPLE_HEADINGS = ("people", "family", "friends", "kişiler", "aile", "arkadaşlar", "insanlar")
NOT_A_PLACE = {"home", "ev", "eve", "evden", "evim", "current location", "last known"}


def file_header(language: str | None = None) -> str:
    """The comment block a new `data/vocabulary.md` starts with (default: the main language)."""
    return t("voice.files.vocabulary", language)


_EMAIL = re.compile(r"\S+@\S+")
_URL = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_TOKENISH = re.compile(r"(sk[_-]|xi-|AIza|ghp_|[A-Za-z0-9_\-]{20,})")
_DIGITS = re.compile(r"\d")


def clean_term(raw: Any) -> str | None:
    """A usable hint or None: trimmed, no email, URL, secret-looking token or mostly digits."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.replace("\n", " ").split()).strip(" -•*\"'.,;:!?()[]{}")
    if not (2 <= len(text) <= MAX_TERM_CHARS) or len(text.split()) > MAX_TERM_WORDS:
        return None
    if _EMAIL.search(text) or _URL.search(text) or _TOKENISH.search(text):
        return None
    if sum(ch.isdigit() for ch in text) > max(2, len(text) // 3):
        return None
    if not any(ch.isalpha() for ch in text):
        return None
    if text.casefold() in NOT_A_PLACE:
        return None
    return text


def _dedupe(terms: Iterable[str]) -> list[str]:
    seen: dict[str, str] = {}
    for term in terms:
        cleaned = clean_term(term)
        if cleaned is None:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen[key] = cleaned
    return list(seen.values())


# --- the user's file ---------------------------------------------------------------------------------------------


def read_user_terms(path: Path) -> list[str]:
    if not path.is_file():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip() if not line.lstrip().startswith("#") else ""
        if line.startswith(("- ", "* ")):
            line = line[2:]
        if line:
            terms.append(line)
    return _dedupe(terms)


def ensure_user_file(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_header(), encoding="utf-8")


# --- collected terms ----------------------------------------------------------------------------------------------


def config_terms(config: Any) -> list[str]:
    terms: list[str] = []
    try:
        team = str(config.football.team or "").strip()
        if team:
            terms.append(team)
    except Exception:
        pass
    terms += SEED_TERMS
    try:
        terms += [entry.name for entry in config.shortcuts.allowed]
    except Exception:
        pass
    try:
        terms += list(config.sheets.aliases.keys())
    except Exception:
        pass
    try:
        for person in getattr(config.presence, "people", []) or []:
            name = getattr(person, "name", None) if not isinstance(person, dict) else person.get("name")
            if name:
                terms.append(str(name))
    except Exception:
        pass
    return terms


def profile_terms(profile: Path) -> list[str]:
    """The Name line and the bullet names under a People / Family / Friends heading of profile.md."""
    if not profile.is_file():
        return []
    terms: list[str] = []
    in_people = False
    for line in profile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_people = any(word in stripped.casefold() for word in PEOPLE_HEADINGS)
            continue
        if stripped.startswith("<!--"):
            continue
        match = re.match(r"[-*]\s*(name|address me as|isim|ad)\s*:\s*(.+)", stripped, re.IGNORECASE)
        if match:
            terms.append(match.group(2))
            continue
        if in_people and stripped.startswith(("- ", "* ")):
            entry = stripped[2:]
            name = re.split(r"[:(—–,]| - ", entry, maxsplit=1)[0]  # "Ayşe (sister): ..." -> "Ayşe"
            terms.append(name.strip())
    return terms


def _read_only_db(path: Path) -> Any:
    """A read-only view of data/jarvis.db for processes without an Assistant (the voice service at start)."""
    import sqlite3  # noqa: PLC0415

    if not path.is_file():
        return None

    class _Ro:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)

    return _Ro()


def history_terms(db: Any, *, days: int = HISTORY_DAYS, limit: int = HISTORY_LIMIT) -> list[str]:
    """Names from earlier tool calls (Spotify queries, Maps places, event locations), most frequent first."""
    if db is None:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # the db stores UTC "Z" times
    tools = tuple(HISTORY_ARGS)
    try:
        rows = db.conn.execute(
            f"SELECT tool, args_json FROM tool_calls WHERE tool IN ({','.join('?' * len(tools))}) AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (*tools, since, limit),
        ).fetchall()
    except Exception as exc:
        log.debug("vocabulary: tool history unavailable: %s", exc)
        return []
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for n, row in enumerate(rows):
        tool, args_json = row[0], row[1]
        try:
            args = json.loads(args_json or "{}")
        except ValueError:
            continue
        for key in HISTORY_ARGS.get(tool, ()):
            for piece in _place_pieces(args.get(key)) if key != "query" or tool != "search_and_play" else [args.get(key)]:
                term = clean_term(piece)
                if term:
                    counts[term] += 1
                    first_seen.setdefault(term, n)
    return sorted(counts, key=lambda t: (-counts[t], first_seen[t]))


def _place_pieces(value: Any) -> list[str]:
    """'Bağdat Cd. No:5, Kadıköy/İstanbul' -> the named parts ('Bağdat Cd.', 'Kadıköy', 'İstanbul')."""
    if not isinstance(value, str):
        return []
    pieces = []
    for part in re.split(r"[,/\n]", value):
        part = re.sub(r"\bNo\s*[:.]?\s*\d+\w*", "", part, flags=re.IGNORECASE)
        part = re.sub(r"\b\d{3,}\b", "", part).strip()
        if part:
            pieces.append(part)
    return pieces


def calendar_terms(config: Any, *, now: datetime | None = None, fetch: Callable[..., Any] | None = None) -> list[str]:
    """Event locations from the recent past to two months ahead (never titles, notes or attendees)."""
    now = now or datetime.now().astimezone()
    try:
        if fetch is None:
            fetch = _calendar_without_prompt
        events, _failures = fetch(config, now - timedelta(days=CALENDAR_PAST_DAYS), now + timedelta(days=CALENDAR_AHEAD_DAYS))
    except Exception as exc:
        log.debug("vocabulary: calendar unavailable: %s", exc)
        return []
    terms: list[str] = []
    for event in events:
        terms += _place_pieces(getattr(event, "location", ""))
    return terms


def _calendar_without_prompt(config: Any, start: datetime, end: datetime) -> tuple[list[Any], dict[str, str]]:
    """Every readable calendar, skipping Apple's when its permission is not granted yet: the voice agent must
    never trigger the macOS Calendar prompt just to collect hints (as `jarvis calendar filters` does)."""
    from jarvis.tools import calendar_merged as cm  # noqa: PLC0415

    fetchers = dict(cm._fetchers())
    if "apple" in fetchers and cm._apple_not_ready():
        fetchers.pop("apple")
    return cm._collect(config, start, end, fetchers)


# --- the merged list -------------------------------------------------------------------------------------------------


@dataclass
class Vocabulary:
    user: list[str] = field(default_factory=list)
    auto: list[str] = field(default_factory=list)
    built_at: float = 0.0

    @property
    def terms(self) -> list[str]:
        return _dedupe([*self.user, *self.auto])

    def for_apple(self) -> list[str]:
        return self.terms[:APPLE_LIMIT]

    def for_keyterms(self) -> list[str]:
        return self.terms[:KEYTERMS_LIMIT]

    def whisper_prompt(self) -> str | None:
        """A comma-separated list that fits Whisper's prompt window; None when empty."""
        picked: list[str] = []
        size = 0
        for term in self.terms:
            if size + len(term) + 2 > WHISPER_PROMPT_CHARS:
                break
            picked.append(term)
            size += len(term) + 2
        return ", ".join(picked) + "." if picked else None


def build(
    config: Any,
    paths: Any,
    *,
    db: Any = None,
    calendar: bool = True,
    calendar_fetch: Callable[..., Any] | None = None,
    corrections: Iterable[str] = (),
    write: bool = True,
) -> Vocabulary:
    """Read the user's file and collect the automatic terms; writes vocabulary.auto.md (unless `write` is
    false). Never raises."""
    if write:
        ensure_user_file(paths.vocabulary_file)
    user = read_user_terms(paths.vocabulary_file)
    if callable(db):
        db = db()
    own = None
    if db is None:
        try:
            db = own = _read_only_db(paths.db_file)
        except Exception as exc:
            log.debug("vocabulary: database unavailable: %s", exc)
    auto: list[str] = []
    for source, collect in (  # most useful first: Apple only gets the first APPLE_LIMIT terms
        ("corrections", lambda: list(corrections)),
        ("profile", lambda: profile_terms(paths.profile_file)),
        ("history", lambda: history_terms(db)),
        ("config", lambda: config_terms(config)),
        ("calendar", lambda: calendar_terms(config, fetch=calendar_fetch) if calendar else []),
    ):
        try:
            auto += collect()
        except Exception as exc:  # one broken source never empties the vocabulary
            log.warning("vocabulary: %s terms failed: %s", source, exc)
    if own is not None:
        try:
            own.conn.close()
        except Exception:
            pass
    user_keys = {term.casefold() for term in user}
    vocab = Vocabulary(user=user, auto=[term for term in _dedupe(auto) if term.casefold() not in user_keys], built_at=time.time())
    if not write:
        return vocab
    try:
        write_auto_file(paths.vocabulary_auto_file, vocab.auto)
    except OSError as exc:
        log.warning("could not write %s: %s", paths.vocabulary_auto_file, exc)
    log.info("vocabulary: %d user term(s), %d collected", len(vocab.user), len(vocab.auto))
    return vocab


def write_auto_file(path: Path, terms: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [t("voice.files.vocabulary_auto"), ""]
    body += [f"- {term}" for term in terms]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


class VocabularyCache:
    """The current vocabulary, rebuilt in the background when older than `refresh_s` or when the user's
    file changed. `get()` never blocks on a rebuild after the first one."""

    def __init__(
        self,
        builder: Callable[[], Vocabulary],
        *,
        refresh_s: float = REFRESH_S,
        watch: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._builder = builder
        self.refresh_s = refresh_s
        self.watch = watch
        self.clock = clock
        self._vocab: Vocabulary | None = None
        self._built: float = 0.0
        self._mtime: float | None = None
        self._lock = threading.Lock()
        self._building = False

    def _watch_mtime(self) -> float | None:
        try:
            return self.watch.stat().st_mtime if self.watch is not None else None
        except OSError:
            return None

    def _rebuild(self) -> None:
        try:
            vocab = self._builder()
        except Exception as exc:
            log.warning("vocabulary rebuild failed: %s", exc)
            vocab = self._vocab or Vocabulary()
        with self._lock:
            self._vocab, self._built, self._mtime, self._building = vocab, self.clock(), self._watch_mtime(), False

    def get(self) -> Vocabulary:
        with self._lock:
            vocab = self._vocab
            stale = vocab is None or self.clock() - self._built >= self.refresh_s or self._watch_mtime() != self._mtime
            start = stale and vocab is not None and not self._building
            if start:
                self._building = True
        if vocab is None:
            self._rebuild()
            return self._vocab or Vocabulary()
        if start:
            threading.Thread(target=self._rebuild, name="jarvis-vocabulary", daemon=True).start()
        return vocab

    def invalidate(self) -> None:
        with self._lock:
            self._built = 0.0
