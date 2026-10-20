"""launchd user agent for the scheduler: `jarvis scheduler install|uninstall|status`.

The agent runs `jarvis scheduler run` (the installed command, `~/.local/bin/jarvis`, else `<venv>/bin/jarvis`) with KeepAlive, so timers fire with the CLI
closed and after a reboot. Every `launchctl` call goes through `_run_launchctl`, which tests
monkeypatch; `status()` never raises.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import services
from jarvis.exceptions import JarvisError

if TYPE_CHECKING:
    from jarvis.config import Paths

log = logging.getLogger("jarvis.scheduler.launchd")

LABEL = services.agent_label("scheduler")
STDOUT_LOG = "scheduler.out.log"
STDERR_LOG = "scheduler.err.log"

_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)
_STATE_RE = re.compile(r"^\s*state\s*=\s*(\w+)", re.MULTILINE)


class SchedulerInstallError(JarvisError):
    """launchctl refused to load or unload the agent."""


@dataclass
class SchedulerStatus:
    installed: bool
    running: bool
    pid: int | None = None
    detail: str = ""


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def jarvis_executable() -> Path:
    """The binary the agent runs: the global `jarvis` command when installed, else the venv's
    (see `jarvis.services`). Always an absolute path."""
    return services.jarvis_executable()


def _uid() -> int:
    return os.getuid()


def _domain() -> str:
    return f"gui/{_uid()}"


def _run_launchctl(argv: list[str]) -> subprocess.CompletedProcess:
    """The single seam for launchctl: argv only, never a shell."""
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True, timeout=30, check=False)


def build_plist(paths: "Paths") -> dict:
    home = Path(paths.home).resolve()
    logs = Path(paths.logs_dir).resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(jarvis_executable()), "scheduler", "run"],
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {"JARVIS_HOME": str(home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean exit
        "ProcessType": "Background",
        "StandardOutPath": str(logs / STDOUT_LOG),
        "StandardErrorPath": str(logs / STDERR_LOG),
    }


def write_plist(paths: "Paths", path: Path | None = None) -> Path:
    target = path or plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        plistlib.dump(build_plist(paths), fh, sort_keys=True)
    return target


def install(paths: "Paths") -> Path:
    """Write the plist and load it. A previous copy is unloaded first so a changed plist
    (new venv path, new home) takes effect. Returns the plist path."""
    exe = jarvis_executable()
    if not exe.is_file():
        raise SchedulerInstallError(
            f"cannot find the jarvis executable at {exe}; install the global command with `uv tool install --editable '.[voice]' --python 3.12` in the repository, or run `uv run jarvis scheduler install` there"
        )
    target = write_plist(paths)
    Path(paths.logs_dir).mkdir(parents=True, exist_ok=True)
    _run_launchctl(["bootout", f"{_domain()}/{LABEL}"])  # ignored: usually "not loaded"
    proc = _run_launchctl(["bootstrap", _domain(), str(target)])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        log.warning("launchctl bootstrap failed (%s); falling back to load -w", detail)
        proc = _run_launchctl(["load", "-w", str(target)])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise SchedulerInstallError(f"launchctl could not load {target}: {detail}")
    log.info("scheduler agent installed: %s", target)
    return target


def uninstall() -> bool:
    """Unload the agent and delete the plist. Returns True when a plist existed."""
    target = plist_path()
    proc = _run_launchctl(["bootout", f"{_domain()}/{LABEL}"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        log.info("launchctl bootout returned %s (%s); trying unload", proc.returncode, detail)
        if target.is_file():
            _run_launchctl(["unload", "-w", str(target)])
    existed = target.is_file()
    if existed:
        target.unlink()
    log.info("scheduler agent uninstalled (plist %s)", "removed" if existed else "was absent")
    return existed


def parse_print_output(text: str) -> tuple[bool, int | None]:
    """(running, pid) from `launchctl print gui/<uid>/<label>` output."""
    pid_match = _PID_RE.search(text)
    state_match = _STATE_RE.search(text)
    pid = int(pid_match.group(1)) if pid_match else None
    state = state_match.group(1).lower() if state_match else ""
    running = state == "running" or (not state and pid is not None)
    return running, pid if running else None


def status() -> SchedulerStatus:
    """Installed = the plist exists; running = launchd reports state running. Never raises."""
    try:
        target = plist_path()
        if not target.is_file():
            return SchedulerStatus(installed=False, running=False, detail=f"not installed ({target} missing)")
        proc = _run_launchctl(["print", f"{_domain()}/{LABEL}"])
        if proc.returncode != 0:
            return SchedulerStatus(installed=True, running=False, detail="plist present but the agent is not loaded")
        running, pid = parse_print_output(proc.stdout or "")
        detail = f"running (pid {pid})" if running else "loaded but not running"
        return SchedulerStatus(installed=True, running=running, pid=pid, detail=detail)
    except Exception as exc:  # doctor and the timer tools must never fail because of launchctl
        log.warning("scheduler status check failed: %s", exc)
        return SchedulerStatus(installed=False, running=False, detail=f"status unknown: {type(exc).__name__}: {exc}")
