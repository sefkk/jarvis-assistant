"""Internet diagnostics as the normal user, no elevated privileges: is the Mac on Wi-Fi or Ethernet, does the
router answer, does DNS resolve, are public hosts reachable, how good is the Wi-Fi signal.

Every probe is an argv command with a timeout or a socket call with a timeout, and every external
dependency (command runner, resolver, TCP connect, CoreWLAN) is an injectable callable so unit
tests never touch the network. `parse_ping_output` is pure. The verdict lives in `diagnose.py`.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from jarvis.network import local
from jarvis.network.local import CommandResult, Runner, run_command

if TYPE_CHECKING:
    from jarvis.config import Config

log = logging.getLogger("jarvis.network")

PING_COUNT = 3
PING_INTERVAL_S = 0.3
PING_REPLY_WAIT_MS = 1000  # macOS `ping -W` is in milliseconds
PING_TIMEOUT_S = 3  # `ping -t`: overall timeout in seconds
PING_COMMAND_TIMEOUT_S = PING_TIMEOUT_S + 3.0
DNS_TIMEOUT_S = 3.0
TCP_TIMEOUT_S = 2.0
TCP_PORTS = (443, 53)

Resolver = Callable[[str, float], "str | None"]  # name, timeout -> address or None
TcpProbe = Callable[[str, int, float], bool]  # host, port, timeout -> connected
WifiProbe = Callable[[], "WifiInfo | None"]


# --- ping ------------------------------------------------------------------------------------------


@dataclass
class PingResult:
    host: str
    sent: int = 0
    received: int = 0
    loss_pct: float = 100.0
    avg_ms: float | None = None

    @property
    def reachable(self) -> bool:
        return self.received > 0


_PING_SUMMARY_RE = re.compile(
    r"(?P<sent>\d+) packets transmitted, (?P<received>\d+) packets received(?:, \+\d+ errors)?, "
    r"(?P<loss>[\d.]+)% packet loss"
)
_PING_RTT_RE = re.compile(r"round-trip min/avg/max/stddev = [\d.]+/(?P<avg>[\d.]+)/[\d.]+/[\d.]+ ms")


def parse_ping_output(host: str, text: str) -> PingResult:
    """macOS ping summary: `3 packets transmitted, 3 packets received, 0.0% packet loss` and
    `round-trip min/avg/max/stddev = 50.183/50.564/50.946/0.382 ms`. With 100% loss there is no
    round-trip line; a `+N errors` field appears when the host sends ICMP errors."""
    result = PingResult(host)
    summary = _PING_SUMMARY_RE.search(text or "")
    if summary:
        result.sent = int(summary.group("sent"))
        result.received = int(summary.group("received"))
        result.loss_pct = round(float(summary.group("loss")), 1)
    rtt = _PING_RTT_RE.search(text or "")
    if rtt and result.received > 0:
        result.avg_ms = round(float(rtt.group("avg")), 1)
    return result


def ping_argv(host: str) -> list[str]:
    return ["ping", "-c", str(PING_COUNT), "-i", str(PING_INTERVAL_S), "-W", str(PING_REPLY_WAIT_MS), "-t", str(PING_TIMEOUT_S), host]


def ping(host: str, run: Runner = run_command) -> PingResult:
    result: CommandResult = run(ping_argv(host), PING_COMMAND_TIMEOUT_S)
    return parse_ping_output(host, result.stdout)  # non-zero exit with a summary still parses (100% loss)


# --- DNS and TCP ------------------------------------------------------------------------------------


@dataclass
class DnsResult:
    name: str
    ok: bool
    address: str | None = None
    ms: float | None = None


def default_resolver(name: str, timeout: float = DNS_TIMEOUT_S) -> str | None:
    """`socket.getaddrinfo` in a worker thread so a hanging resolver cannot block the tool."""
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(socket.getaddrinfo, name, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        try:
            infos = future.result(timeout=timeout)
        except (FutureTimeout, OSError):
            return None
    finally:
        executor.shutdown(wait=False)
    return str(infos[0][4][0]) if infos else None


def default_tcp(host: str, port: int, timeout: float = TCP_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_names(names: list[str], resolver: Resolver) -> list[DnsResult]:
    results = []
    for name in names:
        started = time.perf_counter()
        try:
            address = resolver(name, DNS_TIMEOUT_S)
        except Exception:  # a resolver bug counts as a failure, never as a crash
            address = None
        ms = round((time.perf_counter() - started) * 1000, 1)
        results.append(DnsResult(name, bool(address), address, ms if address else None))
    return results


@dataclass
class HostResult:
    host: str
    ping: PingResult
    tcp_ok: bool = False
    tcp_port: int | None = None

    @property
    def reachable(self) -> bool:
        return self.ping.reachable or self.tcp_ok


def probe_host(host: str, run: Runner, tcp: TcpProbe) -> HostResult:
    """Ping plus a TCP connect (443, then 53): a network that drops ICMP still counts as online."""
    result = HostResult(host, ping(host, run))
    for port in TCP_PORTS:
        try:
            if tcp(host, port, TCP_TIMEOUT_S):
                result.tcp_ok, result.tcp_port = True, port
                break
        except Exception:
            continue
    return result


# --- Wi-Fi (CoreWLAN) ----------------------------------------------------------------------------------

LOCATION_NOTE = "SSID unavailable: Location Services not granted (optional)"
BANDS = {1: "2.4 GHz", 2: "5 GHz", 3: "6 GHz"}
WIDTHS_MHZ = {1: 20, 2: 40, 3: 80, 4: 160}
PHY_MODES = {4: "802.11n", 5: "802.11ac", 6: "802.11ax"}


@dataclass
class WifiInfo:
    interface: str | None = None
    power_on: bool = False
    rssi: int | None = None  # dBm
    noise: int | None = None  # dBm
    channel: int | None = None
    band: str | None = None
    width_mhz: int | None = None
    tx_rate_mbps: float | None = None
    phy: str | None = None
    ssid: str | None = None
    bssid: str | None = None
    note: str | None = None  # "needs Location Services" when the SSID is hidden

    @property
    def snr(self) -> int | None:
        return self.rssi - self.noise if self.rssi is not None and self.noise is not None else None


def corewlan_wifi() -> WifiInfo | None:
    """The current Wi-Fi link through CoreWLAN (pyobjc). None when the framework or the interface
    is unavailable; SSID/BSSID are None without Location Services, which is the normal case."""
    try:
        import CoreWLAN  # noqa: PLC0415 - optional framework, imported lazily

        iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
        if iface is None:
            return None
        info = WifiInfo(interface=str(iface.interfaceName() or "") or None, power_on=bool(iface.powerOn()))
        if not info.power_on:
            return info
        rssi, noise = int(iface.rssiValue()), int(iface.noiseMeasurement())
        info.rssi = rssi if rssi != 0 else None  # 0 means "not associated"
        info.noise = noise if noise != 0 else None
        info.tx_rate_mbps = round(float(iface.transmitRate()), 1) or None
        channel = iface.wlanChannel()
        if channel is not None:
            info.channel = int(channel.channelNumber()) or None
            info.band = BANDS.get(int(channel.channelBand()))
            info.width_mhz = WIDTHS_MHZ.get(int(channel.channelWidth()))
        phy = int(iface.activePHYMode())
        info.phy = PHY_MODES.get(phy, f"PHY mode {phy}" if phy else None)
        info.ssid = str(iface.ssid()) if iface.ssid() else None
        info.bssid = str(iface.bssid()) if iface.bssid() else None
        if info.ssid is None:
            info.note = "needs Location Services"
        return info
    except Exception as exc:  # ImportError, a missing interface, a sandboxed process
        log.debug("wifi info unavailable: %s: %s", type(exc).__name__, exc)
        return None


# --- the whole picture -----------------------------------------------------------------------------------


@dataclass
class Diagnostics:
    interface: str | None = None
    interface_kind: str | None = None  # wifi | ethernet | other
    interface_active: bool = False
    interface_ip: str | None = None
    gateway_ip: str | None = None
    gateway_mac: str | None = None
    gateway: PingResult | None = None
    dns: list[DnsResult] = field(default_factory=list)
    hosts: list[HostResult] = field(default_factory=list)
    wifi: WifiInfo | None = None
    on_home_network: bool | None = None  # None: no home gateway configured yet
    duration_s: float = 0.0

    @property
    def gateway_reachable(self) -> bool:
        return self.gateway is not None and self.gateway.reachable

    @property
    def online(self) -> bool:
        """A public host answered ping or accepted a TCP connection (IP-level reachability)."""
        return any(h.reachable for h in self.hosts)

    @property
    def dns_ok(self) -> bool:
        return bool(self.dns) and any(d.ok for d in self.dns)

    @property
    def gateway_latency_ms(self) -> float | None:
        return self.gateway.avg_ms if self.gateway else None

    @property
    def gateway_loss_pct(self) -> float | None:
        return self.gateway.loss_pct if self.gateway else None

    @property
    def best_public_ping_ms(self) -> float | None:
        values = [h.ping.avg_ms for h in self.hosts if h.ping.avg_ms is not None]
        return min(values) if values else None

    def to_dict(self) -> dict[str, Any]:
        """Compact, JSON-ready (no raw command output)."""
        data = asdict(self)
        data["online"] = self.online
        data["dns_ok"] = self.dns_ok
        return data


def run_diagnostics(
    config: "Config",
    *,
    run: Runner = run_command,
    resolver: Resolver = default_resolver,
    tcp: TcpProbe = default_tcp,
    wifi: WifiProbe = corewlan_wifi,
) -> Diagnostics:
    """The diagnostic steps 1 to 5. Takes a few seconds offline (ping timeouts), under a second online."""
    started = time.perf_counter()
    diag = Diagnostics()
    gateway, mac = local.gateway_mac(run)
    if gateway is not None:
        diag.gateway_ip, diag.gateway_mac = gateway.ip, mac
        info = local.interface_info(gateway.interface, run) if gateway.interface else None
        if info is not None:
            diag.interface, diag.interface_kind = info.name, info.kind
            diag.interface_active, diag.interface_ip = info.active, info.ip
        else:
            diag.interface = gateway.interface or None
        diag.gateway = ping(gateway.ip, run)
        diag.dns = resolve_names(list(config.network.dns_check_names), resolver)
        diag.hosts = [probe_host(host, run, tcp) for host in config.network.public_check_hosts]
    try:
        diag.wifi = wifi()
    except Exception as exc:
        log.debug("wifi probe failed: %s", exc)
    if config.presence.home_gateway_macs:
        diag.on_home_network = config.presence.is_home_gateway(mac)
    diag.duration_s = round(time.perf_counter() - started, 1)
    return diag
