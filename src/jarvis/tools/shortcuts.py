"""Apple Shortcuts bridge: list_shortcuts, run_shortcut.

Every Shortcut built in the Shortcuts app becomes a Jarvis ability without new code: focus modes,
Notes, Music, HomeKit scenes. The macOS `shortcuts` CLI runs as an argv list, never through a
shell. Only Shortcuts listed under `[[shortcuts.allowed]]` in config.toml can run;
each entry carries its own risk (`safe` runs directly, `confirm` asks first), whether it may run
from Telegram (`remote`) and whether its output is untrusted (`taints`). In a tainted turn every
Shortcut asks for confirmation. Input reaches the Shortcut through a temp file (`--input-path`)
and the output comes back through another (`--output-path`); both are deleted afterwards.

`run_command` is module-level so tests can replace it.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolBlockedError, ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from jarvis.config import Config, Paths, ShortcutEntry
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.shortcuts")

SHORTCUTS_CLI = "shortcuts"
LIST_TIMEOUT_S = 15.0
INPUT_MAX_CHARS = 100_000
STDERR_MAX_CHARS = 500
REMOTE_CHANNEL = "telegram"


# --- the CLI ------------------------------------------------------------------------------------


def run_command(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """The `shortcuts` CLI as an argv list. Tests replace this function."""
    if not argv or argv[0] != SHORTCUTS_CLI:
        raise ValueError("run_command only runs the shortcuts CLI")
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ToolExecutionError("the `shortcuts` command-line tool is not available on this Mac (macOS 12 or newer has it)") from None
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"`{' '.join(argv[:2])}` timed out after {timeout:.0f}s") from None


def installed_shortcuts(timeout: float = LIST_TIMEOUT_S) -> list[str]:
    """The names of every Shortcut in the Shortcuts app (`shortcuts list`), one per line."""
    proc = run_command([SHORTCUTS_CLI, "list"], timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:STDERR_MAX_CHARS] or f"exit code {proc.returncode}"
        raise ToolExecutionError(f"`shortcuts list` failed: {detail}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


# --- shared behaviour ---------------------------------------------------------------------------


class _ShortcutsTool(Tool):
    delegate_ok = False  # Shortcuts act on the Mac; the delegate only reads

    @classmethod
    def available(cls, ctx: ToolContext | None) -> bool:
        """Hidden entirely while the allowlist is empty (nothing could run anyway)."""
        if ctx is None:
            return True
        return bool(ctx.config.shortcuts.allowed)

    def _channel(self) -> str:
        runtime = self.ctx.runtime if self.ctx is not None else None
        return getattr(runtime, "channel", None) or "cli"

    def _is_remote(self) -> bool:
        return self._channel() == REMOTE_CHANNEL

    def _entries(self) -> list["ShortcutEntry"]:
        """The allowlist as seen from the current channel: Telegram sees only `remote` entries."""
        entries = list(self.require_ctx().config.shortcuts.allowed)
        if self._is_remote():
            entries = [e for e in entries if e.remote]
        return entries

    def permitted_entry(self, name: str) -> "ShortcutEntry | None":
        """The allowlist entry for `name` if it exists and may run from the current channel."""
        entry = self.require_ctx().config.shortcuts.entry(name)
        if entry is None:
            return None
        if self._is_remote() and not entry.remote:
            return None
        return entry

    def require_entry(self, name: str) -> "ShortcutEntry":
        """The permitted entry, or ToolBlockedError saying why not."""
        config = self.require_ctx().config
        entry = config.shortcuts.entry(name)
        if entry is None:
            raise ToolBlockedError(
                f"this Shortcut is not in the allowlist: {name!r}. Only Shortcuts listed under "
                f"[[shortcuts.allowed]] in config.toml can run; call list_shortcuts to see them"
            )
        if self._is_remote() and not entry.remote:
            raise ToolBlockedError(
                f"the Shortcut {entry.name!r} is not allowed from Telegram (its allowlist entry has remote = false); "
                f"it can be run at the Mac"
            )
        return entry


# --- list_shortcuts -----------------------------------------------------------------------------


def describe_entries(entries: list["ShortcutEntry"], installed: list[str] | None, *, remote_only: bool = False) -> str:
    """One line per allowed Shortcut with its risk and description; marks the ones that are not in
    the Shortcuts app when `installed` is known (None: the check could not run)."""
    if not entries:
        if remote_only:
            return "No Shortcut is allowed from Telegram (no allowlist entry has remote = true)."
        return "No Shortcuts are allowlisted. Add [[shortcuts.allowed]] entries to config.toml (docs/integrations/shortcuts.md)."
    present = {" ".join(n.casefold().split()) for n in installed} if installed is not None else None
    lines = ["Allowed Shortcuts (use the exact name with run_shortcut):"]
    for entry in entries:
        flags = [entry.risk]
        if entry.remote:
            flags.append("remote")
        if entry.taints:
            flags.append("output is untrusted")
        line = f"- {entry.name} [{', '.join(flags)}]: {entry.description}"
        if present is not None and " ".join(entry.name.casefold().split()) not in present:
            line += " (MISSING: not in the Shortcuts app on this Mac; tell the user)"
        lines.append(line)
    return "\n".join(lines)


class ListShortcuts(_ShortcutsTool):
    name = "list_shortcuts"
    description = (
        "List the Apple Shortcuts Jarvis is allowed to run, with their descriptions and risk. "
        "Call it when unsure which Shortcut matches a request. Never guess Shortcut names."
    )
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        entries = self._entries()
        installed: list[str] | None = None
        note = ""
        if entries:
            try:
                installed = installed_shortcuts()
            except ToolExecutionError as exc:
                note = f"\n(could not check which Shortcuts exist on this Mac: {exc})"
        return ToolResult(describe_entries(entries, installed, remote_only=self._is_remote()) + note)


# --- run_shortcut -------------------------------------------------------------------------------


class RunShortcutInput(BaseModel):
    name: str = Field(description="The Shortcut's exact name from the allowlist (see list_shortcuts).")
    input_text: str | None = Field(
        default=None,
        description="Optional text handed to the Shortcut as its input (for example the note to add).",
    )


class RunShortcut(_ShortcutsTool):
    name = "run_shortcut"
    description = (
        "Run one of the allowed Apple Shortcuts on the Mac (focus modes, Notes, Music, HomeKit scenes). "
        "Only allowlisted Shortcuts run; the result is the Shortcut's text output, or 'done'. "
        "Some Shortcuts ask the user for confirmation first."
    )
    input_model = RunShortcutInput
    risk = RiskLevel.SAFE  # per call: `risk_for` says CONFIRM for entries marked confirm
    taints = False  # per call: `taints_for` follows the entry's `taints` flag
    taint_source = "shortcut"

    # --- security hooks (per allowlist entry) -----------------------------------------------

    def risk_for(self, args: BaseModel) -> RiskLevel:
        entry = self.permitted_entry(getattr(args, "name", ""))
        if entry is not None and entry.risk == "confirm":
            return RiskLevel.CONFIRM
        return RiskLevel.SAFE  # unknown names are blocked in run(); never prompt for them

    def taints_for(self, args: BaseModel) -> bool:
        entry = self.permitted_entry(getattr(args, "name", ""))
        return bool(entry is not None and entry.taints)

    def tainted_confirm_reason(self, args: BaseModel, user_text: str) -> str | None:
        entry = self.permitted_entry(getattr(args, "name", ""))
        if entry is None:
            return None  # blocked anyway
        return f"the Shortcut {entry.name!r} would run after untrusted content entered the turn"

    def confirm_preview(self, args: BaseModel) -> str | None:
        assert isinstance(args, RunShortcutInput)
        entry = self.permitted_entry(args.name)
        if entry is None:
            return None
        lines = [f"Shortcut: {entry.name} ({entry.risk}) - {entry.description}"]
        lines.append("Input: " + (args.input_text if args.input_text else "(none)"))
        return "\n".join(lines)

    # --- running -----------------------------------------------------------------------------

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, RunShortcutInput)
        ctx = self.require_ctx()
        entry = self.require_entry(args.name)
        if args.input_text is not None and len(args.input_text) > INPUT_MAX_CHARS:
            raise ToolValidationError(f"input_text is longer than {INPUT_MAX_CHARS} characters")

        installed = installed_shortcuts()
        if " ".join(entry.name.casefold().split()) not in {" ".join(n.casefold().split()) for n in installed}:
            raise ToolExecutionError(
                f"the Shortcut {entry.name!r} is in the allowlist but not in the Shortcuts app on this Mac "
                f"(it was renamed or deleted; `jarvis shortcuts list` shows the current names)"
            )

        timeout = float(ctx.config.shortcuts.timeout_seconds)
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-shortcut-"))
        try:
            output_path = workdir / "output.txt"
            argv = [SHORTCUTS_CLI, "run", entry.name, "--output-path", str(output_path)]
            if args.input_text is not None:
                input_path = workdir / "input.txt"
                input_path.write_text(args.input_text, encoding="utf-8")
                argv += ["--input-path", str(input_path)]
            log.info(
                "running shortcut", extra={"data": {"name": entry.name, "risk": entry.risk, "input_chars": len(args.input_text or "")}}
            )
            try:
                proc = run_command(argv, timeout=timeout)
            except ToolExecutionError as exc:
                if "timed out" in str(exc):
                    raise ToolExecutionError(
                        f"the Shortcut {entry.name!r} timed out after {timeout:.0f}s (shortcuts.timeout_seconds); "
                        f"it may be waiting for input in the Shortcuts app"
                    ) from None
                raise
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:STDERR_MAX_CHARS] or f"exit code {proc.returncode}"
                raise ToolExecutionError(f"the Shortcut {entry.name!r} failed (exit {proc.returncode}): {detail}")
            output = _read_output(output_path) or (proc.stdout or "").strip()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if not output:
            return ToolResult("done")
        limit = ctx.config.context.tool_result_max_chars
        if len(output) > limit:
            output = output[:limit] + f"\n[Shortcut output truncated to {limit} of {len(output)} characters]"
        return ToolResult(output)


def _read_output(path: Path) -> str:
    """The Shortcut's output file as text (empty when the Shortcut produced nothing)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        log.warning("cannot read the Shortcut output file: %s", exc)
        return ""
    if not data.strip():
        return ""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return f"[the Shortcut returned {len(data)} bytes of non-text output]"


# --- jarvis shortcuts list / jarvis doctor -------------------------------------------------------


def overview_lines(config: "Config") -> list[str]:
    """`jarvis shortcuts list`: every Shortcut on this Mac, the allowlisted ones marked, then the
    allowlisted ones that are missing. Raises ToolExecutionError when the CLI is unusable."""
    installed = installed_shortcuts()
    by_key = {" ".join(e.name.casefold().split()): e for e in config.shortcuts.allowed}
    lines = [t("tools.shortcuts.heading", count=len(installed))]
    seen: set[str] = set()
    for name in installed:
        key = " ".join(name.casefold().split())
        entry = by_key.get(key)
        if entry is None:
            lines.append(f"    {name}")
            continue
        seen.add(key)
        flags = [entry.risk] + (["remote"] if entry.remote else []) + (["taints"] if entry.taints else [])
        lines.append(f"  ✓ {name}  [{', '.join(flags)}]  {entry.description}")
    missing = [e for key, e in by_key.items() if key not in seen]
    if missing:
        lines.append(t("tools.shortcuts.missing"))
        for entry in missing:
            lines.append(f"  ✗ {entry.name}")
    lines.append(t("tools.shortcuts.hint"))
    return lines


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """`jarvis doctor`: the CLI exists, and which allowed Shortcuts exist or are missing. Never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    if shutil.which(SHORTCUTS_CLI) is None:
        checks.append(Check("Shortcuts CLI", False, "`shortcuts` not found (macOS 12 or newer ships it at /usr/bin/shortcuts)"))
        return checks
    entries = config.shortcuts.allowed
    if not entries:
        checks.append(
            Check(
                "Shortcuts allowlist",
                True,
                "optional, empty: the Shortcut tools are hidden until [[shortcuts.allowed]] has entries (docs/integrations/shortcuts.md)",
            )
        )
        return checks
    try:
        installed = installed_shortcuts()
    except Exception as exc:  # the doctor reports, never raises
        checks.append(Check("Shortcuts CLI", False, f"`shortcuts list` failed: {type(exc).__name__}: {exc}"))
        return checks
    checks.append(Check("Shortcuts CLI", True, f"{len(installed)} Shortcut(s) on this Mac, {len(entries)} allowlisted"))
    present = {" ".join(n.casefold().split()) for n in installed}
    for entry in entries:
        ok = " ".join(entry.name.casefold().split()) in present
        flags = [entry.risk] + (["remote"] if entry.remote else []) + (["taints"] if entry.taints else [])
        detail = ", ".join(flags) if ok else "missing: not in the Shortcuts app (renamed or deleted? `jarvis shortcuts list`)"
        checks.append(Check(f"Shortcut {entry.name!r}", ok, detail))
    return checks
