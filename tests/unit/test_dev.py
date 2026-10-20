"""Developer tools: git_status on a temp repo and a non-repo, list_repos depth and skips, open_project argv."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis.exceptions import ToolBlockedError, ToolExecutionError, ToolValidationError
from jarvis.security.guards import PathGuard, ShellGuard, UrlGuard
from jarvis.tools import dev
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.tools.dev import GitStatus, ListRepos, OpenProject, find_repos, summarize_status


@pytest.fixture
def root(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


@pytest.fixture
def ctx(config, paths, root: Path) -> ToolContext:
    guard = PathGuard([root])
    return ToolContext(config=config, paths=paths, path_guard=guard, shell_guard=ShellGuard([], guard), url_guard=UrlGuard())


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "first commit", cwd=path)
    return path


def test_tool_metadata():
    for cls in (GitStatus, OpenProject, ListRepos):
        assert cls.risk == RiskLevel.SAFE and cls.taints is False
    assert GitStatus.name == "git_status" and OpenProject.name == "open_project" and ListRepos.name == "list_repos"


# --- git_status -------------------------------------------------------------------------------


def test_git_status_on_a_temp_repo(ctx, root):
    repo = _make_repo(root / "proj")
    (repo / "README.md").write_text("changed\n")
    (repo / "new.txt").write_text("x")
    (repo / "staged.txt").write_text("y")
    _git("add", "staged.txt", cwd=repo)
    tool = GitStatus(ctx)
    text = tool.run(tool.parse_args({"repo": str(repo)})).content
    assert f"Repository: {repo.resolve()}" in text
    assert "Branch: main (no upstream)" in text
    assert "Changes: 1 staged, 1 unstaged, 1 untracked." in text
    assert " M README.md" in text and "?? new.txt" in text and "A  staged.txt" in text
    assert "Last commits:" in text and "first commit" in text


def test_git_status_clean_repo(ctx, root):
    repo = _make_repo(root / "clean")
    tool = GitStatus(ctx)
    text = tool.run(tool.parse_args({"repo": str(repo)})).content
    assert "Working tree clean." in text and "first commit" in text


def test_git_status_on_a_non_repo(ctx, root):
    plain = root / "plain"
    plain.mkdir()
    tool = GitStatus(ctx)
    with pytest.raises(ToolExecutionError, match="not a git repository"):
        tool.run(tool.parse_args({"repo": str(plain)}))


def test_git_status_paths_go_through_the_guard(ctx, root, tmp_path):
    tool = GitStatus(ctx)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"repo": str(outside)}))
    with pytest.raises(ToolExecutionError):
        tool.run(tool.parse_args({"repo": str(root / "missing")}))
    (root / "file.txt").write_text("x")
    with pytest.raises(ToolValidationError, match="not a directory"):
        tool.run(tool.parse_args({"repo": str(root / "file.txt")}))


def test_git_runs_as_argv_with_timeout(ctx, root, monkeypatch):
    repo = root / "r"
    repo.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, "## main...origin/main [ahead 2, behind 1]\n M a.py\n", "")
        return subprocess.CompletedProcess(argv, 0, "abc1234 do things\n", "")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    tool = GitStatus(ctx)
    text = tool.run(tool.parse_args({"repo": str(repo)})).content
    assert "Branch: main (tracking origin/main, ahead 2, behind 1)" in text and "abc1234 do things" in text
    assert [c[0] for c in calls] == [
        ["git", "-C", str(repo.resolve()), "status", "--porcelain=v1", "--branch"],
        ["git", "-C", str(repo.resolve()), "log", "--oneline", "-5"],
    ]
    assert all(c[1]["timeout"] == 15 and "shell" not in c[1] for c in calls)


def test_summarize_status_edge_cases():
    assert "no commits yet" in summarize_status("## No commits yet on main\n", "")
    assert "detached HEAD" in summarize_status("## HEAD (no branch)\n", "")
    assert "up to date" in summarize_status("## dev...origin/dev\n", "")
    text = summarize_status("## main\n" + "\n".join(f"?? f{i}.txt" for i in range(40)), "")
    assert "40 untracked" in text and "... and 10 more" in text


# --- list_repos -------------------------------------------------------------------------------


def _bare_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_list_repos_depth_and_skips(ctx, root):
    _bare_repo(root / "a")  # depth 1
    _bare_repo(root / "b" / "c")  # depth 2
    _bare_repo(root / "d" / "e" / "f")  # depth 3
    _bare_repo(root / "g" / "h" / "i" / "j")  # depth 4: beyond the default
    _bare_repo(root / ".hidden" / "repo")  # hidden parent: skipped
    _bare_repo(root / "node_modules" / "pkg")  # skipped
    _bare_repo(root / "proj" / ".venv" / "lib")  # skipped
    _bare_repo(root / "a" / "nested")  # inside a repo: not listed
    (root / "worktree").mkdir()
    (root / "worktree" / ".git").write_text("gitdir: /somewhere\n")  # a .git file counts too

    tool = ListRepos(ctx)
    text = tool.run(tool.parse_args({})).content
    listed = set(text.splitlines()[1:])
    expected = {str((root / p).resolve()) for p in ("a", "b/c", "d/e/f", "worktree")}
    assert listed == expected
    assert text.startswith("4 repositories:")

    deeper = tool.run(tool.parse_args({"max_depth": 4})).content
    assert str((root / "g/h/i/j").resolve()) in deeper
    shallow = tool.run(tool.parse_args({"max_depth": 1})).content
    assert set(shallow.splitlines()[1:]) == {str((root / "a").resolve()), str((root / "worktree").resolve())}


def test_list_repos_with_root_and_limit(ctx, root, tmp_path):
    for i in range(55):
        _bare_repo(root / "many" / f"r{i:02d}")
    tool = ListRepos(ctx)
    text = tool.run(tool.parse_args({"root": str(root / "many")})).content
    assert text.startswith("50 repositories (limit reached):") and len(text.splitlines()) == 51
    assert len(find_repos(root / "many", 3)) == 50

    empty = root / "empty"
    empty.mkdir()
    assert "No git repositories found" in tool.run(tool.parse_args({"root": str(empty)})).content
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"root": str(outside)}))
    with pytest.raises(ToolValidationError):
        tool.parse_args({"max_depth": 0})


def test_list_repos_does_not_follow_symlinks(ctx, root, tmp_path):
    outside = tmp_path / "outside-repo"
    _bare_repo(outside)
    (root / "link").symlink_to(outside, target_is_directory=True)
    tool = ListRepos(ctx)
    assert "No git repositories found" in tool.run(tool.parse_args({})).content


# --- open_project ----------------------------------------------------------------------------


def test_open_project_prefers_code_then_open(ctx, root, monkeypatch):
    project = root / "proj"
    project.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(dev.shutil, "which", lambda name: "/usr/local/bin/code" if name == "code" else None)
    tool = OpenProject(ctx)
    text = tool.run(tool.parse_args({"path": str(project)})).content
    assert "VS Code" in text
    assert calls[-1][0] == ["code", str(project.resolve())] and "shell" not in calls[-1][1]

    monkeypatch.setattr(dev.shutil, "which", lambda name: None)
    text = tool.run(tool.parse_args({"path": str(project)})).content
    assert "default application" in text
    assert calls[-1][0] == ["open", str(project.resolve())]


def test_open_project_guard_and_failures(ctx, root, tmp_path, monkeypatch):
    tool = OpenProject(ctx)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"path": str(outside)}))
    with pytest.raises(ToolExecutionError, match="no such file"):
        tool.run(tool.parse_args({"path": str(root / "nope")}))

    project = root / "proj"
    project.mkdir()
    monkeypatch.setattr(dev.shutil, "which", lambda name: None)
    monkeypatch.setattr(dev.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "cannot open"))
    with pytest.raises(ToolExecutionError, match="cannot open"):
        tool.run(tool.parse_args({"path": str(project)}))
