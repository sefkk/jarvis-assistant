"""The CLI exits on exact exit words (configurable) and Ctrl-D, with a goodbye line."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from jarvis.config import load_config
from jarvis.exceptions import ConfigError
from jarvis.io.text import TerminalInput, is_exit_word
from jarvis.main import goodbye_line


@pytest.mark.parametrize("text", ["exit", "quit", "bye", "BYE", "  Bye  ", "Quit\n"])
def test_exact_exit_words_exit(text):
    assert is_exit_word(text)


@pytest.mark.parametrize("text", ["bye mesajı yaz", "exit the room", "goodbye", "", "   ", "quit?", "say bye to Ali"])
def test_anything_else_does_not_exit(text):
    assert not is_exit_word(text)


def test_exit_words_come_from_config(config):
    assert config.general.exit_words == ["exit", "quit", "bye"]
    assert is_exit_word("hoşçakal", ["exit", "Hoşçakal"]) and not is_exit_word("bye", ["exit", "hoşçakal"])


def test_config_normalizes_and_validates_exit_words(paths):
    text = paths.config_file.read_text().replace('exit_words = ["exit", "quit", "bye"]', 'exit_words = [" Exit", "ÇIK"]')
    paths.config_file.write_text(text)
    assert load_config(paths.config_file).general.exit_words == ["exit", "çik"]
    paths.config_file.write_text(text.replace('[" Exit", "ÇIK"]', '["bye bye"]'))
    with pytest.raises(ConfigError, match="single words"):
        load_config(paths.config_file)
    paths.config_file.write_text(text.replace('[" Exit", "ÇIK"]', "[]"))
    with pytest.raises(ConfigError):
        load_config(paths.config_file)


def test_terminal_input_exit_and_ctrl_d(monkeypatch):
    console = Console(file=StringIO(), force_terminal=False)
    source = TerminalInput(console, ["exit", "bye"])
    answers = iter(["bye mesajı yaz", "  bye "])
    monkeypatch.setattr(console, "input", lambda prompt: next(answers))
    assert source.read() == "bye mesajı yaz"
    assert source.read() is None

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr(console, "input", eof)
    assert source.read() is None  # Ctrl-D


def test_goodbye_line_uses_language_and_profile_name(config, paths):
    assert goodbye_line(config, paths) == "Görüşürüz."  # no profile yet
    paths.profile_file.write_text("# Profile\n\n- Name: Kerem Yılmaz\n- Address me as: Kerem\n", encoding="utf-8")
    assert goodbye_line(config, paths) == "Görüşürüz, Kerem."
    config.general.language = "en"
    assert goodbye_line(config, paths) == "Bye, Kerem."
