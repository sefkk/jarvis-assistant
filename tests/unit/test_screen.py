"""capture_screen and utils/images, fully mocked: no screencapture, no sips on real screens, no Quartz."""

from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path

import pytest

from jarvis.exceptions import PermissionMissingError, ToolBlockedError, ToolExecutionError, ToolValidationError
from jarvis.security.guards import PathGuard, ShellGuard, UrlGuard
from jarvis.tools import screen
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.screen import CaptureScreen, health_checks
from jarvis.utils import images
from tests.conftest import RecordingSink

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"original-bytes" * 50
FAKE_JPEG = b"\xff\xd8\xff" + b"j" * 5000  # well above the blank threshold


@pytest.fixture
def root(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def ctx(config, paths, root: Path, sink: RecordingSink) -> ToolContext:
    guard = PathGuard([root])
    runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, sink=sink)
    return ToolContext(
        config=config, paths=paths, path_guard=guard, shell_guard=ShellGuard([], guard), url_guard=UrlGuard(), runtime=runtime
    )


@pytest.fixture
def permission(monkeypatch):
    """Screen Recording granted; records how often the system prompt would be triggered."""
    state = {"granted": True, "requests": 0}
    monkeypatch.setattr(screen, "preflight_screen_capture_access", lambda: state["granted"])

    def request():
        state["requests"] += 1
        return False

    monkeypatch.setattr(screen, "request_screen_capture_access", request)
    return state


@pytest.fixture
def capture(monkeypatch, permission):
    """Fake screencapture (writes FAKE_PNG unless cancelled) and fake sips helpers."""
    state = {"argv": [], "cancel": False, "downscale": [], "jpeg": FAKE_JPEG, "paths_seen": []}

    def fake_capture(argv, *, timeout=0):
        state["argv"].append(list(argv))
        out = Path(argv[-1])
        if not state["cancel"]:
            out.write_bytes(FAKE_PNG)
        return subprocess.CompletedProcess(argv, 1 if state["cancel"] else 0, "", "")

    def fake_downscale(src: Path, max_side: int, quality: int = 80):
        assert src.is_file() and src.read_bytes() == FAKE_PNG
        state["downscale"].append((src, max_side))
        state["paths_seen"].append(src)
        return state["jpeg"], 1456, 819

    monkeypatch.setattr(screen, "run_screencapture", fake_capture)
    monkeypatch.setattr(images, "downscale_to_jpeg", fake_downscale)
    monkeypatch.setattr(images, "image_info", lambda path: (2880, 1800))
    return state


def run_tool(ctx: ToolContext, **args):
    tool = CaptureScreen(ctx)
    return tool.run(tool.parse_args(args))


# --- metadata and permission ----------------------------------------------------------------------


def test_tool_metadata():
    assert CaptureScreen.name == "capture_screen"
    assert CaptureScreen.risk == RiskLevel.SAFE and CaptureScreen.taints is True
    assert CaptureScreen.taint_source == "screen" and CaptureScreen.confirm_when_tainted is False
    assert CaptureScreen.delegate_ok is None
    schema = CaptureScreen().api_schema()["input_schema"]
    assert set(schema["properties"]) == {"target", "save_to"} and "required" not in schema
    with pytest.raises(ToolValidationError):
        CaptureScreen().parse_args({"target": "desktop"})


def test_permission_denied_prompts_once_and_explains(ctx, capture, permission):
    permission["granted"] = False
    with pytest.raises(PermissionMissingError, match="Screen Recording") as info:
        run_tool(ctx)
    assert "System Settings > Privacy & Security > Screen Recording" in str(info.value)
    assert "restart Jarvis" in str(info.value)
    assert permission["requests"] == 1
    assert capture["argv"] == []  # nothing was captured


def test_permission_check_failure_is_a_tool_error(ctx, capture, monkeypatch):
    def broken():
        raise ImportError("No module named Quartz")

    monkeypatch.setattr(screen, "preflight_screen_capture_access", broken)
    with pytest.raises(ToolExecutionError, match="Quartz"):
        run_tool(ctx)


# --- capture ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target, flags", [("screen", []), ("window", ["-w"]), ("selection", ["-i"])])
def test_capture_argv_per_target(ctx, capture, target, flags):
    result = run_tool(ctx, target=target)
    assert not result.is_error
    (argv,) = capture["argv"]
    assert argv[:2] == ["screencapture", "-x"]
    assert argv[2 : 2 + len(flags)] == flags
    assert argv[-3:-1] == ["-t", "png"] and argv[-1].endswith("capture.png")
    assert "-w" not in argv[2:] or target == "window"
    assert "-i" not in argv[2:] or target == "selection"


def test_temp_file_and_dir_are_deleted_after_success(ctx, capture):
    run_tool(ctx)
    (src,) = capture["paths_seen"]
    assert "jarvis-screen-" in str(src.parent)
    assert not src.exists() and not src.parent.exists()


def test_temp_dir_is_deleted_when_downscale_fails(ctx, capture, monkeypatch):
    def boom(src, max_side, quality=80):
        capture["paths_seen"].append(src)
        raise ToolExecutionError("sips exploded")

    monkeypatch.setattr(images, "downscale_to_jpeg", boom)
    with pytest.raises(ToolExecutionError, match="sips exploded"):
        run_tool(ctx)
    (src,) = capture["paths_seen"]
    assert not src.parent.exists()


@pytest.mark.parametrize("target", ["window", "selection"])
def test_cancelled_interactive_capture_is_not_an_error(ctx, capture, sink, target):
    capture["cancel"] = True
    result = run_tool(ctx, target=target)
    assert not result.is_error and result.images == []
    assert "cancelled" in result.content.lower()
    assert sink.notices == []  # nothing was captured, so no notice


def test_missing_file_for_whole_screen_is_an_error(ctx, capture):
    capture["cancel"] = True
    with pytest.raises(ToolExecutionError, match="no image"):
        run_tool(ctx)


def test_downscale_uses_configured_max_side_and_fills_the_attachment(ctx, capture, config):
    config.screen.max_image_side_px = 1200
    result = run_tool(ctx)
    assert [max_side for _, max_side in capture["downscale"]] == [1200]
    (img,) = result.images
    assert img.media_type == "image/jpeg" and (img.width, img.height) == (1456, 819)
    assert img.sha256 == hashlib.sha256(FAKE_JPEG).hexdigest()
    assert base64.b64decode(img.data_b64) == FAKE_JPEG
    assert "1456x819" in result.content and "downscaled from 2880x1800" in result.content
    assert "screen" in result.content and "Saved" not in result.content
    assert img.block()["type"] == "image" and img.reference()["type"] == "image_ref"


def test_blank_capture_means_permission_missing(ctx, capture):
    capture["jpeg"] = b"\xff\xd8" + b"\x00" * 300  # a uniform wallpaper compresses to almost nothing
    with pytest.raises(PermissionMissingError, match="blank"):
        run_tool(ctx)


# --- save_to ---------------------------------------------------------------------------------------------


def test_save_to_copies_the_original_capture(ctx, capture, root):
    target = root / "shot.png"
    result = run_tool(ctx, save_to=str(target))
    assert target.read_bytes() == FAKE_PNG  # the full-size original, not the downscaled JPEG
    assert str(target) in result.content and "Saved" in result.content
    assert result.images and not capture["paths_seen"][0].parent.exists()


def test_save_to_jpg_captures_in_jpg_format(ctx, capture, root):
    run_tool(ctx, save_to=str(root / "shot.jpg"))
    (argv,) = capture["argv"]
    assert argv[-3:-1] == ["-t", "jpg"] and argv[-1].endswith("capture.jpg")


def test_save_to_outside_allowed_roots_is_blocked_before_capturing(ctx, capture, tmp_path):
    with pytest.raises(ToolBlockedError, match="outside the allowed roots"):
        run_tool(ctx, save_to=str(tmp_path / "elsewhere.png"))
    assert capture["argv"] == []


def test_save_to_refuses_existing_file_and_bad_suffix(ctx, capture, root):
    existing = root / "old.png"
    existing.write_bytes(b"keep me")
    with pytest.raises(ToolExecutionError, match="already exists"):
        run_tool(ctx, save_to=str(existing))
    assert existing.read_bytes() == b"keep me"
    with pytest.raises(ToolValidationError, match=r"\.png or \.jpg"):
        run_tool(ctx, save_to=str(root / "shot.gif"))
    with pytest.raises(ToolExecutionError, match="directory does not exist"):
        run_tool(ctx, save_to=str(root / "missing" / "shot.png"))
    assert capture["argv"] == []


# --- notice ----------------------------------------------------------------------------------------------


def test_notice_goes_to_the_runtime_sink(ctx, capture, sink):
    run_tool(ctx)
    assert sink.notices == ["📸 Ekran görüntüsü alındı (1456x819)"]  # the test config is Turkish


def test_works_without_a_runtime(ctx, capture):
    ctx.runtime = None
    assert run_tool(ctx).images


# --- doctor ------------------------------------------------------------------------------------------------


def test_health_check_granted_and_denied(config, paths, monkeypatch, permission):
    (check,) = health_checks(config, paths)
    assert check.name == "Screen Recording" and check.ok and "granted" in check.detail
    permission["granted"] = False
    (check,) = health_checks(config, paths)
    assert not check.ok and "Screen Recording" in check.detail
    assert permission["requests"] == 0  # the doctor never prompts

    def broken():
        raise RuntimeError("no Quartz")

    monkeypatch.setattr(screen, "preflight_screen_capture_access", broken)
    (check,) = health_checks(config, paths)
    assert not check.ok and "no Quartz" in check.detail


# --- utils/images --------------------------------------------------------------------------------------------


@pytest.fixture
def fake_sips(monkeypatch):
    """A sips stand-in: `-g` reports the dimensions registered per path, conversions write JPEG bytes."""
    state = {"dims": {}, "argv": [], "jpeg": b"\xff\xd8converted"}

    def run(argv, *, timeout=0):
        state["argv"].append(list(argv))
        assert argv[0] == "sips"
        if argv[1] == "-g":
            path = Path(argv[-1])
            w, h = state["dims"][path.name]
            return subprocess.CompletedProcess(argv, 0, f"{path}\n  pixelWidth: {w}\n  pixelHeight: {h}\n", "")
        out = Path(argv[-1])
        out.write_bytes(state["jpeg"])
        state["dims"][out.name] = state.get("out_dims", (1568, 941))
        return subprocess.CompletedProcess(argv, 0, f"{argv[-3]}\n  {out}\n", "")

    monkeypatch.setattr(images, "run_sips", run)
    return state


def test_downscale_argv_for_a_large_image(fake_sips, tmp_path):
    src = tmp_path / "big.png"
    src.write_bytes(FAKE_PNG)
    fake_sips["dims"]["big.png"] = (2880, 1800)
    data, w, h = images.downscale_to_jpeg(src, 1568, quality=75)
    assert (data, w, h) == (fake_sips["jpeg"], 1568, 941)
    info, convert, info_out = fake_sips["argv"]
    assert info == ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(src)]
    assert convert[:3] == ["sips", "-Z", "1568"]
    assert convert[3:9] == ["-s", "format", "jpeg", "-s", "formatOptions", "75"]
    assert convert[9] == str(src) and convert[10] == "--out" and convert[11].endswith("big.jpg")
    assert info_out[:5] == ["sips", "-g", "pixelWidth", "-g", "pixelHeight"]
    assert not Path(convert[11]).exists() and not Path(convert[11]).parent.exists()  # temp output removed


def test_small_image_is_converted_without_resampling(fake_sips, tmp_path):
    src = tmp_path / "small.png"
    src.write_bytes(FAKE_PNG)
    fake_sips["dims"]["small.png"] = (800, 600)
    fake_sips["out_dims"] = (800, 600)
    data, w, h = images.downscale_to_jpeg(src, 1568)
    assert (w, h) == (800, 600) and data == fake_sips["jpeg"]
    convert = fake_sips["argv"][1]
    assert "-Z" not in convert and convert[1:7] == ["-s", "format", "jpeg", "-s", "formatOptions", "80"]


def test_image_info_parses_sips_output(fake_sips, tmp_path):
    fake_sips["dims"]["x.png"] = (12, 34)
    assert images.image_info(tmp_path / "x.png") == (12, 34)


def test_sips_failures_become_tool_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(images, "run_sips", lambda argv, timeout=0: subprocess.CompletedProcess(argv, 1, "", "bad file"))
    with pytest.raises(ToolExecutionError, match="bad file"):
        images.image_info(tmp_path / "x.png")
    monkeypatch.setattr(images, "run_sips", lambda argv, timeout=0: subprocess.CompletedProcess(argv, 0, "garbage", ""))
    with pytest.raises(ToolExecutionError, match="dimensions"):
        images.image_info(tmp_path / "x.png")


def test_run_sips_only_runs_sips():
    with pytest.raises(ValueError):
        images.run_sips(["rm", "-rf", "/"])


def test_to_attachment_hashes_and_encodes():
    att = images.to_attachment(b"abc", 3, 4, "image/png")
    assert att.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert base64.b64decode(att.data_b64) == b"abc" and (att.width, att.height) == (3, 4)
    assert att.media_type == "image/png"
    with pytest.raises(ValueError):
        images.to_attachment(b"abc", 1, 1, "image/gif")
