"""Per-calendar event filters (`[[calendar.filters]]` in config.toml).

The user's football calendar carries every match of the league; only the configured team's matches
matter. A filter with `mode = "include_only"` keeps, in that one calendar, the events whose title
matches one of its keywords and hides the rest from `agenda`, `daily_brief`, the two list tools and
the match-day detection. Other calendars are never touched. The model gets the hidden events back
with `include_filtered = true`, meant for "is there a Galatasaray match this week" questions.

Matching is case-insensitive and Turkish/diacritic-insensitive (ç/c, ş/s, ğ/g, ı/i, ö/o, ü/u).
A keyword of up to three characters ("FB") matches only as a whole word; a longer one ("Fener")
matches anywhere in the title ("Fenerbahçe"). With `only_matches = true` (the default) the filter
applies only to titles shaped like a fixture ("Galatasaray - Fenerbahçe", "FB vs Beşiktaş"); a
flight or a note that happens to sit in that calendar stays visible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jarvis.utils.calendar_types import CalendarEvent
from jarvis.utils.text import fold

if TYPE_CHECKING:
    from jarvis.config import CalendarFilter, Config

WHOLE_WORD_MAX_LEN = 3  # keywords this short ("FB", "GS") match only as a whole word
# "A - B", "A – B", "A vs B", "A vs. B", "A v B": the title names two sides
_FIXTURE_RE = re.compile(r"\S\s+(?:-|–|—|vs\.?|v\.?)\s+\S", re.IGNORECASE)


def fold_text(text: str) -> str:
    """Accent-, case- and Turkish-dotless-i-insensitive key: 'Kasımpaşa' -> 'kasimpasa'."""
    text = fold(text or "")
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def keyword_in(title: str, keyword: str) -> bool:
    """Does the title contain the keyword? Short keywords only as a whole word (see module doc)."""
    key = fold_text(keyword)
    if not key:
        return False
    text = fold_text(title)
    if len(key) <= WHOLE_WORD_MAX_LEN:
        return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text) is not None
    return key in text


def looks_like_fixture(title: str) -> bool:
    """'Galatasaray - Fenerbahçe', 'FB vs Beşiktaş' yes; 'Flight: TK 0001 from IST to AMS' no."""
    return _FIXTURE_RE.search(title or "") is not None


def filter_for(config: "Config", calendar_name: str) -> "CalendarFilter | None":
    """The configured filter for a calendar name (compared folded), if any."""
    wanted = fold_text(calendar_name)
    for flt in config.calendar.filters:
        if fold_text(flt.calendar) == wanted:
            return flt
    return None


def event_passes(flt: "CalendarFilter", event: CalendarEvent) -> bool:
    if flt.only_matches and not looks_like_fixture(event.title):
        return True
    return any(keyword_in(event.title, kw) for kw in flt.keywords)


@dataclass
class FilterOutcome:
    kept: list[CalendarEvent] = field(default_factory=list)
    hidden: list[CalendarEvent] = field(default_factory=list)

    @property
    def hidden_count(self) -> int:
        return len(self.hidden)

    def note(self) -> str:
        """One line the tools append when something was hidden, so the model knows it can ask for it."""
        if not self.hidden:
            return ""
        calendars = sorted({e.calendar for e in self.hidden})
        return (
            f"({self.hidden_count} event(s) in {', '.join(calendars)} hidden by the calendar filter; "
            f"pass include_filtered=true only if the user asks about them.)"
        )


def apply_filters(config: "Config", events: list[CalendarEvent]) -> FilterOutcome:
    """Split events into kept and hidden according to `config.calendar.filters`. Events of
    calendars without a filter are always kept; order is preserved."""
    outcome = FilterOutcome()
    if not config.calendar.filters:
        outcome.kept = list(events)
        return outcome
    for event in events:
        flt = filter_for(config, event.calendar)
        if flt is None or event_passes(flt, event):
            outcome.kept.append(event)
        else:
            outcome.hidden.append(event)
    return outcome


def describe(flt: "CalendarFilter") -> str:
    """'Futbol: include_only [Fenerbahçe, Fenerbahce, Fener, FB] (fixture-shaped titles only)'."""
    scope = " (fixture-shaped titles only)" if flt.only_matches else ""
    return f"{flt.calendar}: {flt.mode} [{', '.join(flt.keywords)}]{scope}"
