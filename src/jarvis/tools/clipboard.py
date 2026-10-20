"""Clipboard tools: get_clipboard, set_clipboard.

Text travels through `pbpaste` / `pbcopy` as argv subprocesses (text on stdin, never a shell).
An image on the pasteboard is read through AppKit's NSPasteboard, downscaled with `sips` and
attached as an image block. `run_command` and `read_pasteboard_image` are module-level so tests
can monkeypatch them.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.security import sanitize
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolResult
from jarvis.utils import images

log = logging.getLogger("jarvis.tools.clipboard")

COMMAND_TIMEOUT = 15.0
SET_MAX_BYTES = 1_000_000
PREVIEW_CHARS = 80


def run_command(argv: list[str], *, stdin: str | None = None, timeout: float = COMMAND_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """pbpaste / pbcopy as an argv list. Tests replace this function."""
    if not argv or argv[0] not in ("pbpaste", "pbcopy"):
        raise ValueError("run_command only runs pbpaste and pbcopy")
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ToolExecutionError(f"{argv[0]} is not available on this system") from None
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"{argv[0]} timed out after {timeout:.0f}s") from None


def read_pasteboard_image() -> tuple[bytes, str] | None:
    """(bytes, 'png' | 'tiff') for an image on the general pasteboard, or None. Tests replace this."""
    from AppKit import NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF  # noqa: PLC0415 (pyobjc)

    board = NSPasteboard.generalPasteboard()
    for pb_type, label in ((NSPasteboardTypePNG, "png"), (NSPasteboardTypeTIFF, "tiff")):
        data = board.dataForType_(pb_type)
        if data is not None and data.length() > 0:
            return bytes(data), label
    return None


class GetClipboard(Tool):
    name = "get_clipboard"
    description = (
        "Read the current clipboard: the copied text, or the copied image attached as a picture. "
        "Use it when the user refers to something they copied."
    )
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    taints = True  # copied content can come from anywhere
    taint_source = "clipboard"
    delegate_ok = False

    def run(self, args: BaseModel) -> ToolResult:
        ctx = self.require_ctx()
        proc = run_command(["pbpaste"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise ToolExecutionError(f"pbpaste failed: {detail}")
        text = proc.stdout
        if text.strip():
            limit = ctx.config.context.tool_result_max_chars
            if len(text) > limit:
                return ToolResult(text[:limit] + f"\n[clipboard text truncated to {limit} of {len(text)} characters]")
            return ToolResult(text)
        image = self._image_result(ctx.config.screen.max_image_side_px)
        if image is not None:
            return image
        return ToolResult("The clipboard is empty.")

    @staticmethod
    def _image_result(max_side: int) -> ToolResult | None:
        try:
            found = read_pasteboard_image()
        except Exception as exc:  # AppKit missing or broken: the text path still worked
            log.debug("pasteboard image read failed", exc_info=True)
            raise ToolExecutionError(f"cannot read an image from the clipboard: {type(exc).__name__}: {exc}") from exc
        if found is None:
            return None
        data, label = found
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-clip-"))
        try:
            src = workdir / f"clipboard.{label}"
            src.write_bytes(data)
            jpeg, width, height = images.downscale_to_jpeg(src, max_side)
            orig_w, orig_h = images.image_info(src)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        scaled = f", downscaled from {orig_w}x{orig_h}" if (orig_w, orig_h) != (width, height) else ""
        return ToolResult(
            f"The clipboard holds an image ({width}x{height}{scaled}), attached.",
            images=[images.to_attachment(jpeg, width, height, "image/jpeg")],
        )


class SetClipboardInput(BaseModel):
    text: str = Field(description="The text to put on the clipboard.")


class SetClipboard(Tool):
    name = "set_clipboard"
    description = "Put text on the clipboard so the user can paste it (for example a corrected or translated text)."
    input_model = SetClipboardInput
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SetClipboardInput)
        ctx = self.require_ctx()
        text = args.text
        if not text.strip():
            raise ToolValidationError("text is empty")
        if len(text.encode("utf-8")) > SET_MAX_BYTES:
            raise ToolValidationError(f"text is larger than {SET_MAX_BYTES // 1_000_000} MB")
        proc = run_command(["pbcopy"], stdin=text)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise ToolExecutionError(f"pbcopy failed: {detail}")
        self._notify(ctx, text)
        return ToolResult(f"Copied {len(text)} characters to the clipboard.")

    @staticmethod
    def _notify(ctx, text: str) -> None:
        sink = getattr(ctx.runtime, "sink", None) if ctx.runtime is not None else None
        if sink is None:
            return
        try:
            sink.notice(t("tools.clipboard.copied", text=preview(text)))
        except Exception:  # a broken sink must not undo a successful copy
            log.debug("clipboard notice failed", exc_info=True)


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """First `limit` characters on one line, terminal-safe, with an ellipsis when cut."""
    flat = " ".join(sanitize.for_terminal(text).split())
    return flat[:limit] + "…" if len(flat) > limit else flat
