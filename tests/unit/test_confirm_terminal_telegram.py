"""Confirmation reasons on the other channels: the terminal prompt accepts Turkish answers and
reports "declined"; the Telegram buttons report button / timeout."""

from __future__ import annotations

import pytest
from rich.console import Console

from jarvis.interfaces.telegram_bot import TelegramSink
from jarvis.io.base import ConfirmRequest
from jarvis.io.text import TerminalOutput, terminal_answer
from tests.unit.test_telegram_bot import CHAT, callback_update, make_bot


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("y", True),
        ("Y", True),
        ("yes", True),
        ("evet", True),
        ("Evet.", True),
        ("EVET", True),
        ("e", True),
        ("onaylıyorum", True),
        ("Onaylıyorum!", True),
        ("ONAYLIYORUM", True),
        ("tamam", True),
        ("ok", True),
        ("", False),
        ("n", False),
        ("no", False),
        ("hayır", False),
        ("Hayır", False),
        ("h", False),
        ("iptal", False),
        ("maybe", False),
        ("evet mi", False),
        ("yess", False),
        ("YES", True),
        ("CONFIRM", True),
        ("Okay!", True),
        ("NO", False),
        ("İPTAL", False),
    ],
)
def test_terminal_answer(answer, expected):
    assert terminal_answer(answer) is expected


def req():
    return ConfirmRequest(tool="create_reminder", args={"title": "x"}, risk="CONFIRM")


def test_terminal_confirm_accepts_evet_and_defaults_to_no(monkeypatch):
    answers = iter(["evet", "", "hayır", "onaylıyorum"])
    monkeypatch.setattr("jarvis.io.text.Prompt.ask", lambda *a, default=None, **k: next(answers) or (default or ""))
    sink = TerminalOutput(Console(file=open("/dev/null", "w"), force_terminal=False), language="tr")
    assert sink.confirm(req()) is True
    outcome = sink.confirm_detailed(req())
    assert (
        outcome.approved is False and outcome.reason == "declined" and outcome.user_message == "Terminalde onay vermedin; işlemi yapmadım."
    )
    assert sink.confirm(req()) is False
    assert sink.confirm_detailed(req()).approved is True


def test_terminal_confirm_in_english_accepts_both_languages(monkeypatch):
    asked: list[str] = []
    answers = iter(["yes", "evet", "no", ""])

    def ask(prompt, *a, default=None, **k):
        asked.append(prompt)
        return next(answers) or (default or "")

    monkeypatch.setattr("jarvis.io.text.Prompt.ask", ask)
    console = Console(record=True, width=200)
    sink = TerminalOutput(console, language="en")
    assert sink.confirm(req()) is True and sink.confirm(req()) is True and sink.confirm(req()) is False
    outcome = sink.confirm_detailed(req())
    assert outcome.user_message == "You did not confirm in the terminal; I did not do it."
    assert asked[0] == "Run it? [y/yes or n/no]" and "Confirm tool create_reminder" in console.export_text()
    assert sink.thinking_text == "Thinking…"


def test_terminal_confirm_ctrl_c_is_no(monkeypatch):
    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("jarvis.io.text.Prompt.ask", interrupt)
    sink = TerminalOutput(Console(file=open("/dev/null", "w"), force_terminal=False))
    assert sink.confirm_detailed(req()).reason == "declined"


def test_telegram_confirm_detailed_reports_button_and_timeout(config, paths, logs):
    bot, api = make_bot(config, paths)
    sink = TelegramSink(bot, chat_id=CHAT)
    api.batches.append(lambda a: [callback_update(a.last_action()[0], "y")])
    outcome = sink.confirm_detailed(req())
    assert outcome.approved and outcome.how == "button"

    api.batches.append(lambda a: [callback_update(a.last_action()[0], "n")])
    outcome = sink.confirm_detailed(req())
    assert not outcome.approved and outcome.reason == "declined" and "Reddet" in outcome.user_message

    outcome = sink.confirm_detailed(req())  # nobody presses anything: the fake API advances the clock until the deadline
    assert not outcome.approved and outcome.reason == "timeout" and "Süre doldu" in outcome.user_message
