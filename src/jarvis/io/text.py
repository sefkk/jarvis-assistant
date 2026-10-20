"""Terminal implementation of the I/O interfaces, built on `rich`.

Everything that reaches the terminal passes through `sanitize.for_terminal`, so escape
sequences hidden in an email or a web page cannot touch the terminal.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from rich.console import Console
from rich.prompt import Prompt

from jarvis.i18n import resolve, t, t_all
from jarvis.io.base import ConfirmOutcome, ConfirmRequest, InputSource, OutputSink
from jarvis.security.sanitize import for_terminal
from jarvis.utils.text import fold, fold_words

DEFAULT_EXIT_WORDS = ("exit", "quit", "bye")
# The terminal prompt: "y", "yes", "e" and "evet" approve in any language; Enter, "n", "no", "h", "hayır"
# and anything else deny. Folded, so "YES", "Evet" and "EVET" are alike.
TERMINAL_YES = frozenset(fold_words(t_all("confirm.terminal.yes")))
TERMINAL_NO = frozenset(fold_words(t_all("confirm.terminal.no"))) | {""}


def terminal_answer(text: str) -> bool:
    """True only for an explicit yes; a plain Enter or anything unknown is no."""
    return fold(text.strip()).rstrip(".!") in TERMINAL_YES


def thinking_text(language: str | None = None) -> str:
    """The text chat's counterpart of the voice thinking sound: a small spinner while nothing has been
    printed yet, removed by the first text (or notice, confirmation, error)."""
    return t("confirm.terminal.thinking", language)


def is_exit_word(text: str, exit_words=DEFAULT_EXIT_WORDS) -> bool:
    """True only when the whole input is exactly one exit word (case-insensitive, surrounding
    whitespace ignored): "bye" exits, "bye mesajı yaz" does not."""
    return text.strip().casefold() in {w.strip().casefold() for w in exit_words}


class TerminalInput(InputSource):
    def __init__(self, console: Console, exit_words=DEFAULT_EXIT_WORDS) -> None:
        self.console = console
        self.exit_words = tuple(exit_words)

    def read(self) -> str | None:
        try:
            text = self.console.input("[bold cyan]you ›[/] ")
        except (EOFError, KeyboardInterrupt):  # Ctrl-D (and Ctrl-C at the prompt) also exit
            self.console.print()
            return None
        if is_exit_word(text, self.exit_words):
            return None
        return text


class TerminalOutput(OutputSink):
    def __init__(
        self,
        console: Console,
        *,
        debug: bool = False,
        language: str | None = None,
        status_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.console = console
        self.debug_enabled = debug
        self.language = resolve(language)
        self.thinking_text = thinking_text(self.language)
        # `console.status(text)` by default; tests inject a fake. The object needs start()/stop().
        self._status_factory = status_factory or (lambda text: self.console.status(text, spinner="dots"))
        self._status: Any | None = None
        self._wrote_text = False

    def begin_turn(self) -> None:
        """Called before the Assistant starts on a message: show the spinner until output arrives."""
        self._stop_spinner()
        try:
            self._status = self._status_factory(self.thinking_text)
            self._status.start()
        except Exception:  # a cosmetic spinner must never break the chat
            self._status = None

    def _stop_spinner(self) -> None:
        status, self._status = self._status, None
        if status is not None:
            try:
                status.stop()
            except Exception:
                pass

    @property
    def thinking(self) -> bool:
        return self._status is not None

    def _break_line(self) -> None:
        self._stop_spinner()
        if self._wrote_text:
            self.console.print()
            self._wrote_text = False

    def write_delta(self, text: str) -> None:
        self._stop_spinner()
        if not self._wrote_text:
            self.console.print("[bold green]jarvis ›[/] ", end="")
            self._wrote_text = True
        self.console.print(for_terminal(text), end="", markup=False, highlight=False, soft_wrap=True)

    def finish(self) -> None:
        self._break_line()

    def notice(self, text: str) -> None:
        self._break_line()
        self.console.print(for_terminal(text), style="dim", markup=False, highlight=False)

    def error(self, text: str, *, spoken: str | None = None) -> None:
        del spoken
        self._break_line()
        self.console.print(for_terminal(text), style="bold red", markup=False, highlight=False, soft_wrap=True)

    def debug(self, text: str) -> None:
        if not self.debug_enabled:
            return
        self._break_line()
        self.console.print(for_terminal(text), style="dim magenta", markup=False, highlight=False, soft_wrap=True)

    def confirm(self, request: ConfirmRequest) -> bool:
        self._break_line()
        self.console.print(t("confirm.terminal.heading", self.language, tool=for_terminal(request.tool), risk=request.risk))
        if request.reason:
            self.console.print(for_terminal(request.reason), style="yellow", markup=False, highlight=False)
        if request.tainted:
            warning = request.taint_warning or t("confirm.dialog.untrusted", self.language)
            self.console.print(
                t("confirm.terminal.warning", self.language) + for_terminal(warning), style="bold red", markup=False, highlight=False
            )
        if request.command_preview is not None:
            self.console.print(
                t("confirm.terminal.command", self.language) + "\n" + for_terminal(request.command_preview), markup=False, highlight=False
            )
        args = json.dumps(request.args, indent=2, ensure_ascii=False)
        self.console.print(for_terminal(args), markup=False, highlight=False)
        return self._ask_yes_no()

    def _ask_yes_no(self) -> bool:
        try:
            answer = Prompt.ask(t("confirm.terminal.ask", self.language), console=self.console, default="n", show_default=False)
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return False
        return terminal_answer(answer)

    def confirm_detailed(self, request: ConfirmRequest) -> ConfirmOutcome:
        approved = self.confirm(request)
        return ConfirmOutcome(
            approved=approved,
            how="prompt",
            reason=None if approved else "declined",
            user_message=None if approved else t("confirm.terminal.declined", self.language),
        )

    def ask_choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        self._break_line()
        self.console.print(for_terminal(question), style="bold yellow", markup=False, highlight=False)
        for key, label in options:
            self.console.print(f"  {key}: {for_terminal(label)}", markup=False, highlight=False)
        try:
            return Prompt.ask(
                t("confirm.terminal.choice", self.language), choices=[k for k, _ in options], default=default, console=self.console
            )
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return default
