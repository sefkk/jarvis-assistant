"""File tools: search_files (Spotlight), read_file, write_file.

Every path goes through the PathGuard: real path inside allowed_file_roots, outside
denied_paths, never .env, never Jarvis's own data/ and logs/.
"""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolBlockedError, ToolExecutionError, ToolValidationError
from jarvis.tools.base import RiskLevel, Tool, ToolResult

READ_MAX_BYTES = 2_000_000
SEARCH_TIMEOUT = 20


class SearchFilesInput(BaseModel):
    query: str = Field(description="Spotlight query: words from the file name or content, e.g. 'invoice 2026'.")
    root: str | None = Field(default=None, description="Optional directory to search in (must be inside the allowed roots). Default: all.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum number of results.")


class SearchFiles(Tool):
    name = "search_files"
    description = (
        "Find files on this Mac with Spotlight (mdfind) by name or content words. "
        "Searches only the user's allowed folders (Documents, Desktop, Downloads, Developer by default)."
    )
    input_model = SearchFilesInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = False  # per spec only read_file taints; file names are shown, not file contents

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SearchFilesInput)
        ctx = self.require_ctx()
        query = args.query.strip()
        if not query or "\x00" in query:
            raise ToolValidationError("query is empty")
        if query.startswith("-"):
            raise ToolValidationError("query cannot start with '-' (mdfind would read it as an option)")
        roots = [ctx.path_guard.check_dir(args.root)] if args.root else ctx.path_guard.allowed
        results: list[str] = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                proc = subprocess.run(
                    ["mdfind", "-onlyin", str(root), query],
                    capture_output=True,
                    text=True,
                    timeout=SEARCH_TIMEOUT,
                    check=False,
                )
            except FileNotFoundError:
                raise ToolExecutionError("mdfind (Spotlight) is not available") from None
            except subprocess.TimeoutExpired:
                raise ToolExecutionError("Spotlight search timed out") from None
            if proc.returncode != 0:
                raise ToolExecutionError(f"mdfind failed: {proc.stderr.strip() or proc.returncode}")
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ctx.path_guard.check(line)
                except (ToolBlockedError, ToolExecutionError, ToolValidationError):
                    continue  # never list files the user could not read through Jarvis
                results.append(line)
                if len(results) >= args.limit:
                    break
            if len(results) >= args.limit:
                break
        if not results:
            return ToolResult(f"No files found for {query!r}.")
        return ToolResult(f"{len(results)} result(s) for {query!r}:\n" + "\n".join(results))


class ReadFileInput(BaseModel):
    path: str = Field(description="Absolute path or ~ path of a text file inside the allowed folders.")
    max_chars: int = Field(default=12000, ge=100, le=200_000, description="Return at most this many characters.")


class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file from the user's allowed folders. Binary files are refused."
    input_model = ReadFileInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "file"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ReadFileInput)
        real = self.require_ctx().path_guard.check(args.path)
        if not real.is_file():
            raise ToolExecutionError(f"not a regular file: {real}")
        try:
            with real.open("rb") as fh:
                head = fh.read(READ_MAX_BYTES)
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {real}: {exc}") from exc
        if b"\x00" in head[:8192]:
            raise ToolExecutionError(f"{real} looks like a binary file")
        text = head.decode("utf-8", errors="replace")
        note = ""
        if len(text) > args.max_chars:
            text = text[: args.max_chars]
            note = f"\n[truncated to {args.max_chars} characters]"
        return ToolResult(f"{real}:\n{text}{note}")


class WriteFileInput(BaseModel):
    path: str = Field(description="Absolute path or ~ path inside the allowed folders.")
    content: str = Field(description="Full text content to write.")
    overwrite: bool = Field(default=False, description="Set true to replace an existing file.")


class WriteFile(Tool):
    name = "write_file"
    description = "Write a text file inside the user's allowed folders. Asks for confirmation. Does not overwrite unless told to."
    input_model = WriteFileInput
    risk = RiskLevel.CONFIRM
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, WriteFileInput)
        real = self.require_ctx().path_guard.check(args.path, for_write=True)
        if real.exists():
            if not real.is_file():
                raise ToolExecutionError(f"{real} exists and is not a regular file")
            if not args.overwrite:
                raise ToolExecutionError(f"{real} already exists; pass overwrite=true to replace it")
        if not real.parent.is_dir():
            raise ToolExecutionError(f"directory does not exist: {real.parent}")
        try:
            real.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"cannot write {real}: {exc}") from exc
        return ToolResult(f"Wrote {len(args.content)} characters to {real}.")
