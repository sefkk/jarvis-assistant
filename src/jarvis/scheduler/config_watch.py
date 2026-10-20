"""The scheduler re-reads config.toml (and config.local.toml) while it runs, so a schedule change - edited by
hand or written by Jarvis ("send the morning brief at 11") - takes effect on the next check loop without
`jarvis services reinstall`.

`ConfigWatch.current()` is called at the start of every loop: it stats both files and re-parses only when a
modification time changed. A config that does not parse is logged, reported once (Mac notification +
Telegram, with the reason) and otherwise ignored: the jobs keep running with the last valid config until
the file is fixed. Timers and alarms live in SQLite and are not touched by a reload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from jarvis.config import load_config, local_config_path
from jarvis.exceptions import ConfigError
from jarvis.i18n import t
from jarvis.scheduler.notify import notifier_for

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

log = logging.getLogger("jarvis.scheduler.config")

# The notice's title and text come from locales/<language>/timers.toml; read as attributes they are the
# current main language's (INVALID_MESSAGE unformatted).
_TEXTS = {"NOTIFICATION_TITLE": "timers.config.title", "INVALID_MESSAGE": "timers.config.invalid"}


def __getattr__(name: str) -> str:
    key = _TEXTS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return t(key)


class ConfigWatch:
    """Hands out the newest valid config; see the module docstring."""

    def __init__(self, config: "Config", paths: "Paths", notifier=None) -> None:
        self.config = config
        self.paths = paths
        self.stamp = self._stamp()
        self.reloads = 0
        self.last_error: str | None = None  # the error already reported; None while the config parses
        self._notifier: Callable[["Config"], object] | None = notifier  # tests pass a factory

    def _stamp(self) -> tuple[int, int] | None:
        try:
            local = local_config_path(self.paths.config_file)
            return (self.paths.config_file.stat().st_mtime_ns, local.stat().st_mtime_ns if local.is_file() else 0)
        except OSError:
            return None

    def current(self) -> "Config":
        stamp = self._stamp()
        if stamp is None or stamp == self.stamp:
            return self.config
        self.stamp = stamp
        try:
            self.config = load_config(self.paths.config_file)
        except ConfigError as exc:
            self._report(str(exc))
            return self.config
        self.reloads += 1
        self.last_error = None
        log.info("config reloaded: %s changed", self.paths.config_file.name)
        return self.config

    def _report(self, reason: str) -> None:
        log.error("config changed but does not parse; keeping the previous one: %s", reason)
        if reason == self.last_error:  # the same broken file saved again: said once
            return
        self.last_error = reason
        try:
            notifier = self._notifier(self.config) if self._notifier else notifier_for(self.config, ["mac", "telegram"])
            notifier.notify(t("timers.config.title"), t("timers.config.invalid", reason=reason))
        except Exception:  # the notification is a courtesy; the loop must go on
            log.exception("could not report the invalid config")


__all__ = ["ConfigWatch", "INVALID_MESSAGE", "NOTIFICATION_TITLE"]
