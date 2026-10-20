"""The background services as one group: which `jarvis` binary the launchd agents run,
`jarvis services reinstall|status`, and the agents' lines in `jarvis doctor`.

The three agents (scheduler, Telegram, voice) each keep their own launchd module; this module
decides the executable they all point at. Order: the global command that `uv tool install
--editable .` put into uv's tool bin directory (`~/.local/bin/jarvis`) or that is on PATH, else
`<sys.prefix>/bin/jarvis` (the project venv under `uv run`), else the `jarvis` next to
`sys.executable`. Symlinks are never followed: the venv's python is a symlink into Homebrew's
Python framework, where there is no `jarvis`. The plist always holds an absolute path, so an
agent keeps working from any directory and after a reboot.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from jarvis.exceptions import JarvisError
from jarvis.utils.timeutil import now_iso

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.services")

# Every agent's launchd label starts with this prefix; JARVIS_LAUNCHD_PREFIX overrides it so two
# installs (say a stable one and a development checkout) can run side by side.
LABEL_PREFIX = os.environ.get("JARVIS_LAUNCHD_PREFIX", "com.jarvis-assistant")


def agent_label(name: str) -> str:
    """The launchd label of one agent (`scheduler`, `telegram` or `voice`)."""
    return f"{LABEL_PREFIX}.{name}"


@dataclass(frozen=True)
class Agent:
    name: str  # scheduler | telegram | voice
    module: str  # the launchd module with install(paths) / uninstall() / status() / plist_path()
    command: tuple[str, ...]  # the jarvis subcommand the agent runs


AGENTS: tuple[Agent, ...] = (
    Agent("scheduler", "jarvis.scheduler.launchd", ("scheduler", "run")),
    Agent("telegram", "jarvis.interfaces.telegram_launchd", ("telegram", "run")),
    Agent("voice", "jarvis.voice.launchd", ("voice", "run")),
)


# --- which binary -------------------------------------------------------------------------


def tool_bin_dir() -> Path:
    """Where `uv tool install` links executables (`uv tool dir --bin`): $UV_TOOL_BIN_DIR, else
    $XDG_BIN_HOME, else $XDG_DATA_HOME/../bin, else ~/.local/bin."""
    for var in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME"):
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser().parent / "bin"
    return Path.home() / ".local" / "bin"


def _runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def global_executable() -> Path | None:
    """The globally installed `jarvis` command, or None when `uv tool install` has not run yet:
    the one in uv's tool bin directory, else a `jarvis` on PATH that is not this environment's
    own console script (under `uv run` PATH starts with `<venv>/bin`, which is not global)."""
    candidate = tool_bin_dir() / "jarvis"
    if _runnable(candidate):
        return candidate
    found = shutil.which("jarvis")
    if found:
        path = Path(found).absolute()  # absolute, not resolved: keep the path the user installed
        if path not in (venv_executable(), _interpreter_sibling()) and _runnable(path):
            return path
    return None


def venv_executable() -> Path:
    """The `jarvis` console script of the environment running this code (`<venv>/bin/jarvis`).
    `sys.prefix`, not a resolved `sys.executable`: the venv's python is a symlink to the
    Homebrew framework binary, next to which there is no `jarvis`."""
    return Path(sys.prefix) / "bin" / "jarvis"


def _interpreter_sibling() -> Path:
    """`jarvis` in the directory of `sys.executable` as it was invoked (no symlink resolution)."""
    return Path(sys.executable).absolute().parent / "jarvis"


def executable_candidates() -> list[Path]:
    """Where a `jarvis` executable may be, in order of preference, without following symlinks."""
    candidates: list[Path] = []
    for candidate in (global_executable(), venv_executable(), _interpreter_sibling()):
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def jarvis_executable() -> Path:
    """What the launchd agents run: the first candidate that exists (global command, venv script,
    `jarvis` next to the interpreter). When none exists, the venv script, so the installers'
    error names the place where `uv sync` would put it."""
    for candidate in executable_candidates():
        if _runnable(candidate):
            return candidate
    return venv_executable()


def is_global(exe: Path) -> bool:
    return global_executable() is not None and exe == global_executable()


def python_executable_for(exe: Path) -> Path:
    """The interpreter behind a `jarvis` console script (its shebang). Under launchd the microphone
    and Input Monitoring permissions belong to this binary, not to a terminal app."""
    try:
        with Path(exe).resolve().open("rb") as fh:
            first = fh.readline().decode("utf-8", "replace").strip()
    except OSError:
        return Path(sys.executable).resolve()
    if first.startswith("#!"):
        parts = first[2:].split()
        if parts and parts[0] != "/usr/bin/env":
            return Path(parts[0]).resolve()
    return Path(sys.executable).resolve()


def on_path(exe: Path) -> bool:
    """True when typing `jarvis` in a fresh shell runs this executable."""
    found = shutil.which("jarvis")
    if not found:
        return False
    try:
        return Path(found).resolve() == exe.resolve()
    except OSError:
        return False


# --- the agents ----------------------------------------------------------------------------


def _module(agent: Agent):
    return importlib.import_module(agent.module)


def installed_program(agent: Agent) -> Path | None:
    """ProgramArguments[0] of the agent's plist, or None when the agent is not installed."""
    plist = _module(agent).plist_path()
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
        return Path(str(data["ProgramArguments"][0]))
    except (OSError, KeyError, IndexError, ValueError, TypeError, plistlib.InvalidFileException):
        return None


@dataclass
class ServiceReport:
    name: str
    installed: bool
    running: bool
    program: Path | None
    stale: bool  # the plist runs a binary that is gone or is not the current `jarvis` executable
    detail: str


def service_reports() -> list[ServiceReport]:
    """One report per agent; never raises (launchctl problems become the detail text)."""
    current = jarvis_executable()
    reports = []
    for agent in AGENTS:
        try:
            status = _module(agent).status()
            program = installed_program(agent) if status.installed else None
            stale = bool(program is not None and (not program.is_file() or program != current))
            detail = status.detail
            if stale:
                reason = "missing" if not program.is_file() else "not the current command"
                detail += f"; plist runs {program} ({reason}): run `jarvis services reinstall`"
            elif program is not None:
                detail += f"; runs {program}"
            reports.append(ServiceReport(agent.name, status.installed, status.running, program, stale, detail))
        except Exception as exc:  # doctor and status must never fail because of one agent
            log.warning("service status for %s failed: %s", agent.name, exc)
            reports.append(ServiceReport(agent.name, False, False, None, False, f"status unknown: {type(exc).__name__}: {exc}"))
    return reports


def reinstall_all(paths: "Paths", out: Callable[[str], None] | None = None) -> list[tuple[str, Path]]:
    """Rewrite and reload every installed agent so the plists point at the current executable.
    Agents that are not installed are left alone. Returns (name, plist) per rewritten agent."""
    say = out or (lambda text: None)
    exe = jarvis_executable()
    say(
        f"agents will run {exe}"
        + (
            ""
            if is_global(exe)
            else " (not the global command; `uv tool install --editable '.[voice]' --python 3.12` in the repository installs it)"
        )
    )
    done: list[tuple[str, Path]] = []
    for agent in AGENTS:
        module = _module(agent)
        if not module.plist_path().is_file():
            say(f"{agent.name}: not installed, skipped")
            continue
        target = module.install(paths)
        done.append((agent.name, target))
        say(f"{agent.name}: reinstalled {target}")
    if not done:
        say("no agents installed; use `jarvis scheduler install`, `jarvis telegram install` or `jarvis voice install`")
    return done


def format_status(reports: list[ServiceReport], exe: Path | None = None) -> str:
    exe = exe or jarvis_executable()
    lines = [f"jarvis command: {exe}" + ("" if is_global(exe) else " (project venv; the global command is not installed)")]
    width = max(len(r.name) for r in reports)
    for r in reports:
        state = "running" if r.running else ("installed, not running" if r.installed else "not installed")
        lines.append(f"{r.name.ljust(width)}  {state}  {r.detail}")
    return "\n".join(lines)


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """For `jarvis doctor`: the global command and, per installed agent, whether its plist still
    runs the current binary. Never raises, no network."""
    from jarvis.observability.health import Check

    checks: list[Check] = []
    exe = global_executable()
    if exe is None:
        # optional: a pip or `uv sync` install works through its venv script, which the agents then run
        checks.append(
            Check(
                "jarvis command",
                True,
                f"global command not installed, using {jarvis_executable()}; for `jarvis` in every terminal run "
                "`uv tool install --editable '.[voice]' --python 3.12` in the repository",
                warn=True,
            )
        )
    elif not on_path(exe):
        checks.append(Check("jarvis command", False, f"{exe} is not on PATH: run `uv tool update-shell` and open a new terminal"))
    else:
        checks.append(Check("jarvis command", True, str(exe)))
    for r in service_reports():
        if not r.installed:
            checks.append(Check(f"launchd: {r.name}", True, "not installed"))
        else:
            checks.append(Check(f"launchd: {r.name}", not r.stale, r.detail))
    return checks


# --- for the dashboard: restart one agent, uptime, which config an agent runs -------------


def agent_named(name: str) -> Agent:
    for agent in AGENTS:
        if agent.name == name:
            return agent
    raise JarvisError(f"unknown service {name!r}; the services are {', '.join(a.name for a in AGENTS)}")


def restart_agent(name: str, paths: "Paths") -> Path:
    """Restart one installed agent the way `jarvis services reinstall` does it: rewrite its plist and
    reload it (bootout + bootstrap), so the agent runs the current code and config. Raises JarvisError
    when the agent is not installed. Returns the plist path."""
    agent = agent_named(name)
    module = _module(agent)
    if not module.plist_path().is_file():
        raise JarvisError(f"the {name} agent is not installed; run `jarvis {name} install` first")
    target = module.install(paths)
    log.info("%s agent restarted (%s)", name, target)
    return target


def process_started_at(pid: int) -> datetime | None:
    """When the process started, from `ps -o lstart=` (local time); None when it is gone or unreadable."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not text:
        return None
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %Y").astimezone()
    except ValueError:
        return None


def config_version(paths: "Paths") -> str:
    """A short fingerprint of config.toml plus config.local.toml as they are on disk now. Two equal versions
    mean the same settings; it is what the dashboard compares against the version each agent loaded."""
    digest = hashlib.sha256()
    for path in (paths.config_file, paths.local_config_file):
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
        digest.update(b"\0")
    return digest.hexdigest()[:8]


KV_START_PREFIX = "service.start."


def record_start(db, name: str, paths: "Paths") -> dict:
    """Written by an agent when it starts (the scheduler also after every config reload): when, with which
    config version and pid. The dashboard shows it next to the agent; it never fails the agent."""
    record = {"started_at": now_iso(), "config_version": config_version(paths), "pid": os.getpid()}
    try:
        db.kv_set(KV_START_PREFIX + name, json.dumps(record))
    except Exception as exc:  # pragma: no cover - a locked or missing table must not stop the agent
        log.warning("could not record the %s start: %s", name, exc)
    return record


def recorded_start(conn, name: str) -> dict | None:
    """The last `record_start` row for an agent, read through any sqlite3 connection with a `kv` table."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_START_PREFIX + name,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
