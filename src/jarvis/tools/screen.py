"""Screen understanding: `capture_screen`.

Takes a screenshot with macOS `screencapture`, downscales it with `sips` and hands it to the
model as an image block. Nothing stays on disk unless `save_to` is given. Replaces the older
`screenshot` tool.

Permission: Screen Recording for the terminal app. Checked up front through Quartz; a captured
image that is suspiciously small (uniform wallpaper, nothing drawn) is treated as the same
problem, because macOS silently returns an empty desktop when the permission is missing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.utils import images

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.screen")

CAPTURE_TIMEOUT = 120.0  # interactive targets wait for the user's click or drag
PERMISSION_INSTRUCTIONS = (
    "Screen Recording permission is missing. Open System Settings > Privacy & Security > "
    "Screen Recording, enable the terminal app Jarvis runs in (Terminal, iTerm, ...), then "
    "restart Jarvis."
)
# Second line of defense: without the permission macOS captures the bare wallpaper,
# which compresses to almost nothing. A real screen (or a usable selection) of this many pixels
# never fits in so few JPEG bytes.
BLANK_MAX_JPEG_BYTES = 2048
BLANK_MIN_PIXELS = 40_000

Target = Literal["screen", "window", "selection"]
_TARGET_FLAGS: dict[str, list[str]] = {"screen": [], "window": ["-w"], "selection": ["-i"]}
_SAVE_FORMATS = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg"}


# --- permission (monkeypatched in tests) -----------------------------------------------------


def preflight_screen_capture_access() -> bool:
    """True when the current process may record the screen. Never prompts."""
    from Quartz import CGPreflightScreenCaptureAccess  # noqa: PLC0415 (pyobjc, loaded lazily)

    return bool(CGPreflightScreenCaptureAccess())


def request_screen_capture_access() -> bool:
    """Ask macOS for the permission (shows the system prompt once per app)."""
    from Quartz import CGRequestScreenCaptureAccess  # noqa: PLC0415

    return bool(CGRequestScreenCaptureAccess())


def ensure_screen_capture_access() -> None:
    """Raise PermissionMissingError (after triggering the macOS prompt once) when not granted."""
    try:
        granted = preflight_screen_capture_access()
    except Exception as exc:  # pyobjc missing or broken: do not pretend it is granted
        raise ToolExecutionError(f"cannot check the Screen Recording permission: {type(exc).__name__}: {exc}") from exc
    if granted:
        return
    try:
        request_screen_capture_access()
    except Exception:  # the prompt is best effort; the instructions below are what matters
        log.debug("CGRequestScreenCaptureAccess failed", exc_info=True)
    raise PermissionMissingError(PERMISSION_INSTRUCTIONS)


# --- capture (monkeypatched in tests) -------------------------------------------------------------


def run_screencapture(argv: list[str], *, timeout: float = CAPTURE_TIMEOUT) -> subprocess.CompletedProcess[str]:
    if not argv or argv[0] != "screencapture":
        raise ValueError("run_screencapture only runs screencapture")
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ToolExecutionError("screencapture is not available on this system") from None
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"screencapture timed out after {timeout:.0f}s") from None


def capture_argv(target: str, out: Path, fmt: str = "png") -> list[str]:
    """`screencapture -x` (no sound) plus `-w` (click a window) or `-i` (drag a selection)."""
    return ["screencapture", "-x", *_TARGET_FLAGS[target], "-t", fmt, str(out)]


# --- the tool ---------------------------------------------------------------------------------------


class CaptureScreenInput(BaseModel):
    target: Target = Field(
        default="screen",
        description=("'screen' = the whole screen, 'window' = the user clicks a window, 'selection' = the user drags a rectangle."),
    )
    save_to: str | None = Field(
        default=None,
        description=(
            "Optional absolute or ~ path (.png or .jpg) inside the allowed folders to keep the "
            "full-size capture. Default: nothing is kept on disk."
        ),
    )


class CaptureScreen(Tool):
    name = "capture_screen"
    description = (
        "Take a screenshot of the screen, a window or a selection and look at it. The image is "
        "attached to the result so you can describe, read or debug what is on screen. Nothing is "
        "saved unless save_to is given."
    )
    input_model = CaptureScreenInput
    risk = RiskLevel.SAFE
    taints = True  # the screen can show pages or mails with injected instructions
    taint_source = "screen"
    confirm_when_tainted = False
    delegate_ok = None

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CaptureScreenInput)
        ctx = self.require_ctx()
        save_path: Path | None = None
        fmt = "png"
        if args.save_to is not None:
            save_path, fmt = self._check_save_to(args.save_to)
        ensure_screen_capture_access()

        workdir = Path(tempfile.mkdtemp(prefix="jarvis-screen-"))
        try:
            raw = workdir / f"capture.{fmt}"
            proc = run_screencapture(capture_argv(args.target, raw, fmt))
            if not raw.is_file():
                if args.target != "screen":
                    return ToolResult(f"Screenshot cancelled: no {args.target} was chosen.")
                detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
                raise ToolExecutionError(f"screencapture produced no image: {detail}")
            data, width, height = images.downscale_to_jpeg(raw, ctx.config.screen.max_image_side_px)
            orig_w, orig_h = images.image_info(raw)
            if len(data) < BLANK_MAX_JPEG_BYTES and width * height >= BLANK_MIN_PIXELS:
                raise PermissionMissingError(
                    "The capture is blank (wallpaper only), which is how macOS behaves without the permission. " + PERMISSION_INSTRUCTIONS
                )
            saved = ""
            if save_path is not None:
                try:
                    shutil.copyfile(raw, save_path)
                except OSError as exc:
                    raise ToolExecutionError(f"cannot save the screenshot to {save_path}: {exc}") from exc
                saved = f" Saved the full-size capture to {save_path}."
        finally:
            shutil.rmtree(workdir, ignore_errors=True)  # privacy: nothing stays in the temp dir

        self._notify(width, height)
        scaled = f", downscaled from {orig_w}x{orig_h}" if (orig_w, orig_h) != (width, height) else ""
        return ToolResult(
            f"Screenshot of the {args.target} attached ({width}x{height}{scaled}).{saved}",
            images=[images.to_attachment(data, width, height, "image/jpeg")],
        )

    def _check_save_to(self, raw: str) -> tuple[Path, str]:
        suffix = Path(raw.strip()).suffix.lower()
        fmt = _SAVE_FORMATS.get(suffix)
        if fmt is None:
            raise ToolValidationError("save_to must end with .png or .jpg")
        real = self.require_ctx().path_guard.check(raw, for_write=True)
        if real.exists():
            raise ToolExecutionError(f"{real} already exists; choose another name")
        if not real.parent.is_dir():
            raise ToolExecutionError(f"directory does not exist: {real.parent}")
        return real, fmt

    def _notify(self, width: int, height: int) -> None:
        runtime = self.require_ctx().runtime
        sink = getattr(runtime, "sink", None) if runtime is not None else None
        if sink is not None:
            try:
                sink.notice(t("tools.screen.captured", width=width, height=height))  # every time
            except Exception:  # a broken sink must not lose the screenshot
                log.debug("screen notice failed", exc_info=True)


# --- doctor -------------------------------------------------------------------------------------------


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Screen Recording status for `jarvis doctor`. Preflight only: never prompts, never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415 (avoid import cycles at load time)

    try:
        granted = preflight_screen_capture_access()
    except Exception as exc:
        return [Check("Screen Recording", False, f"cannot query Quartz: {type(exc).__name__}: {exc}")]
    if granted:
        return [Check("Screen Recording", True, "granted to this terminal app")]
    return [Check("Screen Recording", False, PERMISSION_INSTRUCTIONS)]
