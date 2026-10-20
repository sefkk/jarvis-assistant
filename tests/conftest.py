"""Shared fixtures: temporary home, a fake Anthropic client, and a recording sink."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

from jarvis.config import Config, Paths, load_config
from jarvis.io.base import ConfirmRequest, OutputSink
from jarvis.observability import logging_setup
from jarvis.observability.db import Database

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- environment ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The suite never reaches the internet: a connection to anything but this machine fails loudly, so a
    test that forgot to mock an API cannot pass by luck (or cost money). Local servers (the dashboard tests)
    still work."""
    import ipaddress
    import socket

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise RuntimeError(f"tests must not use the network (connect to {host})")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _main_language_reset():
    """`i18n` keeps the main language in a module global (set by `bootstrap` and the setup wizard): every
    test starts from the default, so no test sees the language another one left behind."""
    from jarvis import i18n

    i18n.set_language(i18n.DEFAULT_LANGUAGE)
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.fixture(autouse=True)
def _platform_check_off(monkeypatch):
    """CI runners may be older macOS versions or Intel; the platform check has its own tests."""
    from jarvis.utils import platform_check

    monkeypatch.setenv(platform_check.OVERRIDE_ENV, "1")


@pytest.fixture(autouse=True)
def _football_key_absent(monkeypatch):
    """The football tools are optional and hidden without a key. Tests must not depend
    on what the real Keychain holds; a test that wants the tools patches this to True."""
    import jarvis.tools.football as football_tools

    monkeypatch.setattr(football_tools, "api_key_configured", lambda: False)


@pytest.fixture(autouse=True)
def _elevenlabs_offline(monkeypatch):
    """The doctor's ElevenLabs lines read the Keychain and, with a key, call the API. Tests see no
    key and no network; a test that wants them patches these itself."""
    import jarvis.voice.elevenlabs as elevenlabs

    monkeypatch.setattr(elevenlabs, "get_api_key", lambda: None)
    monkeypatch.setattr(elevenlabs, "fetch_subscription", lambda key, **kw: None)


@pytest.fixture(autouse=True)
def location_lookups_off(monkeypatch):
    """The Current Location refresh runs `shortcuts run`, CoreLocationCLI or an IP lookup. Tests
    never touch the Mac or the network: the background refresh is a no-op and every
    source fails fast; a test that wants them replaces these itself. Yields the real
    `refresh_in_background` for the test that exercises the thread."""
    import jarvis.location.service as location_service
    import jarvis.location.sources as location_sources

    real_background = location_service.refresh_in_background
    monkeypatch.setattr(location_service, "refresh_in_background", lambda config, paths, **kw: None)

    def _unavailable(name):
        def source(config, *, timeout, now_iso):
            raise location_sources.SourceError(f"{name} unavailable in tests")

        return source

    monkeypatch.setattr(location_sources, "SOURCES", {name: _unavailable(name) for name in location_sources.SOURCE_ORDER})
    yield real_background


def _shipped_config_text() -> str:
    """config.toml as shipped: `jarvis presence setup` writes the user's router MAC and `[[presence.people]]`
    into the real file, and tests must not depend on what this Mac's setup wrote there."""
    from jarvis.presence.configwrite import _replace_gateway_line, _strip_people_blocks

    lines = (REPO_ROOT / "tests" / "fixtures" / "config.test.toml").read_text(encoding="utf-8").splitlines(keepends=True)
    lines = _strip_people_blocks(_replace_gateway_line(lines, []))
    return "".join(lines) + "\n"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    (tmp_path / "config.toml").write_text(_shipped_config_text(), encoding="utf-8")
    p = Paths(tmp_path)
    p.ensure_dirs()
    return p


@pytest.fixture
def config(paths: Paths) -> Config:
    """The test config (Turkish main language); like `bootstrap`, it sets the main language for `i18n`."""
    from jarvis import i18n

    loaded = load_config(paths.config_file)
    i18n.set_language(loaded.general.language)
    return loaded


@pytest.fixture
def logs(paths: Paths) -> Path:
    logging_setup.setup_logging(paths.logs_dir, level="DEBUG", console=False)
    yield paths.logs_dir
    logging_setup.set_session_id(None)
    logging_setup.set_turn_id(None)


@pytest.fixture
def db(paths: Paths) -> Database:
    database = Database(paths.db_file)
    yield database
    database.close()


# --- SDK message builders -------------------------------------------------------------


def text_message(text: str, *, stop_reason: str = "end_turn", **usage: int) -> Message:
    return make_message([TextBlock(type="text", text=text)], stop_reason=stop_reason, **usage)


def tool_message(name: str, args: dict[str, Any], *, tool_id: str = "tu_1", text: str | None = None) -> Message:
    blocks: list[Any] = []
    if text:
        blocks.append(TextBlock(type="text", text=text))
    blocks.append(ToolUseBlock(type="tool_use", id=tool_id, name=name, input=args))
    return make_message(blocks, stop_reason="tool_use")


def thinking_block(text: str = "let me think", signature: str = "sig_abc") -> ThinkingBlock:
    return ThinkingBlock(type="thinking", thinking=text, signature=signature)


def make_message(
    blocks: list[Any],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 0,
    cache_write: int = 0,
) -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-haiku-5-5",
        content=blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


def api_error(cls, status: int, message: str = "boom", error_type: str = "api_error", request_id: str = "req_test"):
    """Build an SDK status error the way the SDK does (needs a real httpx2 response)."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers={"request-id": request_id})
    return cls(message, response=response, body={"type": "error", "error": {"type": error_type, "message": message}})


def connection_error(cls, message: str = "connection reset"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message=message, request=request)


# --- fake client ----------------------------------------------------------------------


class FakeStream:
    def __init__(self, outcome: Message | Exception) -> None:
        self.outcome = outcome

    def __enter__(self) -> "FakeStream":
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def __iter__(self):
        assert isinstance(self.outcome, Message)
        for block in self.outcome.content:
            if block.type == "text":
                yield SimpleNamespace(type="text", text=block.text, snapshot=block.text)
            elif block.type == "thinking":
                yield SimpleNamespace(type="thinking", thinking=block.thinking, snapshot=block.thinking)
            else:  # tool_use, server_tool_use, a server tool result: the stream announces the block
                yield SimpleNamespace(type="content_block_start", content_block=block)

    def get_final_message(self) -> Message:
        assert isinstance(self.outcome, Message)
        return self.outcome


class FakeMessages:
    def __init__(self, outcomes: list[Message | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **params: Any) -> FakeStream:
        # Snapshot messages so later mutation does not change what we recorded.
        snapshot = dict(params)
        snapshot["messages"] = [dict(m) for m in params["messages"]]
        self.calls.append(snapshot)
        if not self.outcomes:
            raise AssertionError("FakeClient ran out of scripted responses")
        return FakeStream(self.outcomes.pop(0))


class FakeClient:
    def __init__(self, outcomes: list[Message | Exception]) -> None:
        self.messages = FakeMessages(outcomes)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls


# --- recording sink --------------------------------------------------------------------


class RecordingSink(OutputSink):
    def __init__(self, confirm_answer: bool = False, choice_answer: str | None = None) -> None:
        self.deltas: list[str] = []
        self.notices: list[str] = []
        self.debug_lines: list[str] = []
        self.confirms: list[ConfirmRequest] = []
        self.choices: list[tuple[str, list[tuple[str, str]], str]] = []
        self.finished = 0
        self.breaks: list[int] = []
        self.confirm_answer = confirm_answer
        self.choice_answer = choice_answer

    def ask_choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        self.choices.append((question, options, default))
        return self.choice_answer if self.choice_answer is not None else default

    @property
    def text(self) -> str:
        return "".join(self.deltas)

    def write_delta(self, text: str) -> None:
        self.deltas.append(text)

    def finish(self) -> None:
        self.finished += 1

    def confirm(self, request: ConfirmRequest) -> bool:
        self.confirms.append(request)
        return self.confirm_answer

    def notice(self, text: str) -> None:
        self.notices.append(text)

    def debug(self, text: str) -> None:
        self.debug_lines.append(text)

    def segment_break(self) -> None:
        self.breaks.append(len(self.text))  # where the Brain split the reply (offset into `text`)
