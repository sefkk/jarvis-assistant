"""Presence state in the local SQLite database (migration 6): one `presence_state` row per
person, `presence_events` (arrived / left) only for people with `track_history = true`, and a few
`kv` keys for the engine (last scan time, scan counter, whether the Mac is on the home network,
the last IP of each configured device). Parameterized SQL only; timestamps are UTC ISO strings.

Deleting a person from the config leaves their rows behind; reads never fail on them, they are
simply not shown (the tools iterate the config, not the table).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from jarvis.utils.timeutil import local_tz, now_iso

if TYPE_CHECKING:
    from jarvis.config import PresencePerson
    from jarvis.observability.db import Database

STATUSES = ("home", "away", "unknown")
EVENTS = ("arrived", "left")
RANGES = ("day", "week", "month")

KV_LAST_SCAN_AT = "presence.last_scan_at"
KV_MAC_AT_HOME = "presence.mac_at_home"
KV_GATEWAY_MAC = "presence.gateway_mac"
KV_SCAN_COUNT = "presence.scan_count"
KV_DEVICE_IP = "presence.ip."  # + mac


@dataclass
class PersonState:
    person: str
    status: str = "unknown"
    last_seen: datetime | None = None
    since: datetime | None = None
    last_device: str | None = None
    last_ip: str | None = None


@dataclass
class PresenceEvent:
    id: int
    person: str
    event: str
    at: datetime
    device: str | None


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class PresenceStore:
    def __init__(self, db: "Database") -> None:
        self.db = db

    # --- state ---------------------------------------------------------------------------------------

    def get(self, person: str) -> PersonState | None:
        row = self.db.conn.execute("SELECT * FROM presence_state WHERE person = ?", (person,)).fetchone()
        return self._state(row) if row else None

    def all(self) -> list[PersonState]:
        rows = self.db.conn.execute("SELECT * FROM presence_state ORDER BY person").fetchall()
        return [self._state(r) for r in rows]

    def set_state(self, state: PersonState) -> None:
        if state.status not in STATUSES:
            raise ValueError(f"unknown presence status {state.status!r}")
        self.db.conn.execute(
            """INSERT INTO presence_state (person, status, last_seen, since, last_device, last_ip, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(person) DO UPDATE SET status = excluded.status, last_seen = excluded.last_seen,
                   since = excluded.since, last_device = excluded.last_device, last_ip = excluded.last_ip,
                   updated_at = excluded.updated_at""",
            (state.person, state.status, to_iso(state.last_seen), to_iso(state.since), state.last_device, state.last_ip, now_iso()),
        )

    @staticmethod
    def _state(row) -> PersonState:
        status = row["status"] if row["status"] in STATUSES else "unknown"
        return PersonState(
            person=row["person"],
            status=status,
            last_seen=from_iso(row["last_seen"]),
            since=from_iso(row["since"]),
            last_device=row["last_device"],
            last_ip=row["last_ip"],
        )

    # --- events (history) ----------------------------------------------------------------------------

    def add_event(self, person: "PresencePerson", event: str, at: datetime, device: str | None = None) -> bool:
        """Record an arrival or departure. Takes the configured person so `track_history = false`
        is enforced here, not by every caller. Returns False when nothing was stored."""
        if event not in EVENTS:
            raise ValueError(f"unknown presence event {event!r}")
        if not person.track_history:
            return False
        self.db.conn.execute(
            "INSERT INTO presence_events (person, event, at, device) VALUES (?, ?, ?, ?)",
            (person.name, event, to_iso(at), device),
        )
        return True

    def purge_history(self, person_name: str) -> int:
        """Delete every stored event of a person (called when `track_history` is false, so rows
        recorded before the flag was switched off do not linger). Returns the number removed."""
        cur = self.db.conn.execute("DELETE FROM presence_events WHERE person = ?", (person_name,))
        return int(cur.rowcount)

    def history(self, person: str | None, range_: str, now: datetime | None = None) -> list[PresenceEvent]:
        """Events since local midnight (day), the last 7 days (week) or the last 30 days (month)."""
        if range_ not in RANGES:
            raise ValueError(f"unknown range {range_!r} (day, week, month)")
        now = (now or datetime.now(timezone.utc)).astimezone(local_tz())
        if range_ == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now - timedelta(days=7 if range_ == "week" else 30)
        sql = "SELECT id, person, event, at, device FROM presence_events WHERE at >= ?"
        params: list[object] = [to_iso(start)]
        if person is not None:
            sql += " AND person = ?"
            params.append(person)
        rows = self.db.conn.execute(sql + " ORDER BY at, id", params).fetchall()
        return [PresenceEvent(r["id"], r["person"], r["event"], from_iso(r["at"]) or start, r["device"]) for r in rows]

    # --- engine bookkeeping (kv) -----------------------------------------------------------------------

    def last_scan_at(self) -> datetime | None:
        return from_iso(self.db.kv_get(KV_LAST_SCAN_AT))

    def set_last_scan_at(self, at: datetime) -> None:
        self.db.kv_set(KV_LAST_SCAN_AT, to_iso(at) or "")

    def mac_at_home(self) -> bool | None:
        """None until the first scan with a configured home network."""
        value = self.db.kv_get(KV_MAC_AT_HOME)
        return None if value is None else value == "1"

    def set_mac_at_home(self, at_home: bool, gateway_mac: str | None) -> None:
        self.db.kv_set(KV_MAC_AT_HOME, "1" if at_home else "0")
        self.db.kv_set(KV_GATEWAY_MAC, gateway_mac or "")

    def gateway_mac(self) -> str | None:
        return self.db.kv_get(KV_GATEWAY_MAC) or None

    def scan_count(self) -> int:
        try:
            return int(self.db.kv_get(KV_SCAN_COUNT) or 0)
        except ValueError:
            return 0

    def bump_scan_count(self) -> int:
        count = self.scan_count() + 1
        self.db.kv_set(KV_SCAN_COUNT, str(count))
        return count

    def device_ip(self, mac: str) -> str | None:
        return self.db.kv_get(KV_DEVICE_IP + mac) or None

    def set_device_ip(self, mac: str, ip: str) -> None:
        self.db.kv_set(KV_DEVICE_IP + mac, ip)

    def known_ips(self, macs: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for mac in macs:
            ip = self.device_ip(mac)
            if ip:
                found[mac] = ip
        return found
