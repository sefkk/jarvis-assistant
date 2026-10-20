"""Input and output interfaces.

The Assistant only talks to these. The terminal, the voice service, Telegram and the scheduler each
implement them, so the core never knows which interface it serves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Text the model writes right before a tool call is usually running commentary ("Brief'i alıyorum.");
# a longer piece or one with paragraphs is part of the answer (an older model sometimes answered first
# and then called a tool), so it stays.
COMMENTARY_MAX_CHARS = 200


def is_commentary(segment: str) -> bool:
    """Is this text, written before a tool call, a status line rather than part of the reply?"""
    text = segment.strip()
    return len(text) <= COMMENTARY_MAX_CHARS and "\n\n" not in text


def compose_reply(segments: list[str]) -> str:
    """The reply of a turn from its text segments (split at every tool call): the last segment, after
    the earlier ones that are not commentary. Falls back to everything when only commentary was written."""
    if not segments:
        return ""
    *earlier, last = segments
    parts = [s.strip() for s in earlier if s.strip() and not is_commentary(s)]
    if last.strip():
        parts.append(last.strip())
    return "\n\n".join(parts) or "".join(segments).strip()


@dataclass
class ConfirmRequest:
    """What the user sees before a CONFIRM tool runs. `args` are the actual arguments."""

    tool: str
    args: dict[str, Any]
    risk: str
    tainted: bool = False
    taint_sources: list[str] = field(default_factory=list)
    # "This action was triggered after reading untrusted content from ..." when tainted.
    taint_warning: str | None = None
    # Why a normally SAFE tool is asking (for example `remember` in a tainted turn).
    reason: str | None = None
    # For shell commands: the exact string that will run, shown verbatim.
    command_preview: str | None = None


# Why a confirmation was not approved. Logged in the audit line, spoken to the user by the voice
# sink, and quoted in the tool result the model receives.
DENY_REASONS = (
    "timeout",  # nobody answered in time
    "phrase_mismatch",  # something was said, but not a configured confirmation phrase
    "vague",  # only "evet" / "tamam": asked once more, still no phrase
    "negation",  # "hayır", "iptal", "onaylamıyorum" ...
    "voice_not_allowed_tainted",  # the turn is tainted: only the dialog button counts, and it was not pressed
    "voice_not_allowed_high_risk",  # shell / files / delete: only the dialog button counts, and it was not pressed
    "dialog_reject",  # the Reddet button
    "dialog_cancel",  # Escape / the cancel button
    "dialog_parse_error",  # the dialog returned something unreadable
    "superseded",  # a newer confirmation (or a barge-in) replaced this one
    "declined",  # the user said no (terminal prompt, Telegram button)
    "error",  # the confirmation machinery itself failed
)


@dataclass
class ConfirmOutcome:
    """The one decision made for a `ConfirmRequest`: approved or not, how, and why not."""

    approved: bool
    how: str = ""  # dialog | voice_phrase | voice_other | timeout | prompt | button | superseded | error
    reason: str | None = None  # one of DENY_REASONS when not approved
    heard: str | None = None  # what the user said or pressed (raw), when known
    # One short sentence for the user, in their language, saying why ("Süre doldu, onay gelmedi.").
    user_message: str | None = None
    elapsed_ms: float | None = None

    def model_text(self, tool: str) -> str:
        """The text the model receives as the tool result when the action did not run."""
        reason = self.reason or ("declined" if not self.approved else "")
        parts = [f"NOT RUN: {tool} was not approved by the user.", f"Decision: denied. Reason: {reason}."]
        if self.heard:
            parts.append(f"The user's answer was: {self.heard!r}.")
        if self.user_message:
            parts.append(f"The system already told the user this reason: '{self.user_message}'")
        parts.append(
            "Report exactly this decision and reason to the user in one short sentence; "
            "do not say only 'not approved', do not invent another explanation, and do not retry on your own."
        )
        return " ".join(parts)


class InputSource(ABC):
    @abstractmethod
    def read(self) -> str | None:
        """Block until the next user utterance. Return None when the user wants to quit."""


class OutputSink(ABC):
    @abstractmethod
    def write_delta(self, text: str) -> None:
        """Append a chunk of the streamed reply."""

    @abstractmethod
    def finish(self) -> None:
        """The reply for this turn is complete."""

    @abstractmethod
    def confirm(self, request: ConfirmRequest) -> bool:
        """Ask the user to approve a risky action. Default answer is No."""

    def confirm_detailed(self, request: ConfirmRequest) -> ConfirmOutcome:
        """Like `confirm`, but says how and why. Sinks that know the reason (voice, Telegram)
        override this; the default wraps `confirm` (a plain no is "declined")."""
        approved = self.confirm(request)
        return ConfirmOutcome(approved=approved, how="prompt", reason=None if approved else "declined")

    @abstractmethod
    def notice(self, text: str) -> None:
        """A short system message (tool activity, warnings, one-line errors)."""

    @abstractmethod
    def debug(self, text: str) -> None:
        """Diagnostic output (thinking, timings). Shown only in debug mode."""

    def error(self, text: str, *, spoken: str | None = None) -> None:
        """The turn failed for good (after the retries): one short message with the reason category.
        `spoken` is the one-sentence version for a speaking sink. Default: shown as a notice."""
        self.notice(text)

    def segment_break(self) -> None:
        """The model is about to call a tool (its own or a server tool such as web search): the text
        streamed since the previous break was running commentary ("Hava durumuna bakıyorum."), not the
        reply. A sink that delivers one finished message (Telegram, the scheduled brief) starts the reply
        over; a sink that shows or speaks text as it comes keeps it. Default: no-op."""

    def taint_changed(self, sources: list[str]) -> None:
        """The current turn became tainted (or gained a source). Called by the Brain as soon as
        untrusted content enters the turn, before the reply that follows it is streamed, so a
        sink can treat the rest of the turn differently (the voice sink keeps tainted replies
        away from the cloud TTS when `voice.elevenlabs_use_for_tainted` is false). Default: no-op."""

    def ask_choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        """Ask a multiple-choice question; returns the chosen key. Options are (key, label).

        The default implementation degrades to a yes/no confirm: yes picks the first option,
        no picks `default`. Interfaces that can show choices override this.
        """
        request = ConfirmRequest(tool=question, args={k: label for k, label in options}, risk="choice")
        return options[0][0] if self.confirm(request) else default
