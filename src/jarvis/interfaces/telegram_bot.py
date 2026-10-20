"""Telegram interface: the first background interface. It drives the same `Assistant`
through an `OutputSink`; the core does not know it exists.

Shape:
- `TelegramBot.run()` long-polls `getUpdates` in the current thread (no inbound port) and
  handles one message at a time. While a reply is being produced, a confirmation keeps polling
  from inside the wait so the inline buttons work; messages that arrive meanwhile are queued
  and handled after the turn (a `/stop` is handled immediately).
- `TelegramSink` streams by sending one message and editing it about once a second, sends the
  final reply (split at 4096 characters), and asks confirmations with inline buttons.
- Security is enforced here in code: allowlist, restricted tool profile plus a
  `forbidden` set, bound and expiring confirmations, untrusted forwards, kill switch, rate
  limit, no token or secrets in logs or messages. See `tests/security/test_telegram_security.py`.

CLI entry points the integrator wires into `jarvis telegram ...`: `run_bot`, `whoami`,
`set_token`, `enable_from_mac`, `health_checks`, and `telegram_launchd.install/uninstall/status`.

# TODO(setup): voice replies (TTS) are not implemented; see io/voice.py TextToSpeech
# (the speaker side exists for the Mac; sending audio back to the phone is still open).
"""

from __future__ import annotations

import getpass
import json
import logging
import math
import secrets as pysecrets
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from jarvis.config import Config, Paths
from jarvis.exceptions import JarvisError
from jarvis.i18n import LANGUAGES, t
from jarvis.interfaces import telegram_state
from jarvis.interfaces.telegram_api import (
    MAX_FILE_BYTES,
    MAX_MESSAGE_CHARS,
    TelegramApi,
    TelegramApiError,
    inline_keyboard,
    looks_like_token,
    mask_token,
    split_message,
)
from jarvis.io.base import ConfirmOutcome, ConfirmRequest, OutputSink
from jarvis.observability import logging_setup
from jarvis.observability.health import Check
from jarvis.security.sanitize import for_terminal
from jarvis.tools.base import ToolContext
from jarvis.tools.registry import ToolRegistry
from jarvis.utils import secrets

log = logging.getLogger("jarvis.telegram")

TOKEN_SECRET = "telegram_bot_token"
ALLOWED_UPDATES = ["message", "callback_query"]
STREAM_INTERVAL_S = 1.0
ARGS_PREVIEW_CHARS = 1500
MAX_VOICE_SECONDS = 600
POLL_ERROR_BACKOFF_S = 5.0
COMMANDS = {
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
LOCATION_COMMANDS = ("location",)  # commands are English in every language
# Shortcuts to the Mac power tools (tools/power.py). They go through the very same tool objects of the
# remote profile and the same `confirm.gate` as a model call, so `/sleep` shows the Approve / Decline buttons.
TOOL_COMMANDS = {"status": "mac_status", "lock": "lock_screen", "sleep": "sleep_mac"}
FORWARD_INSTRUCTION = "The user forwarded this message."


def help_text(config: Config, language: str | None = None) -> str:
    return t("telegram.help.text", language or config.general.language, team=config.football.team)


def duration_label(minutes: int, language: str | None = None) -> str:
    """'60 min' / '60 dk', '2 h 30 min' / '2 sa 30 dk': minutes up to two hours, like `voice.pause.duration_label`."""
    if minutes < 120:
        return t("telegram.mute.minutes", language, minutes=minutes)
    hours, mins = divmod(minutes, 60)
    if mins:
        return t("telegram.mute.hours_minutes", language, hours=hours, minutes=mins)
    return t("telegram.mute.hours", language, hours=hours)


# --- pending confirmations ------------------------------------------------------------------


@dataclass
class PendingAction:
    action_id: str
    kind: str  # "confirm" or "choice"
    created_at: float
    chat_id: int
    message_id: int | None
    options: dict[str, str] = field(default_factory=dict)  # callback value -> result
    resolved: bool = False
    result: str | None = None


def format_confirm(request: ConfirmRequest, *, include_args: bool = True, language: str | None = None) -> str:
    """The confirmation text: tool, risk, reason, taint warning, the ACTUAL arguments.

    The arguments are never truncated (the user approves what they can see). When
    they do not fit in one Telegram message, `confirm()` sends them in full as separate
    messages first and this header says so.
    """
    lines = [t("telegram.confirm.title", language), t("telegram.confirm.tool", language, tool=request.tool, risk=request.risk)]
    if request.reason:
        lines.append(request.reason)
    if request.tainted:
        lines.append("⚠️ " + (request.taint_warning or t("telegram.confirm.untrusted", language)))
    if request.command_preview is not None:
        lines.append(t("telegram.confirm.command", language, command=request.command_preview))
    if include_args:
        lines.append(t("telegram.confirm.args", language, args=format_args(request)))
    else:
        lines.append(t("telegram.confirm.args_above", language))
    return for_terminal("\n".join(lines))


def format_args(request: ConfirmRequest) -> str:
    return json.dumps(request.args, indent=2, ensure_ascii=False, default=str)


def location_keyboard(language: str | None = None) -> dict[str, Any]:
    """A one-time reply keyboard with Telegram's "share location" button (`request_location`)."""
    button = {"text": t("telegram.location.button", language), "request_location": True}
    return {"keyboard": [[button]], "one_time_keyboard": True, "resize_keyboard": True}


# --- the sink ----------------------------------------------------------------------------------


class TelegramSink(OutputSink):
    """Streams one turn to the private chat. Never raises into the Assistant: API failures are
    logged and the text is retried at `finish`."""

    def __init__(self, bot: "TelegramBot", chat_id: int, *, interval_s: float = STREAM_INTERVAL_S) -> None:
        self.bot = bot
        self.api = bot.api
        self.chat_id = chat_id
        self.clock = bot.clock
        self.interval_s = interval_s
        self._buffer = ""
        self._message_id: int | None = None
        self._last_sent = ""
        self._last_flush: float | None = None

    # -- helpers --

    def _send(self, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.bot.output_muted:
            return {}
        return self.api.send_message(self.chat_id, text, reply_markup=reply_markup)

    def _show(self, text: str) -> None:
        """Send or edit the streaming message with `text` (one chunk, <= 4096)."""
        if self.bot.output_muted:
            return  # after /stop nothing from a reply in progress leaves the Mac, edits included
        if self._message_id is None:
            self._message_id = self._send(text).get("message_id")
            self._last_sent = text
            return
        if text == self._last_sent:
            return
        try:
            self.api.edit_message_text(self.chat_id, self._message_id, text)
        except TelegramApiError as exc:
            log.warning("edit failed (%s); sending instead", exc)
            self._message_id = self._send(text).get("message_id")
        self._last_sent = text

    # -- OutputSink --

    def write_delta(self, text: str) -> None:
        if not text:
            return
        now = self.clock()
        if self._last_flush is None:
            self._last_flush = now
            try:
                if not self.bot.output_muted:
                    self.api.send_chat_action(self.chat_id)
            except TelegramApiError as exc:
                log.debug("typing action failed: %s", exc)
        self._buffer += text
        if now - self._last_flush < self.interval_s:
            return
        self._last_flush = now
        chunks = split_message(self._buffer)
        if not chunks:
            return
        try:
            self._show(chunks[0])
        except TelegramApiError as exc:
            log.warning("streaming send failed: %s", exc)

    def finish(self) -> None:
        chunks = split_message(self._buffer)
        try:
            for i, chunk in enumerate(chunks):
                if i == 0:
                    self._show(chunk)
                else:
                    self._send(chunk)
        except TelegramApiError as exc:
            log.error("could not deliver the reply: %s", exc)
        finally:
            self._buffer = ""
            self._message_id = None
            self._last_sent = ""
            self._last_flush = None

    def segment_break(self) -> None:
        """A tool call follows: the text so far ("Hava durumuna bakıyorum.") stays as its own message,
        and the reply starts a new one below the tool notices instead of being glued onto it."""
        if self._buffer.strip():
            self.finish()

    def notice(self, text: str) -> None:
        try:
            for chunk in split_message("ℹ️ " + text):
                self._send(chunk)
        except TelegramApiError as exc:
            log.warning("notice not delivered: %s", exc)

    def debug(self, text: str) -> None:  # diagnostics never leave the Mac
        return None

    def confirm(self, request: ConfirmRequest) -> bool:
        if request.tool in self.bot.forbidden:
            # Defense in depth: the restricted registry already hides it from the model.
            logging_setup.audit({"tool": "telegram", "decision": "forbidden_tool", "requested_tool": request.tool})
            log.error("refusing confirmation for forbidden remote tool %s", request.tool)
            return False
        lang = self.bot.language
        text = format_confirm(request, language=lang)
        if len(text) > MAX_MESSAGE_CHARS - 200:
            # Too long for one message: the full arguments go first, in as many messages as
            # needed, then the short header with the buttons. Nothing is cut.
            for chunk in split_message(for_terminal(t("telegram.confirm.args_full", lang, args=format_args(request)))):
                self._send(chunk)
            text = format_confirm(request, include_args=False, language=lang)
        # The press is matched by its callback value ("y" / "n"), never by the label.
        buttons = [(t("telegram.confirm.approve", lang), "y"), (t("telegram.confirm.decline", lang), "n")]
        answer = self.bot.ask_buttons(self.chat_id, text, buttons, kind="confirm", tool=request.tool)
        self._last_confirm_answer = answer
        return answer == "y"

    def confirm_detailed(self, request: ConfirmRequest) -> ConfirmOutcome:
        """The phone's buttons: Approve approves, Decline declines, no press in time is a timeout."""
        self._last_confirm_answer = None
        approved = self.confirm(request)
        answer = self._last_confirm_answer
        if approved:
            return ConfirmOutcome(approved=True, how="button", heard=answer)
        if request.tool in self.bot.forbidden:
            return ConfirmOutcome(
                approved=False,
                how="forbidden",
                reason="voice_not_allowed_high_risk",
                user_message=t("telegram.confirm.forbidden", self.bot.language),
            )
        if answer is None:
            return ConfirmOutcome(
                approved=False, how="timeout", reason="timeout", user_message=t("telegram.confirm.timeout", self.bot.language)
            )
        return ConfirmOutcome(
            approved=False, how="button", reason="declined", heard=answer, user_message=t("telegram.confirm.declined", self.bot.language)
        )

    def ask_choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        """Returns the chosen key, or "" when nobody answered (timeout or /stop): the caller
        must treat "" as a refusal for this call only and not record it as a lasting choice."""
        buttons = [(label, str(i)) for i, (_, label) in enumerate(options)]
        by_index = {str(i): key for i, (key, _) in enumerate(options)}
        answer = self.bot.ask_buttons(self.chat_id, for_terminal(question)[:3000], buttons, kind="choice", tool=question[:60])
        return by_index.get(answer or "", "")


# --- the bot ------------------------------------------------------------------------------------


def _default_assistant_factory(config: Config, paths: Paths, registry: ToolRegistry):
    from jarvis.assistant import Assistant

    return Assistant(config, paths, registry=registry, channel="telegram")


def _default_stt_factory(config, paths=None):
    from jarvis.io import voice

    return voice.SpeechToText.from_config(config.voice, paths, config=config)


def remote_tool_patterns(config) -> list[str]:
    """The tools a Telegram turn may use: `telegram.remote_allowed_tools` plus the presence tools
    only when `[presence]` opens them (`allow_remote` for all, `allow_remote_self` for
    `am_i_home` alone). Presence is deliberately never part of the default list, so a stolen phone
    cannot learn when the house is empty; the tools refuse remote calls in code as well."""
    from jarvis.tools.presence import remote_patterns  # noqa: PLC0415

    patterns = list(config.telegram.remote_allowed_tools)
    for pattern in remote_patterns(config):
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


class TelegramBot:
    def __init__(
        self,
        config: Config,
        paths: Paths,
        *,
        api: TelegramApi | None = None,
        assistant_factory: Callable[[Config, Paths, ToolRegistry], Any] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        stt_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.user_id = int(config.telegram.allowed_user_id)
        self.chat_id = int(config.telegram.allowed_chat_id)
        self.confirm_timeout_s = float(config.telegram.confirm_timeout_min) * 60.0
        self.poll_timeout_s = int(config.telegram.poll_timeout_s)
        self.clock = clock
        self.sleep = sleep
        if api is None:
            token = secrets.get_secret(TOKEN_SECRET)
            if not token:
                raise JarvisError("no Telegram bot token in the Keychain; run `jarvis telegram set-token`")
            api = TelegramApi(token)
        self.api = api
        self.assistant_factory = assistant_factory or _default_assistant_factory
        self.stt_factory = stt_factory or (lambda: _default_stt_factory(config, paths))

        # Remote tool profile: the model sees only the allowed subset, and
        # `forbidden` lets the sink refuse anything outside it even if asked.
        ctx = ToolContext.build(config, paths)
        self.full_registry = ToolRegistry.discover(ctx)
        self.registry = self.full_registry.subset_by_patterns(remote_tool_patterns(config))
        self.forbidden: set[str] = set(self.full_registry.names()) - set(self.registry.names())
        for pattern in self.full_registry.unmatched_patterns(list(config.telegram.remote_allowed_tools)):
            log.warning("telegram.remote_allowed_tools: %r matches no tool (typo?); it is ignored", pattern)

        self._assistant: Any = None
        self._session_date: str | None = None
        self._stt: Any = None
        self._offset: int | None = None
        self._pending: dict[str, PendingAction] = {}
        self._queued: deque[dict[str, Any]] = deque()
        self._recent: deque[float] = deque()
        self._stop_requested = False
        self._in_turn = False
        self._stopped = telegram_state.load_state(paths.telegram_state_file).stopped

    # --- lifecycle -------------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self.user_id != 0 and self.chat_id != 0

    def require_configured(self) -> None:
        if not self.configured:
            raise JarvisError(
                "telegram.allowed_user_id and telegram.allowed_chat_id must be set in config.toml "
                "(run `jarvis telegram whoami` to find them); refusing to run"
            )

    @property
    def language(self) -> str:
        """The main language: every message the bot writes itself is in it."""
        return self.config.general.language

    @property
    def output_muted(self) -> bool:
        """True once the kill switch is engaged: nothing from an in-flight turn leaves the Mac."""
        return self._stopped

    def request_stop(self) -> None:
        self._stop_requested = True

    def close(self) -> None:
        if self._assistant is not None:
            try:
                self._assistant.close()
            except Exception as exc:  # closing must not hide the original problem
                log.warning("assistant close failed: %s", exc)
            self._assistant = None
        try:
            self.api.close()
        except Exception:
            pass

    def run(self) -> None:
        """Poll until `request_stop()`; each update is handled before the next poll."""
        self.require_configured()
        self._stop_requested = False
        log.info(
            "telegram bot started",
            extra={"data": {"tools": self.registry.names(), "forbidden": sorted(self.forbidden), "stopped": self._stopped}},
        )
        try:
            while not self._stop_requested:
                try:
                    updates = self._poll(self.poll_timeout_s)
                except TelegramApiError as exc:
                    log.warning("getUpdates failed: %s", exc)
                    self.sleep(max(POLL_ERROR_BACKOFF_S, exc.retry_after or 0))
                    continue
                for update in updates:
                    self._dispatch(update)
                self._drain_queue()
                self._sweep_pending()
        finally:
            log.info("telegram bot stopped")

    # --- polling ---------------------------------------------------------------------------------

    def _poll(self, timeout_s: int) -> list[dict[str, Any]]:
        updates = self.api.get_updates(self._offset, timeout=timeout_s, allowed_updates=ALLOWED_UPDATES)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1 if self._offset is None else max(self._offset, update_id + 1)
        return updates

    def _dispatch(self, update: dict[str, Any], *, during_wait: bool = False) -> None:
        try:
            if "callback_query" in update:
                self._handle_callback(update["callback_query"] or {})
                return
            message = update.get("message")
            if not isinstance(message, dict):
                return  # edited messages, channel posts and other kinds are never requested
            if during_wait:
                if not self._is_allowed(message):
                    self._handle_message(message)  # only audits and drops; strangers are never queued
                elif self._command_of(message) == "stop":
                    self._handle_message(message)  # the kill switch must not wait for the turn
                else:
                    self._queued.append(message)
                return
            self._handle_message(message)
        except Exception:  # one bad update must not take the bot down
            log.exception("update handling crashed")

    def _drain_queue(self) -> None:
        while self._queued and not self._stop_requested:
            self._handle_message(self._queued.popleft())

    # --- allowlist and commands ----------------------------------------------------------------

    def _is_allowed(self, message: dict[str, Any]) -> bool:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        return self.configured and sender.get("id") == self.user_id and chat.get("id") == self.chat_id and chat.get("type") == "private"

    @staticmethod
    def _command_of(message: dict[str, Any]) -> str | None:
        text = message.get("text") or ""
        if not text.startswith("/"):
            return None
        word = text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        return word if word in COMMANDS else None

    @staticmethod
    def _command_arg(message: dict[str, Any]) -> str:
        parts = (message.get("text") or "").split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _audit(self, decision: str, message: dict[str, Any], reason: str, **extra: Any) -> None:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        logging_setup.audit(
            {
                "tool": "telegram",
                "decision": decision,
                "reason": reason,
                "sender_id": sender.get("id"),
                "chat_id": chat.get("id"),
                "chat_type": chat.get("type"),
                **extra,
            }
        )

    def _reload_state(self) -> telegram_state.TelegramState:
        state = telegram_state.load_state(self.paths.telegram_state_file)
        self._stopped = state.stopped
        return state

    def _reply(self, text: str) -> None:
        """A direct reply to the allowed chat (outside the streaming sink)."""
        try:
            self.api.send_long(self.chat_id, text)
        except TelegramApiError as exc:
            log.warning("reply not delivered: %s", exc)

    def _rate_limited(self) -> bool:
        now = self.clock()
        while self._recent and now - self._recent[0] >= 60.0:
            self._recent.popleft()
        if len(self._recent) >= self.config.telegram.max_messages_per_minute:
            return True
        self._recent.append(now)
        return False

    # --- messages ----------------------------------------------------------------------------------

    def _handle_message(self, message: dict[str, Any]) -> None:
        if not self._is_allowed(message):
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            if chat.get("type") != "private":
                reason = "chat_not_private"
            elif sender.get("id") != self.user_id:
                reason = "sender_not_allowed"
            else:
                reason = "chat_not_allowed"
            self._audit("ignored", message, reason)
            return

        command = self._command_of(message)
        state = self._reload_state()
        if state.stopped:
            if command == "start":
                telegram_state.request_reenable(self.paths.telegram_state_file)
                self._audit("reenable_requested", message, "start_while_stopped")
                self._reply(t("telegram.bot.reenable_requested", self.language))
            else:
                self._audit("stopped", message, "kill_switch")
            return

        if command == "stop":
            telegram_state.request_stop(self.paths.telegram_state_file)
            self._stopped = True
            self._deny_all_pending()
            self._audit("stopped", message, "stop_command")
            self._reply(t("telegram.bot.stopped", self.language))
            return

        if self._rate_limited():
            self._audit("rate_limited", message, "max_messages_per_minute")
            return

        if command == "start":
            self._reply(t("telegram.bot.greeting", self.language))
            return
        if command == "help":
            self._reply(help_text(self.config))
            return
        if command == "new":
            self._ensure_assistant().new_session()
            self._session_date = self._today()
            self._reply(t("telegram.bot.new_session", self.language))
            return
        if command in ("mute", "unmute"):
            self._handle_mute(command, self._command_arg(message), message)
            return
        if command in LOCATION_COMMANDS:
            self._reply_with_markup(t("telegram.location.request", self.language), location_keyboard(self.language))
            return
        if command in TOOL_COMMANDS:
            self._handle_tool_command(command, self._command_arg(message), message)
            return
        if command in ("stop_alarm", "snooze"):
            self._handle_alarm_command(command, self._command_arg(message), message)
            return
        if command == "brief":
            from jarvis.scheduler.brief import ON_DEMAND_PROMPT  # noqa: PLC0415

            self._audit("brief", message, "brief_command")
            self._run_turn(ON_DEMAND_PROMPT.format(language=self.config.general.language, team=self.config.football.team))
            return

        forwarded = self._is_forward(message)  # decided first: a forwarded voice note is not the owner's voice
        if isinstance(message.get("location"), dict):
            self._handle_location(message, forwarded=forwarded)
            return
        if isinstance(message.get("voice"), dict):
            self._handle_voice(message, forwarded=forwarded)
            return

        text = message.get("text") or message.get("caption") or ""
        if forwarded:
            if not text.strip():
                self._reply(t("telegram.bot.unsupported", self.language))
                return
            self._audit("forward", message, "untrusted_forward", chars=len(text))
            self._run_turn(FORWARD_INSTRUCTION, untrusted=[("telegram_forward", text)])
            return
        if not text.strip():
            self._reply(t("telegram.bot.unsupported", self.language))
            return
        alarm_reply = self._alarm_word_on_request(text)  # "tamam" / "kapat" / "ertele" while an alarm rings on the Mac
        if alarm_reply:
            self._audit("alarm_word", message, "alarm_ringing")
            self._reply(alarm_reply)
            return
        cancelled = self._cancel_sleep_on_request(text)
        if cancelled:
            self._audit("sleep_cancelled", message, "cancel_word")
            self._reply(cancelled)
            return
        self._learn_voice_correction(text)  # "hayır, X dedim" typed after a misheard voice note
        self._last_voice_transcript = None
        self._run_turn(text)

    # --- alarms: /stop_alarm, /snooze [dk], and the stop / snooze words while one rings ---------------------

    def _alarm_word_on_request(self, text: str) -> str | None:
        """While an alarm rings on the Mac, a whole-message "tamam" / "kapat" / "durdur" stops it and
        "ertele" snoozes it without the model. Anything else, or no ringing alarm: None."""
        from jarvis.scheduler import alarm  # noqa: PLC0415

        try:
            action = alarm.classify_alarm_word(text)
            if action is None or alarm.ringing(self.paths) is None:
                return None
            return self._alarm_action(action, None)
        except Exception:  # a broken state file must not swallow the message
            log.exception("alarm word check failed")
            return None

    def _handle_alarm_command(self, command: str, arg: str, message: dict[str, Any]) -> None:
        minutes: int | None = None
        if command == "snooze" and arg.strip():
            try:
                minutes = max(1, min(60, int(arg.strip().split()[0])))
            except ValueError:
                self._reply(t("telegram.alarm.snooze_usage", self.language))
                return
        self._audit("alarm_command", message, command, minutes=minutes)
        self._reply(self._alarm_action("stop" if command == "stop_alarm" else "snooze", minutes))

    def _alarm_action(self, action: str, minutes: int | None) -> str:
        from datetime import timedelta  # noqa: PLC0415

        from jarvis.scheduler import alarm  # noqa: PLC0415
        from jarvis.utils.timeutil import format_local, now_local  # noqa: PLC0415

        if alarm.ringing(self.paths) is None:
            return alarm.NOT_RINGING_TEXT
        if action == "stop":
            ok = alarm.request_stop(self.paths, by="telegram", wait_s=alarm.ACK_WAIT_S)
            return alarm.STOPPED_TEXT if ok else t("telegram.alarm.stop_no_answer", self.language)
        minutes = minutes or self.config.alarm.snooze_min
        ok = alarm.request_snooze(self.paths, by="telegram", minutes=minutes, wait_s=alarm.ACK_WAIT_S)
        if not ok:
            return t("telegram.alarm.snooze_no_answer", self.language)
        return alarm.SNOOZED_TEXT.format(minutes=minutes, at=format_local(now_local() + timedelta(minutes=minutes), with_date=False))

    def _cancel_sleep_on_request(self, text: str) -> str | None:
        """ "iptal" / "dur" while the Mac is about to sleep (a `sleep_mac` countdown, or a scheduled
        sleep whose one-minute notice is out) cancels it without the model. Anything else: None."""
        from jarvis.tools import power  # noqa: PLC0415

        try:
            return power.cancel_on_request(text, paths=self.paths)
        except Exception:  # a broken timers table must not swallow the message
            log.exception("sleep cancel check failed")
            return None

    def _handle_tool_command(self, command: str, arg: str, message: dict[str, Any]) -> None:
        """/status, /lock, /sleep [seconds]: the matching power tool from the remote profile, gated exactly
        like a model call (`confirm.gate` with this chat's sink: `/sleep` asks with the Approve / Decline buttons)."""
        from jarvis.exceptions import ToolDeniedError, ToolError, ToolValidationError  # noqa: PLC0415
        from jarvis.security import confirm  # noqa: PLC0415
        from jarvis.security.taint import TaintState  # noqa: PLC0415

        name = TOOL_COMMANDS[command]
        tool = self.registry.get(name)
        if tool is None:
            self._audit("ignored", message, "tool_not_remote", command=command, requested_tool=name)
            self._reply(t("telegram.power.not_remote", self.language, command=command, tool=name))
            return
        raw: dict[str, Any] = {}
        if command == "sleep" and arg:
            if not arg.isdigit():
                self._reply(t("telegram.power.sleep_usage", self.language))
                return
            raw = {"delay_s": int(arg)}
        try:
            self._ensure_assistant()  # gives the tools their runtime (database, channel) like any turn
        except Exception as exc:  # no API key yet, for example: the power tools still work without a runtime
            log.warning("assistant unavailable for /%s (%s); running the tool without a runtime", command, exc)
        sink = TelegramSink(self, self.chat_id)
        text = message.get("text") or f"/{command}"
        self._in_turn = True
        try:
            args = tool.parse_args(raw)
            decision = confirm.gate(tool, args, taint=TaintState(), sink=sink, user_text=text)
            result = tool.run(args)
        except ToolValidationError:
            self._audit("ignored", message, "bad_command_arg", command=command)
            usage = "telegram.power.sleep_usage" if command == "sleep" else "telegram.power.bad_arg"
            self._reply(t(usage, self.language, command=command))
            return
        except ToolDeniedError:
            self._audit("denied", message, "command_denied", command=command, for_tool=name)
            self._reply(t("telegram.power.denied", self.language, command=f"/{command}"))
            return
        except ToolError as exc:
            self._audit("error", message, "command_failed", command=command, for_tool=name)
            log.warning("/%s failed: %s", command, exc)
            self._reply(t("telegram.power.failed", self.language, command=command, error=for_terminal(str(exc))[:300]))
            return
        except Exception as exc:  # a tool must never take the bot down
            log.exception("/%s crashed", command)
            self._audit("error", message, "command_crashed", command=command, for_tool=name)
            self._reply(t("telegram.power.crashed", self.language, command=command, error=type(exc).__name__))
            return
        finally:
            self._in_turn = False
        self._audit(decision, message, "command", command=command, for_tool=name)
        self._reply(for_terminal(result.content))

    def _handle_mute(self, command: str, arg: str, message: dict[str, Any]) -> None:
        """/mute [60m] pauses the Mac's voice listening (the service releases the microphone within a
        second); /unmute resumes. Same file as `jarvis voice pause` and the `pause_listening` tool."""
        from jarvis.voice import pause as vpause  # noqa: PLC0415 - light, but only this command needs it

        if command == "unmute":
            resumed = vpause.resume(self.paths.voice_pause_file)
            self._audit("unmute", message, "unmute_command", resumed=resumed)
            self._reply(t("telegram.mute.resumed" if resumed else "telegram.mute.already_on", self.language))
            return
        minutes = vpause.parse_duration(arg) if arg else None
        if arg and minutes is None:
            self._reply(t("telegram.mute.usage", self.language))
            return
        try:
            state = vpause.pause(self.paths.voice_pause_file, by="telegram", minutes=minutes)
        except ValueError as exc:
            self._reply(str(exc))
            return
        self._audit("mute", message, "mute_command", minutes=minutes)
        if minutes:
            self._reply(
                t(
                    "telegram.mute.paused_for",
                    self.language,
                    duration=duration_label(minutes, self.language),
                    until=vpause._local(state.until),
                )
            )
        else:
            self._reply(t("telegram.mute.paused", self.language))

    def _reply_with_markup(self, text: str, reply_markup: dict[str, Any]) -> None:
        try:
            self.api.send_message(self.chat_id, text, reply_markup=reply_markup)
        except TelegramApiError as exc:
            log.warning("reply not delivered: %s", exc)

    def _handle_location(self, message: dict[str, Any], *, forwarded: bool) -> None:
        """A location the owner shared (attach > Location, or the /location button) becomes the Current
        Location for remote turns. A forwarded location is someone else's
        and is ignored; the coordinates are validated and never logged."""
        from jarvis.location import service as location_service  # noqa: PLC0415

        if forwarded:
            self._audit("ignored", message, "forwarded_location")
            self._reply(t("telegram.location.forwarded", self.language))
            return
        location = message.get("location") or {}
        try:
            stored = location_service.store_telegram_location(
                self.paths,
                float(location.get("latitude")),
                float(location.get("longitude")),
                accuracy_m=location.get("horizontal_accuracy"),
            )
        except (TypeError, ValueError):
            self._audit("ignored", message, "invalid_location")
            self._reply(t("telegram.location.invalid", self.language))
            return
        self._audit("location", message, "telegram_location_stored", live=bool(location.get("live_period")))
        when = stored.updated_datetime().astimezone().strftime("%H:%M")
        self._reply_with_markup(t("telegram.location.saved", self.language, when=when), {"remove_keyboard": True})

    @staticmethod
    def _is_forward(message: dict[str, Any]) -> bool:
        return any(
            message.get(key) is not None
            for key in ("forward_origin", "forward_from", "forward_sender_name", "forward_from_chat", "forward_date")
        )

    def _handle_voice(self, message: dict[str, Any], *, forwarded: bool = False) -> None:
        voice = message["voice"]
        duration = int(voice.get("duration") or 0)
        size = int(voice.get("file_size") or 0)
        if duration > MAX_VOICE_SECONDS:
            self._reply(t("telegram.voice.too_long", self.language, seconds=duration, limit=MAX_VOICE_SECONDS // 60))
            return
        if size > MAX_FILE_BYTES:
            self._reply(t("telegram.voice.too_big", self.language))
            return
        from jarvis.io.voice import VoiceUnavailable

        try:
            info = self.api.get_file(str(voice.get("file_id") or ""))
            data = self.api.download_file(str(info.get("file_path") or ""))
        except TelegramApiError as exc:
            log.warning("voice download failed: %s", exc)
            self._reply(t("telegram.voice.download_failed", self.language))
            return
        try:
            if self._stt is None:
                self._stt = self.stt_factory()
            text, language = self._stt.transcribe(data)
        except VoiceUnavailable as exc:
            self._reply(str(exc))
            return
        except Exception as exc:  # a bad recording must not crash the bot
            log.exception("transcription failed")
            self._reply(t("telegram.voice.failed", self.language, error=type(exc).__name__))
            return
        self._audit("voice", message, "transcribed", seconds=duration, chars=len(text), language=language, forwarded=forwarded)
        if not text.strip():
            self._reply(t("telegram.voice.empty", self.language))
            return
        self._reply(t("telegram.voice.heard", language if language in LANGUAGES else self.language, text=text))
        if forwarded:
            self._audit("forward", message, "untrusted_forward_voice", chars=len(text))
            self._run_turn(FORWARD_INSTRUCTION, untrusted=[("telegram_forward", text)])
            return
        self._learn_voice_correction(text)
        self._last_voice_transcript = text
        self._run_turn(text, spoken=True)  # the owner's own voice: trusted, like typed text; names may be misheard

    _last_voice_transcript: str | None = None

    def _learn_voice_correction(self, text: str) -> None:
        """Learn "hayır, X dedim" after a misheard voice note (voice/corrections.py); never fatal."""
        store = getattr(self._stt, "corrections", None)
        if store is None or not self._last_voice_transcript:
            return
        try:
            learned = store.learn(self._last_voice_transcript, text)
        except Exception as exc:
            log.warning("correction learning failed: %s", exc)
            return
        if learned is not None:
            self._audit("voice", {}, "correction_learned", chars=len(learned.meant))

    # --- turns -------------------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock()).astimezone().strftime("%Y-%m-%d")

    def _ensure_assistant(self) -> Any:
        if self._assistant is None:
            self._assistant = self.assistant_factory(self.config, self.paths, self.registry)
            self._session_date = self._today()
            # "📝 Not aldım: …" from the end-of-session memory pass (daily rollover, a quiet gap) goes to the chat
            self._assistant.memory_notify = lambda line: self._reply(line) if not self.output_muted else None
        elif self._session_date != self._today():
            self._assistant.new_session(resume=True)  # one session per day per chat; a chat over midnight goes on
            self._session_date = self._today()
        return self._assistant

    def _run_turn(self, text: str, *, untrusted: list[tuple[str, str]] | None = None, spoken: bool = False) -> None:
        assistant = self._ensure_assistant()
        sink = TelegramSink(self, self.chat_id)
        self._in_turn = True
        try:
            if spoken:
                assistant.handle(text, sink, untrusted=untrusted, spoken=True)
            else:
                assistant.handle(text, sink, untrusted=untrusted)
        except Exception as exc:  # the Assistant catches its own errors; this is the last net
            log.exception("turn crashed in the telegram interface")
            self._reply(t("telegram.bot.turn_error", self.language, error=type(exc).__name__))
        finally:
            self._in_turn = False

    # --- confirmations (inline buttons) -----------------------------------------------------------

    def ask_buttons(self, chat_id: int, text: str, buttons: list[tuple[str, str]], *, kind: str, tool: str = "") -> str | None:
        """Send `text` with one button per (label, value); wait for the owner's press. Returns the
        chosen value, or None on timeout / kill switch / delivery failure (callers treat None as No)."""
        if self._stopped:
            return None
        action_id = pysecrets.token_hex(6)
        prefix = "cf" if kind == "confirm" else "ch"
        markup = inline_keyboard([[(label, f"{prefix}:{action_id}:{value}") for label, value in buttons]])
        try:
            sent = self.api.send_message(chat_id, text, reply_markup=markup)
        except TelegramApiError as exc:
            log.error("could not send the confirmation: %s", exc)
            return None
        action = PendingAction(
            action_id=action_id,
            kind=kind,
            created_at=self.clock(),
            chat_id=chat_id,
            message_id=sent.get("message_id"),
            options={value: value for _, value in buttons},
        )
        self._pending[action_id] = action
        logging_setup.audit({"tool": "telegram", "decision": "confirm_sent", "kind": kind, "action_id": action_id, "for_tool": tool})
        result = self._wait_for(action)
        self._pending.pop(action_id, None)
        labels = {value: label for label, value in buttons}
        if result is None:
            outcome = t("telegram.confirm.outcome_stopped" if self._stopped else "telegram.confirm.outcome_timeout", self.language)
            decision = "confirm_timeout"
        elif kind == "confirm":
            outcome = t("telegram.confirm.outcome_approved" if result == "y" else "telegram.confirm.outcome_declined", self.language)
            decision = "confirmed" if result == "y" else "denied"
        else:
            outcome = t("telegram.confirm.outcome_chosen", self.language, label=labels.get(result, result))
            decision = "chosen"
        logging_setup.audit({"tool": "telegram", "decision": decision, "kind": kind, "action_id": action_id, "for_tool": tool})
        if action.message_id is not None and not self.output_muted:
            try:
                self.api.edit_message_text(chat_id, action.message_id, (text + "\n\n" + outcome)[:MAX_MESSAGE_CHARS])
            except TelegramApiError as exc:
                log.debug("could not update the confirmation message: %s", exc)
        return result

    def _wait_for(self, action: PendingAction) -> str | None:
        deadline = action.created_at + self.confirm_timeout_s
        while not action.resolved:
            remaining = deadline - self.clock()
            if remaining <= 0 or self._stop_requested or self._stopped:
                return None
            timeout = max(1, min(self.poll_timeout_s, int(math.ceil(remaining))))
            try:
                updates = self._poll(timeout)
            except TelegramApiError as exc:
                log.warning("getUpdates failed while waiting for a confirmation: %s", exc)
                self.sleep(min(POLL_ERROR_BACKOFF_S, max(remaining, 0)))
                continue
            for update in updates:
                self._dispatch(update, during_wait=True)
        return action.result

    def _deny_all_pending(self) -> None:
        for action in self._pending.values():
            action.resolved = True
            action.result = None
        self._pending.clear()

    def _sweep_pending(self) -> None:
        now = self.clock()
        for action_id in [a.action_id for a in self._pending.values() if now - a.created_at > self.confirm_timeout_s]:
            self._pending.pop(action_id, None)

    def _handle_callback(self, query: dict[str, Any]) -> None:
        sender = query.get("from") or {}
        query_id = str(query.get("id") or "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if sender.get("id") != self.user_id:
            logging_setup.audit(
                {
                    "tool": "telegram",
                    "decision": "ignored",
                    "reason": "callback_sender_not_allowed",
                    "sender_id": sender.get("id"),
                    "chat_id": chat.get("id"),
                    "chat_type": chat.get("type"),
                }
            )
            return  # silent, like every other non-allowlisted update
        data = str(query.get("data") or "")
        parts = data.split(":", 2)
        action = self._pending.get(parts[1]) if len(parts) == 3 else None
        reason: str | None = None
        if len(parts) != 3 or parts[0] not in ("cf", "ch"):
            reason = "malformed"
        elif action is None:
            reason = "unknown_or_replayed"
        elif action.resolved:
            reason = "already_resolved"
        elif parts[0] != ("cf" if action.kind == "confirm" else "ch"):
            reason = "kind_mismatch"
        elif self.clock() - action.created_at > self.confirm_timeout_s:
            reason = "expired"
            self._pending.pop(action.action_id, None)
        elif parts[2] not in action.options:
            reason = "bad_value"
        elif chat.get("id") != action.chat_id or (action.message_id is not None and message.get("message_id") != action.message_id):
            # The button must be pressed on the very message that asked (ids must be present).
            reason = "wrong_message"
        if reason:
            logging_setup.audit(
                {
                    "tool": "telegram",
                    "decision": "callback_rejected",
                    "reason": reason,
                    "sender_id": sender.get("id"),
                    "chat_id": chat.get("id"),
                    "chat_type": chat.get("type"),
                    "action_id": parts[1] if len(parts) == 3 else None,
                }
            )
            self._answer_callback(query_id, t("telegram.confirm.callback_invalid", self.language))
            return
        assert action is not None
        action.resolved = True
        action.result = action.options[parts[2]]
        self._pending.pop(action.action_id, None)  # a replay of the same button is rejected above
        logging_setup.audit({"tool": "telegram", "decision": "callback_accepted", "action_id": action.action_id, "kind": action.kind})
        self._answer_callback(query_id, t("telegram.confirm.callback_ok", self.language))

    def _answer_callback(self, query_id: str, text: str) -> None:
        if not query_id:
            return
        try:
            self.api.answer_callback_query(query_id, text)
        except TelegramApiError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)


# --- CLI entry points ------------------------------------------------------------------------------


def _record_start(paths: Paths) -> None:
    """Tell the dashboard when this agent started and which config version it loaded (never fatal)."""
    try:
        from jarvis import services  # noqa: PLC0415
        from jarvis.observability.db import Database  # noqa: PLC0415

        db = Database(paths.db_file)
        try:
            services.record_start(db, "telegram", paths)
        finally:
            db.close()
    except Exception:
        log.debug("could not record the telegram agent start", exc_info=True)


def run_bot(config: Config, paths: Paths, *, out: Callable[[str], None] = print) -> int:
    """`jarvis telegram run`: refuses clearly when disabled, unconfigured or without a token."""
    tg = config.telegram
    if not tg.enabled:
        out(t("telegram.cli.disabled"))
        return 1
    if tg.allowed_user_id == 0 or tg.allowed_chat_id == 0:
        out(t("telegram.cli.no_ids"))
        return 1
    if not secrets.get_secret(TOKEN_SECRET):
        out(t("telegram.cli.no_token"))
        return 1
    bot = TelegramBot(config, paths)
    _record_start(paths)

    def _terminate(signum, frame) -> None:  # noqa: ARG001 - launchctl bootout sends SIGTERM
        bot.request_stop()
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:  # not the main thread
        pass
    state = telegram_state.load_state(paths.telegram_state_file)
    out(t("telegram.cli.running_stopped" if state.stopped else "telegram.cli.running"))
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.close()
    return 0


def whoami(
    config: Config,
    paths: Paths,
    timeout_s: float = 120,
    *,
    api: TelegramApi | None = None,
    out: Callable[[str], None] = print,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    """Setup mode (`jarvis telegram whoami`): prints the bot identity, waits for the first
    message and prints its sender id, chat id and chat type. Replies with the ids only to a
    private chat. Run it only while at the Mac, and not while the bot agent is running
    (two pollers on one token conflict)."""
    if api is None:
        token = secrets.get_secret(TOKEN_SECRET)
        if not token:
            out(t("telegram.cli.whoami_no_token"))
            return None
        api = TelegramApi(token)
    try:
        me = api.get_me()
    except TelegramApiError as exc:
        out(t("telegram.cli.get_me_failed", error=exc))
        return None
    out(t("telegram.cli.bot", username=me.get("username", "?"), id=me.get("id", "?"), name=me.get("first_name", "?")))
    out(t("telegram.cli.waiting", seconds=int(timeout_s)))
    offset: int | None = None
    deadline = clock() + timeout_s
    while clock() < deadline:
        try:
            updates = api.get_updates(offset, timeout=min(10, max(1, int(deadline - clock()))), allowed_updates=["message"])
        except TelegramApiError as exc:
            out(t("telegram.cli.get_updates_failed", error=exc))
            return None
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            sender = message.get("from") or {}
            chat = message.get("chat") or {}
            found = {"user_id": sender.get("id"), "chat_id": chat.get("id"), "chat_type": chat.get("type")}
            out(
                t(
                    "telegram.cli.first_message",
                    user_id=found["user_id"],
                    username=sender.get("username", "-"),
                    chat_id=found["chat_id"],
                    chat_type=found["chat_type"],
                )
            )
            if chat.get("type") == "private" and isinstance(chat.get("id"), int):
                try:
                    api.send_message(chat["id"], t("telegram.cli.setup_reply", user_id=found["user_id"], chat_id=found["chat_id"]))
                except TelegramApiError as exc:
                    out(t("telegram.cli.reply_failed", error=exc))
            else:
                out(t("telegram.cli.not_private"))
            out(t("telegram.cli.put_ids"))
            logging_setup.audit({"tool": "telegram", "decision": "whoami", **found})
            return found
    out(t("telegram.cli.no_message"))
    return None


def set_token(*, prompt: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print) -> bool:
    """`jarvis telegram set-token`: store the BotFather token in the Keychain. Never echoed."""
    token = prompt(t("telegram.cli.token_prompt")).strip()
    if not token:
        out(t("telegram.cli.no_token_entered"))
        return False
    if not looks_like_token(token):
        out(t("telegram.cli.token_odd"))
    secrets.set_secret(TOKEN_SECRET, token)
    out(t("telegram.cli.token_stored", name=TOKEN_SECRET))
    return True


def enable_from_mac(paths: Paths) -> str:
    """`jarvis telegram enable`: the Mac side of the two-step re-enable after `/stop`."""
    return telegram_state.enable_from_mac(paths.telegram_state_file)


def health_checks(config: Config, paths: Paths) -> list[Check]:
    """For `jarvis doctor`: never raises, no network."""
    from jarvis.interfaces import telegram_launchd

    checks: list[Check] = []
    tg = config.telegram
    enabled = bool(tg.enabled)
    checks.append(Check("telegram enabled", True, "enabled" if enabled else "disabled (telegram.enabled = false)"))
    try:
        has_token = bool(secrets.get_secret(TOKEN_SECRET))
    except Exception:
        has_token = False
    checks.append(Check("telegram token", has_token or not enabled, "in Keychain" if has_token else "missing: `jarvis telegram set-token`"))
    ids_set = tg.allowed_user_id != 0 and tg.allowed_chat_id != 0
    checks.append(Check("telegram ids", ids_set or not enabled, "user and chat id set" if ids_set else "missing: `jarvis telegram whoami`"))
    try:
        registry = ToolRegistry.discover(ToolContext.build(config, paths))
        unmatched = registry.unmatched_patterns(list(tg.remote_allowed_tools))
        allowed = registry.subset_by_patterns(remote_tool_patterns(config)).names()
        detail = f"{len(allowed)} tool(s) allowed remotely"
        if unmatched:
            detail += "; patterns matching nothing: " + ", ".join(unmatched)
        checks.append(Check("telegram remote tools", not unmatched, detail))
    except Exception as exc:
        checks.append(Check("telegram remote tools", False, f"could not evaluate: {type(exc).__name__}"))
    try:
        agent = telegram_launchd.status()
        checks.append(Check("telegram agent", agent.running or not enabled, mask_token(agent.detail)))
    except Exception as exc:
        checks.append(Check("telegram agent", not enabled, f"status unknown: {type(exc).__name__}"))
    try:
        state = telegram_state.load_state(paths.telegram_state_file)
        if state.stopped:
            detail = f"STOPPED since {state.stopped_at or '?'}" + (
                "; /start received, run `jarvis telegram enable`"
                if state.reenable_requested
                else "; send /start then run `jarvis telegram enable`"
            )
        else:
            detail = "active"
        checks.append(Check("telegram kill switch", not state.stopped, detail))
    except Exception as exc:
        checks.append(Check("telegram kill switch", False, f"state unreadable: {type(exc).__name__}"))
    try:
        from jarvis.io.voice import SpeechToText

        voice_ok = SpeechToText.available()
    except Exception:
        voice_ok = False
    checks.append(
        Check(
            "telegram voice",
            True,
            "mlx-whisper and ffmpeg found"
            if voice_ok
            else "optional: the voice extra (`uv tool install --editable '.[voice]' --python 3.12`) and `brew install ffmpeg`",
        )
    )
    return checks
