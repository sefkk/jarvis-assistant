"""User-facing text in English and Turkish.

Every string a user reads or hears (CLI lines, notifications, Telegram messages, spoken sentences, dialog
buttons, the setup wizard) lives in `src/jarvis/locales/<language>/<area>.toml`; code asks for it by key:

    t("telegram.bot.stopped")                # in the main language (`general.language`)
    t("cli.greeting.morning", "en", name="")  # in a given language, with {placeholders} filled

A key is `<file stem>.<table>.<key>`. Values are strings or lists of strings (word lists). English is the
reference: a key missing in another language falls back to English, and tests/unit/test_i18n.py fails when
the two catalogs differ in keys, value types or placeholders. Word lists that parse what the user says
(stop words, confirmations) usually accept every language at once: `t_all` joins them.

Log lines, tool results for the model and the dashboard stay English and do not come from here.
"""

from __future__ import annotations

import string
import tomllib
from functools import lru_cache
from pathlib import Path

LANGUAGES: tuple[str, ...] = ("en", "tr")
DEFAULT_LANGUAGE = "en"
LANGUAGE_NAMES = {"en": "English", "tr": "Türkçe"}
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_current = DEFAULT_LANGUAGE


def set_language(language: str) -> None:
    """Set the main language (from `general.language`, at startup)."""
    global _current
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}; choose one of {', '.join(LANGUAGES)}")
    _current = language


def current_language() -> str:
    return _current


def resolve(language: str | None) -> str:
    """`language` when it is supported, else the main language."""
    return language if language in LANGUAGES else _current


def _flatten(prefix: str, table: dict, out: dict) -> None:
    for key, value in table.items():
        name = f"{prefix}.{key}"
        if isinstance(value, dict):
            _flatten(name, value, out)
        elif isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            out[name] = value
        else:
            raise ValueError(f"locale key {name}: only strings and lists of strings are allowed")


@lru_cache(maxsize=None)
def catalog(language: str) -> dict[str, str | list[str]]:
    """Every key of one language, flattened ("telegram.stopped" -> text)."""
    folder = LOCALES_DIR / language
    entries: dict[str, str | list[str]] = {}
    for path in sorted(folder.glob("*.toml")):
        with path.open("rb") as fh:
            _flatten(path.stem, tomllib.load(fh), entries)
    return entries


def _lookup(key: str, language: str | None) -> str | list[str]:
    lang = resolve(language)
    value = catalog(lang).get(key)
    if value is None and lang != DEFAULT_LANGUAGE:
        value = catalog(DEFAULT_LANGUAGE).get(key)
    if value is None:
        raise KeyError(f"no locale text for {key!r}")
    return value


def t(key: str, language: str | None = None, /, **values: object) -> str:
    """The text for `key` in `language` (default: the main language), `{placeholders}` filled from `values`."""
    value = _lookup(key, language)
    if not isinstance(value, str):
        raise TypeError(f"locale key {key!r} is a list; use t_list")
    return value.format(**values) if values else value


def t_list(key: str, language: str | None = None) -> list[str]:
    """A word list in one language."""
    value = _lookup(key, language)
    if not isinstance(value, list):
        raise TypeError(f"locale key {key!r} is a string; use t")
    return list(value)


def t_all(key: str) -> list[str]:
    """A word list in every language, the main language first and without duplicates: for parsers that
    must understand the user whatever language they speak right now."""
    words: list[str] = []
    for lang in (_current, *LANGUAGES):
        value = catalog(lang).get(key)
        for word in [value] if isinstance(value, str) else (value or []):
            if word not in words:
                words.append(word)
    if not words:
        raise KeyError(f"no locale text for {key!r}")
    return words


def placeholders(text: str) -> set[str]:
    """The `{name}` fields of a text (for the catalog test)."""
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}
