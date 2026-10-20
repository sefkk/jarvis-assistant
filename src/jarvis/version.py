"""`jarvis --version`: the package version plus the git commit of the editable checkout.

Reads `.git/HEAD` and the ref file directly (no subprocess, no git dependency), so the version
string costs nothing at startup and works under launchd too.
"""

from __future__ import annotations

from pathlib import Path

from jarvis import __version__


def package_version() -> str:
    """`jarvis.__version__`, the single source of the version (pyproject.toml reads it too). An editable
    install shows a new version at once, without reinstalling the tool to refresh its metadata."""
    return __version__


def git_commit(home: Path) -> tuple[str, str] | None:
    """(short commit, branch) of the checkout at `home`, or None when it is not a git repository."""
    git_dir = home / ".git"
    try:
        if git_dir.is_file():  # a linked worktree: "gitdir: <path>"
            pointer = git_dir.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_dir = (home / pointer.split(":", 1)[1].strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head[:7], "detached"
    ref = head.split(":", 1)[1].strip()
    branch = ref.removeprefix("refs/heads/")
    common_dir = git_dir  # a linked worktree keeps HEAD itself; the refs live in the main repository
    try:
        pointer = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        common_dir = (git_dir / pointer).resolve()
    except OSError:
        pass
    for base in dict.fromkeys((git_dir, common_dir)):
        try:
            sha = (base / ref).read_text(encoding="utf-8").strip()
            return sha[:7], branch
        except OSError:
            pass
    try:  # after `git gc` the ref lives in packed-refs
        for line in (common_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0][:7], branch
    except OSError:
        pass
    return None


def version_string(home: Path | None) -> str:
    """`jarvis 1.0.0 (e46303f on dev)` followed by the home directory on a second line."""
    text = f"jarvis {package_version()}"
    commit = git_commit(home) if home is not None else None
    if commit:
        text += f" ({commit[0]} on {commit[1]})"
    if home is not None:
        text += f"\nhome: {home}"
    return text
