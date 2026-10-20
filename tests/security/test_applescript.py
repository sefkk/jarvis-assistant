import shutil
import subprocess

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.tools.system import QUIT_APP_SCRIPT, SET_VOLUME_SCRIPT, QuitApp, SetVolume
from jarvis.utils.applescript import run_applescript

INJECTION = '" & do shell script "touch /tmp/pwned" & "'
ECHO_SCRIPT = """on run argv
    return item 1 of argv
end run"""


def test_injection_attempt_stays_a_literal_argument(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_applescript(QUIT_APP_SCRIPT, [INJECTION])
    assert out == "ok"
    # The script source is the constant, unchanged; the hostile text is a separate argv element.
    assert seen["argv"][:3] == ["osascript", "-e", QUIT_APP_SCRIPT]
    assert seen["argv"][3] == "--" and seen["argv"][4] == INJECTION
    assert "do shell script" not in seen["argv"][2]
    assert "shell" not in str({k: v for k, v in locals().items() if k == "kwargs"})


def test_tools_do_not_interpolate_their_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    QuitApp().run(QuitApp().parse_args({"app": INJECTION.replace("/", "")}))
    assert calls[-1][2] == QUIT_APP_SCRIPT and calls[-1][-1] == INJECTION.replace("/", "")
    SetVolume().run(SetVolume().parse_args({"level": 30}))
    assert calls[-1][2] == SET_VOLUME_SCRIPT and calls[-1][-1] == "30"
    with pytest.raises(ToolValidationError):
        SetVolume().parse_args({"level": 101})
    with pytest.raises(ToolValidationError):
        SetVolume().parse_args({"level": '30; do shell script "id"'})


def test_argument_shape_checks():
    with pytest.raises(ToolValidationError):
        run_applescript(ECHO_SCRIPT, ["a\x00b"])
    with pytest.raises(ToolValidationError):
        run_applescript(ECHO_SCRIPT, [123])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="on run"):
        run_applescript('display dialog "x"', [])


def test_permission_error_is_mapped(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "", "execution error: Not authorized to send Apple events to Spotify. (-1743)"
        ),
    )
    with pytest.raises(PermissionMissingError, match="Automation"):
        run_applescript(ECHO_SCRIPT, ["x"])
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "syntax error"))
    with pytest.raises(ToolExecutionError, match="syntax error"):
        run_applescript(ECHO_SCRIPT, ["x"])


@pytest.mark.skipif(shutil.which("osascript") is None, reason="osascript only exists on macOS")
def test_real_osascript_round_trip():
    """The hostile string comes back verbatim: osascript treated it as data, not code."""
    assert run_applescript(ECHO_SCRIPT, [INJECTION]) == INJECTION
    assert run_applescript(ECHO_SCRIPT, ["-x"]) == "-x"
    assert run_applescript(ECHO_SCRIPT, ["çağrı ünlü"]) == "çağrı ünlü"
