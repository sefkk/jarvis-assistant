"""The Mac's view of the local network, without elevated privileges: the default gateway
(`route -n get default`), the ARP table (`arp -an`), the interface (`ifconfig`, `networksetup`)
and the subnet. Every command runs as an argv list with a timeout; a missing or failing command
yields None or an empty list, never an exception, so callers can degrade gracefully.

Shared by the internet test (gateway reachability, interface status) and by presence (the home
network is recognized by the gateway's MAC address; people's phones are looked up in the ARP table).
Tests inject `run` to replace the command runner.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from jarvis.config import normalize_mac

log = logging.getLogger("jarvis.network")

COMMAND_TIMEOUT_S = 5.0

Runner = Callable[[list[str], float], "CommandResult"]


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(argv: list[str], timeout: float = COMMAND_TIMEOUT_S) -> CommandResult:
    """Run a system command as argv (never a shell). Missing binaries and timeouts become a
    non-zero result instead of an exception."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"{argv[0]} not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]} timed out after {timeout:g}s")
    except OSError as exc:
        return CommandResult(1, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# --- default gateway -------------------------------------------------------------------------------


@dataclass
class Gateway:
    ip: str
    interface: str


_ROUTE_FIELD_RE = re.compile(r"^\s*(gateway|interface):\s*(\S+)\s*$", re.MULTILINE)


def parse_route_output(text: str) -> Gateway | None:
    fields = {m.group(1): m.group(2) for m in _ROUTE_FIELD_RE.finditer(text or "")}
    ip = fields.get("gateway")
    if not ip:
        return None
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None  # a host name here would mean an unusual setup; treat as unknown
    return Gateway(ip=ip, interface=fields.get("interface", ""))


def default_gateway(run: Runner = run_command) -> Gateway | None:
    """The IPv4 default gateway and the interface it sits on, or None when offline."""
    result = run(["route", "-n", "get", "default"], COMMAND_TIMEOUT_S)
    if not result.ok:
        return None
    return parse_route_output(result.stdout)


# --- ARP table ------------------------------------------------------------------------------------------


@dataclass
class ArpEntry:
    ip: str
    mac: str | None  # normalized aa:bb:cc:dd:ee:ff, None for "(incomplete)"
    interface: str

    @property
    def incomplete(self) -> bool:
        return self.mac is None


_ARP_LINE_RE = re.compile(r"^\S+\s+\((?P<ip>[\d.]+)\)\s+at\s+(?P<mac>\S+)\s+on\s+(?P<iface>\S+)")


def parse_arp_output(text: str) -> list[ArpEntry]:
    """`arp -an` lines look like `? (192.168.1.1) at a4:2b:b0:7c:1e:9f on en0 ifscope [ethernet]`;
    `(incomplete)` marks an address that did not answer. macOS prints MACs without leading zeros."""
    entries: list[ArpEntry] = []
    for line in (text or "").splitlines():
        m = _ARP_LINE_RE.match(line.strip())
        if not m:
            continue
        mac_text = m.group("mac")
        mac = None if mac_text.startswith("(") else normalize_mac(mac_text)
        if mac is None and not mac_text.startswith("("):
            continue  # not a MAC at all (unexpected format)
        entries.append(ArpEntry(ip=m.group("ip"), mac=mac, interface=m.group("iface")))
    return entries


def arp_table(run: Runner = run_command) -> list[ArpEntry]:
    result = run(["arp", "-an"], COMMAND_TIMEOUT_S)
    return parse_arp_output(result.stdout) if result.ok else []


def mac_for_ip(ip: str, entries: list[ArpEntry]) -> str | None:
    return next((e.mac for e in entries if e.ip == ip and e.mac), None)


def gateway_mac(run: Runner = run_command) -> tuple[Gateway | None, str | None]:
    """The default gateway and its MAC address from the ARP table (the home-network
    identity). (None, None) when offline; (gateway, None) when the ARP entry is missing."""
    gateway = default_gateway(run)
    if gateway is None:
        return None, None
    return gateway, mac_for_ip(gateway.ip, arp_table(run))


def is_private_mac(mac: str) -> bool:
    """Locally administered address (the second hex digit is 2, 6, a or e): an iPhone's private
    Wi-Fi address, a randomized MAC, or a virtual interface."""
    normalized = normalize_mac(mac)
    if normalized is None:
        return False
    return bool(int(normalized[:2], 16) & 0x02)


# --- interfaces --------------------------------------------------------------------------------------------


@dataclass
class InterfaceInfo:
    name: str
    active: bool
    ip: str | None
    netmask: str | None  # dotted
    mac: str | None
    kind: str = "other"  # wifi | ethernet | other


_IFCONFIG_INET_RE = re.compile(r"^\s*inet\s+(?P<ip>[\d.]+)\s+netmask\s+(?P<mask>0x[0-9a-fA-F]{8})", re.MULTILINE)
_IFCONFIG_ETHER_RE = re.compile(r"^\s*ether\s+(?P<mac>[0-9a-fA-F:]{17})", re.MULTILINE)
_IFCONFIG_STATUS_RE = re.compile(r"^\s*status:\s*(?P<status>\w+)", re.MULTILINE)


def _hex_mask_to_dotted(mask: str) -> str:
    value = int(mask, 16)
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def parse_ifconfig_output(name: str, text: str) -> InterfaceInfo:
    inet = _IFCONFIG_INET_RE.search(text or "")
    ether = _IFCONFIG_ETHER_RE.search(text or "")
    status = _IFCONFIG_STATUS_RE.search(text or "")
    return InterfaceInfo(
        name=name,
        active=bool(status and status.group("status").lower() == "active"),
        ip=inet.group("ip") if inet else None,
        netmask=_hex_mask_to_dotted(inet.group("mask")) if inet else None,
        mac=normalize_mac(ether.group("mac")) if ether else None,
    )


_HARDWARE_PORT_RE = re.compile(r"Hardware Port:\s*(?P<port>.+?)\s*\nDevice:\s*(?P<device>\S+)", re.MULTILINE)


def parse_hardware_ports(text: str) -> dict[str, str]:
    """`networksetup -listallhardwareports` -> {device: kind} with kind wifi | ethernet | other."""
    kinds: dict[str, str] = {}
    for m in _HARDWARE_PORT_RE.finditer(text or ""):
        port = m.group("port").lower()
        if "wi-fi" in port or "airport" in port or "wlan" in port:
            kind = "wifi"
        elif "ethernet" in port or "lan" in port or "thunderbolt" in port or "usb" in port:
            kind = "ethernet"
        else:
            kind = "other"
        kinds[m.group("device")] = kind
    return kinds


def interface_info(name: str, run: Runner = run_command) -> InterfaceInfo | None:
    if not re.fullmatch(r"[a-z0-9]{2,16}", name or ""):
        return None
    result = run(["ifconfig", name], COMMAND_TIMEOUT_S)
    if not result.ok:
        return None
    info = parse_ifconfig_output(name, result.stdout)
    ports = run(["networksetup", "-listallhardwareports"], COMMAND_TIMEOUT_S)
    if ports.ok:
        info.kind = parse_hardware_ports(ports.stdout).get(name, "other")
    return info


def subnet_hosts(ip: str, netmask: str, limit: int = 254) -> list[str]:
    """Every host address of the interface's subnet (at most `limit`, so a /16 is never swept)."""
    try:
        network = ipaddress.ip_network(f"{ip}/{netmask}", strict=False)
    except ValueError:
        return []
    if network.num_addresses - 2 > limit:
        return []
    return [str(h) for h in network.hosts() if str(h) != ip]
