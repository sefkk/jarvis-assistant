import os

import pytest

from jarvis.exceptions import ToolBlockedError, ToolExecutionError, ToolValidationError
from jarvis.tools.files import ReadFile, SearchFiles, WriteFile


def test_allowed_file_is_readable(path_guard, home):
    assert path_guard.check(str(home / "Documents" / "notes.txt")) == (home / "Documents" / "notes.txt").resolve()


def test_traversal_out_of_allowed_root_is_blocked(path_guard, home):
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(home / "Documents" / ".." / ".ssh" / "id_rsa"))
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(home / "Documents" / ".." / "other.txt"), for_write=True)


def test_symlink_escape_is_blocked(path_guard, home):
    link = home / "Documents" / "innocent.txt"
    os.symlink(home / ".ssh" / "id_rsa", link)
    with pytest.raises(ToolBlockedError, match="denied"):
        path_guard.check(str(link))
    dir_link = home / "Downloads" / "keys"
    os.symlink(home / ".ssh", dir_link)
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(dir_link / "id_rsa"))
    # A symlink to somewhere outside every root but not explicitly denied is blocked too.
    outside = home / "elsewhere.txt"
    outside.write_text("x")
    os.symlink(outside, home / "Documents" / "link2.txt")
    with pytest.raises(ToolBlockedError, match="outside the allowed roots"):
        path_guard.check(str(home / "Documents" / "link2.txt"))


def test_denied_paths_env_and_jarvis_dirs(path_guard, home):
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(home / ".ssh" / "id_rsa"))
    with pytest.raises(ToolBlockedError, match=".env"):
        path_guard.check(str(home / "Documents" / ".env"))
    with pytest.raises(ToolBlockedError, match=".env"):
        path_guard.check(str(home / "Documents" / ".env.local"), for_write=True)
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(home / "jarvis" / "data" / "memory.md"))
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(home / "jarvis" / "logs" / "audit.jsonl"), for_write=True)


@pytest.mark.skipif(not os.path.exists(str(__file__).upper()), reason="needs a case-insensitive filesystem")
def test_case_variants_cannot_bypass_denials(path_guard, home):
    """APFS is case-insensitive: .ENV, DATA/ and .SSH must be refused like their exact spellings."""
    with pytest.raises(ToolBlockedError, match=".env"):
        path_guard.check(str(home / "Documents" / ".ENV"))
    with pytest.raises(ToolBlockedError, match=".env"):
        path_guard.check(str(home / "DOCUMENTS" / ".Env.local"), for_write=True)
    with pytest.raises(ToolBlockedError, match="denied"):
        path_guard.check(str(home / "JARVIS" / "DATA" / "memory.md"))
    with pytest.raises(ToolBlockedError, match="denied"):
        path_guard.check(str(home / "jarvis" / "LOGS" / "new.jsonl"), for_write=True)
    with pytest.raises(ToolBlockedError):
        path_guard.check(str(home / ".SSH" / "id_rsa"))
    # and a case variant of an allowed root is still allowed
    assert path_guard.check(str(home / "DOCUMENTS" / "notes.txt")).name == "notes.txt"


def test_write_file_refuses_special_files(ctx, home):
    tool = WriteFile(ctx)
    fifo = home / "Documents" / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ToolExecutionError, match="not a regular file"):
        tool.run(tool.parse_args({"path": str(fifo), "content": "x", "overwrite": True}))
    with pytest.raises(ToolExecutionError, match="not a regular file"):
        tool.run(tool.parse_args({"path": str(home / "Documents" / "project"), "content": "x"}))


def test_search_query_cannot_be_an_mdfind_option(ctx):
    tool = SearchFiles(ctx)
    with pytest.raises(ToolValidationError, match="option"):
        tool.run(tool.parse_args({"query": "-live secret"}))


def test_relative_and_missing_paths(path_guard, home):
    with pytest.raises(ToolValidationError, match="absolute"):
        path_guard.check("Documents/notes.txt")
    with pytest.raises(ToolValidationError):
        path_guard.check("")
    with pytest.raises(ToolValidationError):
        path_guard.check("/tmp/a\x00b")
    with pytest.raises(ToolExecutionError, match="no such file"):
        path_guard.check(str(home / "Documents" / "missing.txt"))
    # writes may target a file that does not exist yet
    assert path_guard.check(str(home / "Documents" / "new.txt"), for_write=True).name == "new.txt"


def test_read_file_tool(ctx, home):
    tool = ReadFile(ctx)
    out = tool.run(tool.parse_args({"path": str(home / "Documents" / "notes.txt")}))
    assert "hello notes" in out.content
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"path": str(home / ".ssh" / "id_rsa")}))
    binary = home / "Downloads" / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02PNG")
    with pytest.raises(ToolExecutionError, match="binary"):
        tool.run(tool.parse_args({"path": str(binary)}))
    big = home / "Downloads" / "big.txt"
    big.write_text("a" * 5000)
    out = tool.run(tool.parse_args({"path": str(big), "max_chars": 1000}))
    assert "[truncated to 1000 characters]" in out.content


def test_write_file_tool(ctx, home):
    tool = WriteFile(ctx)
    assert tool.risk.value == "CONFIRM"
    target = home / "Documents" / "out.txt"
    tool.run(tool.parse_args({"path": str(target), "content": "hi"}))
    assert target.read_text() == "hi"
    with pytest.raises(ToolExecutionError, match="already exists"):
        tool.run(tool.parse_args({"path": str(target), "content": "again"}))
    tool.run(tool.parse_args({"path": str(target), "content": "again", "overwrite": True}))
    assert target.read_text() == "again"
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"path": str(home / ".ssh" / "authorized_keys"), "content": "key"}))
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"path": str(home / "Documents" / ".env"), "content": "x", "overwrite": True}))


def test_search_files_filters_results_through_guard(ctx, home, monkeypatch):
    import subprocess

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["mdfind", "-onlyin"]
        return subprocess.CompletedProcess(argv, 0, f"{home}/Documents/notes.txt\n{home}/.ssh/id_rsa\n{home}/Documents/.env\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = SearchFiles(ctx)
    out = tool.run(tool.parse_args({"query": "notes"}))
    assert "notes.txt" in out.content and "id_rsa" not in out.content and ".env" not in out.content
