"""The three ways to learn where the Mac is, in order of preference:

1. `shortcut`: the Shortcut named in `location.shortcut_name` ("Jarvis Location"), which uses the
   Shortcuts action "Get Current Location" and outputs latitude, longitude and the address as
   text. It runs through the existing Shortcuts bridge (`shortcuts run <name> --output-path`,
   argv only) and only when the name is in the `[[shortcuts.allowed]]` allowlist.
2. `corelocation`: `CoreLocationCLI -once -json` (`brew install --cask corelocationcli`) when it
   is installed.
3. `ip`: IP geolocation (`location.ip_lookup_url`, ipinfo.io by default). City level at best and
   wrong behind a VPN; the result is marked approximate.

Each source is a function `(config, *, timeout, now_iso) -> Location | None` that raises
`SourceError` with a short reason (never containing coordinates) when it cannot answer. Tests
replace `run_shortcut_command`, `run_corelocation_command` and `fetch_ip_json`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from jarvis.location.model import MAX_INPUT_CHARS, fields_from_mapping, location_from_fields, parse_text_output

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.location.model import Location

log = logging.getLogger("jarvis.location")

CORELOCATION_CLI = "CoreLocationCLI"
SOURCE_ORDER: tuple[str, ...] = ("shortcut", "corelocation", "ip")
STDERR_MAX_CHARS = 200


class SourceError(Exception):
    """The source could not answer; the message is short and safe to log (no coordinates)."""


# --- 1. the Shortcut --------------------------------------------------------------------------------------


def run_shortcut_command(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """The Shortcuts bridge (tools/shortcuts.py `run_command`): argv only, never a shell."""
    from jarvis.tools.shortcuts import run_command  # noqa: PLC0415

    return run_command(argv, timeout=timeout)


def shortcut_source(config: "Config", *, timeout: float, now_iso: str) -> "Location | None":
    from jarvis.exceptions import ToolExecutionError  # noqa: PLC0415
    from jarvis.tools.shortcuts import SHORTCUTS_CLI  # noqa: PLC0415

    name = config.location.shortcut_name.strip()
    if not name:
        raise SourceError("no shortcut name configured (location.shortcut_name)")
    entry = config.shortcuts.entry(name)
    if entry is None:
        raise SourceError(f'the Shortcut {name!r} is not in [[shortcuts.allowed]] (add it with risk = "safe", remote = false)')
    if shutil.which(SHORTCUTS_CLI) is None:
        raise SourceError("the `shortcuts` command-line tool is not available")
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-location-"))
    try:
        output_path = workdir / "location.txt"
        argv = [SHORTCUTS_CLI, "run", entry.name, "--output-path", str(output_path)]
        try:
            proc = run_shortcut_command(argv, timeout=timeout)
        except ToolExecutionError as exc:
            raise SourceError(str(exc)[:STDERR_MAX_CHARS]) from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:STDERR_MAX_CHARS] or f"exit code {proc.returncode}"
            raise SourceError(f"the Shortcut {entry.name!r} failed: {detail}")
        try:
            text = output_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = proc.stdout or ""
        except OSError as exc:
            raise SourceError(f"cannot read the Shortcut output: {type(exc).__name__}") from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if len(text) > MAX_INPUT_CHARS:
        raise SourceError("the Shortcut output is too long to be a location")
    location = location_from_fields(parse_text_output(text), source="shortcut", updated_at=now_iso)
    if location is None:
        raise SourceError(
            f"the Shortcut {entry.name!r} returned no usable coordinates (expected lines like `lat: 41.01` and `lon: 28.95`"
            " plus street/city/region/country; see docs/integrations/shortcuts.md)"
        )
    return location


# --- 2. CoreLocationCLI -------------------------------------------------------------------------------------


def run_corelocation_command(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)


def corelocation_installed() -> bool:
    return shutil.which(CORELOCATION_CLI) is not None


def corelocation_source(config: "Config", *, timeout: float, now_iso: str) -> "Location | None":
    if not corelocation_installed():
        raise SourceError("CoreLocationCLI is not installed (optional: brew install --cask corelocationcli)")
    argv = [CORELOCATION_CLI, "-once", "-json"]
    try:
        proc = run_corelocation_command(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SourceError(f"CoreLocationCLI timed out after {timeout:.0f}s") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceError(f"CoreLocationCLI could not run: {type(exc).__name__}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:STDERR_MAX_CHARS] or f"exit code {proc.returncode}"
        raise SourceError(f"CoreLocationCLI failed (is Location Services granted?): {detail}")
    text = (proc.stdout or "").strip()[:MAX_INPUT_CHARS]
    fields: dict[str, Any]
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    fields = fields_from_mapping(data) if isinstance(data, dict) else parse_text_output(text)
    location = location_from_fields(fields, source="corelocation", updated_at=now_iso)
    if location is None:
        raise SourceError("CoreLocationCLI returned no usable coordinates")
    return location


# --- 3. IP geolocation ----------------------------------------------------------------------------------------


def fetch_ip_json(url: str, *, timeout: float) -> Any:
    """GET `url` and return the parsed JSON (tests replace this). Only https URLs are accepted."""
    import httpx  # noqa: PLC0415 - not needed at import time

    if not url.lower().startswith("https://"):
        raise SourceError("location.ip_lookup_url must be an https URL")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(url, headers={"Accept": "application/json", "User-Agent": "jarvis-location/1"})
    except httpx.HTTPError as exc:
        raise SourceError(f"IP lookup failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise SourceError(f"IP lookup failed with status {response.status_code}")
    if len(response.content) > MAX_INPUT_CHARS:
        raise SourceError("IP lookup answer too long")
    try:
        return response.json()
    except ValueError:
        raise SourceError("IP lookup answer was not JSON") from None


def ip_source(config: "Config", *, timeout: float, now_iso: str) -> "Location | None":
    if not config.location.ip_fallback:
        raise SourceError("IP geolocation is disabled (location.ip_fallback = false)")
    data = fetch_ip_json(config.location.ip_lookup_url, timeout=timeout)
    if not isinstance(data, dict):
        raise SourceError("IP lookup answer was not an object")
    fields = fields_from_mapping(data)
    fields.pop("street", None)  # city level only, whatever the service claims
    fields.pop("neighborhood", None)
    location = location_from_fields(fields, source="ip", updated_at=now_iso, approximate=True)
    if location is None:
        raise SourceError("IP lookup returned no usable coordinates")
    return location


SOURCES: dict[str, Callable[..., "Location | None"]] = {
    "shortcut": shortcut_source,
    "corelocation": corelocation_source,
    "ip": ip_source,
}
