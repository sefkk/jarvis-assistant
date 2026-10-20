"""Config edits that keep comments, order and formatting (tomlkit, never a library that drops
comments). A setting lives in `config.local.toml` when that overlay already defines it (so the edit stays
effective), otherwise in `config.toml`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table

from jarvis.config import LOCAL_CONFIG_NAME, local_config_path


def split_path(path: str) -> tuple[list[str], str]:
    parts = path.split(".")
    return parts[:-1], parts[-1]


def has_key(data: dict | None, path: str) -> bool:
    node: Any = data or {}
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def target_file(config_file: Path, local_data: dict | None, path: str) -> Path:
    """Where an edit to `path` must go: the overlay when it already holds the key, else config.toml."""
    local = local_config_path(config_file)
    if local.is_file() and has_key(local_data, path):
        return local
    return config_file


def set_value(document: tomlkit.TOMLDocument, path: str, value: Any) -> None:
    """`doc["brief"]["schedule"]["weekday_time"] = "08:30"` for "brief.schedule.weekday_time", creating the
    tables that do not exist yet. The inline comment of an existing key survives (tomlkit keeps the trivia)."""
    tables, key = split_path(path)
    node: Any = document
    for name in tables:
        if name not in node or not isinstance(node[name], (Table, dict)):
            node[name] = tomlkit.table()
        node = node[name]
    if (
        isinstance(value, float)
        and value.is_integer()
        and key in node
        and isinstance(node[key].unwrap(), int)
        and not isinstance(node[key].unwrap(), bool)
    ):
        value = int(value)  # keep an integer key an integer when the form sent 3.0
    node[key] = value


def render_with(text: str, changes: dict[str, Any]) -> str:
    """The TOML text with every `path -> value` applied."""
    document = tomlkit.parse(text) if text.strip() else tomlkit.document()
    for path, value in changes.items():
        set_value(document, path, value)
    return tomlkit.dumps(document)


__all__ = ["LOCAL_CONFIG_NAME", "has_key", "render_with", "set_value", "split_path", "target_file"]
