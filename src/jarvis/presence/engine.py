"""The presence state machine and the scheduler entry point.

`evaluate` is pure: given the config, the stored states and one scan result it returns the new
states and the events (arrivals, departures). A person is home the moment any of their devices is
seen; away only after `away_after_min` without a sighting (a phone that sleeps for three minutes
stays home); unknown until the first scan. The first scan that does not see a person marks them
away without an event, because nothing was known before.

`tick` is what the scheduler calls every few seconds. It does nothing until `scan_interval_s` has
passed since the last scan, then: finds the gateway MAC and decides whether the Mac is on the home
network (emitting `mac_joined_home_network` / `mac_left_home_network` on a change); only when at
home, scans the configured devices, persists the states, records history for `track_history`
people and emits `person_arrived` / `person_left` (plus `i_arrived_home` / `i_left_home` for the
`is_me` person); then hands the events to the hooks. When the Mac is away, nobody is scanned and
the stored states are left as they were (the tools explain that the Mac is away). Every exception
is caught and logged: the scheduler must never die because of presence.

Logging: names, states and counts only (package docstring).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from jarvis.network import local
from jarvis.observability import logging_setup
from jarvis.presence import hooks as presence_hooks
from jarvis.presence.scanner import ScanResult, scan_devices
from jarvis.presence.store import PersonState, PresenceStore
from jarvis.utils.timeutil import now_utc

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.db import Database
    from jarvis.scheduler.notify import Notifier

log = logging.getLogger("jarvis.presence")

EVENT_KINDS = (
    "i_arrived_home",
    "i_left_home",
    "person_arrived",
    "person_left",
    "mac_joined_home_network",
    "mac_left_home_network",
)
SWEEP_EVERY = 5  # the UDP sweep runs on scans 1, 6, 11, ... (or when a device has no known IP)


@dataclass
class Event:
    kind: str
    at: datetime
    person: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown presence event kind {self.kind!r}")


# --- the pure part ----------------------------------------------------------------------------------------


def evaluate(
    config: "Config",
    states: dict[str, PersonState],
    scan: ScanResult,
    now: datetime,
) -> tuple[dict[str, PersonState], list[Event]]:
    presence = config.presence
    away_after = timedelta(minutes=presence.away_after_min)
    new_states: dict[str, PersonState] = {}
    events: list[Event] = []
    for person in presence.people:
        prev = states.get(person.name) or PersonState(person=person.name)
        seen = [d for d in scan.devices if d.person == person.name and d.seen]
        if seen:
            state = PersonState(
                person=person.name,
                status="home",
                last_seen=now,
                since=prev.since if prev.status == "home" and prev.since else now,
                last_device=seen[0].mac,
                last_ip=seen[0].ip,
            )
            if prev.status != "home":
                events.append(Event("person_arrived", now, person.name))
                if person.is_me:
                    events.append(Event("i_arrived_home", now, person.name))
        elif prev.status == "home":
            if prev.last_seen is not None and now - prev.last_seen < away_after:
                state = prev  # debounce: a sleeping phone is not a departure
            else:
                state = PersonState(person.name, "away", prev.last_seen, now, prev.last_device, prev.last_ip)
                events.append(Event("person_left", now, person.name))
                if person.is_me:
                    events.append(Event("i_left_home", now, person.name))
        elif prev.status == "unknown":
            state = PersonState(person.name, "away", None, now, None, None)  # first scan: nothing known before
        else:
            state = prev
        new_states[person.name] = state
    return new_states, events


# --- the scheduler entry point -----------------------------------------------------------------------------

Scanner = Callable[..., ScanResult]


def tick(
    config: "Config",
    paths: "Paths",
    db: "Database",
    notifier: "Notifier | None",
    *,
    now: datetime | None = None,
    run: local.Runner = local.run_command,
    scanner: Scanner = scan_devices,
    hooks: Callable[..., list[str]] = presence_hooks.dispatch,
    voice_status: Callable[[], bool] | None = None,
    force: bool = False,
) -> list[Event]:
    """One scheduler step. Returns the events it emitted (empty when it was not time to scan)."""
    try:
        return _tick(
            config, paths, db, notifier, now=now or now_utc(), run=run, scanner=scanner, hooks=hooks, voice_status=voice_status, force=force
        )
    except Exception:
        log.exception("presence tick failed")
        return []


def scan_once(config: "Config", paths: "Paths", db: "Database", notifier: "Notifier | None" = None, **kwargs) -> list[Event]:
    """`jarvis presence scan`: a scan right now, regardless of the interval."""
    return tick(config, paths, db, notifier, force=True, **kwargs)


def _tick(config, paths, db, notifier, *, now, run, scanner, hooks, voice_status, force) -> list[Event]:
    presence = config.presence
    store = PresenceStore(db)
    last = store.last_scan_at()
    if not force and last is not None and (now - last).total_seconds() < presence.scan_interval_s:
        return []
    store.set_last_scan_at(now)
    if not presence.home_gateway_macs:
        log.debug("presence: no home network configured; nothing to scan")
        return []

    _, mac = local.gateway_mac(run)
    at_home = presence.is_home_gateway(mac)
    previous = store.mac_at_home()
    events: list[Event] = []
    if previous is not None and previous != at_home:
        events.append(Event("mac_joined_home_network" if at_home else "mac_left_home_network", now))
    store.set_mac_at_home(at_home, mac if at_home else None)  # a foreign gateway is not recorded

    for person in presence.people:
        if not person.track_history and store.purge_history(person.name):
            log.info("presence: history of %s removed (track_history = false)", person.name)
    summary: dict[str, object] = {"tool": "presence", "decision": "scan", "mac_at_home": at_home}
    if at_home:
        count = store.bump_scan_count()
        macs = [m for p in presence.people for m in p.devices]
        known = store.known_ips(macs)
        sweep = (count - 1) % SWEEP_EVERY == 0 or any(m not in known for m in macs)
        scan = scanner(config, run=run, known_ips=known, sweep=sweep)
        states = {s.person: s for s in store.all()}
        new_states, person_events = evaluate(config, states, scan, now)
        for state in new_states.values():
            store.set_state(state)
        for probe in scan.seen():
            if probe.ip:
                store.set_device_ip(probe.mac, probe.ip)
        for event in person_events:
            if event.kind in ("person_arrived", "person_left"):
                person = presence.person(event.person or "")
                if person is not None:
                    store.add_event(
                        person, "arrived" if event.kind == "person_arrived" else "left", now, new_states[person.name].last_device
                    )
        events.extend(person_events)
        summary.update(
            scan_count=count,
            swept=scan.swept,
            devices_seen=len(scan.seen()),
            states={name: s.status for name, s in new_states.items()},
        )
        log.info("presence scan %d: %s", count, ", ".join(f"{n}={s.status}" for n, s in new_states.items()) or "no people configured")
    else:
        log.info("presence: Mac is not on the home network; people are not scanned")

    summary["events"] = [e.kind + (f"({e.person})" if e.person else "") for e in events]
    if events:
        log.info("presence events: %s", ", ".join(summary["events"]))  # type: ignore[arg-type]
        hooks(events, config, paths, notifier, voice_status=voice_status)
    logging_setup.audit(summary)
    return events
