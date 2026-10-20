"""launchd user agent for the Telegram bot: `jarvis telegram install|uninstall|status`.

Same shape as `scheduler/launchd.py`: the agent runs `jarvis telegram run` (the installed command, `~/.local/bin/jarvis`, else `<venv>/bin/jarvis`) with
KeepAlive so the bot restarts after a crash and survives a reboot. `launchctl` is reached only
through `_run_launchctl` (argv, never a shell; tests monkeypatch it) and `status()` never raises.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import services
from jarvis.exceptions import JarvisError
from jarvis.scheduler.launchd import parse_print_output

if TYPE_CHECKING:
    from jarvis.config import Paths

log = logging.getLogger("jarvis.telegram.launchd")

LABEL = services.agent_label("telegram")
STDOUT_LOG = "telegram.out.log"
STDERR_LOG = "telegram.err.log"


class TelegramInstallError(JarvisError):
    """launchctl refused to load or unload the agent."""


@dataclass
class TelegramAgentStatus:
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
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True, timeout=30, check=False)


LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def build_plist(paths: "Paths") -> dict:
    home = Path(paths.home).resolve()
    logs = Path(paths.logs_dir).resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(jarvis_executable()), "telegram", "run"],
        "WorkingDirectory": str(home),
        # PATH: launchd's default has no /opt/homebrew/bin, where ffmpeg (voice) lives.
        "EnvironmentVariables": {"JARVIS_HOME": str(home), "PATH": LAUNCHD_PATH},
        "RunAtLoad": True,
        # Restart after a crash, but not after a clean exit (disabled or unconfigured bot).
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": 10,  # seconds between restarts when the bot keeps crashing (bad token)
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
    exe = jarvis_executable()
    if not exe.is_file():
        raise TelegramInstallError(
            f"cannot find the jarvis executable at {exe}; install the global command with `uv tool install --editable '.[voice]' --python 3.12` in the repository, or run `uv run jarvis telegram install` there"
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
            raise TelegramInstallError(f"launchctl could not load {target}: {detail}")
    log.info("telegram agent installed: %s", target)
    return target


def uninstall() -> bool:
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
    log.info("telegram agent uninstalled (plist %s)", "removed" if existed else "was absent")
    return existed


def status() -> TelegramAgentStatus:
    try:
        target = plist_path()
        if not target.is_file():
            return TelegramAgentStatus(installed=False, running=False, detail=f"not installed ({target} missing)")
        proc = _run_launchctl(["print", f"{_domain()}/{LABEL}"])
        if proc.returncode != 0:
            return TelegramAgentStatus(installed=True, running=False, detail="plist present but the agent is not loaded")
        running, pid = parse_print_output(proc.stdout or "")
        detail = f"running (pid {pid})" if running else "loaded but not running"
        return TelegramAgentStatus(installed=True, running=running, pid=pid, detail=detail)
    except Exception as exc:  # doctor must never fail because of launchctl
        log.warning("telegram agent status check failed: %s", exc)
        return TelegramAgentStatus(installed=False, running=False, detail=f"status unknown: {type(exc).__name__}: {exc}")
