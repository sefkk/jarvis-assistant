"""Apple Shortcuts bridge: the tools over a fake `shortcuts` CLI, the config
section, the doctor lines and `jarvis shortcuts list`. The real CLI is never called."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis.config import ShortcutEntry, load_config
from jarvis.exceptions import ConfigError, ToolExecutionError, ToolValidationError
from jarvis.tools import shortcuts
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.shortcuts import ListShortcuts, RunShortcut, RunShortcutInput, health_checks, overview_lines

INSTALLED = ["Focus On", "Add to Notes", "Read Page", "Lights Off", "Pegasus Check-in"]


class FakeCli:
    """Stands in for `run_command`: records argv, answers `list`, and for `run` reads the input
    file, writes `output` to the output file and exits with `returncode`."""

    def __init__(self, installed=None, *, output: str | bytes | None = None, returncode: int = 0, stderr: str = ""):
        self.installed = list(INSTALLED if installed is None else installed)
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.input_seen: str | None = None
        self.paths_seen: list[Path] = []

    def __call__(self, argv, *, timeout):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        assert argv[0] == "shortcuts"
        if argv[1] == "list":
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.installed) + "\n", "")
        assert argv[1] == "run"
        opts = dict(zip(argv[3::2], argv[4::2]))
        if "--input-path" in opts:
            path = Path(opts["--input-path"])
            self.paths_seen.append(path)
            self.input_seen = path.read_text(encoding="utf-8")
        out = Path(opts["--output-path"])
        self.paths_seen.append(out)
        if self.output is not None:
            out.write_bytes(self.output if isinstance(self.output, bytes) else self.output.encode("utf-8"))
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


@pytest.fixture
def cfg(config):
    # A fixed allowlist, independent of whatever the repo's config.toml currently lists.
    config.shortcuts.allowed[:] = [
        ShortcutEntry(name="Focus On", description="Turns on the Work focus mode", risk="safe"),
        ShortcutEntry(name="Add to Notes", description="Adds the given text to Notes", risk="confirm"),
        ShortcutEntry(name="Read Page", description="Reads a web page", risk="safe", taints=True),
        ShortcutEntry(name="Lights Off", description="Turns the living room lights off", risk="safe", remote=True),
    ]
    return config


@pytest.fixture
def ctx(cfg, paths):
    context = ToolContext.build(cfg, paths)
    context.runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None)
    return context


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCli()
    monkeypatch.setattr(shortcuts, "run_command", fake)
    return fake


# --- list_shortcuts ------------------------------------------------------------------------------


def test_list_shows_allowed_entries_with_risk_and_description(ctx, cli):
    text = ListShortcuts(ctx).run(ListShortcuts.input_model()).content
    assert "- Focus On [safe]: Turns on the Work focus mode" in text
    assert "- Add to Notes [confirm]: Adds the given text to Notes" in text
    assert "- Read Page [safe, output is untrusted]" in text
    assert "- Lights Off [safe, remote]" in text
    assert "MISSING" not in text and "Pegasus" not in text  # only the allowlist, never every Shortcut
    assert cli.calls == [["shortcuts", "list"]]


def test_list_marks_an_allowed_shortcut_that_is_missing_on_this_mac(ctx, monkeypatch):
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(installed=["Focus On"]))
    text = ListShortcuts(ctx).run(ListShortcuts.input_model()).content
    assert "- Focus On [safe]: Turns on the Work focus mode\n" in text
    assert "- Add to Notes [confirm]: Adds the given text to Notes (MISSING" in text


def test_list_still_answers_when_the_cli_fails(ctx, monkeypatch):
    def broken(argv, *, timeout):
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr(shortcuts, "run_command", broken)
    text = ListShortcuts(ctx).run(ListShortcuts.input_model()).content
    assert "- Focus On [safe]" in text and "could not check which Shortcuts exist" in text and "boom" in text


def test_list_with_empty_allowlist(config, paths):
    config.shortcuts.allowed.clear()
    text = ListShortcuts(ToolContext.build(config, paths)).run(ListShortcuts.input_model()).content
    assert "No Shortcuts are allowlisted" in text


# --- run_shortcut -------------------------------------------------------------------------------


def test_run_without_input_uses_argv_and_returns_done(ctx, cli):
    result = RunShortcut(ctx).run(RunShortcutInput(name="Focus On"))
    assert result.content == "done" and not result.is_error
    assert cli.calls[0] == ["shortcuts", "list"]
    run_argv = cli.calls[1]
    assert run_argv[:3] == ["shortcuts", "run", "Focus On"]
    assert "--output-path" in run_argv and "--input-path" not in run_argv
    assert cli.timeouts[1] == float(ctx.config.shortcuts.timeout_seconds)
    assert not any(p.exists() for p in cli.paths_seen) and not cli.paths_seen[0].parent.exists()  # temp dir gone


def test_run_with_input_passes_a_temp_file_and_reads_the_output_file(ctx, monkeypatch):
    fake = FakeCli(output="Added to Notes: buy milk\n")
    monkeypatch.setattr(shortcuts, "run_command", fake)
    result = RunShortcut(ctx).run(RunShortcutInput(name="Add to Notes", input_text="buy milk\nand eggs"))
    assert result.content == "Added to Notes: buy milk"
    assert fake.input_seen == "buy milk\nand eggs"
    run_argv = fake.calls[1]
    assert run_argv[:3] == ["shortcuts", "run", "Add to Notes"]
    assert "--input-path" in run_argv and "--output-path" in run_argv
    assert not any(p.exists() for p in fake.paths_seen)  # input and output files deleted afterwards


def test_run_falls_back_to_stdout_when_no_output_file(ctx, monkeypatch):
    def fake(argv, *, timeout):
        if argv[1] == "list":
            return subprocess.CompletedProcess(argv, 0, "Focus On\n", "")
        return subprocess.CompletedProcess(argv, 0, "printed\n", "")

    monkeypatch.setattr(shortcuts, "run_command", fake)
    assert RunShortcut(ctx).run(RunShortcutInput(name="Focus On")).content == "printed"


def test_run_timeout_is_a_tool_error_and_cleans_up(ctx, monkeypatch, tmp_path):
    seen: list[Path] = []

    def slow(argv, **kw):
        if argv[1] == "list":
            return subprocess.CompletedProcess(argv, 0, "Focus On\n", "")
        seen.append(Path(argv[argv.index("--output-path") + 1]).parent)
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(shortcuts.subprocess, "run", slow)
    with pytest.raises(ToolExecutionError, match="timed out after 30s"):
        RunShortcut(ctx).run(RunShortcutInput(name="Focus On"))
    assert seen and not seen[0].exists()


def test_run_nonzero_exit_reports_stderr(ctx, monkeypatch):
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(returncode=1, stderr="The operation couldn't be completed."))
    with pytest.raises(ToolExecutionError, match=r"'Focus On' failed \(exit 1\): The operation couldn't be completed"):
        RunShortcut(ctx).run(RunShortcutInput(name="Focus On"))


def test_run_reports_a_missing_shortcut_instead_of_running(ctx, monkeypatch):
    fake = FakeCli(installed=["Pegasus Check-in"])
    monkeypatch.setattr(shortcuts, "run_command", fake)
    with pytest.raises(ToolExecutionError, match="'Focus On' is in the allowlist but not in the Shortcuts app"):
        RunShortcut(ctx).run(RunShortcutInput(name="Focus On"))
    assert cli_runs(fake) == []  # `shortcuts run` never happened


def test_run_cli_missing_is_a_clear_error(ctx, monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(shortcuts.subprocess, "run", missing)
    with pytest.raises(ToolExecutionError, match="not available on this Mac"):
        RunShortcut(ctx).run(RunShortcutInput(name="Focus On"))


def test_run_matches_the_name_case_insensitively_but_runs_the_configured_name(ctx, cli):
    RunShortcut(ctx).run(RunShortcutInput(name="focus  on"))
    assert cli.calls[1][2] == "Focus On"


def test_output_is_truncated_to_the_configured_cap(ctx, monkeypatch):
    ctx.config.context.tool_result_max_chars = 100
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(output="x" * 500))
    content = RunShortcut(ctx).run(RunShortcutInput(name="Read Page")).content
    assert content.startswith("x" * 100) and "truncated to 100 of 500 characters" in content


def test_binary_output_is_described_not_decoded(ctx, monkeypatch):
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(output=b"\xff\xfe\x00\x01PNG"))
    assert "7 bytes of non-text output" in RunShortcut(ctx).run(RunShortcutInput(name="Focus On")).content


def test_input_too_long_is_a_validation_error(ctx, cli):
    with pytest.raises(ToolValidationError, match="input_text is longer"):
        RunShortcut(ctx).run(RunShortcutInput(name="Add to Notes", input_text="x" * (shortcuts.INPUT_MAX_CHARS + 1)))
    assert cli.calls == []


def test_run_command_only_runs_the_shortcuts_cli():
    with pytest.raises(ValueError):
        shortcuts.run_command(["osascript", "-e", "1"], timeout=1)


def cli_runs(fake: FakeCli) -> list[list[str]]:
    return [c for c in fake.calls if c[1] == "run"]


# --- per-call risk and taint hooks --------------------------------------------------------------


def test_risk_and_taint_follow_the_allowlist_entry(ctx):
    tool = RunShortcut(ctx)
    assert tool.risk == RiskLevel.SAFE and tool.taints is False  # class level, overridden per call
    assert tool.risk_for(RunShortcutInput(name="Focus On")) == RiskLevel.SAFE
    assert tool.risk_for(RunShortcutInput(name="Add to Notes")) == RiskLevel.CONFIRM
    assert tool.risk_for(RunShortcutInput(name="Nope")) == RiskLevel.SAFE  # blocked in run(), never prompted
    assert tool.taints_for(RunShortcutInput(name="Read Page")) is True
    assert tool.taints_for(RunShortcutInput(name="Focus On")) is False
    assert tool.taints_for(RunShortcutInput(name="Nope")) is False
    assert tool.delegate_ok is False and ListShortcuts.delegate_ok is False


def test_confirm_preview_shows_name_description_and_exact_input(ctx):
    preview = RunShortcut(ctx).confirm_preview(RunShortcutInput(name="Add to Notes", input_text="call mum"))
    assert preview == "Shortcut: Add to Notes (confirm) - Adds the given text to Notes\nInput: call mum"
    assert RunShortcut(ctx).confirm_preview(RunShortcutInput(name="Focus On")).endswith("Input: (none)")
    assert RunShortcut(ctx).confirm_preview(RunShortcutInput(name="Nope")) is None


def test_tools_are_hidden_while_the_allowlist_is_empty(config, paths):
    assert {"list_shortcuts", "run_shortcut"} <= set(ToolRegistry.discover(ToolContext.build(config, paths)).names())
    config.shortcuts.allowed.clear()
    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    assert "run_shortcut" not in registry.names() and "list_shortcuts" not in registry.names()
    assert registry.hidden["run_shortcut"] == "shortcuts"
    assert not registry.unmatched_patterns(["shortcuts.*"])  # hidden, not a typo in remote_allowed_tools


# --- config -------------------------------------------------------------------------------------


def write_config(paths, extra: str):
    base = paths.config_file.read_text()
    head = base.split("[shortcuts]")[0]
    paths.config_file.write_text(head + "[shortcuts]\ntimeout_seconds = 5\n" + extra)
    return load_config(paths.config_file)


def test_config_defaults_and_lookup(paths):
    cfg = write_config(paths, '[[shortcuts.allowed]]\nname = " Focus  On "\ndescription = "Work   focus"\n')
    entry = cfg.shortcuts.allowed[0]
    assert (entry.name, entry.description, entry.risk, entry.remote, entry.taints) == ("Focus  On", "Work focus", "confirm", False, False)
    assert cfg.shortcuts.timeout_seconds == 5
    assert cfg.shortcuts.entry("Focus  On") is entry and cfg.shortcuts.entry("focus on") is entry
    assert cfg.shortcuts.entry("Focus") is None and cfg.shortcuts.entry("") is None


def test_config_rejects_duplicate_names(paths):
    with pytest.raises(ConfigError, match="duplicate name 'focus on'"):
        write_config(
            paths,
            '[[shortcuts.allowed]]\nname = "Focus On"\ndescription = "a"\n[[shortcuts.allowed]]\nname = "focus on"\ndescription = "b"\n',
        )


@pytest.mark.parametrize(
    "entry, message",
    [
        ('name = "-x"\ndescription = "a"\n', "must not start with '-'"),
        ('name = ""\ndescription = "a"\n', "name"),
        ('name = "Focus On"\n', "description"),
        ('name = "Focus On"\ndescription = "a"\nrisk = "blocked"\n', "risk"),
        ('name = "Focus On"\ndescription = "a"\nremote = "maybe"\n', "remote"),
        ('name = "Focus On"\ndescription = "a"\nextra = 1\n', "extra"),
    ],
)
def test_config_rejects_bad_entries(paths, entry, message):
    with pytest.raises(ConfigError, match=message):
        write_config(paths, "[[shortcuts.allowed]]\n" + entry)


def test_repo_config_has_the_dnd_entries_and_remote_pattern(config):
    names = [e.name for e in config.shortcuts.allowed]
    assert "Rahatsız Etme Aç" in names and "Rahatsız Etme Kapat" in names
    assert len(names) == len(set(names))
    assert "shortcuts.*" in config.telegram.remote_allowed_tools


# --- doctor and `jarvis shortcuts list` -----------------------------------------------------------


def test_health_checks_report_cli_entries_and_missing(cfg, paths, monkeypatch):
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(installed=["Focus On", "Lights Off", "Other"]))
    checks = {c.name: c for c in health_checks(cfg, paths)}
    assert checks["Shortcuts CLI"].ok and "3 Shortcut(s) on this Mac, 4 allowlisted" in checks["Shortcuts CLI"].detail
    assert checks["Shortcut 'Focus On'"].ok and checks["Shortcut 'Focus On'"].detail == "safe"
    assert checks["Shortcut 'Lights Off'"].detail == "safe, remote"
    assert not checks["Shortcut 'Add to Notes'"].ok and "missing" in checks["Shortcut 'Add to Notes'"].detail
    assert not checks["Shortcut 'Read Page'"].ok


def test_health_checks_without_cli_or_entries_never_raise(config, paths, monkeypatch):
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: None)
    checks = health_checks(config, paths)
    assert len(checks) == 1 and not checks[0].ok and "not found" in checks[0].detail

    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: "/usr/bin/shortcuts")
    config.shortcuts.allowed.clear()
    checks = health_checks(config, paths)
    assert len(checks) == 1 and checks[0].ok and "optional, empty" in checks[0].detail

    config.shortcuts.allowed.append(ShortcutEntry(name="Focus On", description="x"))

    def broken(argv, **kw):
        raise ToolExecutionError("`shortcuts list` failed: no")

    monkeypatch.setattr(shortcuts, "run_command", broken)
    checks = health_checks(config, paths)
    assert len(checks) == 1 and not checks[0].ok and "`shortcuts list` failed" in checks[0].detail


def test_doctor_includes_the_shortcuts_module(cfg, paths, monkeypatch):
    from jarvis.observability import health

    monkeypatch.setattr(shortcuts, "run_command", FakeCli())
    assert "jarvis.tools.shortcuts" in health.INTEGRATION_MODULES
    names = [c.name for c in health.integration_checks(cfg, paths)]
    assert "Shortcuts CLI" in names and "Shortcut 'Focus On'" in names


def test_overview_marks_allowlisted_and_missing(cfg, monkeypatch):
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(installed=["Pegasus Check-in", "Focus On", "lights off"]))
    from jarvis import i18n

    i18n.set_language("en")
    lines = overview_lines(cfg)
    assert lines[0].startswith("Shortcuts on this Mac (3)")
    assert "    Pegasus Check-in" in lines
    assert "  ✓ Focus On  [safe]  Turns on the Work focus mode" in lines
    assert "  ✓ lights off  [safe, remote]  Turns the living room lights off" in lines
    assert "  ✗ Add to Notes" in lines and "  ✗ Read Page" in lines
    assert lines[-1].startswith("To allow a Shortcut")
    i18n.set_language("tr")
    lines = overview_lines(cfg)
    assert lines[0].startswith("Bu Mac'teki Kestirmeler (3)") and lines[-1].startswith("Bir Kestirmeye izin vermek için")


def test_cli_shortcuts_list(cfg, paths, monkeypatch, capsys):
    from jarvis import main as main_module

    monkeypatch.setattr(main_module, "bootstrap", lambda **kw: (cfg, paths))
    monkeypatch.setattr(shortcuts, "run_command", FakeCli(installed=["Focus On", "Other"]))
    assert main_module.main(["shortcuts", "list"]) == 0
    out = capsys.readouterr().out
    assert "✓ Focus On" in out and "Other" in out and "✗ Add to Notes" in out
    assert main_module.completion_script(main_module.build_parser(), "zsh").count("shortcuts") >= 1
