"""Writing the setup helper's answers into `config.local.toml`: the router's MAC address and
the household's phones are personal data, so they live in the gitignored overlay next to config.toml
(merged over it by `load_config`), never in the tracked file. The file is edited in place, keeping
comments and any other local settings:

- the `home_gateway_macs = [...]` line inside `[presence]` is replaced (trailing comment kept), or the
  table is created;
- every real (uncommented) `[[presence.people]]` block is removed and fresh blocks are appended at
  the end of the file, one per person. Commented example blocks survive because only key = value
  lines directly under a real block header are removed.

A `config.local.toml.bak` is written first when the file exists; afterwards the whole config is
reloaded with `load_config` to prove it still parses, and the overlay is restored (or removed) if not.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from jarvis.config import PresencePerson, load_config, local_config_path
from jarvis.exceptions import ConfigError

_GATEWAY_LINE_RE = re.compile(r"^\s*home_gateway_macs\s*=")
_KEY_LINE_RE = re.compile(r"^\s*[A-Za-z0-9_\-]+\s*=")
PEOPLE_HEADER = "[[presence.people]]"


def _toml_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(_toml_string(i) for i in items) + "]"


def people_blocks(people: list[PresencePerson]) -> str:
    blocks = []
    for person in people:
        blocks.append(
            f"{PEOPLE_HEADER}\nname = {_toml_string(person.name)}\ndevices = {_toml_list(person.devices)}\n"
            f"is_me = {'true' if person.is_me else 'false'}\ntrack_history = {'true' if person.track_history else 'false'}\n"
        )
    return "\n".join(blocks)


def _replace_gateway_line(lines: list[str], macs: list[str]) -> list[str]:
    value = f"home_gateway_macs = {_toml_list(macs)}"
    in_presence = False
    header_index = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("#"):
            in_presence = stripped == "[presence]"
            if in_presence:
                header_index = i
            continue
        if in_presence and _GATEWAY_LINE_RE.match(line):
            comment = ""
            rest = line.split("=", 1)[1]
            if "#" in rest:
                comment = "  #" + rest.split("#", 1)[1].rstrip("\n")
            lines[i] = f"{value}{comment}\n"
            return lines
    if header_index is not None:
        lines.insert(header_index + 1, f"{value}\n")
        return lines
    lines.append(f"\n[presence]\n{value}\n")
    return lines


def _strip_people_blocks(lines: list[str]) -> list[str]:
    """Drop every real `[[presence.people]]` header with the key lines (and blank lines) under it."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == PEOPLE_HEADER:
            skipping = True
            continue
        if skipping and (_KEY_LINE_RE.match(line) or not stripped or stripped.startswith("#")):
            continue  # keys, blank lines and comments inside the block go with it
        skipping = False
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


LOCAL_HEADER = """\
# Personal settings for this Mac, merged over config.toml at start (tables merge key by key; values,
# arrays and [[presence.people]] blocks given here replace the ones in config.toml). Ignored by git:
# anything that identifies you or your household belongs here, not in config.toml.
# `jarvis presence setup` writes the home network and the people below.
"""


def write_presence_config(config_file: Path, home_gateway_macs: list[str], people: list[PresencePerson]) -> Path | None:
    """Rewrite the presence settings in `config.local.toml` next to `config_file`. Returns the backup
    path, or None when the overlay did not exist yet."""
    local = local_config_path(config_file)
    backup: Path | None = None
    if local.is_file():
        backup = local.with_name(local.name + ".bak")
        shutil.copy2(local, backup)
        lines = local.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = LOCAL_HEADER.splitlines(keepends=True)
    lines = _replace_gateway_line(lines, home_gateway_macs)
    lines = _strip_people_blocks(lines)
    text = "".join(lines).rstrip("\n") + "\n"
    if people:
        text += "\n" + people_blocks(people)
    local.write_text(text, encoding="utf-8")
    try:
        load_config(config_file)
    except Exception as exc:
        if backup is not None:
            shutil.copy2(backup, local)
            raise ConfigError(f"the new {local.name} did not parse ({exc}); restored from {backup}") from None
        local.unlink(missing_ok=True)
        raise ConfigError(f"the new {local.name} did not parse ({exc}); removed it again") from None
    return backup
