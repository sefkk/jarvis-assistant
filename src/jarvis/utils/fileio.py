"""Atomic text writes: the new content lands in a temporary file next to the target and replaces
it with one `os.replace`, so a reader never sees a half-written file and a crash leaves the old
file intact. Used for profile.md (the auto-managed Current Location block) and the location state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            tmp.chmod(path.stat().st_mode & 0o777)  # keep the target's permissions
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
