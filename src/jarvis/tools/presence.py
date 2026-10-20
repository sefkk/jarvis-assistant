"""Presence tools: `am_i_home`, `who_is_home`, `is_home`, `presence_history`
and `list_network_devices`, plus the doctor lines and `jarvis presence status`.

All SAFE and read-only. `list_network_devices` taints the turn: hostnames are chosen by whoever
owns the device, so each one is sanitized, cut to one line and capped before it reaches the model.

Remote restriction: from Telegram every presence tool raises ToolBlockedError
unless `presence.allow_remote` is on; `am_i_home` alone is also allowed with
`presence.allow_remote_self`. `remote_patterns(config)` tells the Telegram bot which patterns to
add to its tool subset, so a blocked tool is not even offered to the model there.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolBlockedError, ToolExecutionError
from jarvis.i18n import t
from jarvis.network import local
from jarvis.observability.db import Database
from jarvis.presence import scanner
from jarvis.presence.store import PersonState, PresenceStore
from jarvis.security.sanitize import for_model
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolContext, ToolResult
from jarvis.utils.timeutil import format_local, now_utc

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.presence")

REMOTE_CHANNEL = "telegram"
LOCAL_CHANNELS = ("cli", "voice")  # everything else counts as remote
SETUP_HINT = "home network not configured: run `jarvis presence setup`"
NEVER_SCANNED = (
    "no presence scan has run yet: the scheduler agent runs the scans (`jarvis scheduler install`), or run `jarvis presence scan` once"
)
ANSWER_HOME = "Yes, the user is home."
ANSWER_PHONE_AWAY = "The user's phone is not on the network, so they are probably out (last seen: {last_seen})."
ANSWER_MAC_AWAY = "The Mac is not on the home network, so the user is not home."


def remote_patterns(config: "Config") -> list[str]:
    """Tool patterns the Telegram bot may add to its subset."""
    if config.presence.allow_remote:
        return ["presence.*"]
    if config.presence.allow_remote_self:
        return ["presence.am_i_home"]
    return []


def _local(dt: datetime | None) -> str:
    return format_local(dt) if dt else "never"


def _person_line(state: PersonState | None, name: str) -> str:
    if state is None or state.status == "unknown":
        return f"- {name}: unknown (not scanned yet)"
    since = f", since {_local(state.since)}" if state.since else ""
    return f"- {name}: {state.status}{since}; last seen {_local(state.last_seen)}"


# --- base ------------------------------------------------------------------------------------------------


class _PresenceTool(Tool):
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = True

    def __init__(self, ctx: ToolContext | None = None, db: Database | None = None) -> None:
        super().__init__(ctx)
        self._db = db
        self._own_db: Database | None = None

    @property
    def db(self) -> Database:
        if self._db is not None:
            return self._db
        ctx = self.require_ctx()
        if ctx.runtime is not None and ctx.runtime.db is not None:
            return ctx.runtime.db
        if self._own_db is None:  # no Assistant around (evals, scripts): open the project db
            self._own_db = Database(ctx.paths.db_file)
        return self._own_db

    @property
    def store(self) -> PresenceStore:
        return PresenceStore(self.db)

    def _check_remote(self, tool_name: str) -> None:
        ctx = self.require_ctx()
        runtime = ctx.runtime
        # No runtime means no Assistant (the CLI commands, the doctor, evals): local by definition.
        # Otherwise only the local channels pass; any remote channel (Telegram today, anything
        # added later) needs the explicit opt-in below.
        if runtime is None or getattr(runtime, "channel", "cli") in LOCAL_CHANNELS:
            return
        presence = ctx.config.presence
        if presence.allow_remote or (presence.allow_remote_self and tool_name == "am_i_home"):
            return
        raise ToolBlockedError("presence tools are not available from Telegram (presence.allow_remote = false)")


# --- am_i_home ----------------------------------------------------------------------------------------


class AmIHome(_PresenceTool):
    name = "am_i_home"
    description = (
        "Answer 'am I home?': whether the Mac is on the home network (router MAC) and whether the user's own phone "
        "was seen there recently. Returns the combined answer plus both signals. Use for 'am I home', "
        "'evde miyim', 'neredeyim' questions."
    )

    def run(self, args: BaseModel) -> ToolResult:
        self._check_remote(self.name)
        ctx = self.require_ctx()
        presence = ctx.config.presence
        if not presence.home_gateway_macs:
            return ToolResult(SETUP_HINT)
        store = self.store
        mac_home = store.mac_at_home()
        if mac_home is None:
            return ToolResult(NEVER_SCANNED)
        last_scan = _local(store.last_scan_at())
        if not mac_home:
            return ToolResult(f"{ANSWER_MAC_AWAY} (Mac not on the home network as of the last scan at {last_scan}.)")
        me = presence.me()
        if me is None:
            return ToolResult(
                f"Mac is on the home network (last scan {last_scan}), but no person has is_me = true in presence.people, "
                "so the phone signal is unavailable: run `jarvis presence setup` and pick your phone."
            )
        state = store.get(me.name)
        if state is not None and state.status == "home":
            return ToolResult(f"{ANSWER_HOME} (Mac on the home network; {me.name}'s phone last seen {_local(state.last_seen)}.)")
        last_seen = _local(state.last_seen) if state and state.last_seen else "never"
        return ToolResult(f"{ANSWER_PHONE_AWAY.format(last_seen=last_seen)} (Mac on the home network; last scan {last_scan}.)")


# --- who_is_home / is_home ------------------------------------------------------------------------------


class WhoIsHome(_PresenceTool):
    name = "who_is_home"
    description = (
        "List every configured household member with their presence status (home / away / unknown) and when they were "
        "last seen on the home network, plus whether the Mac itself is on the home network. Use for 'who is home', "
        "'evde kimler var'."
    )

    def run(self, args: BaseModel) -> ToolResult:
        self._check_remote(self.name)
        ctx = self.require_ctx()
        presence = ctx.config.presence
        if not presence.home_gateway_macs:
            return ToolResult(SETUP_HINT)
        store = self.store
        mac_home = store.mac_at_home()
        if mac_home is None:
            return ToolResult(NEVER_SCANNED)
        lines = [
            f"Mac on the home network: {'yes' if mac_home else 'no (people are not scanned while the Mac is away; states below are the last known)'}",
            f"Last scan: {_local(store.last_scan_at())}",
        ]
        if not presence.people:
            lines.append("No people configured: run `jarvis presence setup`.")
        for person in presence.people:
            lines.append(_person_line(store.get(person.name), person.name + (" (me)" if person.is_me else "")))
        return ToolResult("\n".join(lines))


class IsHomeInput(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="The person's name as configured in presence.people.")


class IsHome(_PresenceTool):
    name = "is_home"
    description = (
        "Whether one configured household member is at home, with the time they were last seen. Use for 'is X home?', 'X evde mi?'."
    )
    input_model = IsHomeInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, IsHomeInput)
        self._check_remote(self.name)
        ctx = self.require_ctx()
        presence = ctx.config.presence
        person = presence.person(args.name)
        if person is None:
            return ToolResult(
                f"{args.name.strip()} is not a configured person (presence.people in config.local.toml, written by `jarvis presence setup`)."
            )
        if not presence.home_gateway_macs:
            return ToolResult(SETUP_HINT)
        store = self.store
        if store.mac_at_home() is None:
            return ToolResult(NEVER_SCANNED)
        line = _person_line(store.get(person.name), person.name)
        note = "" if store.mac_at_home() else " (the Mac is not on the home network, so this is the last known state)"
        return ToolResult(line.lstrip("- ") + note)


# --- presence_history --------------------------------------------------------------------------------------


class PresenceHistoryInput(BaseModel):
    name: str | None = Field(default=None, max_length=64, description="A configured person, or omit for everyone with history.")
    range: Literal["day", "week", "month"] = Field(default="day", description="day = since midnight, week = 7 days, month = 30 days.")


class PresenceHistory(_PresenceTool):
    name = "presence_history"
    description = (
        "Arrivals and departures of configured household members (only those with track_history = true) for today, "
        "the last week or the last month. Use for 'when did X get home?', 'X ne zaman geldi?', 'kim ne zaman çıktı?'."
    )
    input_model = PresenceHistoryInput
    delegate_ok = False  # movement patterns are the most sensitive presence data; the delegate never needs them

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PresenceHistoryInput)
        self._check_remote(self.name)
        ctx = self.require_ctx()
        presence = ctx.config.presence
        if args.name:
            person = presence.person(args.name)
            if person is None:
                return ToolResult(
                    f"{args.name.strip()} is not a configured person (presence.people in config.local.toml, written by `jarvis presence setup`)."
                )
            if not person.track_history:
                return ToolResult(f"No history is kept for {person.name} (track_history = false): only the current state is stored.")
            names = [person.name]
        else:
            names = [p.name for p in presence.people if p.track_history]
            if not names:
                return ToolResult("No one has track_history = true, so no arrivals or departures are recorded.")
        events = [e for e in self.store.history(None, args.range, now_utc()) if e.person in names]
        if not events:
            return ToolResult(f"No arrivals or departures recorded for {', '.join(names)} in this {args.range}.")
        lines = [f"{len(events)} event(s) this {args.range}:"]
        lines += [f"- {format_local(e.at)}: {e.person} {e.event}" for e in events]
        return ToolResult("\n".join(lines))


# --- list_network_devices ------------------------------------------------------------------------------


class ListNetworkDevices(_PresenceTool):
    name = "list_network_devices"
    description = (
        "List the devices currently seen on the local network (IP, MAC, vendor from the MAC prefix, hostname) and which "
        "ones belong to configured people. Mainly for setup. Hostnames are untrusted text chosen by the device owners."
    )
    input_model = EmptyInput
    taints = True
    taint_source = "network"
    delegate_ok = False

    def __init__(
        self,
        ctx: ToolContext | None = None,
        db: Database | None = None,
        *,
        run: local.Runner = local.run_command,
        discover=scanner.discover_devices,
    ) -> None:
        super().__init__(ctx, db)
        self._run = run
        self._discover = discover

    def run(self, args: BaseModel) -> ToolResult:
        self._check_remote(self.name)
        ctx = self.require_ctx()
        try:
            devices = self._discover(ctx.config, self._run)
        except Exception as exc:
            raise ToolExecutionError(f"network discovery failed: {type(exc).__name__}") from None
        if not devices:
            return ToolResult("No devices found on the local network (offline, or the ARP table is empty).")
        lines = [
            f"{len(devices)} device(s) on the local network (hostnames are untrusted, chosen by the device owners):",
            "ip | mac | vendor | hostname | person",
        ]
        for d in devices:
            hostname = scanner.clean_hostname(for_model(d.hostname or "")) or "-"
            lines.append(f"{d.ip} | {d.mac} | {d.vendor} | {hostname} | {d.person or '-'}")
        log.info("list_network_devices: %d device(s), %d registered", len(devices), sum(1 for d in devices if d.person))
        return ToolResult("\n".join(lines))


# --- doctor and status ---------------------------------------------------------------------------------------


def _remote_detail(config: "Config") -> str:
    if config.presence.allow_remote:
        return "allowed from Telegram (allow_remote = true)"
    if config.presence.allow_remote_self:
        return "only am_i_home from Telegram (allow_remote_self = true)"
    return "blocked from Telegram (allow_remote = false)"


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """`jarvis doctor` lines. Never raises; an unconfigured presence is informational, not a failure."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    presence = config.presence
    checks: list[Check] = []
    gateways = len(presence.home_gateway_macs)
    checks.append(
        Check(
            "presence home network",
            True,
            f"configured ({gateways} gateway MAC(s))" if gateways else "not configured: run `jarvis presence setup`",
        )
    )
    me = presence.me()
    if presence.people:
        detail = f"{len(presence.people)} configured, me = {me.name if me else 'nobody (no is_me = true)'}"
    else:
        detail = "none configured: run `jarvis presence setup`"
    checks.append(Check("presence people", True, detail))
    try:
        db = Database(paths.db_file)
        try:
            store = PresenceStore(db)
            last, at_home = store.last_scan_at(), store.mac_at_home()
        finally:
            db.close()
        if last is None or at_home is None:
            detail = "never: the scheduler agent runs the scans; `jarvis scheduler install`"
        else:
            detail = f"{format_local(last)}, Mac at home: {'yes' if at_home else 'no'}"
        checks.append(Check("presence last scan", True, detail))
    except Exception as exc:
        checks.append(Check("presence last scan", False, f"cannot read the database: {type(exc).__name__}: {exc}"))
    detail = _remote_detail(config)
    listed = [p for p in config.telegram.remote_allowed_tools if p == "presence.*" or p.startswith("presence.")]
    if listed and not config.presence.allow_remote:
        detail += (
            f"; note: telegram.remote_allowed_tools lists {', '.join(listed)}, which the tools still refuse in code"
            " (set presence.allow_remote / allow_remote_self instead and drop the pattern)"
        )
    checks.append(Check("presence remote", True, detail, warn=bool(listed) and not config.presence.allow_remote))
    return checks


def _status_person_line(state: PersonState | None, name: str) -> str:
    """`_person_line` in the user's language, for the terminal."""
    if state is None or state.status == "unknown":
        return t("presence.status.person_unknown", name=name)
    never = t("presence.status.never")
    since = t("presence.status.since", when=format_local(state.since)) if state.since else ""
    status = t(f"presence.status.state.{state.status}") if state.status in ("home", "away") else state.status
    return t(
        "presence.status.person",
        name=name,
        state=status,
        since=since,
        last_seen=format_local(state.last_seen) if state.last_seen else never,
    )


def status_lines(config: "Config", paths: "Paths", db: Database) -> list[str]:
    """`jarvis presence status`: the doctor's information plus every person's state, in the user's language."""
    presence = config.presence
    store = PresenceStore(db)
    lines = [
        t("presence.status.home_network", count=len(presence.home_gateway_macs))
        if presence.home_gateway_macs
        else t("presence.status.home_network_missing")
    ]
    last, at_home = store.last_scan_at(), store.mac_at_home()
    if last is None or at_home is None:
        lines.append(t("presence.status.last_scan_never"))
    else:
        lines.append(
            t(
                "presence.status.last_scan",
                when=format_local(last),
                number=store.scan_count(),
                at_home=t("presence.status.yes" if at_home else "presence.status.no"),
            )
        )
    remote = "allowed" if presence.allow_remote else "self" if presence.allow_remote_self else "blocked"
    lines.append(t(f"presence.status.remote_{remote}"))
    hooks = [
        name
        for name, on in (
            ("arrive-greet", presence.on_arrive_greet),
            ("leave-pause-voice", presence.on_leave_pause_voice),
            ("notify-person-changes", presence.notify_person_changes),
        )
        if on
    ]
    lines.append(t("presence.status.automations", names=", ".join(hooks)) if hooks else t("presence.status.automations_none"))
    if not presence.people:
        lines.append(t("presence.status.people_none"))
    for person in presence.people:
        state = store.get(person.name)
        label = (
            person.name
            + (t("presence.status.me") if person.is_me else "")
            + ("" if person.track_history else t("presence.status.no_history"))
        )
        lines.append(_status_person_line(state, label))
    return lines
