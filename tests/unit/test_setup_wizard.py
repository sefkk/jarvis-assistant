"""`jarvis setup`: a fresh install gets every file with the right content and permissions, a second run
changes nothing, an existing config only gains the keys it lacks (after a backup), and other platforms stop."""

from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path

import pytest
from rich.console import Console

from jarvis import i18n
from jarvis.config import Paths, load_config
from jarvis.setup_wizard import SECRET_NAMES, fill_missing_keys, run_setup
from jarvis.utils import platform_check, secrets

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_LINES = ("five-a-side", "Nina Simone", "nina simon")


@pytest.fixture(autouse=True)
def _keychain_untouched(monkeypatch):
    stored: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get_secret", lambda name: stored.get(name))
    monkeypatch.setattr(secrets, "set_secret", lambda name, value: stored.__setitem__(name, value))
    before = i18n.current_language()
    yield stored
    i18n.set_language(before)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "home"
    target.mkdir()
    (target / "config.example.toml").write_text((REPO_ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _console() -> Console:
    return Console(record=True, width=200)


def _answers(language: str) -> dict:
    return {"language": language, "name": "Ada Example", "address_as": "Ada", "home_location": "Lisbon, Portugal"}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("language", ["en", "tr"])
def test_fresh_setup_creates_every_file_with_content_and_permissions(home, language):
    assert run_setup(home, _console(), answers=_answers(language)) == 0
    paths = Paths(home)
    config = load_config(paths.config_file)
    assert config.general.language == language and config.voice.stt_language == language
    assert config.general.exit_words[:3] == ["exit", "quit", "bye"] and ("çıkış" in config.general.exit_words) == (language == "tr")
    assert config.weather.fixed_location == "Lisbon, Portugal"
    assert [f.calendar for f in config.calendar.filters] == [{"en": "Football", "tr": "Futbol"}[language]]
    assert config.football.team == "Fenerbahçe"
    assert not config.general.use_voice and not config.telegram.enabled and not config.brief.schedule.enabled
    profile = paths.profile_file.read_text(encoding="utf-8")
    assert "Ada Example" in profile and "Lisbon, Portugal" in profile
    assert ("## Preferences" if language == "en" else "## Tercihler") in profile
    for name in ("memory.md", "vocabulary.md", "corrections.md"):
        text = (paths.data_dir / name).read_text(encoding="utf-8")
        assert not any(sample in text for sample in SAMPLE_LINES), name
        assert all(not line.startswith("- ") for line in text.splitlines()), f"{name} must start empty"
    for folder in (paths.data_dir, paths.logs_dir, paths.backups_dir):
        assert _mode(folder) == 0o700, folder
    for file in (paths.config_file, paths.profile_file, paths.memory_file, paths.vocabulary_file, paths.corrections_file):
        assert _mode(file) == 0o600, file
    assert not paths.env_file.exists()
    assert paths.sounds_dir.is_dir()


def test_the_profile_template_parses_in_both_languages(home):
    from jarvis.profile import load_profile

    run_setup(home, _console(), answers=_answers("tr"))
    profile = load_profile(Paths(home).profile_file)
    assert profile.fields["name"] == "Ada Example" and profile.fields["location"] == "Lisbon, Portugal"
    assert profile.name == "Ada"


def test_running_setup_again_overwrites_and_duplicates_nothing(home):
    run_setup(home, _console(), answers=_answers("en"))
    paths = Paths(home)
    files = [paths.config_file, paths.profile_file, paths.memory_file, paths.vocabulary_file, paths.corrections_file]
    paths.memory_file.write_text(paths.memory_file.read_text(encoding="utf-8") + "- 2026-01-01: Likes tea.\n", encoding="utf-8")
    before = {f: f.read_text(encoding="utf-8") for f in files}
    other = {"language": "en", "name": "Somebody Else", "address_as": "X", "home_location": "Oslo"}
    assert run_setup(home, _console(), answers=other) == 0
    assert {f: f.read_text(encoding="utf-8") for f in files} == before
    assert not any(paths.backups_dir.iterdir()), "nothing changed, so nothing was backed up"


def test_an_existing_config_only_gains_missing_keys_after_a_backup(home):
    paths = Paths(home)
    paths.config_file.write_text(
        '# my own comment\n[general]\nlanguage = "tr"\nuse_voice = true  # keep me\n\n'
        '[models]\nmain_model = "claude-haiku-5-5"\ndelegate_model = "claude-sonnet-5-5"\n',
        encoding="utf-8",
    )
    original = paths.config_file.read_text(encoding="utf-8")
    assert run_setup(home, _console(), answers={"language": "tr"}) == 0
    text = paths.config_file.read_text(encoding="utf-8")
    assert text.startswith("# my own comment") and "use_voice = true  # keep me" in text
    config = load_config(paths.config_file)
    assert config.general.use_voice and config.models.main_model == "claude-haiku-5-5"
    assert config.security.allowed_file_roots  # added from the example
    backups = [p for p in paths.backups_dir.iterdir() if p.is_dir()]
    assert len(backups) == 1 and (backups[0] / "config.toml").read_text(encoding="utf-8") == original
    manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == ["config.toml"]


def test_fill_missing_keys_keeps_values_and_reports_the_added_keys():
    example = '[a]\nx = 1  # comment x\ny = 2\n\n[b]\nz = "new"\n'
    user = "[a]\nx = 5\n"
    text, added = fill_missing_keys(example, user)
    data = tomllib.loads(text)
    assert data == {"a": {"x": 5, "y": 2}, "b": {"z": "new"}}
    assert added == ["a.y", "b"]
    assert fill_missing_keys(example, text) == (text, [])


def test_language_only_switches_the_language_with_a_backup(home, monkeypatch):
    run_setup(home, _console(), answers=_answers("en"))
    monkeypatch.setattr("jarvis.setup_wizard.choose_language", lambda prompter, current: "tr")
    assert run_setup(home, _console(), language_only=True) == 0
    assert load_config(Paths(home).config_file).general.language == "tr"
    assert any(Paths(home).backups_dir.iterdir())


def test_setup_stops_on_an_unsupported_platform(home, monkeypatch):
    monkeypatch.setattr(platform_check, "unsupported_reason", lambda: "Jarvis runs on macOS only (this is Linux).")
    console = _console()
    assert run_setup(home, console, answers=_answers("en")) == 1
    shown = console.export_text()
    assert "macOS only" in shown and "Apple Silicon" in shown
    assert not (home / "config.toml").exists()


def test_platform_check(monkeypatch):
    monkeypatch.delenv(platform_check.OVERRIDE_ENV, raising=False)
    assert platform_check.unsupported_reason("Darwin", "arm64", "26.0") is None
    assert platform_check.unsupported_reason("Darwin", "arm64", "26.6.2") is None
    assert "macOS 26" in platform_check.unsupported_reason("Darwin", "arm64", "15.7")
    assert "Apple Silicon" in platform_check.unsupported_reason("Darwin", "x86_64", "26.0")
    assert "macOS only" in platform_check.unsupported_reason("Linux", "x86_64", "")
    monkeypatch.setenv(platform_check.OVERRIDE_ENV, "1")
    assert platform_check.unsupported_reason("Linux", "x86_64", "") is None


def test_the_keychain_names_cover_every_secret_the_code_reads():
    import re

    names = set()
    for path in (REPO_ROOT / "src" / "jarvis").rglob("*.py"):
        names |= set(re.findall(r'get_secret\(\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert names <= set(SECRET_NAMES), names - set(SECRET_NAMES)
