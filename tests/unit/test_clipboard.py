"""get_clipboard / set_clipboard, fully mocked: no pbpaste, pbcopy, sips or AppKit."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from jarvis.brain import Brain
from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.observability import logging_setup
from jarvis.observability.timing import TurnTimer
from jarvis.observability.usage import UsageTracker
from jarvis.security.guards import PathGuard, ShellGuard, UrlGuard
from jarvis.tools import clipboard
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.clipboard import GetClipboard, SetClipboard, preview
from jarvis.tools.registry import ToolRegistry
from jarvis.utils import images
from tests.conftest import FakeClient, RecordingSink, text_message, tool_message

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"clip" * 100
FAKE_JPEG = b"\xff\xd8\xff" + b"j" * 3000

NOTICE_PREFIX = "📋 Panoya kopyalandı: "  # the test config is Turkish


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def ctx(config, paths, tmp_path: Path, sink: RecordingSink) -> ToolContext:
    guard = PathGuard([tmp_path])
    runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, sink=sink)
    return ToolContext(
        config=config, paths=paths, path_guard=guard, shell_guard=ShellGuard([], guard), url_guard=UrlGuard(), runtime=runtime
    )


@pytest.fixture
def commands(monkeypatch):
    """Fake pbpaste/pbcopy: records argv and stdin, returns the scripted clipboard text."""
    state = {"calls": [], "text": "", "returncode": 0}

    def run(argv, *, stdin=None, timeout=0):
        state["calls"].append((list(argv), stdin))
        return subprocess.CompletedProcess(argv, state["returncode"], state["text"] if argv[0] == "pbpaste" else "", "")

    monkeypatch.setattr(clipboard, "run_command", run)
    monkeypatch.setattr(clipboard, "read_pasteboard_image", lambda: None)
    return state


def get(ctx):
    tool = GetClipboard(ctx)
    return tool.run(tool.parse_args({}))


def set_(ctx, text):
    tool = SetClipboard(ctx)
    return tool.run(tool.parse_args({"text": text}))


# --- metadata ---------------------------------------------------------------------------------------


def test_tool_metadata():
    assert GetClipboard.name == "get_clipboard" and SetClipboard.name == "set_clipboard"
    assert GetClipboard.risk == RiskLevel.SAFE and GetClipboard.taints and GetClipboard.taint_source == "clipboard"
    assert SetClipboard.risk == RiskLevel.SAFE and SetClipboard.taints is False
    assert GetClipboard.delegate_ok is False and SetClipboard.delegate_ok is False
    assert SetClipboard().api_schema()["input_schema"]["required"] == ["text"]


# --- get_clipboard -------------------------------------------------------------------------------------


def test_get_text(ctx, commands):
    commands["text"] = "Merhaba dünya\nsecond line"
    result = get(ctx)
    assert result.content == "Merhaba dünya\nsecond line" and not result.is_error and result.images == []
    assert commands["calls"] == [(["pbpaste"], None)]


def test_get_text_is_truncated_to_tool_result_max_chars(ctx, commands, config):
    config.context.tool_result_max_chars = 500
    commands["text"] = "x" * 1200
    result = get(ctx)
    assert result.content.startswith("x" * 500) and "x" * 501 not in result.content
    assert "truncated to 500 of 1200" in result.content


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_empty_clipboard(ctx, commands, text):
    commands["text"] = text
    result = get(ctx)
    assert result.content == "The clipboard is empty." and not result.is_error and result.images == []


def test_pbpaste_failure_is_a_tool_error(ctx, commands):
    commands["returncode"] = 1
    with pytest.raises(ToolExecutionError, match="pbpaste failed"):
        get(ctx)


def test_get_image(ctx, commands, monkeypatch, config):
    config.screen.max_image_side_px = 1000
    seen = []

    def fake_downscale(src: Path, max_side: int, quality: int = 80):
        assert src.is_file() and src.read_bytes() == FAKE_PNG and src.suffix == ".png"
        seen.append((src, max_side))
        return FAKE_JPEG, 800, 600

    monkeypatch.setattr(clipboard, "read_pasteboard_image", lambda: (FAKE_PNG, "png"))
    monkeypatch.setattr(images, "downscale_to_jpeg", fake_downscale)
    monkeypatch.setattr(images, "image_info", lambda path: (1600, 1200))
    result = get(ctx)
    (img,) = result.images
    assert img.media_type == "image/jpeg" and (img.width, img.height) == (800, 600)
    assert img.sha256 == hashlib.sha256(FAKE_JPEG).hexdigest() and base64.b64decode(img.data_b64) == FAKE_JPEG
    assert "800x600" in result.content and "downscaled from 1600x1200" in result.content
    (src, max_side) = seen[0]
    assert max_side == 1000 and not src.exists() and not src.parent.exists()  # temp file removed


def test_get_image_tiff_and_broken_appkit(ctx, commands, monkeypatch):
    seen = []

    def fake_downscale(src, max_side, quality=80):
        seen.append(src.suffix)
        return FAKE_JPEG, 10, 10

    monkeypatch.setattr(clipboard, "read_pasteboard_image", lambda: (b"II*\x00", "tiff"))
    monkeypatch.setattr(images, "downscale_to_jpeg", fake_downscale)
    monkeypatch.setattr(images, "image_info", lambda path: (10, 10))
    assert get(ctx).images and seen == [".tiff"]

    def broken():
        raise ImportError("No module named AppKit")

    monkeypatch.setattr(clipboard, "read_pasteboard_image", broken)
    with pytest.raises(ToolExecutionError, match="AppKit"):
        get(ctx)


# --- set_clipboard ------------------------------------------------------------------------------------


def test_set_uses_pbcopy_with_text_on_stdin(ctx, commands, sink):
    result = set_(ctx, "düzeltilmiş metin")
    assert commands["calls"] == [(["pbcopy"], "düzeltilmiş metin")]
    assert "17 characters" in result.content and not result.is_error
    assert sink.notices == [NOTICE_PREFIX + "düzeltilmiş metin"]


def test_set_preview_is_cut_at_80_chars_and_terminal_safe(ctx, commands, sink):
    text = "\x1b[31mred\x1b[0m " + "a" * 100 + "\nline two"
    set_(ctx, text)
    (notice,) = sink.notices
    assert notice.startswith(NOTICE_PREFIX) and notice.endswith("…")
    body = notice[len(NOTICE_PREFIX) :]
    assert "\x1b" not in body and "\n" not in body and len(body) == 81
    assert body.startswith("red " + "a" * 76)


def test_set_rejects_empty_and_huge_text(ctx, commands):
    with pytest.raises(ToolValidationError, match="empty"):
        set_(ctx, "   ")
    with pytest.raises(ToolValidationError, match="1 MB"):
        set_(ctx, "x" * 1_000_001)
    assert commands["calls"] == []


def test_set_pbcopy_failure_and_missing_runtime(ctx, commands):
    commands["returncode"] = 1
    with pytest.raises(ToolExecutionError, match="pbcopy failed"):
        set_(ctx, "x")
    commands["returncode"] = 0
    ctx.runtime = None
    assert not set_(ctx, "x").is_error


def test_preview_helper():
    assert preview("short") == "short"
    assert preview("a" * 81) == "a" * 80 + "…"
    assert preview("  spaced \t out  ") == "spaced out"


# --- through the Brain: image block first, reference only in the transcript ------------------------------


def test_clipboard_image_reaches_model_and_transcript_keeps_a_reference(ctx, config, db, logs, commands, monkeypatch):
    monkeypatch.setattr(clipboard, "read_pasteboard_image", lambda: (FAKE_PNG, "png"))
    monkeypatch.setattr(images, "downscale_to_jpeg", lambda src, max_side, quality=80: (FAKE_JPEG, 640, 480))
    monkeypatch.setattr(images, "image_info", lambda path: (640, 480))
    registry = ToolRegistry()
    registry.register(GetClipboard(ctx))
    client = FakeClient([tool_message("get_clipboard", {}), text_message("a cat")])
    brain = Brain(client, config, registry, db, UsageTracker(config, db), rng=lambda: 0.0)
    db.start_session("s", "m", "d")
    db.start_turn("t", "s", "x")
    logging_setup.set_session_id("s")
    logging_setup.set_turn_id("t")
    outcome = brain.run_turn(
        system="s",
        messages=[{"role": "user", "content": "what did I copy?"}],
        sink=RecordingSink(),
        turn_id="t",
        timer=TurnTimer("t", db),
        user_text="what did I copy?",
    )
    assert outcome.tainted and outcome.taint_sources == ["clipboard"]
    sent = client.calls[1]["messages"][2]["content"][0]["content"]
    assert sent[0]["type"] == "image" and sent[0]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(sent[0]["source"]["data"]) == FAKE_JPEG
    assert sent[1]["type"] == "text" and sent[1]["text"].startswith('<untrusted source="clipboard">')
    assert not config.screen.store_images
    stored = db.conn.execute("SELECT content_json FROM messages WHERE role='user' ORDER BY id DESC").fetchone()
    assert base64.b64encode(FAKE_JPEG).decode() not in stored["content_json"]
    ref = json.loads(stored["content_json"])[0]["content"][0]
    assert ref["type"] == "image_ref" and ref["media_type"] == "image/jpeg"
    # The transcript keeps the attachment's own reference: raw-byte sha256 and dimensions.
    import hashlib

    assert ref["sha256"] == hashlib.sha256(FAKE_JPEG).hexdigest() and ref["width"] and ref["height"]
