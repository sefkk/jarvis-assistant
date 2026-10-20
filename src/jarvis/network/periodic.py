"""Optional periodic internet checks through the scheduler agent. Off by default:
`network.periodic_check_min` runs the quick diagnostics (no data used), `periodic_speedtest_hours`
a full speed test. The scheduler calls `tick` every few seconds; it returns at once when nothing
is due. The user is notified only on a transition (internet down, internet back), never on the
first check and never while the state is unchanged. State lives in the `kv` table so a restart of
the agent does not repeat notifications.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from jarvis.i18n import t
from jarvis.network.diagnose import likely_cause
from jarvis.network.local import Runner, run_command
from jarvis.network.probes import run_diagnostics
from jarvis.network.speedtest import run_speed_test
from jarvis.network.store import NetworkStore, iso_utc, parse_iso
from jarvis.utils.timeutil import now_utc

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.observability.db import Database
    from jarvis.scheduler.notify import Notifier

log = logging.getLogger("jarvis.network")

KV_LAST_QUICK = "network.last_quick_check"
KV_LAST_SPEED = "network.last_speedtest"
KV_ONLINE = "network.online"  # "1" / "0": the usable-internet state after the last periodic check

NOTIFY_TITLE = "Jarvis"


def _due(last: str | None, now: datetime, interval: timedelta) -> bool:
    parsed = parse_iso(last)
    return parsed is None or now - parsed >= interval


def tick(
    config: "Config",
    db: "Database",
    notifier: "Notifier",
    *,
    now: datetime | None = None,
    run: Runner = run_command,
    diagnostics: Callable[..., Any] = run_diagnostics,
    speedtest: Callable[..., Any] = run_speed_test,
) -> str | None:
    """One scheduler pass. Returns a short log line when a check ran, None when nothing was due.
    Never raises: the scheduler loop must survive a broken probe."""
    net = config.network
    if net.periodic_check_min <= 0 and net.periodic_speedtest_hours <= 0:
        return None
    now = now or now_utc()
    try:
        speed_due = net.periodic_speedtest_hours > 0 and _due(db.kv_get(KV_LAST_SPEED), now, timedelta(hours=net.periodic_speedtest_hours))
        quick_due = net.periodic_check_min > 0 and _due(db.kv_get(KV_LAST_QUICK), now, timedelta(minutes=net.periodic_check_min))
        if not speed_due and not quick_due:
            return None
        return _check(config, db, notifier, now=now, run=run, diagnostics=diagnostics, speedtest=speedtest, full=speed_due)
    except Exception as exc:
        log.exception("periodic network check failed")
        return f"network check failed: {type(exc).__name__}: {exc}"


def _check(config, db, notifier, *, now, run, diagnostics, speedtest, full: bool) -> str:
    diag = diagnostics(config, run=run)
    speed = None
    if full:
        db.kv_set(KV_LAST_SPEED, iso_utc(now))  # even when it fails: never hammer the test endpoint
        try:
            speed = speedtest(config, run=run)
        except Exception as exc:  # the diagnostics are still worth storing as a quick row
            log.warning("periodic speed test failed: %s", exc)
    db.kv_set(KV_LAST_QUICK, iso_utc(now))  # a full test includes the quick diagnostics
    verdict = likely_cause(diag, speed)
    kind = "full" if speed is not None else "quick"
    NetworkStore(db).add(kind, diag, speed, verdict, now=now)

    previous = db.kv_get(KV_ONLINE)
    state = "0" if verdict.failure else "1"
    db.kv_set(KV_ONLINE, state)
    note = ""
    if previous is not None and previous != state:
        message = t("network.up") if state == "1" else t("network.down", cause=t(f"network.cause.{verdict.key}"))
        try:
            notifier.notify(NOTIFY_TITLE, message)
            note = ", notified: " + ("back online" if state == "1" else "offline")
        except Exception as exc:  # a broken notifier must not lose the state change
            log.error("network notification failed: %s", exc)
            note = ", notification failed"
    return f"network {kind} check: {verdict.key}{note}"
