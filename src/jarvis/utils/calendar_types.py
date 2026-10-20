"""Calendar event shape shared by the Google, Apple and merged calendar tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jarvis.utils.timeutil import format_local, local_tz


@dataclass
class CalendarEvent:
    source: str  # "google" or "apple"
    calendar: str  # calendar name
    title: str
    start: datetime  # aware
    end: datetime  # aware
    all_day: bool = False
    location: str = ""
    notes: str = ""
    id: str = ""
    read_only: bool = False
    attendees: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple[datetime, int, str]:
        return (self.start.astimezone(local_tz()), 0 if self.all_day else 1, self.title.lower())

    def one_line(self) -> str:
        when = self.start.astimezone(local_tz()).strftime("%a %Y-%m-%d") + (
            " all day" if self.all_day else f" {format_local(self.start, with_date=False)}-{format_local(self.end, with_date=False)}"
        )
        extra = f" @ {self.location}" if self.location else ""
        ident = f" [id {self.id}]" if self.id else ""
        return f"{when}: {self.title}{extra} ({self.source}: {self.calendar}){ident}"


def format_events(events: list[CalendarEvent], *, empty: str = "No events.") -> str:
    if not events:
        return empty
    return "\n".join(e.one_line() for e in sorted(events, key=CalendarEvent.sort_key))
