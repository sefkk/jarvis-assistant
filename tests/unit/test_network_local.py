"""The local-network helpers shared by the internet test and presence: route, arp,
ifconfig and networksetup parsing from real macOS output, MAC normalization, the subnet list.
Commands are faked; nothing runs on the network."""

from __future__ import annotations

import pytest

from jarvis.config import normalize_mac
from jarvis.network import local
from jarvis.network.local import CommandResult

ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
"""
ARP = """? (192.168.1.1) at a4:2b:b0:7c:1e:9f on en0 ifscope [ethernet]
? (192.168.1.23) at 8:10:d4:e1:21:27 on en0 ifscope [ethernet]
? (192.168.1.47) at (incomplete) on en0 ifscope [ethernet]
? (192.168.1.56) at 5a:fc:6f:7e:8a:f3 on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
garbage line
"""
IFCONFIG = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	options=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
	ether c8:89:f3:c1:fd:cc
	inet6 fe80::1c8e:4b0b:2d1e:8b0%en0 prefixlen 64 secured scopeid 0xc
	inet 192.168.1.21 netmask 0xffffff00 broadcast 192.168.1.255
	nd6 options=201<PERFORMNUD,DAD>
	media: autoselect
	status: active
"""
PORTS = """
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: c8:89:f3:c1:fd:cc

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 36:1e:9a:2b:3c:4d

Hardware Port: Ethernet Adapter (en4)
Device: en4
Ethernet Address: ba:e5:2a:ec:0f:5c
"""


def fake_runner(outputs: dict[str, CommandResult]):
    calls: list[list[str]] = []

    def run(argv, timeout):
        calls.append(list(argv))
        return outputs.get(argv[0], CommandResult(1, "", "unknown"))

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a4:2b:b0:7C:1E:9F", "a4:2b:b0:7c:1e:9f"),
        ("8:47:65:dc:c3:5b", "08:47:65:dc:c3:5b"),
        ("a4-2b-b0-7c-1e-9f", "a4:2b:b0:7c:1e:9f"),
        ("  1:0:5e:0:0:fb ", "01:00:5e:00:00:fb"),
        ("(incomplete)", None),
        ("a4:2b:b0:7c:1e", None),
        ("zz:47:65:dc:c3:5b", None),
        ("", None),
    ],
)
def test_normalize_mac(raw, expected):
    assert normalize_mac(raw) == expected


def test_parse_route_and_default_gateway():
    gw = local.parse_route_output(ROUTE)
    assert gw is not None and gw.ip == "192.168.1.1" and gw.interface == "en0"
    assert local.parse_route_output("route: writing to routing socket: not in table") is None
    assert local.parse_route_output("    gateway: router.local\n") is None
    run = fake_runner({"route": CommandResult(0, ROUTE)})
    assert local.default_gateway(run) == local.Gateway("192.168.1.1", "en0")
    assert run.calls == [["route", "-n", "get", "default"]]
    assert local.default_gateway(fake_runner({"route": CommandResult(1, "", "no route")})) is None


def test_parse_arp_table_normalizes_and_marks_incomplete():
    entries = local.parse_arp_output(ARP)
    by_ip = {e.ip: e for e in entries}
    assert by_ip["192.168.1.1"].mac == "a4:2b:b0:7c:1e:9f" and by_ip["192.168.1.1"].interface == "en0"
    assert by_ip["192.168.1.23"].mac == "08:10:d4:e1:21:27"  # leading zero restored
    assert by_ip["192.168.1.47"].mac is None and by_ip["192.168.1.47"].incomplete
    assert by_ip["224.0.0.251"].mac == "01:00:5e:00:00:fb"
    assert "garbage" not in str(entries)
    assert local.mac_for_ip("192.168.1.47", entries) is None
    assert local.mac_for_ip("192.168.1.56", entries) == "5a:fc:6f:7e:8a:f3"
    assert local.arp_table(fake_runner({"arp": CommandResult(1, "", "boom")})) == []


def test_gateway_mac_combines_route_and_arp():
    run = fake_runner({"route": CommandResult(0, ROUTE), "arp": CommandResult(0, ARP)})
    gateway, mac = local.gateway_mac(run)
    assert gateway.ip == "192.168.1.1" and mac == "a4:2b:b0:7c:1e:9f"
    run = fake_runner({"route": CommandResult(0, ROUTE), "arp": CommandResult(0, "")})
    assert local.gateway_mac(run) == (local.Gateway("192.168.1.1", "en0"), None)
    assert local.gateway_mac(fake_runner({})) == (None, None)


def test_private_mac_flag():
    assert local.is_private_mac("5a:fc:6f:7e:8a:f3")  # second hex digit a: locally administered
    assert local.is_private_mac("02:00:00:00:00:01")
    assert not local.is_private_mac("a4:2b:b0:7c:1e:9f")
    assert not local.is_private_mac("not a mac")


def test_interface_info_from_ifconfig_and_hardware_ports():
    run = fake_runner({"ifconfig": CommandResult(0, IFCONFIG), "networksetup": CommandResult(0, PORTS)})
    info = local.interface_info("en0", run)
    assert info is not None
    assert info.active and info.ip == "192.168.1.21" and info.netmask == "255.255.255.0"
    assert info.mac == "c8:89:f3:c1:fd:cc" and info.kind == "wifi"
    assert run.calls[0] == ["ifconfig", "en0"]
    kinds = local.parse_hardware_ports(PORTS)
    assert kinds == {"en0": "wifi", "bridge0": "ethernet", "en4": "ethernet"}
    assert local.interface_info("en0; rm -rf /", run) is None  # names are validated before they become argv
    assert local.interface_info("en9", fake_runner({"ifconfig": CommandResult(1, "", "does not exist")})) is None
    inactive = local.parse_ifconfig_output("en1", "en1: flags=8863 mtu 1500\n\tstatus: inactive\n")
    assert not inactive.active and inactive.ip is None and inactive.mac is None


def test_subnet_hosts_excludes_self_and_refuses_large_subnets():
    hosts = local.subnet_hosts("192.168.1.21", "255.255.255.0")
    assert len(hosts) == 253 and "192.168.1.21" not in hosts and hosts[0] == "192.168.1.1" and hosts[-1] == "192.168.1.254"
    assert local.subnet_hosts("10.0.0.5", "255.255.0.0") == []
    assert local.subnet_hosts("bad", "255.255.255.0") == []


def test_run_command_handles_missing_binary_and_timeout():
    missing = local.run_command(["/nonexistent/binary-for-jarvis-tests"], timeout=1)
    assert not missing.ok and missing.returncode == 127 and "not found" in missing.stderr
    slow = local.run_command(["sleep", "2"], timeout=0.05)
    assert not slow.ok and slow.returncode == 124 and "timed out" in slow.stderr
    ok = local.run_command(["echo", "hi"], timeout=2)
    assert ok.ok and ok.stdout.strip() == "hi"
