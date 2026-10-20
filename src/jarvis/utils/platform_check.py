"""Which Macs Jarvis supports: macOS 26 or newer on Apple Silicon. `jarvis setup` and `jarvis doctor` stop
with a clear message anywhere else (EventKit full access, the Apple speech helper, `networkQuality`, the
login framework's screen lock and the MLX speech models all assume it).

`JARVIS_ALLOW_UNSUPPORTED_PLATFORM=1` skips the check, for development and CI machines that only run the
tests or the setup in a scratch home."""

from __future__ import annotations

import os
import platform

MIN_MACOS_MAJOR = 26
OVERRIDE_ENV = "JARVIS_ALLOW_UNSUPPORTED_PLATFORM"


def unsupported_reason(system: str | None = None, machine: str | None = None, mac_version: str | None = None) -> str | None:
    """Why this computer is not supported (English, for the bilingual message), or None when it is."""
    if os.environ.get(OVERRIDE_ENV) == "1":
        return None
    system = system if system is not None else platform.system()
    if system != "Darwin":
        return f"Jarvis runs on macOS only (this is {system or 'an unknown system'})."
    machine = machine if machine is not None else platform.machine()
    if machine != "arm64":
        return f"Jarvis needs a Mac with Apple Silicon (M1 or newer); this Mac is {machine or 'unknown'}."
    mac_version = mac_version if mac_version is not None else platform.mac_ver()[0]
    try:
        major = int((mac_version or "0").split(".")[0])
    except ValueError:
        major = 0
    if major < MIN_MACOS_MAJOR:
        return f"Jarvis needs macOS {MIN_MACOS_MAJOR} or newer; this Mac runs macOS {mac_version or 'unknown'}."
    return None


def unsupported_message(reason: str) -> str:
    """The reason in English and Turkish: the language is not chosen yet when setup checks the platform."""
    return (
        f"{reason}\n"
        f"Jarvis yalnızca Apple Silicon işlemcili, macOS {MIN_MACOS_MAJOR} veya daha yeni bir Mac'te çalışır.\n"
        f"(Set {OVERRIDE_ENV}=1 to skip this check on a development machine.)"
    )
