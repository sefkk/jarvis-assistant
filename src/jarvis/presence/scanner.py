"""Finding the configured devices on the home network without root.

Phones sleep their Wi-Fi radio, so one probe is not enough. Per configured device, in order:

1. `ping -c 1 -W 800 -t 2 <ip>` to the device's last known IP (exit code 0 = reply);
2. a TCP connect to port 62078 (iPhones answer there even with the screen off; a refused
   connection also proves the device is there, only a timeout means absent);
3. the ARP table (`arp -an`), which counts as proof only when this scan refreshed it: after the
   subnet sweep, or when a probe made the kernel resolve an address that was not resolved before.

The "sweep" is one empty UDP datagram to every host of the /24 (port 9, discard): the kernel must
ARP-resolve each address to send it, so ~1.5 s later `arp -an` lists every device that answered.
No root, no port scanning beyond 62078, nothing beyond a /24 (`local.subnet_hosts` refuses more).
The engine runs the sweep on every fifth scan or when a configured device has no known IP.

Every probe is injectable so tests never touch the network. Nothing here logs a MAC address, an IP
or a hostname: callers log names and states (package docstring).
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from jarvis.network import local
from jarvis.network.local import ArpEntry, InterfaceInfo, Runner
from jarvis.presence.vendors import vendor_for
from jarvis.security.sanitize import for_model

if TYPE_CHECKING:
    from jarvis.config import Config

log = logging.getLogger("jarvis.presence")

IPHONE_PORT = 62078  # lockdownd: iPhones accept (or refuse) connections there while the radio is awake
PROBE_TIMEOUT_S = 1.0
PING_ARGV = ["ping", "-c", "1", "-W", "800", "-t", "2"]  # macOS: -W in ms, -t in seconds
SWEEP_SETTLE_S = 1.5
HOSTNAME_MAX_CHARS = 60

Nudge = Callable[[str], None]
Probe = Callable[[str], bool]


# --- results --------------------------------------------------------------------------------------------


@dataclass
class DeviceProbe:
    """One configured device after a scan."""

    mac: str
    person: str
    seen: bool
    ip: str | None = None
    how: str | None = None  # arp | ping | tcp


@dataclass
class ScanResult:
    devices: list[DeviceProbe] = field(default_factory=list)
    arp_devices: list[ArpEntry] = field(default_factory=list)  # complete entries only
    swept: bool = False

    def seen(self) -> list[DeviceProbe]:
        return [d for d in self.devices if d.seen]


@dataclass
class Device:
    """A device on the network, for setup and `list_network_devices`."""

    ip: str
    mac: str
    vendor: str
    hostname: str | None
    person: str | None


# --- real probes ----------------------------------------------------------------------------------------


def udp_nudge(ip: str) -> None:
    """One empty datagram to the discard port: enough to make the kernel ARP-resolve `ip`."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(b"", (ip, 9))
    except OSError:
        pass  # unreachable, no buffer space, or similar: the sweep is best effort


def ping_host(ip: str, run: Runner = local.run_command) -> bool:
    return run([*PING_ARGV, ip], 3.0).ok


def tcp_probe(ip: str, port: int = IPHONE_PORT, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True when the host answered at all (accepted or refused); False on a timeout or no route."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


# --- the sweep --------------------------------------------------------------------------------------------


def sweep_subnet(info: InterfaceInfo, nudge: Nudge = udp_nudge, *, sleep: Callable[[float], None] = time.sleep) -> int:
    """Nudge every host of the interface's subnet, wait for the ARP replies. Returns the host count."""
    if not info.ip or not info.netmask:
        return 0
    hosts = local.subnet_hosts(info.ip, info.netmask)
    for host in hosts:
        nudge(host)
    if hosts:
        sleep(SWEEP_SETTLE_S)
    return len(hosts)


def _current_interface(run: Runner) -> InterfaceInfo | None:
    gateway = local.default_gateway(run)
    if gateway is None or not gateway.interface:
        return None
    return local.interface_info(gateway.interface, run)


def _complete_entries(run: Runner) -> dict[str, str]:
    """mac -> ip for every resolved ARP entry."""
    return {e.mac: e.ip for e in local.arp_table(run) if e.mac}


# --- the scan ----------------------------------------------------------------------------------------------


def scan_devices(
    config: "Config",
    *,
    run: Runner = local.run_command,
    known_ips: dict[str, str] | None = None,
    sweep: bool = False,
    nudge: Nudge = udp_nudge,
    ping: Probe | None = None,
    tcp: Probe | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanResult:
    """Probe only the configured devices ("keep scans light")."""
    known_ips = known_ips or {}
    ping = ping or (lambda ip: ping_host(ip, run))
    tcp = tcp or tcp_probe
    result = ScanResult()

    if sweep:
        info = _current_interface(run)
        if info is not None and sweep_subnet(info, nudge, sleep=sleep):
            result.swept = True

    before = _complete_entries(run)
    result.arp_devices = [ArpEntry(ip=ip, mac=mac, interface="") for mac, ip in before.items()]

    probed_unseen: list[DeviceProbe] = []
    for person in config.presence.people:
        for mac in person.devices:
            ip = before.get(mac) or known_ips.get(mac)
            probe = DeviceProbe(mac=mac, person=person.name, seen=False, ip=ip)
            if ip and ping(ip):
                probe.seen, probe.how = True, "ping"
            elif ip and tcp(ip):
                probe.seen, probe.how = True, "tcp"
            elif result.swept and mac in before:
                probe.seen, probe.how = True, "arp"
            elif ip and mac not in before:
                probed_unseen.append(probe)  # the probe may have resolved the address
            result.devices.append(probe)

    if probed_unseen:
        after = _complete_entries(run)
        for probe in probed_unseen:
            if probe.mac in after:
                probe.seen, probe.how, probe.ip = True, "arp", after[probe.mac]
    return result


# --- discovery (setup, list_network_devices) ------------------------------------------------------------

_HOSTNAME_LINE_RE = re.compile(r"^\s*name:\s*(.+?)\s*$", re.MULTILINE)


def lookup_hostname(ip: str, run: Runner = local.run_command) -> str | None:
    """`dscacheutil -q host -a ip_address <ip>`: the first `name:` line, cleaned, or None."""
    result = run(["dscacheutil", "-q", "host", "-a", "ip_address", ip], 2.0)
    if not result.ok:
        return None
    m = _HOSTNAME_LINE_RE.search(result.stdout)
    return clean_hostname(m.group(1)) if m else None


def clean_hostname(text: str | None) -> str | None:
    """Hostnames are chosen by whoever owns the device: one line, visible characters, capped."""
    if not text:
        return None
    cleaned = for_model(text)
    cleaned = " ".join(cleaned.split())  # newlines, tabs and runs of spaces collapse to one space
    cleaned = cleaned.replace("|", "/")  # never break a table row
    return cleaned[:HOSTNAME_MAX_CHARS] or None


def discover_devices(
    config: "Config",
    run: Runner = local.run_command,
    *,
    nudge: Nudge = udp_nudge,
    sleep: Callable[[float], None] = time.sleep,
    hostname: Callable[[str], str | None] | None = None,
) -> list[Device]:
    """Sweep the subnet once, then list every resolved ARP entry with vendor, hostname and the
    configured person (if any). For setup and the tool only, never for the periodic scan."""
    hostname = hostname or (lambda ip: lookup_hostname(ip, run))
    info = _current_interface(run)
    if info is not None:
        sweep_subnet(info, nudge, sleep=sleep)
    gateway = local.default_gateway(run)
    devices: list[Device] = []
    for mac, ip in sorted(_complete_entries(run).items(), key=lambda item: _ip_key(item[1])):
        if not _is_unicast(ip, mac):
            continue  # broadcast and multicast entries are not devices
        person = config.presence.person_for_device(mac)
        vendor = "gateway (router)" if gateway is not None and ip == gateway.ip else vendor_for(mac)
        devices.append(Device(ip=ip, mac=mac, vendor=vendor, hostname=hostname(ip), person=person.name if person else None))
    return devices


def _is_unicast(ip: str, mac: str) -> bool:
    """The ARP table also holds the subnet broadcast and the mDNS/SSDP multicast groups."""
    if mac == "ff:ff:ff:ff:ff:ff" or mac.startswith("01:00:5e:") or mac.startswith("33:33:"):
        return False
    try:
        first = int(ip.split(".")[0])
    except ValueError:
        return False
    return not (224 <= first <= 239) and not ip.endswith(".255")


def _ip_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999,)
