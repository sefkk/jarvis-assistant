"""Internet test: ping and networkQuality parsing, every likely_cause rule, missing
Location Services, history aggregation, the periodic tick (down/up notifications only on a
transition), the tools and the doctor lines. Every runner, resolver, TCP probe and Wi-Fi accessor
is a fake: no network, no subprocess."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.exceptions import ToolExecutionError
from jarvis.network import diagnose, periodic, probes
from jarvis.network.diagnose import likely_cause
from jarvis.network.local import CommandResult
from jarvis.network.probes import Diagnostics, DnsResult, HostResult, PingResult, WifiInfo, parse_ping_output, run_diagnostics
from jarvis.network.speedtest import SpeedResult, parse_network_quality, run_speed_test, speedtest_argv
from jarvis.network.store import NetworkStore
from jarvis.scheduler.notify import Notifier
from jarvis.tools import network as network_tools
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.network import InternetHistory, InternetTest, health_checks

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

PING_OK = """PING 192.168.1.1 (192.168.1.1): 56 data bytes
64 bytes from 192.168.1.1: icmp_seq=0 ttl=64 time=50.183 ms
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=50.946 ms
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=50.564 ms

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 50.183/50.564/50.946/0.382 ms
"""
PING_PARTIAL = """--- 1.1.1.1 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 12.1/13.0/13.9/0.9 ms
"""
PING_LOST = """PING 1.1.1.1 (1.1.1.1): 56 data bytes
Request timeout for icmp_seq 0

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 0 packets received, 100.0% packet loss
"""
PING_ERRORS = """--- 8.8.8.8 ping statistics ---
5 packets transmitted, 2 packets received, +3 errors, 60.0% packet loss
round-trip min/avg/max/stddev = 20.0/22.5/25.0/2.5 ms
"""

# A trimmed real `networkQuality -c` sample.
NETWORK_QUALITY_JSON = json.dumps(
    {
        "base_rtt": 165.97,
        "dl_flows": 12,
        "dl_throughput": 23527770,
        "end_date": "2026-10-16T11:59:40+03:00",
        "interface_name": "en0",
        "os_version": "Version 26.0 (Build 25A354)",
        "other": {"interface-type": {"wifi": 157}},
        "responsiveness": 95.96,
        "start_date": "2026-10-16T11:59:19+03:00",
        "test_endpoint": "mensura.cdn-apple.com",
        "ul_flows": 12,
        "ul_throughput": 6894218,
    }
)

ROUTE = "   route to: default\ndestination: default\n       mask: default\n    gateway: 192.168.1.1\n  interface: en0\n"
ARP = "? (192.168.1.1) at a4:2b:b0:7c:1e:9f on en0 ifscope [ethernet]\n"
IFCONFIG = "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n\tether a4:83:e7:11:22:33\n\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255\n\tstatus: active\n"
PORTS = "Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: a4:83:e7:11:22:33\n"


class FakeRunner:
    """argv[0] -> CommandResult; records every call. Unknown commands fail like a missing binary."""

    def __init__(self, outputs: dict[str, str | CommandResult] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv: list[str], timeout: float) -> CommandResult:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        key = argv[0]
        if key == "ping":
            key = f"ping {argv[-1]}"
        out = self.outputs.get(key)
        if out is None:
            return CommandResult(127, "", f"{argv[0]} not found")
        return out if isinstance(out, CommandResult) else CommandResult(0, out)


def online_runner(**overrides) -> FakeRunner:
    outputs = {
        "route": ROUTE,
        "arp": ARP,
        "ifconfig": IFCONFIG,
        "networksetup": PORTS,
        "ping 192.168.1.1": PING_OK,
        "ping 1.1.1.1": PING_OK.replace("192.168.1.1", "1.1.1.1"),
        "ping 8.8.8.8": PING_OK.replace("192.168.1.1", "8.8.8.8"),
        "networkQuality": NETWORK_QUALITY_JSON,
    }
    outputs.update(overrides)
    return FakeRunner(outputs)


def resolver_ok(name: str, timeout: float) -> str | None:
    return "17.253.144.10"


def resolver_fail(name: str, timeout: float) -> str | None:
    return None


def tcp_ok(host: str, port: int, timeout: float) -> bool:
    return True


def tcp_fail(host: str, port: int, timeout: float) -> bool:
    return False


def good_wifi() -> WifiInfo:
    return WifiInfo(
        interface="en0",
        power_on=True,
        rssi=-55,
        noise=-92,
        channel=36,
        band="5 GHz",
        width_mhz=80,
        tx_rate_mbps=866.0,
        phy="802.11ax",
        ssid=None,
        note="needs Location Services",
    )


def no_wifi() -> WifiInfo | None:
    return None


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def notify(self, title: str, message: str) -> None:
        if self.fail:
            raise RuntimeError("osascript exploded")
        self.sent.append((title, message))


# --- ping parsing --------------------------------------------------------------------------------------------


def test_parse_ping_ok():
    r = parse_ping_output("192.168.1.1", PING_OK)
    assert (r.sent, r.received, r.loss_pct, r.avg_ms) == (3, 3, 0.0, 50.6)
    assert r.reachable


def test_parse_ping_partial_loss():
    r = parse_ping_output("1.1.1.1", PING_PARTIAL)
    assert (r.received, r.loss_pct, r.avg_ms) == (2, 33.3, 13.0)
    assert r.reachable


def test_parse_ping_total_loss_and_garbage():
    r = parse_ping_output("1.1.1.1", PING_LOST)
    assert (r.sent, r.received, r.loss_pct, r.avg_ms) == (3, 0, 100.0, None)
    assert not r.reachable
    empty = parse_ping_output("x", "")
    assert not empty.reachable and empty.loss_pct == 100.0


def test_parse_ping_errors_variant():
    r = parse_ping_output("8.8.8.8", PING_ERRORS)
    assert (r.sent, r.received, r.loss_pct, r.avg_ms) == (5, 2, 60.0, 22.5)


def test_ping_argv_and_timeout():
    runner = online_runner()
    probes.ping("192.168.1.1", runner)
    assert runner.calls == [["ping", "-c", "3", "-i", "0.3", "-W", "1000", "-t", "3", "192.168.1.1"]]
    assert runner.timeouts[0] > probes.PING_TIMEOUT_S


# --- networkQuality ---------------------------------------------------------------------------------------------


def test_parse_network_quality_real_sample():
    s = parse_network_quality(NETWORK_QUALITY_JSON)
    assert (s.download_mbps, s.upload_mbps, s.responsiveness_rpm, s.latency_ms, s.interface) == (23.5, 6.9, 96, 166.0, "en0")
    assert s.raw["test_endpoint"] == "mensura.cdn-apple.com" and "dl_throughput" not in s.raw


@pytest.mark.parametrize("text", ["", "not json", '{"foo": 1}', "[1, 2]"])
def test_parse_network_quality_rejects_bad_output(text):
    with pytest.raises(ToolExecutionError):
        parse_network_quality(text)


def test_run_speed_test_exact_argv(config):
    runner = online_runner()
    s = run_speed_test(config, run=runner, which=lambda name: "/usr/bin/networkQuality")
    assert runner.calls == [["networkQuality", "-c", "-M", "30"]]
    assert runner.timeouts == [60.0]
    assert speedtest_argv(config) == ["networkQuality", "-c", "-M", "30"]
    assert s.download_mbps == 23.5


def test_run_speed_test_missing_binary_and_failure(config):
    with pytest.raises(ToolExecutionError, match="not found"):
        run_speed_test(config, run=online_runner(), which=lambda name: None)
    runner = FakeRunner({"networkQuality": CommandResult(1, "", "Error: no network")})
    with pytest.raises(ToolExecutionError, match="no network"):
        run_speed_test(config, run=runner, which=lambda name: "/usr/bin/networkQuality")


# --- diagnostics --------------------------------------------------------------------------------------------------


def test_run_diagnostics_online(config):
    runner = online_runner()
    diag = run_diagnostics(config, run=runner, resolver=resolver_ok, tcp=tcp_ok, wifi=good_wifi)
    assert (diag.interface, diag.interface_kind, diag.interface_active, diag.interface_ip) == ("en0", "wifi", True, "192.168.1.20")
    assert (diag.gateway_ip, diag.gateway_mac, diag.gateway_latency_ms, diag.gateway_loss_pct) == (
        "192.168.1.1",
        "a4:2b:b0:7c:1e:9f",
        50.6,
        0.0,
    )
    assert [d.name for d in diag.dns] == config.network.dns_check_names and diag.dns_ok
    assert [h.host for h in diag.hosts] == config.network.public_check_hosts and diag.online
    assert diag.hosts[0].tcp_ok and diag.hosts[0].tcp_port == 443
    assert diag.wifi is not None and diag.wifi.snr == 37 and diag.wifi.ssid is None
    assert diag.on_home_network is None  # no home gateway configured
    pinged = [c[-1] for c in runner.calls if c[0] == "ping"]
    assert pinged == ["192.168.1.1", *config.network.public_check_hosts]
    assert likely_cause(diag).key == "ok"
    data = diag.to_dict()
    assert data["online"] is True and data["wifi"]["rssi"] == -55 and json.dumps(data)


def test_run_diagnostics_home_network(config):
    config.presence.home_gateway_macs = ["a4:2b:b0:7c:1e:9f"]  # normalized form (the validator only runs at load time)
    diag = run_diagnostics(config, run=online_runner(), resolver=resolver_ok, tcp=tcp_ok, wifi=no_wifi)
    assert diag.on_home_network is True
    config.presence.home_gateway_macs = ["00:11:22:33:44:55"]
    diag = run_diagnostics(config, run=online_runner(), resolver=resolver_ok, tcp=tcp_ok, wifi=no_wifi)
    assert diag.on_home_network is False


def test_run_diagnostics_offline_touches_nothing_else(config):
    runner = FakeRunner({"route": CommandResult(1, "", "route: writing to routing socket: not in table")})
    diag = run_diagnostics(config, run=runner, resolver=resolver_ok, tcp=tcp_ok, wifi=no_wifi)
    assert diag.gateway_ip is None and not diag.online and diag.dns == [] and diag.hosts == []
    assert all(c[0] != "ping" for c in runner.calls)
    assert likely_cause(diag).key == "offline"


def test_run_diagnostics_survives_broken_wifi_and_resolver(config):
    def boom():
        raise RuntimeError("CoreWLAN exploded")

    def bad_resolver(name, timeout):
        raise OSError("resolver exploded")

    diag = run_diagnostics(config, run=online_runner(), resolver=bad_resolver, tcp=tcp_ok, wifi=boom)
    assert diag.wifi is None and not diag.dns_ok and diag.online


def test_wifi_without_location_services_is_reported_not_failed(config):
    diag = run_diagnostics(config, run=online_runner(), resolver=resolver_ok, tcp=tcp_ok, wifi=good_wifi)
    text = network_tools.format_report(diag, None, likely_cause(diag))
    assert "SSID unavailable: Location Services not granted (optional)" in text
    assert "rssi -55 dBm" in text and "snr 37 dB" in text and "channel 36 (5 GHz, 80 MHz)" in text


# --- likely_cause rules -------------------------------------------------------------------------------------------


def _diag(
    *, gateway=True, gw_ms=20.0, gw_loss=0.0, hosts_ok=True, host_ms=30.0, dns_ok=True, wifi=None, kind="wifi", active=True
) -> Diagnostics:
    d = Diagnostics(interface="en0", interface_kind=kind, interface_active=active, interface_ip="192.168.1.20")
    if gateway:
        d.gateway_ip, d.gateway_mac = "192.168.1.1", "a4:2b:b0:7c:1e:9f"
        d.gateway = PingResult("192.168.1.1", 3, 0 if gw_loss >= 100 else 3, gw_loss, None if gw_loss >= 100 else gw_ms)
        d.dns = [DnsResult("apple.com", dns_ok, "1.2.3.4" if dns_ok else None), DnsResult("google.com", dns_ok)]
        ping = PingResult("1.1.1.1", 3, 3 if hosts_ok else 0, 0.0 if hosts_ok else 100.0, host_ms if hosts_ok else None)
        d.hosts = [HostResult("1.1.1.1", ping, tcp_ok=hosts_ok, tcp_port=443 if hosts_ok else None)]
    d.wifi = wifi
    return d


def test_rule_offline_no_gateway():
    v = likely_cause(_diag(gateway=False))
    assert (v.key, v.cause) == ("offline", "Wi-Fi connection or router problem") and v.failure


def test_rule_offline_inactive_interface():
    assert likely_cause(_diag(active=False)).key == "offline"


def test_rule_gateway_unreachable():
    v = likely_cause(_diag(gw_loss=100.0))
    assert (v.key, v.cause) == ("gateway_unreachable", "Wi-Fi connection or router problem")


def test_rule_no_internet():
    v = likely_cause(_diag(hosts_ok=False))
    assert (v.key, v.cause) == ("no_internet", "The router is not connected to the internet / internet provider problem")


def test_rule_no_internet_tcp_only_counts_as_online():
    d = _diag(hosts_ok=False)
    d.hosts[0].tcp_ok, d.hosts[0].tcp_port = True, 443  # ICMP blocked, TCP works
    assert likely_cause(d).key == "ok"


def test_rule_dns():
    v = likely_cause(_diag(dns_ok=False))
    assert (v.key, v.cause) == ("dns", "DNS problem") and v.failure


def test_rule_weak_wifi_rssi_and_snr():
    weak = WifiInfo(power_on=True, rssi=-80, noise=-95)
    v = likely_cause(_diag(wifi=weak))
    assert (v.key, v.cause) == ("weak_wifi", "Weak Wi-Fi signal, move closer to the router") and not v.failure
    noisy = WifiInfo(power_on=True, rssi=-60, noise=-70)  # snr 10
    assert likely_cause(_diag(wifi=noisy)).key == "weak_wifi"
    assert likely_cause(_diag(wifi=weak, kind="ethernet")).key == "ok"  # Wi-Fi signal is irrelevant on Ethernet
    fine = WifiInfo(power_on=True, rssi=-60, noise=-90)
    assert likely_cause(_diag(wifi=fine)).key == "ok"


def test_rule_slow_full_test():
    def speed(**kw):
        base = dict(download_mbps=50.0, upload_mbps=10.0, responsiveness_rpm=500, latency_ms=30.0)
        base.update(kw)
        return SpeedResult(**base)

    assert likely_cause(_diag(), speed()).key == "ok"
    v = likely_cause(_diag(), speed(download_mbps=4.2))
    assert (v.key, v.cause) == ("slow", "The connection works but is slow")
    assert likely_cause(_diag(), speed(responsiveness_rpm=20)).key == "slow"
    assert likely_cause(_diag(), speed(latency_ms=450.0)).key == "slow"
    assert likely_cause(_diag(), speed(download_mbps=None, responsiveness_rpm=None, latency_ms=None)).key == "ok"


def test_rule_slow_quick_check():
    assert likely_cause(_diag(gw_ms=150.0)).key == "slow"
    assert likely_cause(_diag(host_ms=600.0)).key == "slow"
    assert likely_cause(_diag(gw_ms=5.0, host_ms=100.0)).key == "ok"


def test_rule_order_failures_before_wifi_and_speed():
    weak = WifiInfo(power_on=True, rssi=-85, noise=-90)
    assert likely_cause(_diag(hosts_ok=False, wifi=weak), SpeedResult(1.0, 1.0, 10, 900.0)).key == "no_internet"
    assert likely_cause(_diag(dns_ok=False, wifi=weak)).key == "dns"


def test_rule_ok():
    v = likely_cause(_diag())
    assert (v.key, v.cause) == ("ok", "The connection is normal") and not v.failure
    assert diagnose.FAILURE_VERDICTS == {"offline", "gateway_unreachable", "no_internet", "dns"}


# --- history --------------------------------------------------------------------------------------------------------


def _speed(down, up=10.0, rpm=400, rtt=30.0) -> SpeedResult:
    return SpeedResult(down, up, rpm, rtt, "en0")


def test_history_aggregation(db):
    store = NetworkStore(db)
    ok = likely_cause(_diag())
    store.add("full", _diag(), _speed(100.0, rtt=20.0), ok, now=NOW - timedelta(hours=1))
    store.add("full", _diag(), _speed(20.0, rtt=80.0), ok, now=NOW - timedelta(hours=2))
    store.add("full", _diag(), _speed(5.0, rtt=500.0), diagnose.verdict("slow"), now=NOW - timedelta(days=3))
    store.add("quick", _diag(hosts_ok=False), None, likely_cause(_diag(hosts_ok=False)), now=NOW - timedelta(days=5))
    store.add("quick", _diag(gw_ms=10.0), None, ok, now=NOW - timedelta(days=20))
    store.add("quick", _diag(), None, ok, now=NOW - timedelta(days=40))  # outside every window

    day = store.history("day", now=NOW)
    assert (day.count, day.failures, day.slow, day.avg_download_mbps, day.avg_latency_ms) == (2, 0, 0, 60.0, 50.0)
    assert day.last and day.last["kind"] == "full" and day.last["verdict"] == "ok"

    week = store.history("week", now=NOW)
    assert (week.count, week.failures, week.slow) == (4, 1, 1)
    assert week.avg_download_mbps == round((100 + 20 + 5) / 3, 1) and week.avg_upload_mbps == 10.0
    assert week.avg_gateway_latency_ms == 20.0
    assert [w.value for w in week.worst_latency] == [500.0, 80.0, 20.0]
    assert [w.value for w in week.slowest_download] == [5.0, 20.0, 100.0]
    assert week.worst_latency[0].verdict == "slow"
    assert week.format().startswith("Internet history, last week: 4 tests, 1 failures, 1 slow")

    month = store.history("month", now=NOW)
    assert (month.count, month.failures) == (5, 1)
    assert len(month.worst_latency) == 3 and month.worst_latency[-1].value == 20.0
    assert store.count() == 6 and store.last()["created_at"].startswith("2026-10-16T11:00")

    with pytest.raises(ValueError):
        store.history("year", now=NOW)
    with pytest.raises(ValueError):
        store.add("hourly", _diag(), None, ok)


def test_history_empty_and_failure_rows(db):
    store = NetworkStore(db)
    assert store.history("week", now=NOW).format().startswith("No internet tests stored")
    assert store.count() == 0 and store.last() is None
    rid = store.add("quick", _diag(gateway=False), None, diagnose.verdict("offline"), now=NOW)
    row = store.last()
    assert row["id"] == rid and row["online"] == 0 and row["verdict"] == "offline" and row["download_mbps"] is None
    assert row["likely_cause"] == "Wi-Fi connection or router problem" and row["on_home_network"] is None
    detail = json.loads(row["detail_json"])
    assert detail["diagnostics"]["online"] is False and len(row["detail_json"]) < 4000
    assert store.history("day", now=NOW).failures == 1


# --- periodic tick ----------------------------------------------------------------------------------------------------


class FakeProbes:
    """Scripted diagnostics: each call pops the next online/offline state."""

    def __init__(self, states: list[bool]) -> None:
        self.states = list(states)
        self.calls = 0
        self.speed_calls = 0

    def diagnostics(self, config, run=None):
        self.calls += 1
        state = self.states.pop(0) if self.states else True
        return _diag(hosts_ok=state)

    def speedtest(self, config, run=None):
        self.speed_calls += 1
        return _speed(80.0)


def test_tick_off_by_default_runs_nothing(config, db):
    assert config.network.periodic_check_min == 0 and config.network.periodic_speedtest_hours == 0
    runner = online_runner()
    fake = FakeProbes([True])
    notifier = RecordingNotifier()
    assert periodic.tick(config, db, notifier, now=NOW, run=runner, diagnostics=fake.diagnostics, speedtest=fake.speedtest) is None
    assert fake.calls == 0 and fake.speed_calls == 0 and runner.calls == [] and notifier.sent == []
    assert NetworkStore(db).count() == 0 and db.kv_get(periodic.KV_ONLINE) is None


def test_tick_quick_check_when_due(config, db):
    config.network.periodic_check_min = 10
    fake = FakeProbes([True, True])
    notifier = RecordingNotifier()
    line = periodic.tick(config, db, notifier, now=NOW, diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert line == "network quick check: ok"
    assert fake.calls == 1 and fake.speed_calls == 0
    assert NetworkStore(db).last()["kind"] == "quick" and db.kv_get(periodic.KV_ONLINE) == "1"
    assert notifier.sent == []  # first ever check: no notification
    # not due again for 10 minutes
    assert periodic.tick(config, db, notifier, now=NOW + timedelta(minutes=5), diagnostics=fake.diagnostics) is None
    assert fake.calls == 1
    assert periodic.tick(config, db, notifier, now=NOW + timedelta(minutes=10), diagnostics=fake.diagnostics) == "network quick check: ok"
    assert fake.calls == 2 and NetworkStore(db).count() == 2


def test_tick_notifies_only_on_transitions(config, db):
    config.network.periodic_check_min = 1
    fake = FakeProbes([True, True, False, False, True])
    notifier = RecordingNotifier()
    lines = []
    for i in range(5):
        lines.append(
            periodic.tick(config, db, notifier, now=NOW + timedelta(minutes=i), diagnostics=fake.diagnostics, speedtest=fake.speedtest)
        )
    assert fake.calls == 5
    assert notifier.sent == [
        ("Jarvis", "İnternet bağlantısı kesildi: Modem internete bağlı değil / servis sağlayıcı sorunu"),
        ("Jarvis", "İnternet geri geldi"),
    ]
    assert lines[2] == "network quick check: no_internet, notified: offline"
    assert lines[3] == "network quick check: no_internet"
    assert lines[4] == "network quick check: ok, notified: back online"
    assert db.kv_get(periodic.KV_ONLINE) == "1"
    assert NetworkStore(db).history("day", now=NOW + timedelta(minutes=5)).failures == 2


def test_transition_notifications_follow_the_main_language(config, db):
    from jarvis import i18n

    i18n.set_language("en")
    config.network.periodic_check_min = 1
    fake = FakeProbes([True, False, True])
    notifier = RecordingNotifier()
    for i in range(3):
        periodic.tick(config, db, notifier, now=NOW + timedelta(minutes=i), diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert notifier.sent == [
        ("Jarvis", "Internet connection lost: The router is not connected to the internet / internet provider problem"),
        ("Jarvis", "Internet is back"),
    ]


def test_tick_speed_test_only_when_enabled(config, db):
    config.network.periodic_check_min = 5
    fake = FakeProbes([True] * 4)
    notifier = RecordingNotifier()
    periodic.tick(config, db, notifier, now=NOW, diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert fake.speed_calls == 0

    config.network.periodic_speedtest_hours = 6
    line = periodic.tick(config, db, notifier, now=NOW + timedelta(minutes=5), diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert line == "network full check: ok" and fake.speed_calls == 1
    row = NetworkStore(db).last()
    assert row["kind"] == "full" and row["download_mbps"] == 80.0
    # the next quick check does not repeat the speed test; six hours later it does
    periodic.tick(config, db, notifier, now=NOW + timedelta(minutes=10), diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert fake.speed_calls == 1 and NetworkStore(db).last()["kind"] == "quick"
    periodic.tick(config, db, notifier, now=NOW + timedelta(hours=6, minutes=5), diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert fake.speed_calls == 2
    assert notifier.sent == []


def test_tick_speed_test_alone_without_quick_checks(config, db):
    config.network.periodic_speedtest_hours = 1
    fake = FakeProbes([True, True])
    periodic.tick(config, db, RecordingNotifier(), now=NOW, diagnostics=fake.diagnostics, speedtest=fake.speedtest)
    assert fake.calls == 1 and fake.speed_calls == 1
    assert (
        periodic.tick(
            config, db, RecordingNotifier(), now=NOW + timedelta(minutes=30), diagnostics=fake.diagnostics, speedtest=fake.speedtest
        )
        is None
    )
    assert fake.calls == 1


def test_tick_survives_exceptions(config, db):
    config.network.periodic_check_min = 1
    config.network.periodic_speedtest_hours = 1

    def broken(config, run=None):
        raise RuntimeError("probe exploded")

    fake = FakeProbes([True, False])
    line = periodic.tick(config, db, RecordingNotifier(), now=NOW, diagnostics=broken, speedtest=fake.speedtest)
    assert line is not None and "failed" in line and "probe exploded" in line
    assert NetworkStore(db).count() == 0
    # a failing speed test still stores the diagnostics as a quick row and does not retry at once
    line = periodic.tick(config, db, RecordingNotifier(), now=NOW, diagnostics=fake.diagnostics, speedtest=broken)
    assert line == "network quick check: ok" and NetworkStore(db).last()["kind"] == "quick"
    assert db.kv_get(periodic.KV_LAST_SPEED) is not None
    # a failing notifier does not lose the state change
    line = periodic.tick(
        config, db, RecordingNotifier(fail=True), now=NOW + timedelta(minutes=1), diagnostics=fake.diagnostics, speedtest=fake.speedtest
    )
    assert line == "network quick check: no_internet, notification failed" and db.kv_get(periodic.KV_ONLINE) == "0"


# --- tools --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def ctx(config, paths, db) -> ToolContext:
    c = ToolContext.build(config, paths)
    c.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None)
    return c


@pytest.fixture
def fake_probes(monkeypatch):
    """The tools call the module-level probes; replace them with fakes (no network)."""
    runner = online_runner()
    monkeypatch.setattr(
        network_tools,
        "run_diagnostics",
        lambda config: run_diagnostics(config, run=runner, resolver=resolver_ok, tcp=tcp_ok, wifi=good_wifi),
    )
    monkeypatch.setattr(
        network_tools, "run_speed_test", lambda config: run_speed_test(config, run=runner, which=lambda n: "/usr/bin/networkQuality")
    )
    monkeypatch.setattr(network_tools.speedtest, "speedtest_available", lambda which=None: True)
    return runner


def test_internet_test_full(ctx, db, fake_probes):
    tool = InternetTest(ctx)
    assert tool.risk is RiskLevel.SAFE and tool.taints is False and tool.delegate_ok is True
    result = tool.run(tool.parse_args({}))
    lines = result.content.splitlines()
    assert lines[0] == "verdict: ok | likely cause: The connection is normal"
    assert "download 23.5 Mbit/s, upload 6.9 Mbit/s, responsiveness 96 RPM, latency 166 ms" in result.content
    assert "router 192.168.1.1: 50.6 ms, 0% loss" in result.content
    assert "DNS: ok apple.com, google.com" in result.content
    assert "1.1.1.1 (ping 50.6 ms, 0% loss, tcp 443 ok)" in result.content
    assert "SSID unavailable: Location Services not granted (optional)" in result.content
    assert "interface: en0 (wifi, active, ip 192.168.1.20); home network: unknown" in result.content
    assert lines[-1].startswith("full test took")
    assert ["networkQuality", "-c", "-M", "30"] in fake_probes.calls
    row = NetworkStore(db).last()
    assert row["kind"] == "full" and row["download_mbps"] == 23.5 and row["verdict"] == "ok" and row["wifi_rssi"] == -55


def test_internet_test_quick(ctx, db, fake_probes):
    tool = InternetTest(ctx)
    result = tool.run(tool.parse_args({"full": False}))
    assert result.content.startswith("verdict: ok | likely cause: The connection is normal\nrouter 192.168.1.1")
    assert "speed:" not in result.content and result.content.splitlines()[-1].startswith("quick test took")
    assert all(c[0] != "networkQuality" for c in fake_probes.calls)
    assert NetworkStore(db).last()["kind"] == "quick"


def test_internet_test_without_network_quality(ctx, db, fake_probes, monkeypatch):
    monkeypatch.setattr(network_tools.speedtest, "speedtest_available", lambda which=None: False)
    result = InternetTest(ctx).run(network_tools.InternetTestInput(full=True))
    assert "speed: skipped: networkQuality not found" in result.content
    assert NetworkStore(db).last()["kind"] == "quick"


def test_internet_test_offline_verdict(ctx, db, monkeypatch):
    runner = FakeRunner({"route": CommandResult(1, "", "not in table")})
    monkeypatch.setattr(
        network_tools,
        "run_diagnostics",
        lambda config: run_diagnostics(config, run=runner, resolver=resolver_fail, tcp=tcp_fail, wifi=no_wifi),
    )
    monkeypatch.setattr(network_tools.speedtest, "speedtest_available", lambda which=None: False)
    result = InternetTest(ctx).run(network_tools.InternetTestInput(full=False))
    assert result.content.startswith("verdict: offline | likely cause: Wi-Fi connection or router problem")
    assert "router: no default gateway" in result.content
    assert NetworkStore(db).last()["online"] == 0


def test_internet_history_tool(ctx, db):
    tool = InternetHistory(ctx)
    assert tool.risk is RiskLevel.SAFE and tool.taints is False
    assert tool.run(tool.parse_args({})).content.startswith("No internet tests stored in the last week")
    NetworkStore(db).add("full", _diag(), _speed(42.0), likely_cause(_diag()))
    text = tool.run(tool.parse_args({"range": "day"})).content
    assert text.startswith("Internet history, last day: 1 tests, 0 failures") and "download 42 Mbit/s" in text
    schema = tool.api_schema()
    assert schema["input_schema"]["properties"]["range"]["enum"] == ["day", "week", "month"]


def test_internet_tools_open_their_own_db_without_runtime(config, paths, monkeypatch):
    c = ToolContext.build(config, paths)
    tool = InternetHistory(c)
    assert "No internet tests" in tool.run(tool.parse_args({})).content
    tool._own_db.close()


# --- doctor -------------------------------------------------------------------------------------------------------------


def test_health_checks(config, paths, db, monkeypatch):
    monkeypatch.setattr(network_tools.shutil, "which", lambda name: "/usr/bin/networkQuality" if name == "networkQuality" else None)
    checks = {c.name: c for c in health_checks(config, paths, wifi=good_wifi)}
    assert checks["networkQuality"].ok and checks["networkQuality"].detail == "/usr/bin/networkQuality"
    local = checks["Local Network permission"]
    assert local.ok and not local.warn and "Privacy & Security > Local Network" in local.detail
    loc = checks["Location Services (Wi-Fi name)"]
    assert loc.ok and loc.warn and loc.detail.startswith("SSID/BSSID hidden: optional")
    assert checks["network periodic check"].detail.startswith("off")
    assert checks["network history"].ok and checks["network history"].detail == "no tests stored yet"

    NetworkStore(db).add("quick", _diag(), None, likely_cause(_diag()))
    config.network.periodic_check_min = 15
    config.network.periodic_speedtest_hours = 12
    checks = {c.name: c for c in health_checks(config, paths, wifi=lambda: WifiInfo(power_on=True, rssi=-50, noise=-90, ssid="Home"))}
    assert checks["Location Services (Wi-Fi name)"].detail == "granted: SSID Home" and not checks["Location Services (Wi-Fi name)"].warn
    assert (
        checks["network periodic check"].detail == "quick check every 15 min, notifies on down/up; full speed test every 12 h (uses data)"
    )
    assert checks["network history"].detail.endswith("(quick): ok") and checks["network history"].detail.startswith(
        "1 test(s) stored; last "
    )

    monkeypatch.setattr(network_tools.shutil, "which", lambda name: None)
    checks = {c.name: c for c in health_checks(config, paths, wifi=no_wifi)}
    assert not checks["networkQuality"].ok and "diagnostics still work" in checks["networkQuality"].detail
    assert checks["Location Services (Wi-Fi name)"].ok and "unavailable" in checks["Location Services (Wi-Fi name)"].detail


def test_health_checks_never_raise(config, paths):
    def boom():
        raise RuntimeError("CoreWLAN exploded")

    checks = health_checks(config, paths, wifi=boom)
    assert [c.name for c in checks] == [
        "networkQuality",
        "Local Network permission",
        "Location Services (Wi-Fi name)",
        "network periodic check",
        "network history",
    ]
