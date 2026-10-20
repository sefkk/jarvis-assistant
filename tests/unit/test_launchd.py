"""launchd agent management with a fake launchctl runner: nothing is ever loaded for real."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from jarvis import services
from jarvis.scheduler import launchd
from jarvis.scheduler.launchd import LABEL, SchedulerInstallError, SchedulerStatus

PRINT_OUTPUT = """\
gui/501/com.jarvis-assistant.scheduler = {
\tactive count = 1
\tpath = /Users/alex/Library/LaunchAgents/com.jarvis-assistant.scheduler.plist
\tstate = running

\tprogram = /Users/alex/src/jarvis/.venv/bin/jarvis
\targuments = {
\t\t/Users/alex/src/jarvis/.venv/bin/jarvis
\t\tscheduler
\t\trun
\t}

\tpid = 48213
\truns = 1
\tproperties = keepalive | runatload
}
"""


class FakeLaunchctl:
    def __init__(self, results: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        code, out, err = self.results.get(argv[0], (0, "", ""))
        return subprocess.CompletedProcess(["launchctl", *argv], code, out, err)


@pytest.fixture
def plist(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    monkeypatch.setattr(launchd, "plist_path", lambda: target)
    monkeypatch.setattr(launchd, "_uid", lambda: 501)
    return target


@pytest.fixture
def exe(tmp_path, monkeypatch) -> Path:
    fake = tmp_path / "venv" / "bin" / "jarvis"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchd, "jarvis_executable", lambda: fake)
    return fake


def test_build_plist_content(paths, exe):
    data = launchd.build_plist(paths)
    assert data["Label"] == LABEL == f"{services.LABEL_PREFIX}.scheduler"
    assert data["ProgramArguments"] == [str(exe), "scheduler", "run"]
    assert data["WorkingDirectory"] == str(paths.home.resolve())
    assert data["EnvironmentVariables"]["JARVIS_HOME"] == str(paths.home.resolve())
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["StandardOutPath"] == str(paths.logs_dir.resolve() / "scheduler.out.log")
    assert data["StandardErrorPath"] == str(paths.logs_dir.resolve() / "scheduler.err.log")


def test_jarvis_executable_falls_back_to_the_venv_script(monkeypatch):
    """Without the global command the agents run `<venv>/bin/jarvis`, taken from
    sys.prefix (sys.executable is a symlink into Homebrew's framework, with no `jarvis` next to it)."""
    from jarvis import services

    monkeypatch.setattr(services.sys, "prefix", "/opt/venv")
    monkeypatch.setattr(services.sys, "executable", "/opt/venv/bin/python3")
    monkeypatch.setattr(services, "global_executable", lambda: None)
    assert launchd.jarvis_executable() == Path("/opt/venv/bin/jarvis")


def test_jarvis_executable_prefers_the_global_command(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "jarvis"
    exe.parent.mkdir()
    exe.write_text("#!/opt/tool/bin/python\n")
    exe.chmod(0o755)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(exe.parent))
    assert launchd.jarvis_executable() == exe and exe.is_absolute()


@pytest.fixture
def symlinked_venv(tmp_path, monkeypatch):
    """The real layout: `<venv>/bin/python` is a symlink into Homebrew's Python framework, which
    has no `jarvis`; the console script lives only in `<venv>/bin`. No global command, nothing
    on PATH."""
    from jarvis import services

    framework = tmp_path / "Cellar" / "python@3.12" / "Frameworks" / "Python.framework" / "Versions" / "3.12" / "bin"
    framework.mkdir(parents=True)
    real_python = framework / "python3.12"
    real_python.write_text("")
    real_python.chmod(0o755)
    venv = tmp_path / "repo" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(real_python)
    script = venv / "bin" / "jarvis"
    script.write_text(f"#!{venv / 'bin' / 'python'}\nfrom jarvis.main import main\n")
    script.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(empty))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(services.sys, "prefix", str(venv))
    monkeypatch.setattr(services.sys, "executable", str(venv / "bin" / "python"))
    return venv, framework


def test_jarvis_executable_does_not_follow_the_interpreter_symlink(symlinked_venv):
    """Regression: `Path(sys.executable).resolve()` led into Homebrew's framework
    bin, where `jarvis` does not exist, and `scheduler install` refused to run."""
    from jarvis import services

    venv, framework = symlinked_venv
    exe = launchd.jarvis_executable()
    assert exe == venv / "bin" / "jarvis" and exe.is_file()
    assert not exe.is_relative_to(framework)
    assert Path(services.sys.executable).resolve().parent == framework  # the trap is really there
    assert services.global_executable() is None


def test_jarvis_executable_uses_the_script_next_to_the_interpreter_last(symlinked_venv, tmp_path, monkeypatch):
    """When sys.prefix has no `jarvis` (an interpreter run outside its venv), the `jarvis` next to
    the interpreter as invoked is used, still without resolving the symlink."""
    from jarvis import services

    venv, framework = symlinked_venv
    monkeypatch.setattr(services.sys, "prefix", str(tmp_path / "other_prefix"))
    assert launchd.jarvis_executable() == venv / "bin" / "jarvis"
    (venv / "bin" / "jarvis").unlink()
    # Nothing exists anywhere: the venv path is reported, never a path inside the framework.
    missing = launchd.jarvis_executable()
    assert missing == tmp_path / "other_prefix" / "bin" / "jarvis" and not missing.is_relative_to(framework)


def test_global_executable_found_on_path_when_tool_bin_dir_is_empty(symlinked_venv, tmp_path, monkeypatch):
    from jarvis import services

    venv, _ = symlinked_venv
    elsewhere = tmp_path / "elsewhere" / "bin"
    elsewhere.mkdir(parents=True)
    other = elsewhere / "jarvis"
    other.write_text("#!/opt/tool/bin/python\n")
    other.chmod(0o755)
    monkeypatch.setenv("PATH", f"{venv / 'bin'}:{elsewhere}")
    # `<venv>/bin/jarvis` comes first on PATH (as under `uv run`) but is not the global command.
    monkeypatch.setattr(services.shutil, "which", lambda name: str(venv / "bin" / "jarvis"))
    assert services.global_executable() is None
    assert launchd.jarvis_executable() == venv / "bin" / "jarvis"
    monkeypatch.setattr(services.shutil, "which", lambda name: str(other))
    assert services.global_executable() == other
    assert launchd.jarvis_executable() == other


def test_every_installer_writes_the_venv_script_when_there_is_no_global_command(symlinked_venv, paths):
    import importlib

    from jarvis import services

    venv, framework = symlinked_venv
    for agent in services.AGENTS:
        program = Path(importlib.import_module(agent.module).build_plist(paths)["ProgramArguments"][0])
        assert program == venv / "bin" / "jarvis" and program.is_absolute() and not program.is_relative_to(framework)


def test_install_writes_plist_and_bootstraps(paths, plist, exe, monkeypatch):
    fake = FakeLaunchctl({"bootout": (3, "", "Boot-out failed: 3: No such process")})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    written = launchd.install(paths)
    assert written == plist and plist.is_file()
    with plist.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["Label"] == LABEL and data["ProgramArguments"][0] == str(exe)
    assert fake.calls == [
        ["bootout", f"gui/501/{LABEL}"],
        ["bootstrap", "gui/501", str(plist)],
    ]


def test_install_falls_back_to_load_when_bootstrap_fails(paths, plist, exe, monkeypatch):
    fake = FakeLaunchctl({"bootstrap": (5, "", "Bootstrap failed: 5: Input/output error")})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(paths)
    assert fake.calls[-2:] == [["bootstrap", "gui/501", str(plist)], ["load", "-w", str(plist)]]


def test_install_raises_when_everything_fails(paths, plist, exe, monkeypatch):
    fake = FakeLaunchctl({"bootstrap": (5, "", "boom"), "load": (1, "", "nope")})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    with pytest.raises(SchedulerInstallError, match="nope"):
        launchd.install(paths)


def test_install_refuses_without_executable(paths, plist, monkeypatch, tmp_path):
    monkeypatch.setattr(launchd, "jarvis_executable", lambda: tmp_path / "missing" / "jarvis")
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    with pytest.raises(SchedulerInstallError, match="jarvis executable"):
        launchd.install(paths)
    assert fake.calls == [] and not plist.exists()


def test_uninstall_boots_out_and_removes_plist(paths, plist, exe, monkeypatch):
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"<plist/>")
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    assert launchd.uninstall() is True
    assert not plist.exists()
    assert fake.calls == [["bootout", f"gui/501/{LABEL}"]]


def test_uninstall_falls_back_to_unload_and_tolerates_missing_plist(plist, monkeypatch):
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"<plist/>")
    fake = FakeLaunchctl({"bootout": (113, "", "Could not find service")})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    assert launchd.uninstall() is True
    assert fake.calls == [["bootout", f"gui/501/{LABEL}"], ["unload", "-w", str(plist)]]
    fake.calls.clear()
    assert launchd.uninstall() is False  # nothing to remove; no unload for a missing plist
    assert fake.calls == [["bootout", f"gui/501/{LABEL}"]]


def test_parse_print_output():
    assert launchd.parse_print_output(PRINT_OUTPUT) == (True, 48213)
    assert launchd.parse_print_output(PRINT_OUTPUT.replace("state = running", "state = waiting")) == (False, None)
    assert launchd.parse_print_output("garbage") == (False, None)


def test_status_running(plist, monkeypatch):
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"<plist/>")
    fake = FakeLaunchctl({"print": (0, PRINT_OUTPUT, "")})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    st = launchd.status()
    assert st == SchedulerStatus(installed=True, running=True, pid=48213, detail="running (pid 48213)")
    assert fake.calls == [["print", f"gui/501/{LABEL}"]]


def test_status_installed_but_not_loaded(plist, monkeypatch):
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"<plist/>")
    fake = FakeLaunchctl({"print": (113, "", "Could not find service")})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    st = launchd.status()
    assert st.installed and not st.running and st.pid is None and "not loaded" in st.detail


def test_status_missing_plist_does_not_call_launchctl(plist, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    st = launchd.status()
    assert st == SchedulerStatus(installed=False, running=False, pid=None, detail=st.detail)
    assert "not installed" in st.detail and fake.calls == []


def test_status_never_raises(plist, monkeypatch):
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"<plist/>")

    def explode(argv):
        raise subprocess.TimeoutExpired(["launchctl"], 30)

    monkeypatch.setattr(launchd, "_run_launchctl", explode)
    st = launchd.status()
    assert not st.installed and not st.running and "TimeoutExpired" in st.detail
