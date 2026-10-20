"""Startup health check: the core checks plus the ones each module contributes."""

from __future__ import annotations

import importlib
import os
import sqlite3
from dataclasses import dataclass

from jarvis.config import Config, Paths
from jarvis.utils import secrets


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = False
    warn: bool = False  # ok, but worth a look (`[warn]` in the doctor; never fails it)


def _writable(path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


def run_checks(config: Config, paths: Paths, *, full: bool = False) -> list[Check]:
    overlay = f" + {paths.local_config_file.name}" if paths.local_config_file.is_file() else ""
    checks: list[Check] = [Check("config.toml", True, f"valid ({paths.config_file}{overlay})")]

    key = secrets.get_secret(secrets.ANTHROPIC_API_KEY)
    checks.append(
        Check(
            "Anthropic API key",
            bool(key),
            "found" if key else "missing: run `jarvis key set anthropic_api_key` or set ANTHROPIC_API_KEY",
            fatal=True,
        )
    )

    for label, path in (("data/ writable", paths.data_dir), ("logs/ writable", paths.logs_dir)):
        ok = _writable(path)
        checks.append(Check(label, ok, str(path) if ok else f"cannot write to {path}"))

    try:
        conn = sqlite3.connect(str(paths.db_file))
        conn.execute("SELECT 1")
        conn.close()
        checks.append(Check("SQLite database", True, str(paths.db_file)))
    except sqlite3.Error as exc:
        checks.append(Check("SQLite database", False, str(exc)))

    checks.append(_budget_check(config, paths))
    has_profile = paths.profile_file.is_file()
    # A missing profile is a hint, not a failure (prompt to run setup, but still work).
    checks.append(Check("profile.md", True, str(paths.profile_file) if has_profile else "missing (optional): run `jarvis setup`"))

    if full:
        checks.append(setup_files_check(paths))
        checks.extend(integration_checks(config, paths))
    return checks


SETUP_FILES = ("memory.md", "vocabulary.md", "corrections.md")


def setup_files_check(paths: Paths) -> Check:
    """The files `jarvis setup` creates next to profile.md; each is optional for a run, but a fresh install
    should have them all."""
    missing = [f"data/{name}" for name in SETUP_FILES if not (paths.data_dir / name).is_file()]
    if not missing:
        return Check("setup files", True, ", ".join(f"data/{name}" for name in SETUP_FILES))
    return Check("setup files", True, f"missing: {', '.join(missing)}: `jarvis setup` creates them", warn=True)


# Each integration module exposes `health_checks(config, paths) -> list[Check]` (never raising,
# never prompting). They are only run by `jarvis doctor`, not at startup.
INTEGRATION_MODULES = (
    "jarvis.utils.google_auth",
    "jarvis.tools.apple_calendar",
    "jarvis.tools.calendar_merged",  # [[calendar.filters]]: active filters and what they hide this week
    "jarvis.tools.apple_reminders",
    "jarvis.tools.spotify",
    "jarvis.tools.screen",
    "jarvis.tools.training",
    "jarvis.tools.timers",
    "jarvis.tools.power",  # Mac power: pmset without sudo, the lock call, a scheduled sleep
    "jarvis.tools.alarms",  # alarms: the sound file, caffeinate, the Reminders permission for the phone backup, pending alarms
    "jarvis.tools.football",
    "jarvis.tools.google_sheets",
    "jarvis.tools.maps",
    "jarvis.tools.shortcuts",
    "jarvis.tools.network",  # networkQuality, Local Network / Location Services, periodic check, history
    "jarvis.tools.presence",  # home network, people, last scan, remote access
    "jarvis.location.service",  # Current Location: the Shortcut, CoreLocationCLI, the stored location's age
    "jarvis.interfaces.telegram_bot",
    "jarvis.voice.health",
    "jarvis.services",  # the global command and the launchd agents' program paths
)


def integration_checks(config: Config, paths: Paths) -> list[Check]:
    checks: list[Check] = []
    for module_name in INTEGRATION_MODULES:
        label = module_name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_name)
            fn = getattr(module, "health_checks", None)
            if fn is None:
                checks.append(Check(label, False, "module has no health_checks()"))
                continue
            checks.extend(fn(config, paths))
        except Exception as exc:  # a broken integration must not break the doctor
            checks.append(Check(label, False, f"check failed: {type(exc).__name__}: {exc}"))
    return checks


def _budget_check(config: Config, paths: Paths) -> Check:
    try:
        from jarvis.observability.db import Database
        from jarvis.observability.usage import UsageTracker

        db = Database(paths.db_file)
        try:
            spent = UsageTracker(config, db).spent_today()
        finally:
            db.close()
    except Exception as exc:  # a broken db is reported by the SQLite check above
        return Check("today's budget", False, f"could not read spend: {exc}")
    budget = config.budget.daily_budget_usd
    if budget <= 0:
        return Check("today's budget", True, f"${spent:.4f} spent; no daily budget set (checks disabled)")
    # informational: a reached budget is worth a look, never a failure (Jarvis keeps answering)
    return Check(
        "today's budget",
        True,
        f"${spent:.4f} of ${budget:.2f} used" + ("; budget reached" if spent >= budget else ""),
        warn=spent >= budget,
    )


def format_checklist(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        mark = ("warn" if c.warn else "ok  ") if c.ok else "FAIL"
        lines.append(f"[{mark}] {c.name.ljust(width)}  {c.detail}")
    return "\n".join(lines)
