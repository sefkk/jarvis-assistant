"""Telegram channel for the `Notifier` interface: timers and pomodoro can
notify the phone. Messages go only to the configured private chat, and never while the kill
switch (/stop) is active or the interface is disabled: a stolen phone must not keep receiving
agenda or mail summaries."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.exceptions import JarvisError
from jarvis.interfaces import telegram_state
from jarvis.interfaces.telegram_api import TelegramApi
from jarvis.scheduler.notify import Notifier
from jarvis.utils import secrets

if TYPE_CHECKING:
    from jarvis.config import Config

log = logging.getLogger("jarvis.telegram.notify")

TOKEN_SECRET = "telegram_bot_token"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, config: "Config", *, api: TelegramApi | None = None, state_file: Path | None = None) -> None:
        self.chat_id = int(config.telegram.allowed_chat_id)
        self.enabled = bool(config.telegram.enabled)
        if self.chat_id == 0:
            raise JarvisError("telegram notifier: set telegram.allowed_chat_id in config.toml (`jarvis telegram whoami` prints it)")
        if state_file is None:
            from jarvis.config import Paths, resolve_home

            state_file = Paths(resolve_home()).telegram_state_file
        self.state_file = state_file
        if api is None:
            token = secrets.get_secret(TOKEN_SECRET)
            if not token:
                raise JarvisError("telegram notifier: no bot token in the Keychain; run `jarvis telegram set-token`")
            api = TelegramApi(token)
        self.api = api

    def blocked_reason(self) -> str | None:
        if not self.enabled:
            return "telegram.enabled is false"
        if telegram_state.load_state(self.state_file).stopped:
            return "the kill switch (/stop) is active"
        return None

    def notify(self, title: str, message: str) -> None:
        reason = self.blocked_reason()
        if reason:
            log.warning("telegram notification not sent: %s", reason)
            return
        text = f"{title[:200]}: {message[:3000]}" if message else title[:200]
        self.api.send_long(self.chat_id, text)
        log.info("telegram notification sent", extra={"data": {"title": title[:80]}})
