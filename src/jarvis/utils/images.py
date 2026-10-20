"""Image helpers built on macOS `sips` (no Pillow): downscale to JPEG, read dimensions, build
the ImageAttachment a tool hands to the model.

`run_sips` is the single subprocess entry point so tests can monkeypatch it.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from jarvis.exceptions import ToolExecutionError
from jarvis.tools.base import ImageAttachment

SIPS_TIMEOUT = 30.0
_DIM_RE = re.compile(r"^\s*(pixelWidth|pixelHeight):\s*(\d+)\s*$", re.MULTILINE)


def run_sips(argv: list[str], *, timeout: float = SIPS_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run `sips` with an argv list (never a shell). Tests replace this function."""
    if not argv or argv[0] != "sips":
        raise ValueError("run_sips only runs sips")
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ToolExecutionError("sips is not available on this system") from None
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"sips timed out after {timeout:.0f}s") from None


def _check(proc: subprocess.CompletedProcess[str], what: str) -> None:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise ToolExecutionError(f"sips could not {what}: {detail}")


def image_info(path: Path) -> tuple[int, int]:
    """(width, height) in pixels as reported by `sips -g pixelWidth -g pixelHeight`."""
    proc = run_sips(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)])
    _check(proc, f"read {path.name}")
    found = {key: int(value) for key, value in _DIM_RE.findall(proc.stdout)}
    if "pixelWidth" not in found or "pixelHeight" not in found:
        raise ToolExecutionError(f"sips did not report the dimensions of {path.name}: {proc.stdout.strip()[:200]!r}")
    return found["pixelWidth"], found["pixelHeight"]


def downscale_to_jpeg(src: Path, max_side: int, quality: int = 80) -> tuple[bytes, int, int]:
    """Convert `src` to a JPEG whose longest side is at most `max_side` pixels.

    Returns (jpeg_bytes, width, height). Images already within `max_side` are only converted,
    not resampled. The temporary output file is removed before returning.
    """
    if max_side < 1:
        raise ValueError("max_side must be positive")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")
    width, height = image_info(src)
    argv = ["sips"]
    if max(width, height) > max_side:
        argv += ["-Z", str(max_side)]
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-img-"))
    dst = workdir / (src.stem + ".jpg")
    try:
        argv += ["-s", "format", "jpeg", "-s", "formatOptions", str(quality), str(src), "--out", str(dst)]
        _check(run_sips(argv), f"convert {src.name} to JPEG")
        if not dst.is_file():
            raise ToolExecutionError(f"sips reported success but wrote no file for {src.name}")
        try:
            data = dst.read_bytes()
        except OSError as exc:
            raise ToolExecutionError(f"cannot read the converted image: {exc}") from exc
        out_w, out_h = image_info(dst)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return data, out_w, out_h


def to_attachment(data: bytes, width: int, height: int, media_type: str = "image/jpeg") -> ImageAttachment:
    if media_type not in ("image/jpeg", "image/png"):
        raise ValueError(f"unsupported media type {media_type}")
    return ImageAttachment(
        media_type=media_type,
        data_b64=base64.b64encode(data).decode("ascii"),
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
    )
