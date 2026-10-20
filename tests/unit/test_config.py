import os

import pytest

from jarvis.config import load_config, load_dotenv
from jarvis.exceptions import ConfigError


def test_repo_config_loads(config):
    assert config.models.main_model == "claude-sonnet-5-5"
    assert config.loop.max_tool_iterations == 10
    assert config.security.allowed_file_roots[0] == "~/Documents"
    assert "claude-sonnet-5-5" in config.budget.prices and "claude-haiku-5-5" in config.budget.prices


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_is_config_error(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text("[general\nlog_level = 'DEBUG'\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)


def test_bad_value_reports_field_path(paths):
    text = paths.config_file.read_text().replace("max_tool_iterations = 10", "max_tool_iterations = 0")
    paths.config_file.write_text(text)
    with pytest.raises(ConfigError) as exc:
        load_config(paths.config_file)
    assert "loop.max_tool_iterations" in str(exc.value)


def test_effort_defaults_and_validation(config, paths):
    assert config.models.main_effort == "medium" and config.models.delegate_effort == "high"
    text = paths.config_file.read_text().replace('main_effort = "medium"', 'main_effort = "max"')
    paths.config_file.write_text(text)
    with pytest.raises(ConfigError, match="main_thinking = false requires main_effort"):
        load_config(paths.config_file)
    paths.config_file.write_text(text.replace("main_thinking = false", "main_thinking = true"))
    assert load_config(paths.config_file).models.main_effort == "max"  # fine once thinking is on
    paths.config_file.write_text(text.replace('main_effort = "max"', 'main_effort = "turbo"'))
    with pytest.raises(ConfigError, match="main_effort"):
        load_config(paths.config_file)


def test_unknown_key_is_rejected(paths):
    paths.config_file.write_text(paths.config_file.read_text() + "\n[general]\nmystery = 1\n")
    with pytest.raises(ConfigError):
        load_config(paths.config_file)


def test_dotenv_does_not_override_existing_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nJARVIS_TEST_A="from-file"\nJARVIS_TEST_B=keep\n')
    monkeypatch.setenv("JARVIS_TEST_B", "from-env")
    monkeypatch.delenv("JARVIS_TEST_A", raising=False)
    load_dotenv(env)
    assert os.environ["JARVIS_TEST_A"] == "from-file"
    assert os.environ["JARVIS_TEST_B"] == "from-env"
    monkeypatch.delenv("JARVIS_TEST_A")


def test_local_overlay_merges_over_config(paths):
    paths.local_config_file.write_text(
        '[general]\nlog_level = "DEBUG"\n\n[presence]\nhome_gateway_macs = ["AA:BB:CC:DD:EE:FF"]\n\n'
        '[[presence.people]]\nname = "Me"\ndevices = ["aa:bb:cc:dd:ee:01"]\nis_me = true\n'
    )
    cfg = load_config(paths.config_file)
    assert cfg.general.log_level == "DEBUG"
    assert cfg.loop.max_tool_iterations == 10  # untouched tables keep config.toml's values
    assert cfg.presence.scan_interval_s == 120  # sibling keys inside a merged table too
    assert cfg.presence.home_gateway_macs == ["aa:bb:cc:dd:ee:ff"] and cfg.presence.me().name == "Me"


def test_local_overlay_errors_name_the_file(paths):
    paths.local_config_file.write_text("[general\n")
    with pytest.raises(ConfigError, match="config.local.toml is not valid TOML"):
        load_config(paths.config_file)
    paths.local_config_file.write_text("[loop]\nmax_tool_iterations = 0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(paths.config_file)
    assert "config.local.toml" in str(exc.value) and "loop.max_tool_iterations" in str(exc.value)


def test_merge_config_data_replaces_arrays_and_merges_tables():
    from jarvis.config import merge_config_data

    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1, "people": [{"name": "old"}]}
    out = merge_config_data(base, {"a": {"y": [3]}, "people": [{"name": "new"}], "c": {"z": 0}})
    assert out == {"a": {"x": 1, "y": [3]}, "b": 1, "people": [{"name": "new"}], "c": {"z": 0}}
    assert base["a"] == {"x": 1, "y": [1, 2]}  # the inputs are not mutated


def test_persona_humor_level_defaults_and_bounds(config, paths):
    from pydantic import ValidationError

    from jarvis.config import ConfigError, PersonaConfig, load_config

    assert config.persona.humor_level == 2
    assert PersonaConfig(humor_level=0).humor_level == 0 and PersonaConfig(humor_level=3).humor_level == 3
    with pytest.raises(ValidationError):
        PersonaConfig(humor_level=4)
    text = paths.config_file.read_text(encoding="utf-8").replace("humor_level = 2", "humor_level = -1")
    assert text != paths.config_file.read_text(encoding="utf-8")
    paths.config_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="persona.humor_level"):
        load_config(paths.config_file)
