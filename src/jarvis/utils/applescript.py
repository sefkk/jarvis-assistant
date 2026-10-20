"""Safe osascript runner: the script source is a constant and values travel as argv.

Scripts must use `on run argv` and read their inputs from `argv`. Never build script source
from strings that come from the model or from tool output.
"""

from __future__ import annotations

import subprocess
from typing import Sequence

from jarvis.exceptions import PermissionMissingError, ToolExecutionError
from jarvis.security.guards import check_applescript_args

# osascript error numbers that mean the Automation permission is missing or was refused.
_PERMISSION_MARKERS = ("-1743", "Not authorized to send Apple events", "-10004")


def run_applescript(script: str, args: Sequence[str] = (), *, timeout: float = 15.0) -> str:
    """Run `script` with `args` available as `argv`. Returns stdout with the trailing newline removed."""
    if "on run" not in script:
        raise ValueError("AppleScript must define `on run argv` and read inputs from argv")
    argv = ["osascript", "-e", script, "--", *check_applescript_args(args)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ToolExecutionError("osascript is not available on this system") from None
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"AppleScript timed out after {timeout:.0f}s") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        if any(marker in detail for marker in _PERMISSION_MARKERS):
            raise PermissionMissingError(
                "macOS refused the Apple event. Allow Jarvis's terminal under "
                "System Settings > Privacy & Security > Automation, then try again. "
                f"({detail})"
            )
        raise ToolExecutionError(f"AppleScript failed: {detail}")
    return proc.stdout.rstrip("\n")
