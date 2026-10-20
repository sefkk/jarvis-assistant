"""Developer tools: git_status, open_project, list_repos.

Every path goes through the PathGuard. git and the editor are run as argv lists, never a shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError
from jarvis.tools.base import RiskLevel, Tool, ToolResult

GIT_TIMEOUT = 15
OPEN_TIMEOUT = 15
MAX_LISTED_FILES = 30
MAX_REPOS = 50
SKIP_DIR_NAMES = {"node_modules", ".venv", "venv", "__pycache__", "site-packages", "Pods", "DerivedData"}


def _parse_branch_header(header: str) -> str:
    """`## main...origin/main [ahead 1, behind 2]` -> a readable line."""
    body = header[3:].strip()
    status = ""
    if body.endswith("]") and " [" in body:
        body, _, status = body[:-1].rpartition(" [")
    branch, sep, upstream = body.partition("...")
    text = f"Branch: {branch}"
    if sep and upstream:
        text += f" (tracking {upstream}, {status or 'up to date'})"
    else:
        text += " (no upstream)"
    return text


def _git(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *argv], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False, cwd=cwd)
    except FileNotFoundError:
        raise ToolExecutionError("git is not installed (run `xcode-select --install`)") from None
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"git timed out after {GIT_TIMEOUT}s") from None
    except OSError as exc:
        raise ToolExecutionError(f"could not run git: {exc}") from None


def summarize_status(porcelain: str, log: str) -> str:
    """Turn `git status --porcelain=v1 --branch` and `git log --oneline` output into a short summary."""
    lines = porcelain.splitlines()
    header = lines[0] if lines and lines[0].startswith("## ") else ""
    entries = [line for line in lines if not line.startswith("## ")]
    out: list[str] = []
    if header.startswith("## No commits yet on "):
        out.append(f"Branch: {header[len('## No commits yet on ') :]} (no commits yet)")
    elif header.startswith("## HEAD (no branch)"):
        out.append("Branch: detached HEAD")
    elif header:
        out.append(_parse_branch_header(header))
    staged = unstaged = untracked = 0
    files: list[str] = []
    for entry in entries:
        if len(entry) < 3:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x == "?" and y == "?":
            untracked += 1
            files.append(f"?? {path}")
            continue
        if x not in (" ", "?", "!"):
            staged += 1
        if y not in (" ", "?", "!"):
            unstaged += 1
        files.append(f"{x}{y} {path}")
    if not files:
        out.append("Working tree clean.")
    else:
        out.append(f"Changes: {staged} staged, {unstaged} unstaged, {untracked} untracked.")
        out.extend("  " + f for f in files[:MAX_LISTED_FILES])
        if len(files) > MAX_LISTED_FILES:
            out.append(f"  ... and {len(files) - MAX_LISTED_FILES} more")
    commits = [c for c in log.splitlines() if c.strip()]
    if commits:
        out.append("Last commits:")
        out.extend("  " + c for c in commits)
    return "\n".join(out)


class GitStatusInput(BaseModel):
    repo: str = Field(description="Absolute path (or ~/...) of a git repository.")


class GitStatus(Tool):
    name = "git_status"
    description = "Summarize a git repository: branch, ahead/behind, changed files and the last 5 commits."
    input_model = GitStatusInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, GitStatusInput)
        ctx = self.require_ctx()
        repo = ctx.path_guard.check_dir(args.repo)
        status = _git(["-C", str(repo), "status", "--porcelain=v1", "--branch"])
        if status.returncode != 0:
            detail = (status.stderr or status.stdout).strip()
            if "not a git repository" in detail.lower():
                raise ToolExecutionError(f"{repo} is not a git repository")
            raise ToolExecutionError(f"git status failed: {detail or f'exit code {status.returncode}'}")
        log = _git(["-C", str(repo), "log", "--oneline", "-5"])
        log_text = log.stdout if log.returncode == 0 else ""
        return ToolResult(f"Repository: {repo}\n" + summarize_status(status.stdout, log_text))


class OpenProjectInput(BaseModel):
    path: str = Field(description="Absolute path (or ~/...) of the project folder or file to open.")


class OpenProject(Tool):
    name = "open_project"
    description = "Open a project folder in the editor (VS Code if `code` is installed, otherwise the Finder default)."
    input_model = OpenProjectInput
    risk = RiskLevel.SAFE
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, OpenProjectInput)
        ctx = self.require_ctx()
        target = ctx.path_guard.check(args.path)
        if shutil.which("code"):
            argv, editor = ["code", str(target)], "VS Code"
        else:
            argv, editor = ["open", str(target)], "the default application"
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=OPEN_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError(f"could not run {argv[0]}: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
            raise ToolExecutionError(f"could not open {target}: {detail}")
        return ToolResult(f"Opened {target} in {editor}.")


class ListReposInput(BaseModel):
    root: str | None = Field(default=None, description="Folder to scan. Default: all allowed roots.")
    max_depth: int = Field(default=3, ge=1, le=6, description="How many folder levels deep to look.")


def find_repos(root: Path, max_depth: int, limit: int = MAX_REPOS) -> list[Path]:
    """Directories containing `.git` below `root`, at most `max_depth` levels down. A repository
    is not searched further (nested checkouts and submodules are ignored). Hidden directories,
    node_modules and virtualenvs are skipped; symlinks are not followed."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if len(found) >= limit:
            return
        try:
            with os.scandir(directory) as it:
                children = sorted((e for e in it if e.is_dir(follow_symlinks=False)), key=lambda e: e.name.lower())
        except OSError:
            return
        names = {e.name for e in children}
        if ".git" in names or (directory / ".git").is_file():
            found.append(directory)
            return
        if depth >= max_depth:
            return
        for entry in children:
            if entry.name.startswith(".") or entry.name in SKIP_DIR_NAMES:
                continue
            walk(Path(entry.path), depth + 1)
            if len(found) >= limit:
                return

    walk(root, 0)
    return found


class ListRepos(Tool):
    name = "list_repos"
    description = "List git repositories under the allowed folders (or a given folder), up to 50."
    input_model = ListReposInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ListReposInput)
        ctx = self.require_ctx()
        roots = [ctx.path_guard.check_dir(args.root)] if args.root else [r for r in ctx.path_guard.allowed if r.is_dir()]
        repos: list[Path] = []
        for root in roots:
            for repo in find_repos(root, args.max_depth, MAX_REPOS - len(repos)):
                if repo not in repos:
                    repos.append(repo)
            if len(repos) >= MAX_REPOS:
                break
        if not repos:
            where = ", ".join(str(r) for r in roots) or "the allowed roots"
            return ToolResult(f"No git repositories found under {where} (depth {args.max_depth}).")
        head = f"{len(repos)} repositories" + (" (limit reached)" if len(repos) >= MAX_REPOS else "") + ":"
        return ToolResult("\n".join([head, *(str(r) for r in repos)]))
