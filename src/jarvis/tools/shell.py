"""run_shell: always confirmed, never shell=True, denylist and timeout enforced."""

from __future__ import annotations

import os
import signal
import subprocess

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError
from jarvis.tools.base import RiskLevel, Tool, ToolResult

OUTPUT_MAX_CHARS = 20_000


class RunShellInput(BaseModel):
    command: str = Field(description="The exact command line to run, e.g. 'ls -la ~/Downloads'.")
    cwd: str | None = Field(default=None, description="Working directory (inside the allowed folders). Default: ~/Documents.")


class RunShell(Tool):
    name = "run_shell"
    description = (
        "Run a shell command on this Mac and return its output. The user must approve the exact command. "
        "Dangerous commands are refused. Pipes and redirects work but are shown to the user as such."
    )
    input_model = RunShellInput
    risk = RiskLevel.CONFIRM
    taints = True
    taint_source = "shell"

    def _prepare(self, args: RunShellInput):
        return self.require_ctx().shell_guard.check(args.command, args.cwd)

    def confirm_preview(self, args: BaseModel) -> str | None:
        assert isinstance(args, RunShellInput)
        return self._prepare(args).preview()

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, RunShellInput)
        ctx = self.require_ctx()
        command = self._prepare(args)
        timeout = ctx.config.security.shell_timeout_seconds
        try:
            # Own session so that a timeout can kill the whole process group (pipelines,
            # background jobs), not just the direct child.
            proc = subprocess.Popen(
                command.argv,
                cwd=str(command.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                env=ctx.shell_guard.child_env(),
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ToolExecutionError(f"command not found: {command.argv[0]}") from None
        except OSError as exc:
            raise ToolExecutionError(f"could not run command: {exc}") from exc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            stdout, stderr = proc.communicate()
            raise ToolExecutionError(
                f"command timed out after {timeout}s and was killed" + (f"; partial output:\n{_cap(stdout)}" if stdout else "")
            ) from None

        parts = [f"exit code: {proc.returncode}"]
        if stdout:
            parts.append("stdout:\n" + _cap(stdout))
        if stderr:
            parts.append("stderr:\n" + _cap(stderr))
        return ToolResult("\n".join(parts), is_error=False)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()


def _cap(text: str) -> str:
    if len(text) > OUTPUT_MAX_CHARS:
        return text[:OUTPUT_MAX_CHARS] + f"\n[truncated: {len(text) - OUTPUT_MAX_CHARS} more characters]"
    return text
