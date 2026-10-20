"""launchd user agent for the voice service: `jarvis voice install|uninstall|status`.

Same shape as the scheduler and Telegram agents: `jarvis voice run` (the installed command, `~/.local/bin/jarvis`, else `<venv>/bin/jarvis`) with KeepAlive, so
the service starts at login and restarts after a crash. Under launchd the microphone and Input
Monitoring permissions belong to the Python executable of the venv, not to a terminal app
(docs/integrations/voice.md). `launchctl` is reached only through `_run_launchctl` (argv) and
`status()` never raises.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import services
from jarvis.exceptions import JarvisError
from jarvis.scheduler.launchd import parse_print_output

if TYPE_CHECKING:
    from jarvis.config import Paths

log = logging.getLogger("jarvis.voice.launchd")

LABEL = services.agent_label("voice")
STDOUT_LOG = "voice.out.log"
STDERR_LOG = "voice.err.log"
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOAD_ATTEMPTS = 4  # bootstrap retries: launchd answers "Input/output error" while it still tears down the old instance
LOAD_RETRY_S = 1.5
UNLOAD_WAIT_S = 15.0  # after `bootout`: how long to wait for launchd to drop the old instance before bootstrapping
UNLOAD_POLL_S = 0.25


class VoiceInstallError(JarvisError):
    """launchctl refused to load or unload the agent."""


@dataclass
class VoiceAgentStatus:
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


def python_executable() -> Path:
    """The interpreter that will hold the microphone and Input Monitoring permissions under launchd:
    the one behind the `jarvis` binary the agent runs (uv's tool environment, or the venv)."""
    return services.python_executable_for(jarvis_executable())


def _uid() -> int:
    return os.getuid()


def _domain() -> str:
    return f"gui/{_uid()}"


def _run_launchctl(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True, timeout=30, check=False)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def is_loaded() -> bool:
    """launchd knows the label in the user domain (loaded, running or not)."""
    return _run_launchctl(["print", f"{_domain()}/{LABEL}"]).returncode == 0


def _wait_until_unloaded() -> None:
    """`bootout` returns before the old instance has exited, and launchd drops the label only then.

    A `bootstrap` in that gap either fails with "Bootstrap failed: 5: Input/output error" or
    succeeds and is wiped together with the old instance a moment later: on 2026-10-16 00:09
    `jarvis voice install` reported success and no agent was left. So wait for the label to go.
    """
    for _ in range(int(UNLOAD_WAIT_S / UNLOAD_POLL_S)):
        if not is_loaded():
            return
        _sleep(UNLOAD_POLL_S)
    log.warning("the previous voice agent is still loaded after %.0f s; bootstrapping anyway", UNLOAD_WAIT_S)


def _load(target: Path) -> None:
    """Bootstrap the plist and make sure launchd really took it (`print` after every attempt).

    The old `load -w` fallback can return 0 without loading anything (2026-10-15 23:58: the voice
    agent vanished after a restart and nobody noticed until "Hey Jarvis" stayed silent), so
    nothing here trusts an exit code: bootstrap, verify, wait and retry; `load -w` is the last
    resort and is verified the same way.
    """
    detail = ""
    for attempt in range(1, LOAD_ATTEMPTS + 1):
        proc = _run_launchctl(["bootstrap", _domain(), str(target)])
        if is_loaded():
            if attempt > 1:
                log.info("launchctl bootstrap succeeded on attempt %d", attempt)
            return
        if proc.returncode == 0:
            detail = "bootstrap returned 0 but the agent is not loaded (wiped with the previous instance?)"
        else:
            detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        log.warning("launchctl bootstrap failed (%s); attempt %d/%d", detail, attempt, LOAD_ATTEMPTS)
        if attempt < LOAD_ATTEMPTS:
            _sleep(LOAD_RETRY_S)
    log.warning("launchctl bootstrap kept failing; falling back to load -w")
    proc = _run_launchctl(["load", "-w", str(target)])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
    elif is_loaded():
        return
    else:
        detail = f"load -w returned 0 but the agent is not loaded (last bootstrap error: {detail})"
    raise VoiceInstallError(
        f"launchctl could not load {target}: {detail}; wait a few seconds and run `jarvis voice install` again "
        f"(`jarvis voice status` shows whether it is running)"
    )


def build_plist(paths: "Paths") -> dict:
    home = Path(paths.home).resolve()
    logs = Path(paths.logs_dir).resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(jarvis_executable()), "voice", "run"],
        "WorkingDirectory": str(home),
        # PATH: launchd's default has no /opt/homebrew/bin (ffmpeg); `say`, `afplay` and
        # `osascript` are in /usr/bin.
        "EnvironmentVariables": {"JARVIS_HOME": str(home), "PATH": LAUNCHD_PATH},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean exit
        # Interactive, not Background: the service plays and records audio and shows dialogs.
        "ProcessType": "Interactive",
        "ThrottleInterval": 10,
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
        raise VoiceInstallError(
            f"cannot find the jarvis executable at {exe}; install the global command with `uv tool install --editable '.[voice]' --python 3.12` in the repository, or run `uv run jarvis voice install` there"
        )
    target = write_plist(paths)
    Path(paths.logs_dir).mkdir(parents=True, exist_ok=True)
    _run_launchctl(["bootout", f"{_domain()}/{LABEL}"])  # ignored: usually "not loaded"
    _wait_until_unloaded()
    _load(target)
    log.info("voice agent installed: %s", target)
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
    log.info("voice agent uninstalled (plist %s)", "removed" if existed else "was absent")
    return existed


def status() -> VoiceAgentStatus:
    try:
        target = plist_path()
        if not target.is_file():
            return VoiceAgentStatus(installed=False, running=False, detail=f"not installed ({target} missing)")
        proc = _run_launchctl(["print", f"{_domain()}/{LABEL}"])
        if proc.returncode != 0:
            return VoiceAgentStatus(installed=True, running=False, detail="plist present but the agent is not loaded")
        running, pid = parse_print_output(proc.stdout or "")
        detail = f"running (pid {pid})" if running else "loaded but not running"
        return VoiceAgentStatus(installed=True, running=running, pid=pid, detail=detail)
    except Exception as exc:  # doctor must never fail because of launchctl
        log.warning("voice agent status check failed: %s", exc)
        return VoiceAgentStatus(installed=False, running=False, detail=f"status unknown: {type(exc).__name__}: {exc}")
