"""Security fixtures: a fake home directory with allowed and denied folders."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.security.guards import PathGuard, ShellGuard, UrlGuard
from jarvis.tools.base import ToolContext


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """tmp_path acts as $HOME: Documents/ and Downloads/ are allowed, .ssh/ is denied."""
    for d in ("Documents", "Downloads", "Documents/project", ".ssh", "Library/Keychains", "jarvis/data", "jarvis/logs"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "Documents" / "notes.txt").write_text("hello notes\n")
    (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE KEY\n")
    (tmp_path / "Documents" / ".env").write_text("SECRET=1\n")
    (tmp_path / "jarvis" / "data" / "memory.md").write_text("- 2026-01-01: fact\n")
    return tmp_path


@pytest.fixture
def path_guard(home: Path) -> PathGuard:
    return PathGuard(
        allowed_roots=[home / "Documents", home / "Downloads"],
        denied_paths=[home / ".ssh", home / "Library" / "Keychains"],
        extra_denied=[home / "jarvis" / "data", home / "jarvis" / "logs"],
    )


@pytest.fixture
def shell_guard(config, path_guard: PathGuard) -> ShellGuard:
    return ShellGuard(config.security.shell_denylist, path_guard)


@pytest.fixture
def ctx(config, paths, path_guard, shell_guard) -> ToolContext:
    return ToolContext(config=config, paths=paths, path_guard=path_guard, shell_guard=shell_guard, url_guard=UrlGuard())
