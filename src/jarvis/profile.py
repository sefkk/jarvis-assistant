"""data/profile.md: written by the user (or `jarvis setup`), read-only for Jarvis, with one
exception for the Current Location: Jarvis's own code (never the model, never a tool) rewrites the
single auto-managed block between `<!-- jarvis:auto current_location START -->` and the matching END
marker, where the Current Location line lives. Everything outside the markers is never touched."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

AUTO_BLOCK_KEY = "current_location"
_AUTO_START = "<!-- jarvis:auto {key} START -->"
_AUTO_END = "<!-- jarvis:auto {key} END -->"
PREFERENCES_HEADINGS = ("preferences", "tercihler")  # the English and the Turkish template
PREFERENCES_HEADING_RE = re.compile(r"^## (?:preferences|tercihler)\s*$", re.IGNORECASE | re.MULTILINE)

FIELDS: list[tuple[str, str, str]] = [
    # key, label in profile.md (English template), description
    ("name", "Name", "What is your full name"),
    ("address_as", "Address me as", "How should Jarvis address you"),
    ("job", "Job", "What do you do"),
    ("location", "Location", "Where do you live (city, country)"),
    ("languages", "Languages", "Languages you use with Jarvis (e.g. Turkish, English)"),
    ("weather_location", "Weather location", "Default location for weather"),
]

# The labels of the Turkish template (templates/profile.example.tr.md) for the same fields.
LABEL_ALIASES: dict[str, str] = {
    "ad": "name",
    "hitap": "address_as",
    "meslek": "job",
    "konum": "location",
    "diller": "languages",
    "hava durumu konumu": "weather_location",
}

_FIELD_RE = re.compile(r"^- ([^:]+):\s*(.*)$")


@dataclass
class Profile:
    fields: dict[str, str] = field(default_factory=dict)
    preferences: str = ""

    @property
    def name(self) -> str:
        return self.fields.get("address_as") or self.fields.get("name") or ""

    def to_markdown(self) -> str:
        lines = ["# Profile", ""]
        for key, label, _ in FIELDS:
            lines.append(f"- {label}: {self.fields.get(key, '')}")
        lines += ["", "## Preferences", "", self.preferences.strip(), ""]
        return "\n".join(lines)


def read_profile_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def parse_profile(text: str) -> Profile:
    labels = {label.lower(): key for key, label, _ in FIELDS} | LABEL_ALIASES
    profile = Profile()
    in_preferences = False
    prefs: list[str] = []
    for line in text.splitlines():
        if line.strip().lower().removeprefix("## ").strip() in PREFERENCES_HEADINGS and line.strip().startswith("## "):
            in_preferences = True
            continue
        if in_preferences:
            prefs.append(line)
            continue
        m = _FIELD_RE.match(line.strip())
        if m and m.group(1).strip().lower() in labels:
            profile.fields[labels[m.group(1).strip().lower()]] = m.group(2).strip()
    profile.preferences = "\n".join(prefs).strip()
    return profile


def load_profile(path: Path) -> Profile | None:
    text = read_profile_text(path)
    return parse_profile(text) if text is not None else None


# --- the auto-managed block ---------------------------------------------------------------------------


def auto_block_markers(key: str = AUTO_BLOCK_KEY) -> tuple[str, str]:
    return _AUTO_START.format(key=key), _AUTO_END.format(key=key)


def find_auto_block(text: str, key: str = AUTO_BLOCK_KEY) -> tuple[int, int] | None:
    """(start, end) character offsets of the whole block including both marker lines, or None."""
    start_marker, end_marker = auto_block_markers(key)
    start = text.find(start_marker)
    if start < 0:
        return None
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        return None
    return start, end + len(end_marker)


def auto_block_body(text: str, key: str = AUTO_BLOCK_KEY) -> str | None:
    """The lines between the markers (stripped), or None when there is no block."""
    span = find_auto_block(text, key)
    if span is None:
        return None
    start_marker, end_marker = auto_block_markers(key)
    inner = text[span[0] + len(start_marker) : span[1] - len(end_marker)]
    return inner.strip("\r\n")


def replace_auto_block(text: str, key: str, body: str) -> str:
    """profile.md with ONLY the block for `key` replaced by `body` (one or more lines). A missing block
    is created: before the `## Preferences` heading when there is one, else at the end. Nothing
    outside the markers changes, byte for byte. Idempotent."""
    start_marker, end_marker = auto_block_markers(key)
    block = f"{start_marker}\n{body.strip()}\n{end_marker}"
    span = find_auto_block(text, key)
    if span is not None:
        return text[: span[0]] + block + text[span[1] :]
    heading = PREFERENCES_HEADING_RE.search(text)
    if heading is not None:
        before = text[: heading.start()]
        if before and not before.endswith("\n"):
            before += "\n"
        if not before.endswith("\n\n") and before:
            before += "\n"
        return before + block + "\n\n" + text[heading.start() :]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text and not text.endswith("\n\n") else "") + block + "\n"
