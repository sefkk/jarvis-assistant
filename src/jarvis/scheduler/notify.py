"""Notification channels. The scheduler and the timer tools talk to a `Notifier`;
The Telegram channel sits behind the same interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from jarvis.utils.applescript import run_applescript

log = logging.getLogger("jarvis.notify")

# Values travel as argv: title, message, sound name (never interpolated).
NOTIFICATION_SCRIPT = """on run argv
    set theTitle to item 1 of argv
    set theMessage to item 2 of argv
    set theSound to item 3 of argv
    if theSound is "" then
        display notification theMessage with title theTitle
    else
        display notification theMessage with title theTitle sound name theSound
    end if
    return "ok"
end run"""


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def notify(self, title: str, message: str) -> None: ...


class MacNotifier(Notifier):
    name = "mac"

    def __init__(self, sound: str = "Glass") -> None:
        self.sound = sound

    def notify(self, title: str, message: str) -> None:
        run_applescript(NOTIFICATION_SCRIPT, [title[:200], message[:500], self.sound[:50]], timeout=10)


class MultiNotifier(Notifier):
    """Fans out to every channel; one failing channel never blocks the others."""

    name = "multi"

    def __init__(self, channels: list[Notifier]) -> None:
        self.channels = channels

    def notify(self, title: str, message: str) -> None:
        for channel in self.channels:
            try:
                channel.notify(title, message)
            except Exception as exc:  # a broken channel must not stop a timer from firing elsewhere
                log.error("notification through %s failed: %s", channel.name, exc)


def notifier_for(config, channel_names: list[str] | None = None) -> Notifier:
    """Build the notifier from `timers.notify_channels` (or `channel_names`). Unknown or unavailable
    channels are skipped."""
    channels: list[Notifier] = []
    for name in channel_names if channel_names is not None else config.timers.notify_channels:
        if name == "mac":
            channels.append(MacNotifier(config.timers.notification_sound))
        elif name == "telegram":
            try:
                from jarvis.interfaces.telegram_notify import TelegramNotifier

                channels.append(TelegramNotifier(config))
            except Exception as exc:  # not built yet or not configured
                log.warning("telegram notifications unavailable: %s", exc)
    if not channels:
        channels.append(MacNotifier(config.timers.notification_sound))
    return channels[0] if len(channels) == 1 else MultiNotifier(channels)


def sleep_notifier_for(config) -> Notifier:
    """The channels for a scheduled Mac sleep (tools/power.py): always the Mac *and* Telegram when the
    bot is configured, whatever `timers.notify_channels` says, because the one-minute notice is the
    user's chance to cancel from the phone."""
    return notifier_for(config, ["mac", "telegram"])


def alarm_notifiers_for(config) -> tuple[Notifier, Notifier | None]:
    """(Mac, Telegram or None) for alarms (scheduler/alarm.py): the Telegram channel is the phone backup
    and the "missed" message, so it is built whenever the bot is configured, whatever `timers.notify_channels` says."""
    mac = MacNotifier(config.timers.notification_sound)
    telegram: Notifier | None = None
    try:
        from jarvis.interfaces.telegram_notify import TelegramNotifier  # noqa: PLC0415

        telegram = TelegramNotifier(config)
    except Exception as exc:  # not configured
        log.info("telegram unavailable for alarms: %s", exc)
    return mac, telegram
