"""System tools: get_time, open_app, quit_app, set_volume.

Screenshots live in tools/screen.py (`capture_screen`), which replaced the old `screenshot` tool."""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field, field_validator

from jarvis.exceptions import ToolExecutionError
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolResult
from jarvis.utils.applescript import run_applescript
from jarvis.utils.timeutil import now_local, now_utc

# AppleScript sources are constants; values arrive through argv.
QUIT_APP_SCRIPT = """on run argv
    set appName to item 1 of argv
    if application appName is running then
        tell application appName to quit
        return "quit " & appName
    end if
    return appName & " was not running"
end run"""

SET_VOLUME_SCRIPT = """on run argv
    set level to (item 1 of argv) as integer
    set volume output volume level
    return "volume set to " & level
end run"""


class GetTime(Tool):
    name = "get_time"
    description = "Get the current local date, time, weekday and timezone on this Mac."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        local = now_local()
        utc = now_utc()
        tz = local.tzname() or ""
        offset = local.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}" if offset else ""
        text = (
            f"Local time: {local.strftime('%Y-%m-%d %H:%M:%S')} ({local.strftime('%A')}), "
            f"timezone {tz} (UTC{offset}). UTC: {utc.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        return ToolResult(text)


def _check_app_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("application name is empty")
    if value.startswith("-"):
        raise ValueError("application name cannot start with '-'")
    if "/" in value or "\x00" in value:
        raise ValueError("application name cannot contain '/'")
    return value


class AppNameInput(BaseModel):
    app: str = Field(description="Application name as shown in /Applications, for example 'Safari' or 'Spotify'.")

    @field_validator("app")
    @classmethod
    def _check_app(cls, value: str) -> str:
        return _check_app_name(value)


class OpenApp(Tool):
    name = "open_app"
    description = "Open (or bring to the front) a macOS application by name."
    input_model = AppNameInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, AppNameInput)
        try:
            proc = subprocess.run(["open", "-a", args.app], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError(f"could not run `open`: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise ToolExecutionError(f"could not open {args.app}: {detail}")
        return ToolResult(f"Opened {args.app}.")


class QuitApp(Tool):
    name = "quit_app"
    description = "Quit a running macOS application by name (asks the app to quit normally)."
    input_model = AppNameInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, AppNameInput)
        return ToolResult(run_applescript(QUIT_APP_SCRIPT, [args.app]) or f"Asked {args.app} to quit.")


class SetVolumeInput(BaseModel):
    level: int = Field(ge=0, le=100, description="Output volume from 0 (mute) to 100.")


class SetVolume(Tool):
    name = "set_volume"
    description = "Set the Mac's output volume (0 to 100)."
    input_model = SetVolumeInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SetVolumeInput)
        return ToolResult(run_applescript(SET_VOLUME_SCRIPT, [str(args.level)]) or f"Volume set to {args.level}.")
