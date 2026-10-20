"""Internet tools: `internet_test` (diagnostics plus the networkQuality speed test,
verdict computed in code, every result stored) and `internet_history` (averages, worst times,
failures). Plus the `jarvis doctor` lines. SAFE, no taint: everything comes from local commands
and our own database, never from a web page.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError
from jarvis.network import probes, speedtest
from jarvis.network.diagnose import Verdict, likely_cause
from jarvis.network.probes import Diagnostics, run_diagnostics
from jarvis.network.speedtest import SpeedResult, run_speed_test
from jarvis.network.store import RANGES, NetworkStore, parse_iso
from jarvis.observability.db import Database
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.utils.timeutil import format_local

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

Range = Literal["day", "week", "month"]


def _n(value: float | int | None, unit: str = "") -> str:
    return "?" if value is None else f"{value:g}{unit}"


def format_report(diag: Diagnostics, speed: SpeedResult | None, verdict: Verdict, *, speed_note: str | None = None) -> str:
    """Compact structured text for the model: the verdict first, then the numbers."""
    lines = [f"verdict: {verdict.key} | likely cause: {verdict.cause}"]
    if speed is not None:
        lines.append(
            f"speed: download {_n(speed.download_mbps)} Mbit/s, upload {_n(speed.upload_mbps)} Mbit/s, "
            f"responsiveness {_n(speed.responsiveness_rpm)} RPM, latency {_n(speed.latency_ms)} ms (networkQuality, {speed.duration_s:g} s)"
        )
    elif speed_note:
        lines.append(f"speed: {speed_note}")
    if diag.gateway_ip:
        lines.append(f"router {diag.gateway_ip}: {_n(diag.gateway_latency_ms)} ms, {_n(diag.gateway_loss_pct)}% loss")
    else:
        lines.append("router: no default gateway (not connected)")
    if diag.dns:
        ok = [d.name for d in diag.dns if d.ok]
        failed = [d.name for d in diag.dns if not d.ok]
        lines.append(f"DNS: ok {', '.join(ok) or '-'}" + (f"; failed {', '.join(failed)}" if failed else ""))
    if diag.hosts:
        parts = []
        for h in diag.hosts:
            ping = f"ping {_n(h.ping.avg_ms)} ms, {_n(h.ping.loss_pct)}% loss" if h.ping.reachable else "ping no answer"
            tcp = f"tcp {h.tcp_port} ok" if h.tcp_ok else "tcp failed"
            parts.append(f"{h.host} ({ping}, {tcp})")
        lines.append("public hosts: " + "; ".join(parts))
    wifi = diag.wifi
    if wifi is not None and wifi.power_on and wifi.rssi is not None:
        lines.append(
            f"Wi-Fi: rssi {wifi.rssi} dBm, noise {_n(wifi.noise)} dBm, snr {_n(wifi.snr)} dB, channel {_n(wifi.channel)} "
            f"({wifi.band or '?'}, {_n(wifi.width_mhz)} MHz), rate {_n(wifi.tx_rate_mbps)} Mbit/s, {wifi.phy or '?'}"
        )
        lines.append(f"SSID: {wifi.ssid}" if wifi.ssid else probes.LOCATION_NOTE)
    elif wifi is not None and not wifi.power_on:
        lines.append("Wi-Fi: off")
    iface = f"{diag.interface or '?'} ({diag.interface_kind or '?'}, {'active' if diag.interface_active else 'inactive'}, ip {diag.interface_ip or '-'})"
    home = {True: "yes", False: "no", None: "unknown (no home gateway configured; run `jarvis presence setup`)"}[diag.on_home_network]
    lines.append(f"interface: {iface}; home network: {home}")
    total = diag.duration_s + (speed.duration_s if speed else 0.0)
    lines.append(f"{'full' if speed else 'quick'} test took {total:.0f} s")
    return "\n".join(lines)


class _DbMixin(Tool):
    _own_db: Database | None = None

    @property
    def db(self) -> Database:
        ctx = self.require_ctx()
        if ctx.runtime is not None and getattr(ctx.runtime, "db", None) is not None:
            return ctx.runtime.db
        if self._own_db is None:  # no Assistant around (evals, scripts): open the project db
            self._own_db = Database(ctx.paths.db_file)
        return self._own_db


class InternetTestInput(BaseModel):
    full: bool = Field(
        default=True,
        description="true: diagnostics plus the networkQuality speed test (about 20 s, uses data). "
        "false: diagnostics only (interface, router, DNS, public hosts, Wi-Fi signal), fast.",
    )


class InternetTest(_DbMixin):
    name = "internet_test"
    description = (
        "Test the internet connection and explain what is wrong: interface, router reachability, DNS, "
        "public hosts, Wi-Fi signal, and with full=true the download/upload speed and responsiveness "
        "(macOS networkQuality). Returns a verdict with a likely cause. Every result is stored for internet_history."
    )
    input_model = InternetTestInput
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = True

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, InternetTestInput)
        ctx = self.require_ctx()
        diag = run_diagnostics(ctx.config)
        speed: SpeedResult | None = None
        note: str | None = None
        if args.full:
            if not speedtest.speedtest_available():
                note = "skipped: networkQuality not found on this Mac"
            else:
                try:
                    speed = run_speed_test(ctx.config)
                except ToolExecutionError as exc:
                    note = f"skipped: {exc}"
        verdict = likely_cause(diag, speed)
        NetworkStore(self.db).add("full" if speed else "quick", diag, speed, verdict)
        return ToolResult(format_report(diag, speed, verdict, speed_note=note))


class InternetHistoryInput(BaseModel):
    range: Range = Field(default="week", description="How far back: day, week or month.")


class InternetHistory(_DbMixin):
    name = "internet_history"
    description = (
        "How the internet connection has been: averages of download/upload/latency, the worst times, "
        "and the number of failures over the last day, week or month (from stored internet_test results)."
    )
    input_model = InternetHistoryInput
    risk = RiskLevel.SAFE
    taints = False
    delegate_ok = True

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, InternetHistoryInput)
        if args.range not in RANGES:  # Literal already rejects others; belt and braces for the SQL window
            raise ToolExecutionError(f"unknown range {args.range!r}")
        return ToolResult(NetworkStore(self.db).history(args.range).format())


# --- jarvis doctor ---------------------------------------------------------------------------------------

LOCAL_NETWORK_HINT = (
    "macOS prompts the first time internet_test or the presence scan probes the LAN under the terminal app "
    "or the launchd python; if the router never answers, enable the app under System Settings > "
    "Privacy & Security > Local Network (docs/integrations/presence.md)"
)
LOCATION_HINT = (
    "SSID/BSSID hidden: optional, grant Location Services to the terminal app if you want the Wi-Fi name in internet_test "
    "(System Settings > Privacy & Security > Location Services; docs/integrations/presence.md)"
)


def health_checks(config: "Config", paths: "Paths", *, wifi: Callable[[], probes.WifiInfo | None] = probes.corewlan_wifi) -> list["Check"]:
    """Never raises, never prompts, never touches the network (a doctor must stay fast and silent)."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    path = shutil.which(speedtest.BINARY)
    checks.append(
        Check("networkQuality", path is not None, path or "not found: the speed test needs macOS 12 or newer (diagnostics still work)")
    )
    checks.append(Check("Local Network permission", True, "cannot be queried without a prompt; " + LOCAL_NETWORK_HINT))

    try:
        info = wifi()
    except Exception:
        info = None
    if info is None:
        checks.append(
            Check("Location Services (Wi-Fi name)", True, "Wi-Fi info unavailable (CoreWLAN missing or no Wi-Fi interface); optional")
        )
    elif info.ssid:
        checks.append(Check("Location Services (Wi-Fi name)", True, f"granted: SSID {info.ssid}"))
    elif not info.power_on or info.rssi is None:
        checks.append(Check("Location Services (Wi-Fi name)", True, "Wi-Fi off or not associated; cannot tell yet (optional)"))
    else:
        checks.append(Check("Location Services (Wi-Fi name)", True, LOCATION_HINT, warn=True))

    net = config.network
    periodic = (
        "off (network.periodic_check_min = 0)"
        if net.periodic_check_min <= 0
        else f"quick check every {net.periodic_check_min} min, notifies on down/up"
    )
    if net.periodic_speedtest_hours > 0:
        periodic += f"; full speed test every {net.periodic_speedtest_hours} h (uses data)"
    checks.append(Check("network periodic check", True, periodic))

    try:
        detail = _history_detail(paths)
        checks.append(Check("network history", True, detail))
    except Exception as exc:
        checks.append(Check("network history", False, f"cannot read the database: {type(exc).__name__}: {exc}"))
    return checks


def _history_detail(paths: "Paths") -> str:
    if not paths.db_file.is_file():
        return "no tests stored yet"
    db = Database(paths.db_file)
    try:
        store = NetworkStore(db)
        count = store.count()
        last = store.last()
    finally:
        db.close()
    if not count or last is None:
        return "no tests stored yet"
    at = parse_iso(last["created_at"])
    when = format_local(at) if at else str(last["created_at"])
    return f"{count} test(s) stored; last {when} ({last['kind']}): {last['verdict']}"
