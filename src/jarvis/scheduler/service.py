"""The background scheduler process behind `jarvis scheduler run`.

Minimal on purpose: its own SQLite connection, the configured notifier, one `fire_due` per
tick. No LLM, no Assistant. launchd keeps it alive (see launchd.py); it logs through the
normal logging setup that `main.bootstrap()` configures.

The same loop also drives the periodic jobs that must run with the CLI closed:
the presence scan (`jarvis.presence.engine.tick`, every `presence.scan_interval_s`, only on the
home network), the optional internet checks (`jarvis.network.periodic.tick`, off by default), the alarm
keep-awake and the automatic morning brief (`jarvis.scheduler.brief.tick`, `[brief.schedule]`).
Each job decides itself whether it is due and returns at once otherwise; a failing job is logged
and never stops the timers. The loop re-reads config.toml when its modification time changed
(config_watch.py), so the jobs' schedules follow the file without a restart.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import TYPE_CHECKING, Callable

from jarvis import services
from jarvis.observability.db import Database
from jarvis.scheduler.config_watch import ConfigWatch
from jarvis.scheduler.engine import fire_due
from jarvis.scheduler.notify import alarm_notifiers_for, notifier_for, sleep_notifier_for
from jarvis.scheduler.store import TimerStore

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

log = logging.getLogger("jarvis.scheduler")

DEFAULT_INTERVAL_S = 10.0
LATE_AFTER_S = 120.0


def _presence_job(config, paths, db, notifier) -> None:
    from jarvis.presence import engine  # noqa: PLC0415 - imported here so a broken module cannot stop the timers

    engine.tick(config, paths, db, notifier)


def _network_job(config, paths, db, notifier) -> None:  # noqa: ARG001 - the jobs share one signature
    from jarvis.network import periodic  # noqa: PLC0415

    periodic.tick(config, db, notifier)


def _alarm_keep_awake_job(config, paths, db, notifier) -> None:
    from jarvis.scheduler import alarm  # noqa: PLC0415

    alarm.keep_awake_job(config, paths, db, notifier)


def _brief_job(config, paths, db, notifier) -> None:
    from jarvis.scheduler import brief  # noqa: PLC0415

    brief.job(config, paths, db, notifier)


# name -> job(config, paths, db, notifier); tests replace the list
PERIODIC_JOBS: list[tuple[str, "Callable[..., None]"]] = [
    ("presence", _presence_job),
    ("network", _network_job),
    ("alarm_keep_awake", _alarm_keep_awake_job),
    ("brief", _brief_job),
]


def alarm_handler_for(config: "Config", paths: "Paths", *, blocking: bool = False):
    """The alarm handler (ringer, phone backup, Mac + Telegram). None when it cannot be built: the alarm
    rows then notify like timers instead of stopping the scheduler."""
    from jarvis.scheduler import alarm  # noqa: PLC0415

    try:
        mac, telegram = alarm_notifiers_for(config)
        return alarm.handler_for(config, paths, mac=mac, telegram=telegram, blocking=blocking)
    except Exception:
        log.exception("alarm handler unavailable; alarms will notify like timers")
        return None


def run_periodic_jobs(config: "Config", paths: "Paths", db: Database, notifier) -> None:
    """One pass over the periodic jobs. Every job is isolated: an exception is logged and the
    next job still runs."""
    for name, job in PERIODIC_JOBS:
        try:
            job(config, paths, db, notifier)
        except Exception:  # never let presence or network checks take the timers down
            log.exception("periodic job %s failed", name)


def run_once(config: "Config", paths: "Paths") -> int:
    """One tick: fire whatever is due and return how many timers fired."""
    db = Database(paths.db_file)
    try:
        fired = fire_due(
            TimerStore(db),
            notifier_for(config),
            late_after_s=LATE_AFTER_S,
            sleep_notifier=sleep_notifier_for(config),
            alarm_handler=alarm_handler_for(config, paths, blocking=True),
        )  # one tick: ring to the end here
    finally:
        db.close()
    return len(fired)


def run_forever(
    config: "Config", paths: "Paths", interval_s: float = DEFAULT_INTERVAL_S, stop_event: threading.Event | None = None
) -> None:
    """Tick every `interval_s` seconds until `stop_event` is set (or SIGTERM/SIGINT arrives).
    Exceptions inside a tick are logged and the loop goes on."""
    stop = stop_event or threading.Event()
    _install_signal_handlers(stop)
    db = Database(paths.db_file)
    notifier = notifier_for(config)
    sleep_notifier = sleep_notifier_for(config)  # Mac + Telegram for the one-minute sleep notice
    alarm_handler = alarm_handler_for(config, paths)  # rings in a thread; the loop keeps ticking
    store = TimerStore(db)
    watch = ConfigWatch(config, paths)  # the periodic jobs see config changes on the next loop
    services.record_start(db, "scheduler", paths)  # the dashboard shows which config version the agent runs
    recorded_reloads = watch.reloads
    log.info("scheduler started (interval %.0fs, notifier %s, db %s)", interval_s, notifier.name, paths.db_file)
    try:
        while not stop.is_set():
            try:
                fired = fire_due(store, notifier, late_after_s=LATE_AFTER_S, sleep_notifier=sleep_notifier, alarm_handler=alarm_handler)
                if fired:
                    log.info("tick: %d timer(s) fired", len(fired))
            except Exception:  # a bad tick (locked db, corrupt row) must not kill the agent
                log.exception("scheduler tick failed")
            run_periodic_jobs(watch.current(), paths, db, notifier)
            if watch.reloads != recorded_reloads:
                recorded_reloads = watch.reloads
                services.record_start(db, "scheduler", paths)
            stop.wait(interval_s)
    finally:
        try:
            from jarvis.scheduler.alarm import KEEP_AWAKE  # noqa: PLC0415

            KEEP_AWAKE.stop()
        except Exception:
            log.debug("keep-awake stop failed", exc_info=True)
        db.close()
        log.info("scheduler stopped")


def _install_signal_handlers(stop: threading.Event) -> None:
    """Stop cleanly on SIGTERM (launchctl bootout) and SIGINT. Only possible in the main thread."""

    def handler(signum, frame) -> None:  # noqa: ARG001
        log.info("signal %s received, stopping", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread (tests), or not supported
            return
