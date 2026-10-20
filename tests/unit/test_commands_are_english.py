"""Commands are the same in every language: `jarvis doctor` stays `jarvis doctor` and `/location` stays
`/location` whatever `general.language` says. Only the messages are translated. These snapshots fail when a
command is renamed, translated or added, so the change is deliberate (update the list and the docs)."""

from __future__ import annotations

import argparse
import re

import pytest

from jarvis.interfaces import telegram_bot
from jarvis.main import build_parser

CLI_COMMANDS = """
chat setup uninstall doctor stats usage export sessions
memory | memory forget
google | google login | google logout
health | health sync | health import
scheduler | scheduler run | scheduler install | scheduler uninstall | scheduler status
alarms | alarms list | alarms cancel | alarms stop | alarms snooze | alarms test
brief | brief schedule | brief schedule on | brief schedule off
telegram | telegram run | telegram install | telegram uninstall | telegram status | telegram set-token | telegram whoami | telegram enable
voice | voice run | voice install | voice uninstall | voice status | voice pause | voice resume | voice calibrate
voice debug-record | voice test-stt | voice list-voices | voice set-elevenlabs-key | voice say
services | services reinstall | services status
dashboard
shortcuts | shortcuts list
network | network test | network history
presence | presence setup | presence status | presence scan
calendar | calendar filters
location | location show | location refresh
completion | completion zsh | completion bash
key | key set | key delete
"""

TELEGRAM_COMMANDS = {
    "start",
    "stop",
    "help",
    "new",
    "mute",
    "unmute",
    "location",
    "status",
    "lock",
    "sleep",
    "stop_alarm",
    "snooze",
    "brief",
}

ENGLISH_NAME = re.compile(r"^-{0,2}[a-z][a-z0-9_-]*$")


def _expected_cli() -> set[str]:
    paths: set[str] = set()
    for line in CLI_COMMANDS.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 1:
            paths.update(parts[0].split())
        else:
            paths.update(parts)
    return paths


def _walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> tuple[set[str], set[str]]:
    """(command paths, every option string and option choice) of the parser tree."""
    commands: set[str] = set()
    names: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                commands.add(" ".join((*prefix, name)))
                sub_commands, sub_names = _walk(sub, (*prefix, name))
                commands |= sub_commands
                names |= sub_names
        elif action.option_strings:
            names.update(action.option_strings)
            names.update(str(choice) for choice in action.choices or ())
        elif action.choices:
            commands.update(" ".join((*prefix, str(choice))) for choice in action.choices)
    return commands, names


def test_the_cli_commands_are_the_fixed_english_set():
    commands, _ = _walk(build_parser())
    assert commands == _expected_cli()


@pytest.mark.parametrize("language", ["en", "tr"])
def test_every_cli_name_is_plain_english_ascii_in_every_language(language, monkeypatch):
    from jarvis import i18n

    i18n.set_language(language)
    commands, options = _walk(build_parser())
    words = {word for command in commands for word in command.split()} | options
    assert all(ENGLISH_NAME.match(word) for word in words), sorted(w for w in words if not ENGLISH_NAME.match(w))


def test_the_telegram_commands_are_the_fixed_english_set():
    assert set(telegram_bot.COMMANDS) == TELEGRAM_COMMANDS
    assert telegram_bot.LOCATION_COMMANDS == ("location",)


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_telegram_help_names_only_real_commands(language):
    from jarvis import i18n

    text = i18n.t("telegram.help.text", language, team="X")
    named = set(re.findall(r"(?<![\w/])/([a-z_]+)\b", text))
    assert named and named <= TELEGRAM_COMMANDS, named - TELEGRAM_COMMANDS
