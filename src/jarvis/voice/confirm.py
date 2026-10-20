"""Confirmations in voice mode: the on-screen dialog, the spoken-answer classifier,
the deny reasons and the one-decision arbiter.

Two ways to approve, decided here in code and never by the model:
- the macOS dialog (AppleScript `display dialog`, argv-based, with a timeout) showing the tool and
  its ACTUAL arguments, buttons Confirm / Reject (Onayla / Reddet) in the language of the turn, with
  Reject as the default and the cancel button;
- an explicit spoken phrase from `voice.confirm_phrases`, found as whole words inside a short
  answer after case folding that treats the Turkish dotted and dotless i alike ("Evet, onaylıyorum."
  and "yes, confirm" pass). Plain "evet", "yes", "tamam", "ok" never approve (the sink asks once
  more); anything with a negation ("hayır", "iptal", "no", "cancel") always denies, in either language.

Some actions accept only the dialog: `run_shell`, `write_file`, every delete tool, and every
confirmation inside a tainted turn (`dialog_only`). Silence, the Escape key and the timeout deny.

Exactly one decision is made per pending confirmation (`PendingConfirmation`, lock-protected):
the first valid decision wins, a dialog click always beats a voice timeout, and the losing path
is closed (the dialog is killed, the microphone released).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from jarvis.i18n import resolve, t, t_all
from jarvis.io.base import ConfirmRequest
from jarvis.utils.text import fold

log = logging.getLogger("jarvis.voice.confirm")

DIALOG_TITLE = "Jarvis"
DIALOG_TEXT_MAX_CHARS = 4000  # display dialog gets unreadable far before this; the full args are logged
APPLESCRIPT_TIMEOUT_MARGIN_S = 5.0

# Tools whose confirmation is never accepted by voice, in addition to every tool whose
# name marks a delete action and every confirmation in a tainted turn.
SCREEN_ONLY_TOOLS = frozenset({"run_shell", "write_file"})
DELETE_MARKERS = ("delete", "remove", "forget")

# The dialog script. Values travel as argv: text, confirm label, deny label, seconds.
# The deny label is both the default and the cancel button, so Enter and Escape deny; the timeout says so.
DIALOG_SCRIPT = """on run argv
    set theText to item 1 of argv
    set okLabel to item 2 of argv
    set noLabel to item 3 of argv
    set secs to (item 4 of argv) as integer
    try
        set r to display dialog theText with title "Jarvis" buttons {noLabel, okLabel} default button noLabel cancel button noLabel giving up after secs with icon caution
        if gave up of r then return "timeout"
        return button returned of r
    on error number -128
        return "cancel"
    end try
end run"""

# Multiple-choice dialog (delegate permissions): up to three buttons, the default button given by
# label; the first button doubles as the cancel button (Escape -> that label).
CHOICE_SCRIPT = """on run argv
    set theText to item 1 of argv
    set secs to (item 2 of argv) as integer
    set defaultLabel to item 3 of argv
    set labels to items 4 thru -1 of argv
    try
        set r to display dialog theText with title "Jarvis" buttons labels default button defaultLabel giving up after secs with icon note
        if gave up of r then return "timeout"
        return button returned of r
    on error number -128
        return "cancel"
    end try
end run"""


# --- spoken phrases ------------------------------------------------------------------------------


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_APOSTROPHE_RE = re.compile(r"[\'’ʼ‘`]")  # "don't" -> "dont", "İstanbul'dan" -> "istanbuldan"


def confirm_label(language: str | None = None) -> str:
    """The dialog's approve button ("Confirm" / "Onayla")."""
    return t("confirm.dialog.confirm", language)


def deny_label(language: str | None = None) -> str:
    """The dialog's deny button ("Reject" / "Reddet"), also its default and cancel button."""
    return t("confirm.dialog.deny", language)


def normalize_phrase(text: str) -> str:
    """NFKC, case-folded with every i-like letter as "i" (`utils.text.fold`), apostrophes and punctuation
    removed, whitespace collapsed: "ONAYLIYORUM!" and "onaylıyorum" become "onayliyorum", "CONFIRM" "confirm"."""
    text = fold(unicodedata.normalize("NFKC", text or ""))
    text = _APOSTROPHE_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def fold_dotless(text: str) -> str:
    """Whisper sometimes writes "onayliyorum": compare with ı and i folded together."""
    return text.replace("ı", "i")


def _word_set(key: str) -> frozenset[str]:
    """A spoken word list in every language, normalised like the answers."""
    return frozenset(normalize_phrase(word) for word in t_all(key) if normalize_phrase(word))


# Words that make an answer a refusal whatever else it contains (checked first).
NEGATION_WORDS = _word_set("confirm.words.negation")
# A bare acknowledgement: not enough on its own, the user is asked once more.
VAGUE_WORDS = _word_set("confirm.words.vague")
# Hedges: "onaylıyorum galiba" is not an approval.
HEDGE_WORDS = _word_set("confirm.words.hedge")
MAX_EXTRA_WORDS = 3  # a confirmation phrase counts only inside a short answer


@dataclass(frozen=True)
class AnswerVerdict:
    """What a spoken answer means: confirm | deny | vague | other, with the normalised text."""

    kind: str
    normalized: str
    matched_phrase: str | None = None

    @property
    def confirms(self) -> bool:
        return self.kind == "confirm"


class ConfirmPhraseMatcher:
    """Classifies a spoken answer against the configured phrases. Pure code, nothing fuzzy beyond
    case folding (the Turkish dotted and dotless i alike): a phrase must appear as whole words, the rest
    of the answer must be short (at most `MAX_EXTRA_WORDS` other words) and free of negations and hedges.
    Negations, hedges and vague words of every language count, whatever the language of the turn."""

    def __init__(self, phrases: list[str]) -> None:
        self.phrases = {normalize_phrase(p) for p in phrases if normalize_phrase(p)}
        if not self.phrases:
            raise ValueError("voice.confirm_phrases must contain at least one non-empty phrase")

    def classify(self, spoken: str) -> AnswerVerdict:
        normalized = normalize_phrase(spoken)
        if not normalized:
            return AnswerVerdict("other", normalized)
        words = normalized.split(" ")
        if any(w in NEGATION_WORDS for w in words):
            return AnswerVerdict("deny", normalized)
        # The longest configured phrase found as whole words wins.
        best: tuple[int, str, int] | None = None  # (phrase words, phrase, position)
        for phrase in self.phrases:
            pw = phrase.split(" ")
            for i in range(len(words) - len(pw) + 1):
                if words[i : i + len(pw)] == pw and (best is None or len(pw) > best[0]):
                    best = (len(pw), phrase, i)
        if best is not None:
            n, phrase, pos = best
            extra = words[:pos] + words[pos + n :]
            if any(w in HEDGE_WORDS for w in extra):
                return AnswerVerdict("vague", normalized, phrase)
            if len(extra) <= MAX_EXTRA_WORDS:
                return AnswerVerdict("confirm", normalized, phrase)
            return AnswerVerdict("other", normalized, phrase)
        if all(w in VAGUE_WORDS for w in words):
            return AnswerVerdict("vague", normalized)
        return AnswerVerdict("other", normalized)

    def matches(self, spoken: str) -> bool:
        return self.classify(spoken).confirms


def dialog_only(request: ConfirmRequest) -> bool:
    """Voice confirmation is not accepted for these; the dialog button is required."""
    return dialog_only_reason(request) is not None


def dialog_only_reason(request: ConfirmRequest) -> str | None:
    """Why the dialog is required: "voice_not_allowed_tainted" / "voice_not_allowed_high_risk" / None."""
    if request.tainted:
        return "voice_not_allowed_tainted"
    name = (request.tool or "").lower()
    if name in SCREEN_ONLY_TOOLS or any(marker in name for marker in DELETE_MARKERS):
        return "voice_not_allowed_high_risk"
    return None


# --- texts -----------------------------------------------------------------------------------------


def dialog_text(request: ConfirmRequest, *, voice_allowed: bool, language: str | None = None) -> str:
    """What the dialog shows: tool, risk, reason, taint warning, command, the actual arguments."""
    lines = [t("confirm.dialog.tool", language, tool=request.tool, risk=request.risk)]
    if request.reason:
        lines.append(request.reason)
    if request.tainted:
        lines.append("⚠️ " + (request.taint_warning or t("confirm.dialog.untrusted", language)))
    if request.command_preview is not None:
        lines.append(t("confirm.dialog.command", language) + "\n" + request.command_preview)
    lines.append(t("confirm.dialog.arguments", language) + "\n" + json.dumps(request.args, indent=2, ensure_ascii=False, default=str))
    lines.append(t("confirm.dialog.voice_hint" if voice_allowed else "confirm.dialog.button_only", language))
    text = "\n".join(lines)
    if len(text) > DIALOG_TEXT_MAX_CHARS:
        tail = "\n" + t("confirm.dialog.truncated", language)
        text = text[: DIALOG_TEXT_MAX_CHARS - len(tail)] + tail
    return text


def _args_summary(args: dict, limit: int = 3) -> str:
    parts = []
    for key, value in list(args.items())[:limit]:
        if isinstance(value, (dict, list)):
            continue
        text = str(value)
        parts.append(f"{key} {text[:60]}")
    return ", ".join(parts)


def spoken_question(request: ConfirmRequest, *, voice_allowed: bool, language: str | None = None) -> str:
    """One short spoken sentence saying what is about to run and how to answer. It asks for the
    phrase, not for a yes, so "evet" answers are rarer."""
    summary = _args_summary(request.args)
    what = request.tool.replace("_", " ")
    if summary:
        what += f", {summary}"
    return t("confirm.spoken.question" if voice_allowed else "confirm.spoken.question_button", language, what=what)


# One short sentence per deny reason (`confirm.deny.*`), spoken by the sink right after the decision and
# quoted to the model. "{heard}" is the user's (shortened) answer where it helps.
DENY_MESSAGE_REASONS = (
    "timeout",
    "phrase_mismatch",
    "vague",
    "negation",
    "voice_not_allowed_tainted",
    "voice_not_allowed_high_risk",
    "dialog_reject",
    "dialog_cancel",
    "dialog_parse_error",
    "superseded",
    "error",
)
HEARD_MAX_CHARS = 60


def reask_message(language: str | None = None) -> str:
    return t("confirm.spoken.reask", language)


def confirmed_message(language: str | None = None) -> str:
    return t("confirm.spoken.confirmed", language)


def deny_message(reason: str, language: str | None = None, heard: str | None = None) -> str:
    snippet = (heard or "").strip().replace("\n", " ")
    if len(snippet) > HEARD_MAX_CHARS:
        snippet = snippet[: HEARD_MAX_CHARS - 1] + "…"
    key = reason if reason in DENY_MESSAGE_REASONS else "error"
    return t(f"confirm.deny.{key}", language, heard=snippet or "…")


# --- the one decision ----------------------------------------------------------------------------------


@dataclass
class Decision:
    decision: str  # confirm | deny | timeout
    how: str  # dialog | voice_phrase | voice_other | timeout | superseded | error
    reason: str | None = None  # a DENY_REASONS entry when not confirmed
    heard: str | None = None


@dataclass
class PendingConfirmation:
    """The arbiter: the first decision wins, under a lock. A voice *timeout* never beats a dialog
    result that is already there (the caller polls the dialog before submitting a timeout, and
    `settle` refuses a timeout once a dialog decision exists)."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    decision: Decision | None = None
    on_settled: list[Callable[[Decision], None]] = field(default_factory=list)

    @property
    def settled(self) -> bool:
        return self.decision is not None

    def settle(self, decision: Decision) -> bool:
        """True when this decision won. Later decisions are logged and dropped."""
        with self._lock:
            if self.decision is not None:
                log.info(
                    "confirmation already settled (%s/%s); dropping %s/%s",
                    self.decision.decision,
                    self.decision.how,
                    decision.decision,
                    decision.how,
                )
                return False
            self.decision = decision
            hooks = list(self.on_settled)
        for hook in hooks:
            try:
                hook(decision)
            except Exception:  # closing the other path must never break the decision
                log.exception("confirmation settle hook failed")
        return True


# --- the dialog --------------------------------------------------------------------------------------


@dataclass
class DialogHandle:
    """A running osascript dialog. `result()` is None while it is still open. Thread-safe: the
    watcher thread and the confirmation loop may both poll it."""

    process: subprocess.Popen
    _result: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def poll(self) -> str | None:
        with self._lock:
            if self._result is not None:
                return self._result
            if self.process.poll() is None:
                return None
            out, _ = self.process.communicate()
            self._result = (out or "").strip() or "timeout"
            return self._result

    def result(self) -> str | None:
        return self.poll()

    def cancel(self) -> None:
        """Close the dialog. A click that already happened is kept, never overwritten."""
        if self.poll() is not None:
            return
        with self._lock:
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass
            if self._result is None:
                self._result = "cancelled"


def start_dialog(
    text: str,
    *,
    timeout_s: float,
    language: str | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> DialogHandle:
    """Open the confirmation dialog without blocking, its buttons in `language`. Argv only; UTF-8 output."""
    argv = ["osascript", "-e", DIALOG_SCRIPT, "--", text, confirm_label(language), deny_label(language), str(int(timeout_s))]
    proc = popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace"
    )
    return DialogHandle(proc)


def dialog_decision(result: str | None, language: str | None = None) -> str:
    """Map the dialog's output to confirm / deny / timeout / cancelled."""
    return dialog_outcome(result, language)[0]


def dialog_outcome(result: str | None, language: str | None = None) -> tuple[str, str | None]:
    """(decision, reason) for the dialog's raw output. Only the exact confirm label of the language the
    dialog was opened in confirms."""
    if result is None:
        return "deny", "dialog_parse_error"
    text = result.strip()
    if text == confirm_label(resolve(language)):
        return "confirm", None
    if text == deny_label(resolve(language)):
        return "deny", "dialog_reject"
    if text == "cancel":
        return "deny", "dialog_cancel"
    if text == "timeout":
        return "timeout", "timeout"
    if text == "cancelled":  # we closed it ourselves: the other path decided
        return "cancelled", "superseded"
    log.warning("unreadable dialog output: %r", result[:80])
    return "deny", "dialog_parse_error"


def ask_choice_dialog(
    question: str,
    options: list[tuple[str, str]],
    default: str,
    *,
    timeout_s: float,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Blocking multiple-choice dialog; returns the chosen key or `default` on timeout / cancel.

    `display dialog` allows at most three buttons; longer option lists fall back to the first three
    (the delegate's always / once / never fits). Button labels are the option labels, shortened.
    """
    shown = options[:3]
    labels = [label[:40] for _, label in shown]
    by_label = {label[:40]: key for key, label in shown}
    default_label = next((label[:40] for key, label in shown if key == default), labels[0])
    argv = ["osascript", "-e", CHOICE_SCRIPT, "--", question[:DIALOG_TEXT_MAX_CHARS], str(int(timeout_s)), default_label, *labels]
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s + APPLESCRIPT_TIMEOUT_MARGIN_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("choice dialog failed: %s", exc)
        return default
    answer = (proc.stdout or "").strip()
    return by_label.get(answer, default)
