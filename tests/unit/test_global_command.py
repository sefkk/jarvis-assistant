"""`jarvis` as a global command. Home resolution order, running from other directories
(subprocess), absolute paths in the launchd plists, `--version`, lazy imports, the fast start,
`jarvis services`, and the generated shell completion."""

from __future__ import annotations

import importlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import jarvis.config as config_mod
import jarvis.main as main_mod
from jarvis import services
from jarvis.config import package_repo_root, resolve_home
from jarvis.exceptions import ConfigError
from jarvis.main import build_parser, cmd_chat, completion_script, greeting_line, main
from jarvis.observability import health
from jarvis.version import git_commit, version_string
from tests.unit.test_launchd import FakeLaunchctl
from tests.unit.test_voice_misc import FakeLaunchctl as StatefulLaunchctl

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- 12.2 home resolution ------------------------------------------------------------------


def test_home_comes_from_jarvis_home_first(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    assert resolve_home() == tmp_path.resolve()


def test_home_env_must_be_a_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="JARVIS_HOME"):
        resolve_home()


def test_home_defaults_to_the_editable_repository(monkeypatch):
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    assert resolve_home() == REPO_ROOT


def test_home_error_is_clear_for_a_non_editable_install(monkeypatch):
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    monkeypatch.setattr(config_mod, "package_repo_root", lambda: None)
    with pytest.raises(ConfigError, match="JARVIS_HOME.*uv tool install --editable"):
        resolve_home()


def test_package_repo_root_recognizes_only_the_src_layout(tmp_path):
    assert package_repo_root() == REPO_ROOT
    fake = tmp_path / "lib" / "python3.12" / "site-packages" / "jarvis" / "config.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    assert package_repo_root(str(fake)) is None


def test_missing_config_names_the_home(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match=f"Jarvis home: {re.escape(str(tmp_path))}"):
        config_mod.load_config(tmp_path / "config.toml")


# --- 12.2 subprocess: the same home from any directory -------------------------------------


def _jarvis(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "jarvis.main", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "jarvis_home"
    target.mkdir()
    shutil.copy(REPO_ROOT / "tests" / "fixtures" / "config.test.toml", target / "config.toml")
    return target


def test_runs_from_any_directory_with_the_same_data_and_logs(home, tmp_path):
    env = {**os.environ, "JARVIS_HOME": str(home)}
    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    for cwd in (Path.home(), Path("/tmp"), other_repo):
        proc = _jarvis(["--version"], cwd, env)
        assert proc.returncode == 0 and f"home: {home}" in proc.stdout, proc.stderr
        proc = _jarvis(["sessions"], cwd, env)
        assert proc.returncode == 0, proc.stderr
    assert (home / "logs" / "jarvis.log").is_file() and (home / "data" / "jarvis.db").is_file()
    assert not (other_repo / "data").exists() and not (other_repo / "logs").exists()


def test_without_jarvis_home_the_repository_is_used_from_elsewhere():
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_HOME"}
    proc = subprocess.run(
        [sys.executable, "-c", "from jarvis.config import resolve_home; print(resolve_home())"],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == str(REPO_ROOT)


# --- 12.1 --version -------------------------------------------------------------------------


def test_git_commit_reads_head_ref_and_packed_refs(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/dev\n")
    (git / "refs" / "heads" / "dev").write_text("abcdef1234567890\n")
    assert git_commit(tmp_path) == ("abcdef1", "dev")
    (git / "refs" / "heads" / "dev").unlink()
    (git / "packed-refs").write_text("# pack-refs\n1234567abcdef refs/heads/dev\n")
    assert git_commit(tmp_path) == ("1234567", "dev")
    (git / "HEAD").write_text("fedcba9876543210\n")
    assert git_commit(tmp_path) == ("fedcba9", "detached")
    assert git_commit(tmp_path / "not_a_repo") is None


def test_git_commit_in_a_linked_worktree_reads_the_ref_from_the_main_repository(tmp_path):
    main_git = tmp_path / "main" / ".git"
    worktree_git = main_git / "worktrees" / "wt"
    worktree_git.mkdir(parents=True)
    (main_git / "refs" / "heads" / "release").mkdir(parents=True)
    (main_git / "refs" / "heads" / "release" / "public").write_text("0123456789abcdef\n")
    (worktree_git / "HEAD").write_text("ref: refs/heads/release/public\n")
    (worktree_git / "commondir").write_text("../..\n")
    checkout = tmp_path / "wt"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {worktree_git}\n")
    assert git_commit(checkout) == ("0123456", "release/public")


def test_version_string_has_version_commit_and_home(tmp_path):
    text = version_string(REPO_ROOT)
    assert re.match(r"jarvis \d+\.\d+\.\d+ \([0-9a-f]{7} on \S+\)\nhome: " + re.escape(str(REPO_ROOT)) + "$", text)
    assert version_string(tmp_path) == f"jarvis {text.split()[1]}\nhome: {tmp_path}"  # no git: no commit
    assert version_string(None).startswith("jarvis ")


def test_version_flag_prints_to_stdout_and_exits_zero(capsys, monkeypatch):
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("jarvis 1.0.0") and "home: " in out


# --- 12.4 lazy imports and the fast start -------------------------------------------------

HEAVY_PREFIXES = (
    "anthropic",
    "googleapiclient",
    "google.",
    "google_auth",
    "EventKit",
    "objc",
    "Quartz",
    "AVFoundation",
    "mlx_whisper",
    "sounddevice",
    "openwakeword",
    "onnxruntime",
    "pynput",
    "keyring",
    "jarvis.assistant",
    "jarvis.brain",
    "jarvis.tools",
    "jarvis.voice",
    "jarvis.interfaces",
    "jarvis.utils.eventkit",
    "jarvis.utils.google_auth",
)


def test_importing_main_does_not_import_the_heavy_modules():
    code = "import sys, jarvis.main; print('\\n'.join(sorted(sys.modules)))"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    loaded = proc.stdout.split()
    offenders = [m for m in loaded if m.startswith(HEAVY_PREFIXES) or m == "google"]
    assert offenders == []


def test_prompt_appears_in_under_a_second(home):
    """The chat start with an immediate Ctrl-D: health checks, greeting, prompt, goodbye. The
    heavy imports run in the warm-up thread and are never joined here."""
    env = {**os.environ, "JARVIS_HOME": str(home), "ANTHROPIC_API_KEY": "sk-test"}
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "jarvis.main"], cwd="/tmp", env=env, input="", capture_output=True, text=True, timeout=60)
    elapsed = time.perf_counter() - started
    assert "dinliyorum" in proc.stdout, proc.stdout + proc.stderr
    assert elapsed < 1.5, f"startup took {elapsed:.2f}s"


class ScriptedInput:
    def __init__(self, items: list[str | None]) -> None:
        self.items = list(items)

    def read(self) -> str | None:
        return self.items.pop(0) if self.items else None


@pytest.fixture
def quiet_start(monkeypatch):
    """No Keychain, no Health inbox: the chat's startup checks become free."""
    monkeypatch.setattr(health, "run_checks", lambda config, paths, **kw: [])
    monkeypatch.setattr(main_mod, "startup_health_sync", lambda config, paths: None)


def test_chat_exits_without_building_the_assistant(config, paths, quiet_start):
    console = Console(file=StringIO(), width=200, force_terminal=False)
    built: list[int] = []
    rc = cmd_chat(config, paths, console, debug=False, source=ScriptedInput([None]), assistant_factory=lambda: built.append(1))
    out = console.file.getvalue()
    assert rc == 0 and built == []
    assert "dinliyorum" in out and "Görüşürüz" in out
    assert "tools:" not in out and "session" not in out  # one greeting line, no banner


def test_chat_builds_the_assistant_on_the_first_message_only(config, paths, quiet_start):
    console = Console(file=StringIO(), width=200, force_terminal=False)
    handled: list[str] = []
    closed: list[int] = []
    fake = SimpleNamespace(
        session_id="abc123",
        registry=SimpleNamespace(names=lambda: ["get_time"]),
        handle=lambda text, sink: (
            handled.append(text)
            or SimpleNamespace(
                turn_id="t1",
                total_ms=1.0,
                timings={},
                compacted=False,
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0),
            )
        ),
        close=lambda: closed.append(1),
    )
    builds: list[int] = []

    def factory():
        builds.append(1)
        return fake

    rc = cmd_chat(config, paths, console, debug=True, source=ScriptedInput(["", "merhaba", "tekrar", None]), assistant_factory=factory)
    assert rc == 0 and builds == [1] and handled == ["merhaba", "tekrar"] and closed == [1]
    out = console.file.getvalue()
    assert "prompt ready in" in out and "session abc123" in out and "get_time" in out


def test_greeting_line_uses_time_language_and_name(config, paths):
    morning, noon, night = datetime(2026, 10, 14, 8), datetime(2026, 10, 14, 15), datetime(2026, 10, 14, 22)
    assert greeting_line(config, paths, now=morning) == "Günaydın, dinliyorum."  # no profile yet
    paths.profile_file.write_text("# Profile\n\n- Name: Kerem Yılmaz\n- Address me as: Kerem\n", encoding="utf-8")
    assert greeting_line(config, paths, now=morning) == "Günaydın Kerem, dinliyorum."
    assert greeting_line(config, paths, now=noon) == "İyi günler Kerem, dinliyorum."
    assert greeting_line(config, paths, now=night) == "İyi akşamlar Kerem, dinliyorum."
    config.general.language = "en"
    assert greeting_line(config, paths, now=night) == "Good evening Kerem, I'm listening."
    assert "\n" not in greeting_line(config, paths)


# --- 12.4 shell completion ------------------------------------------------------------------


@pytest.mark.parametrize("shell", ["zsh", "bash"])
def test_completion_script_is_generated_from_the_parser(shell, capsys):
    script = completion_script(build_parser(), shell)
    for name in ("services", "reinstall", "list-voices", "set-elevenlabs-key", "completion", "doctor", "login"):
        assert name in script
    assert main(["completion", shell]) == 0 and capsys.readouterr().out.strip() == script.strip()
    binary = shutil.which(shell)
    if binary:
        proc = subprocess.run([binary, "-n", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr


# --- 12.3 the binary the agents run -------------------------------------------------------


@pytest.fixture
def global_exe(tmp_path, monkeypatch) -> Path:
    """A fake `~/.local/bin/jarvis` as `uv tool install` creates it, with its own interpreter."""
    exe = tmp_path / "bin" / "jarvis"
    python = tmp_path / "tools" / "jarvis" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    exe.parent.mkdir()
    exe.write_text(f"#!{python}\nfrom jarvis.main import main\n")
    exe.chmod(0o755)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(exe.parent))
    return exe


def test_tool_bin_dir_order(monkeypatch, tmp_path):
    for var in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert services.tool_bin_dir() == Path.home() / ".local" / "bin"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert services.tool_bin_dir() == tmp_path / "bin"
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "xdg_bin"))
    assert services.tool_bin_dir() == tmp_path / "xdg_bin"
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "uv_bin"))
    assert services.tool_bin_dir() == tmp_path / "uv_bin"


def test_every_plist_runs_the_absolute_global_binary(paths, global_exe):
    for agent in services.AGENTS:
        module = importlib.import_module(agent.module)
        data = module.build_plist(paths)
        program = data["ProgramArguments"][0]
        assert program == str(global_exe) and Path(program).is_absolute()
        assert data["ProgramArguments"][1:] == list(agent.command)
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            assert Path(data[key]).is_absolute(), key
        assert Path(data["EnvironmentVariables"]["JARVIS_HOME"]).is_absolute()
        assert data["EnvironmentVariables"]["JARVIS_HOME"] == str(paths.home.resolve())


def test_python_executable_follows_the_shebang(global_exe, tmp_path):
    from jarvis.voice import launchd as voice_launchd

    expected = (tmp_path / "tools" / "jarvis" / "bin" / "python").resolve()
    assert services.python_executable_for(global_exe) == expected
    assert voice_launchd.python_executable() == expected
    assert services.python_executable_for(tmp_path / "missing") == Path(sys.executable).resolve()


@pytest.fixture
def fake_agents(tmp_path, monkeypatch):
    """Every launchd module writes into a temp LaunchAgents folder and talks to a fake launchctl."""
    folder = tmp_path / "LaunchAgents"
    fakes = {}
    for agent in services.AGENTS:
        module = importlib.import_module(agent.module)
        target = folder / f"{module.LABEL}.plist"
        monkeypatch.setattr(module, "plist_path", lambda target=target: target)
        monkeypatch.setattr(module, "_uid", lambda: 501)
        fake = FakeLaunchctl({"bootout": (3, "", "not loaded"), "print": (0, "state = running\npid = 42\n", "")})
        if agent.name == "voice":  # its install verifies with `print`: nothing loaded until bootstrap ran
            fake = StatefulLaunchctl()
            monkeypatch.setattr(module, "_sleep", lambda s: None)
        monkeypatch.setattr(module, "_run_launchctl", fake)
        fakes[agent.name] = (module, target, fake)
    return fakes


def _write_old_plist(target: Path, program: str, command: tuple[str, ...]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        plistlib.dump({"Label": target.stem, "ProgramArguments": [program, *command]}, fh)


def test_reinstall_rewrites_only_the_installed_agents(paths, global_exe, fake_agents, tmp_path):
    old = str(tmp_path / "old_venv" / "bin" / "jarvis")
    _write_old_plist(fake_agents["scheduler"][1], old, ("scheduler", "run"))
    _write_old_plist(fake_agents["voice"][1], old, ("voice", "run"))
    lines: list[str] = []
    done = services.reinstall_all(paths, lines.append)
    assert [name for name, _ in done] == ["scheduler", "voice"]
    for name in ("scheduler", "voice"):
        module, target, fake = fake_agents[name]
        data = plistlib.loads(target.read_bytes())
        assert data["ProgramArguments"][0] == str(global_exe)
        expected = ["bootout", "print", "bootstrap", "print"] if name == "voice" else ["bootout", "bootstrap"]
        assert [c[0] for c in fake.calls] == expected
    assert not fake_agents["telegram"][1].exists() and fake_agents["telegram"][2].calls == []
    assert any("telegram: not installed, skipped" in line for line in lines)
    assert lines[0] == f"agents will run {global_exe}"


def test_reinstall_with_nothing_installed_says_so(paths, global_exe, fake_agents):
    lines: list[str] = []
    assert services.reinstall_all(paths, lines.append) == []
    assert any("no agents installed" in line for line in lines)


def test_service_reports_flag_a_stale_program(paths, global_exe, fake_agents, tmp_path):
    gone = tmp_path / "gone" / "bin" / "jarvis"
    _write_old_plist(fake_agents["scheduler"][1], str(gone), ("scheduler", "run"))
    _write_old_plist(fake_agents["telegram"][1], str(global_exe), ("telegram", "run"))
    reports = {r.name: r for r in services.service_reports()}
    assert reports["scheduler"].installed and reports["scheduler"].running and reports["scheduler"].stale
    assert "missing" in reports["scheduler"].detail and "jarvis services reinstall" in reports["scheduler"].detail
    assert reports["telegram"].installed and not reports["telegram"].stale and str(global_exe) in reports["telegram"].detail
    assert not reports["voice"].installed and not reports["voice"].stale
    text = services.format_status(list(reports.values()))
    assert text.startswith(f"jarvis command: {global_exe}") and "scheduler" in text and "not installed" in text


def test_service_reports_never_raise(global_exe, fake_agents, monkeypatch):
    module, target, _ = fake_agents["voice"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not a plist")
    monkeypatch.setattr(module, "status", lambda: (_ for _ in ()).throw(OSError("boom")))
    reports = {r.name: r for r in services.service_reports()}
    assert "status unknown" in reports["voice"].detail and not reports["voice"].stale


def test_doctor_lines_for_the_command_and_the_agents(config, paths, global_exe, fake_agents, tmp_path, monkeypatch):
    monkeypatch.setattr(services, "on_path", lambda exe: True)
    _write_old_plist(fake_agents["scheduler"][1], str(tmp_path / "gone" / "jarvis"), ("scheduler", "run"))
    checks = {c.name: c for c in services.health_checks(config, paths)}
    assert checks["jarvis command"].ok and checks["jarvis command"].detail == str(global_exe)
    assert not checks["launchd: scheduler"].ok and "reinstall" in checks["launchd: scheduler"].detail
    assert checks["launchd: telegram"].ok and checks["launchd: telegram"].detail == "not installed"

    monkeypatch.setattr(services, "on_path", lambda exe: False)
    checks = {c.name: c for c in services.health_checks(config, paths)}
    assert not checks["jarvis command"].ok and "uv tool update-shell" in checks["jarvis command"].detail

    monkeypatch.setattr(services, "global_executable", lambda: None)
    checks = {c.name: c for c in services.health_checks(config, paths)}
    command = checks["jarvis command"]
    assert command.ok and command.warn and "uv tool install --editable" in command.detail
    assert "jarvis.services" in health.INTEGRATION_MODULES


def test_services_cli(paths, config, global_exe, fake_agents, capsys, monkeypatch):
    monkeypatch.setattr(main_mod, "bootstrap", lambda **kw: (config, paths))
    _write_old_plist(fake_agents["scheduler"][1], str(global_exe), ("scheduler", "run"))
    assert main(["services", "status"]) == 0
    out = capsys.readouterr().out
    assert "scheduler" in out and "running" in out
    assert main(["services", "reinstall"]) == 0
    assert "scheduler: reinstalled" in capsys.readouterr().out
