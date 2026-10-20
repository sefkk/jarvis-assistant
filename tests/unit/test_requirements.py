"""The pip requirements files are generated from uv.lock and must not drift from it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_requirements_files_match_the_lock_file():
    proc = subprocess.run(
        [str(REPO_ROOT / "scripts" / "update_requirements.sh"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_requirements_files_install_the_project_and_pin_everything():
    core = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    voice = (REPO_ROOT / "requirements-voice.txt").read_text(encoding="utf-8").splitlines()
    dev = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    assert "-e ." in core and "-e ." in voice and "-e ." not in dev
    for lines in (core, voice, dev):
        pins = [line for line in lines if line and not line.startswith((" ", "#", "-e"))]
        assert pins and all("==" in line for line in pins)
    assert any(line.startswith("mlx-whisper==") for line in voice)
    assert not any(line.startswith("mlx-whisper==") for line in core)
    assert any(line.startswith("pytest==") for line in dev) and any(line.startswith("ruff==") for line in dev)
