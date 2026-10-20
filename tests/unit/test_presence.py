"""Presence: home network by gateway MAC, the `am_i_home` answers, the
debounce, events and history, the hooks, the config writer, the setup wizard. No real network and
no real subprocess: a fake runner answers route/arp/ifconfig/ping, probes are injected."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from jarvis import i18n
from jarvis.config import PresenceConfig, PresencePerson, load_config
from jarvis.exceptions import ConfigError
from jarvis.network import local
from jarvis.network.local import CommandResult
from jarvis.presence import engine, hooks
from jarvis.presence import setup as presence_setup
from jarvis.presence.configwrite import write_presence_config
from jarvis.presence.engine import Event, evaluate, tick
from jarvis.presence.scanner import Device, DeviceProbe, ScanResult, clean_hostname, discover_devices, scan_devices
from jarvis.presence.store import PresenceStore
from jarvis.presence.vendors import PRIVATE, UNKNOWN, vendor_for
from jarvis.scheduler.notify import Notifier
from jarvis.tools.base import Runtime, ToolContext
from jarvis.tools.presence import (
    ANSWER_HOME,
    ANSWER_MAC_AWAY,
    AmIHome,
    IsHome,
    ListNetworkDevices,
    PresenceHistory,
    WhoIsHome,
    health_checks,
    status_lines,
)
from jarvis.voice import announce
from jarvis.voice import pause as vpause

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
GW_MAC = "a4:2b:b0:7c:1e:9f"
FOREIGN_GW = "00:11:22:33:44:55"
PHONE = "aa:bb:cc:dd:ee:01"
PHONE2 = "aa:bb:cc:dd:ee:02"
GUEST_PHONE = "aa:bb:cc:dd:ee:03"
OTHER = "d0:ad:be:ef:00:99"  # a device nobody registered (not a private address)
PHONE_IP, PHONE2_IP, GUEST_IP, OTHER_IP = "192.168.1.50", "192.168.1.51", "192.168.1.52", "192.168.1.99"


class FakeNet:
    """Answers the commands the presence code runs; records what it was asked."""

    def __init__(
        self,
        gateway_mac: str | None = GW_MAC,
        arp: dict[str, str] | None = None,
        ping_ok: set[str] | None = None,
        tcp_ok: set[str] | None = None,
        hostnames: dict[str, str] | None = None,
    ) -> None:
        self.gateway_mac = gateway_mac
        self.arp = dict(arp or {})
        self.ping_ok = set(ping_ok or ())
        self.tcp_ok = set(tcp_ok or ())
        self.hostnames = dict(hostnames or {})
        self.calls: list[list[str]] = []
        self.nudged: list[str] = []
        self.pinged: list[str] = []

    def run(self, argv: list[str], timeout: float) -> CommandResult:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv) and 0 < timeout <= 10
        self.calls.append(argv)
        cmd = argv[0]
        if cmd == "route":
            return CommandResult(0, "   route to: default\ndestination: default\n    gateway: 192.168.1.1\n  interface: en0\n")
        if cmd == "arp":
            entries = dict(self.arp)
            if self.gateway_mac:
                entries["192.168.1.1"] = self.gateway_mac
            lines = [f"? ({ip}) at {mac} on en0 ifscope [ethernet]" for ip, mac in entries.items()]
            lines.append("? (192.168.1.77) at (incomplete) on en0 ifscope [ethernet]")
            return CommandResult(0, "\n".join(lines) + "\n")
        if cmd == "ifconfig":
            return CommandResult(
                0,
                "en0: flags=8863<UP> mtu 1500\n\tether 11:22:33:44:55:66\n\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255\n\tstatus: active\n",
            )
        if cmd == "networksetup":
            return CommandResult(0, "Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 11:22:33:44:55:66\n")
        if cmd == "ping":
            assert argv[1:-1] == ["-c", "1", "-W", "800", "-t", "2"]
            self.pinged.append(argv[-1])
            return CommandResult(0 if argv[-1] in self.ping_ok else 2, "")
        if cmd == "dscacheutil":
            ip = argv[-1]
            return CommandResult(0, f"name: {self.hostnames[ip]}\nip_address: {ip}\n" if ip in self.hostnames else "")
        raise AssertionError(f"unexpected command {argv}")

    def nudge(self, ip: str) -> None:
        self.nudged.append(ip)

    def tcp(self, ip: str) -> bool:
        return ip in self.tcp_ok


class RecordingNotifier(Notifier):
    name = "mac"  # the presence hooks keep person notifications on the Mac channel

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.sent.append((title, message))


def presence_config(config, **overrides):
    people = [
        PresencePerson(name="Kerem", devices=[PHONE, PHONE2], is_me=True, track_history=True),
        PresencePerson(name="Guest", devices=[GUEST_PHONE], track_history=False),
    ]
    values = dict(home_gateway_macs=[GW_MAC], people=people, scan_interval_s=120, away_after_min=10)
    values.update(overrides)
    return config.model_copy(update={"presence": PresenceConfig(**values)})


class Harness:
    """tick() with the fake network and injected probes; keeps the scan calls."""

    def __init__(self, config, paths, db, net: FakeNet, notifier=None, voice_running: bool = True) -> None:
        self.config, self.paths, self.db, self.net = config, paths, db, net
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.voice_running = voice_running
        self.scans: list[dict] = []

    def scanner(self, config, **kw):
        self.scans.append(kw)
        return scan_devices(config, nudge=self.net.nudge, tcp=self.net.tcp, sleep=lambda s: None, **kw)

    def tick(self, now: datetime, force: bool = True) -> list[Event]:
        return tick(
            self.config,
            self.paths,
            self.db,
            self.notifier,
            now=now,
            run=self.net.run,
            scanner=self.scanner,
            voice_status=lambda: self.voice_running,
            force=force,
        )


def tool_ctx(config, paths, db, channel: str = "cli") -> ToolContext:
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None, channel=channel)
    return ctx


# --- home network ---------------------------------------------------------------------------------------


def test_gateway_mac_from_arp_fixture():
    net = FakeNet()
    gateway, mac = local.gateway_mac(net.run)
    assert gateway.ip == "192.168.1.1" and gateway.interface == "en0" and mac == GW_MAC
    assert local.gateway_mac(FakeNet(gateway_mac=None).run)[1] is None


def test_tick_records_mac_at_home_and_scans_only_on_the_home_network(config, paths, db):
    cfg = presence_config(config)
    home = Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP}))
    assert home.tick(T0) and PresenceStore(db).mac_at_home() is True and len(home.scans) == 1
    assert PresenceStore(db).gateway_mac() == GW_MAC

    away = Harness(cfg, paths, db, FakeNet(gateway_mac=FOREIGN_GW, arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP}))
    events = away.tick(T0 + timedelta(minutes=5))
    assert [e.kind for e in events] == ["mac_left_home_network"]
    assert away.scans == []  # nobody is scanned from a foreign network
    store = PresenceStore(db)
    assert store.mac_at_home() is False and store.gateway_mac() is None  # the foreign gateway is not recorded
    assert store.get("Kerem").status == "home"  # states untouched while the Mac is away

    back = Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP}))
    assert [e.kind for e in back.tick(T0 + timedelta(minutes=10))] == ["mac_joined_home_network"]


def test_tick_without_home_network_only_records_the_scan_time(config, paths, db):
    cfg = presence_config(config, home_gateway_macs=[])
    h = Harness(cfg, paths, db, FakeNet())
    assert h.tick(T0) == [] and h.scans == [] and h.net.calls == []
    assert PresenceStore(db).last_scan_at() == T0 and PresenceStore(db).mac_at_home() is None


def test_tick_waits_for_the_scan_interval(config, paths, db):
    h = Harness(presence_config(config), paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP}))
    assert h.tick(T0, force=False) != []  # the first tick scans
    assert h.tick(T0 + timedelta(seconds=30), force=False) == [] and len(h.scans) == 1
    assert h.tick(T0 + timedelta(seconds=119), force=False) == [] and len(h.scans) == 1
    h.tick(T0 + timedelta(seconds=120), force=False)
    assert len(h.scans) == 2


def test_tick_never_raises(config, paths, db):
    def exploding_scanner(config, **kw):
        raise RuntimeError("boom")

    cfg = presence_config(config)
    events = tick(cfg, paths, db, None, now=T0, run=FakeNet().run, scanner=exploding_scanner, force=True)
    assert events == []


# --- the state machine ---------------------------------------------------------------------------------


def test_arrival_emits_one_event_and_records_history(config, paths, db):
    cfg = presence_config(config)
    h = Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP}))
    events = h.tick(T0)
    assert [(e.kind, e.person) for e in events] == [("person_arrived", "Kerem"), ("i_arrived_home", "Kerem")]
    store = PresenceStore(db)
    state = store.get("Kerem")
    assert (
        state.status == "home" and state.last_seen == T0 and state.since == T0 and state.last_device == PHONE and state.last_ip == PHONE_IP
    )
    assert [(e.person, e.event) for e in store.history(None, "day", T0)] == [("Kerem", "arrived")]
    assert h.tick(T0 + timedelta(minutes=2)) == []  # still home: no second arrival
    assert store.get("Guest").status == "away"  # never seen: away after the first scan, no event


def test_debounce_then_away_with_exactly_one_left_event(config, paths, db):
    cfg = presence_config(config)
    present = FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP})
    Harness(cfg, paths, db, present).tick(T0)
    gone = FakeNet(arp={}, ping_ok=set())
    h = Harness(cfg, paths, db, gone)
    assert h.tick(T0 + timedelta(minutes=3)) == []
    assert PresenceStore(db).get("Kerem").status == "home"  # a phone asleep for 3 minutes is still home
    assert h.tick(T0 + timedelta(minutes=6)) == []
    events = h.tick(T0 + timedelta(minutes=11))
    assert [(e.kind, e.person) for e in events] == [("person_left", "Kerem"), ("i_left_home", "Kerem")]
    state = PresenceStore(db).get("Kerem")
    assert state.status == "away" and state.last_seen == T0 and state.since == T0 + timedelta(minutes=11)
    assert h.tick(T0 + timedelta(minutes=20)) == []
    history = PresenceStore(db).history("Kerem", "day", T0)
    assert [e.event for e in history] == ["arrived", "left"]
    assert gone.pinged.count(PHONE_IP) >= 1  # the last known IP is reused when the ARP entry is gone


def test_any_device_of_a_person_counts(config, paths, db):
    cfg = presence_config(config)
    net = FakeNet(arp={PHONE2_IP: PHONE2}, tcp_ok={PHONE2_IP})  # only the second device answers, and only on TCP
    events = Harness(cfg, paths, db, net).tick(T0)
    assert [e.kind for e in events] == ["person_arrived", "i_arrived_home"]
    state = PresenceStore(db).get("Kerem")
    assert state.status == "home" and state.last_device == PHONE2


def test_track_history_false_keeps_the_state_but_no_events(config, paths, db):
    cfg = presence_config(config)
    net = FakeNet(arp={GUEST_IP: GUEST_PHONE}, ping_ok={GUEST_IP})
    h = Harness(cfg, paths, db, net)
    events = h.tick(T0)
    assert [(e.kind, e.person) for e in events] == [("person_arrived", "Guest")]  # no i_arrived_home: not me
    store = PresenceStore(db)
    assert store.get("Guest").status == "home"
    assert store.history(None, "month", T0) == []
    Harness(cfg, paths, db, FakeNet()).tick(T0 + timedelta(minutes=15))
    assert store.get("Guest").status == "away" and store.history(None, "month", T0) == []


def test_evaluate_is_pure_and_handles_stale_rows(config):
    cfg = presence_config(config)
    scan = ScanResult(
        devices=[DeviceProbe(PHONE, "Kerem", False), DeviceProbe(PHONE2, "Kerem", False), DeviceProbe(GUEST_PHONE, "Guest", False)]
    )
    states = {"Kerem": engine.PersonState("Kerem", "home", last_seen=None)}  # a home row without a timestamp
    new, events = evaluate(cfg, states, scan, T0)
    assert new["Kerem"].status == "away" and [e.kind for e in events] == ["person_left", "i_left_home"]
    assert states["Kerem"].status == "home"  # the input was not mutated
    with pytest.raises(ValueError):
        Event("teleported", T0)


# --- the scanner ---------------------------------------------------------------------------------------------


def test_sweep_runs_on_every_fifth_scan(config, paths, db):
    cfg = presence_config(config, people=[PresencePerson(name="Kerem", devices=[PHONE], is_me=True)])
    net = FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP})
    h = Harness(cfg, paths, db, net)
    for i in range(10):
        h.tick(T0 + timedelta(minutes=3 * i))
    assert [kw["sweep"] for kw in h.scans] == [True, False, False, False, False, True, False, False, False, False]
    assert len(net.nudged) == 2 * 253  # every /24 host except the Mac itself, twice
    assert all(not ip.endswith(".20") for ip in net.nudged)


def test_sweep_when_a_device_has_no_known_ip(config, paths, db):
    cfg = presence_config(config)
    net = FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP})  # the guest phone was never seen: no IP known
    h = Harness(cfg, paths, db, net)
    for i in range(3):
        h.tick(T0 + timedelta(minutes=3 * i))
    assert [kw["sweep"] for kw in h.scans] == [True, True, True]


def test_scan_devices_probe_order_and_arp_proof(config):
    cfg = presence_config(config)
    # Without a sweep a stale ARP entry alone is not proof; a probe must answer.
    net = FakeNet(arp={PHONE_IP: PHONE})
    result = scan_devices(cfg, run=net.run, known_ips={}, sweep=False, nudge=net.nudge, tcp=net.tcp, sleep=lambda s: None)
    assert not any(d.seen for d in result.devices) and net.pinged == [PHONE_IP]
    # After a sweep the refreshed ARP entry counts.
    result = scan_devices(cfg, run=net.run, known_ips={}, sweep=True, nudge=net.nudge, tcp=net.tcp, sleep=lambda s: None)
    seen = {d.mac: d.how for d in result.devices if d.seen}
    assert seen == {PHONE: "arp"} and result.swept
    # A known IP is probed even when the ARP entry is gone; ping wins over tcp.
    net = FakeNet(arp={}, ping_ok={PHONE2_IP}, tcp_ok={PHONE2_IP})
    result = scan_devices(cfg, run=net.run, known_ips={PHONE2: PHONE2_IP}, sweep=False, nudge=net.nudge, tcp=net.tcp, sleep=lambda s: None)
    assert {d.mac: d.how for d in result.devices if d.seen} == {PHONE2: "ping"}
    # Only configured devices are probed: the unregistered one is never pinged.
    net = FakeNet(arp={OTHER_IP: OTHER, PHONE_IP: PHONE})
    scan_devices(cfg, run=net.run, known_ips={}, sweep=False, nudge=net.nudge, tcp=net.tcp, sleep=lambda s: None)
    assert OTHER_IP not in net.pinged


def test_discover_devices_lists_vendor_hostname_and_person(config):
    cfg = presence_config(config)
    net = FakeNet(arp={PHONE_IP: PHONE, OTHER_IP: "b8:27:eb:12:34:56"}, hostnames={OTHER_IP: "pi.local"})
    devices = discover_devices(cfg, net.run, nudge=net.nudge, sleep=lambda s: None)
    by_ip = {d.ip: d for d in devices}
    assert by_ip[PHONE_IP].person == "Kerem" and by_ip[PHONE_IP].hostname is None
    assert by_ip[OTHER_IP] == Device(OTHER_IP, "b8:27:eb:12:34:56", "Raspberry Pi", "pi.local", None)
    assert by_ip["192.168.1.1"].person is None and len(net.nudged) == 253
    assert by_ip["192.168.1.1"].vendor == "gateway (router)"
    assert [d.ip for d in devices] == ["192.168.1.1", PHONE_IP, OTHER_IP]


def test_discover_devices_skips_broadcast_and_multicast_entries(config):
    cfg = presence_config(config)
    net = FakeNet(
        arp={
            PHONE_IP: PHONE,
            "192.168.1.255": "ff:ff:ff:ff:ff:ff",
            "224.0.0.251": "01:00:5e:00:00:fb",
            "239.255.255.250": "01:00:5e:7f:ff:fa",
        }
    )
    devices = discover_devices(cfg, net.run, nudge=net.nudge, sleep=lambda s: None)
    assert [d.ip for d in devices] == ["192.168.1.1", PHONE_IP]


def test_vendor_lookup():
    assert vendor_for("b8:27:eb:aa:bb:cc") == "Raspberry Pi" and vendor_for("3C:06:30:00:00:01") == "Apple"
    assert vendor_for("aa:bb:cc:dd:ee:01") == PRIVATE  # locally administered: an iPhone's private address
    assert vendor_for("00:11:22:33:44:55") == UNKNOWN and vendor_for("not a mac") == UNKNOWN and vendor_for(None) == UNKNOWN


def test_clean_hostname():
    assert clean_hostname("  Kerem's\niPhone\t | x ") == "Kerem's iPhone / x"
    assert clean_hostname("") is None and clean_hostname(None) is None
    assert len(clean_hostname("a" * 200)) == 60


# --- the store ---------------------------------------------------------------------------------------------


def test_history_ranges(db):
    store = PresenceStore(db)
    me = PresencePerson(name="Kerem", devices=[PHONE], is_me=True)
    now = T0.astimezone()
    store.add_event(me, "arrived", now - timedelta(hours=1))
    store.add_event(me, "left", now - timedelta(days=3))
    store.add_event(me, "arrived", now - timedelta(days=20))
    store.add_event(me, "left", now - timedelta(days=40))
    assert [e.event for e in store.history("Kerem", "day", now)] == ["arrived"] or now.hour < 1
    assert len(store.history("Kerem", "week", now)) == 2
    assert len(store.history("Kerem", "month", now)) == 3
    assert store.history("Nobody", "month", now) == []
    with pytest.raises(ValueError):
        store.history(None, "year", now)
    assert store.add_event(PresencePerson(name="Guest", devices=[GUEST_PHONE], track_history=False), "arrived", now) is False
    assert store.get("Nobody") is None and store.all() == []


# --- the tools ------------------------------------------------------------------------------------------------


def test_am_i_home_three_answers(config, paths, db):
    cfg = presence_config(config)
    tool = AmIHome(tool_ctx(cfg, paths, db))
    assert (
        "run `jarvis presence setup`"
        in AmIHome(tool_ctx(presence_config(config, home_gateway_macs=[]), paths, db)).run(tool.parse_args({})).content
    )
    assert "no presence scan has run yet" in tool.run(tool.parse_args({})).content

    Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP})).tick(T0)
    assert tool.run(tool.parse_args({})).content.startswith(ANSWER_HOME)

    Harness(cfg, paths, db, FakeNet()).tick(T0 + timedelta(minutes=15))
    text = tool.run(tool.parse_args({})).content
    assert text.startswith("The user's phone is not on the network, so they are probably out (last seen: ")
    assert T0.astimezone().strftime("%Y-%m-%d %H:%M") in text  # local time

    Harness(cfg, paths, db, FakeNet(gateway_mac=FOREIGN_GW)).tick(T0 + timedelta(minutes=20))
    assert tool.run(tool.parse_args({})).content.startswith(ANSWER_MAC_AWAY)


def test_am_i_home_without_me(config, paths, db):
    cfg = presence_config(config, people=[PresencePerson(name="Guest", devices=[GUEST_PHONE])])
    Harness(cfg, paths, db, FakeNet()).tick(T0)
    text = AmIHome(tool_ctx(cfg, paths, db)).run(AmIHome.input_model()).content
    assert "Mac is on the home network" in text and "is_me" in text


def test_who_is_home_and_is_home(config, paths, db):
    cfg = presence_config(config)
    ctx = tool_ctx(cfg, paths, db)
    who = WhoIsHome(ctx)
    assert "jarvis scheduler install" in who.run(who.parse_args({})).content
    Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP})).tick(T0)
    text = who.run(who.parse_args({})).content
    assert "Mac on the home network: yes" in text and "- Kerem (me): home" in text and "- Guest: away" in text
    assert PHONE not in text and PHONE_IP not in text
    is_home = IsHome(ctx)
    assert is_home.run(is_home.parse_args({"name": "kerem"})).content.startswith("Kerem: home")
    assert "not a configured person" in is_home.run(is_home.parse_args({"name": "Stranger"})).content


def test_presence_history_tool(config, paths, db):
    cfg = presence_config(config)
    ctx = tool_ctx(cfg, paths, db)
    tool = PresenceHistory(ctx)
    assert "No arrivals or departures" in tool.run(tool.parse_args({"name": "Kerem"})).content
    assert "track_history = false" in tool.run(tool.parse_args({"name": "Guest"})).content
    assert "not a configured person" in tool.run(tool.parse_args({"name": "Nobody"})).content
    now = datetime.now(timezone.utc)
    Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE, GUEST_IP: GUEST_PHONE}, ping_ok={PHONE_IP, GUEST_IP})).tick(now)
    text = tool.run(tool.parse_args({"range": "week"})).content
    assert "Kerem arrived" in text and "Guest" not in text


def test_list_network_devices_marks_registered_people(config, paths, db):
    cfg = presence_config(config)
    net = FakeNet(arp={PHONE_IP: PHONE, OTHER_IP: OTHER}, hostnames={OTHER_IP: "tv.local"})
    tool = ListNetworkDevices(
        tool_ctx(cfg, paths, db), run=net.run, discover=lambda c, r: discover_devices(c, r, nudge=net.nudge, sleep=lambda s: None)
    )
    text = tool.run(tool.parse_args({})).content
    assert f"{PHONE_IP} | {PHONE} | {PRIVATE} | - | Kerem" in text
    assert f"{OTHER_IP} | {OTHER} | unknown | tv.local | -" in text
    assert tool.taints and tool.taint_source == "network" and tool.delegate_ok is False


# --- hooks ----------------------------------------------------------------------------------------------------


def arrive(name="Kerem"):
    return [Event("person_arrived", T0, name), Event("i_arrived_home", T0, name)]


def test_hooks_do_nothing_when_disabled(config, paths):
    cfg = presence_config(config)
    notifier = RecordingNotifier()
    ran = hooks.dispatch(arrive() + [Event("mac_left_home_network", T0)], cfg, paths, notifier, voice_status=lambda: True)
    assert ran == [] and notifier.sent == [] and not paths.voice_announce_file.exists() and not paths.voice_pause_file.exists()


def test_arrive_greet_respects_voice_running_and_paused(config, paths):
    cfg = presence_config(config, on_arrive_greet=True)
    assert hooks.dispatch(arrive(), cfg, paths, None, voice_status=lambda: False) == ["arrive_greet"]
    assert not paths.voice_announce_file.exists()  # agent not running: nothing to say it with
    vpause.pause(paths.voice_pause_file, by="cli")
    hooks.dispatch(arrive(), cfg, paths, None, voice_status=lambda: True)
    assert not paths.voice_announce_file.exists()  # paused: stay silent
    vpause.resume(paths.voice_pause_file)
    hooks.dispatch(arrive(), cfg, paths, None, voice_status=lambda: True)
    ann = announce.take(paths.voice_announce_file)
    assert ann is not None and ann.kind == "turn" and ann.source == "presence" and "daily_brief" in ann.text and "(tr)" in ann.text
    hooks.dispatch([Event("person_arrived", T0, "Guest")], cfg, paths, None, voice_status=lambda: True)
    assert not paths.voice_announce_file.exists()  # someone else arriving is not a greeting


def test_leave_pause_voice_and_resume_only_undo_a_presence_pause(config, paths):
    cfg = presence_config(config, on_leave_pause_voice=True)
    left, joined = [Event("mac_left_home_network", T0)], [Event("mac_joined_home_network", T0)]
    assert hooks.dispatch(left, cfg, paths, None, voice_status=lambda: True) == ["leave_pause_voice"]
    assert vpause.load_state(paths.voice_pause_file).by == "presence"
    hooks.dispatch(joined, cfg, paths, None, voice_status=lambda: True)
    assert not vpause.is_paused(paths.voice_pause_file)
    # a manual pause is neither replaced nor undone
    vpause.pause(paths.voice_pause_file, by="cli")
    hooks.dispatch(left, cfg, paths, None, voice_status=lambda: True)
    assert vpause.load_state(paths.voice_pause_file).by == "cli"
    hooks.dispatch(joined, cfg, paths, None, voice_status=lambda: True)
    assert vpause.load_state(paths.voice_pause_file).by == "cli" and vpause.is_paused(paths.voice_pause_file)


def test_notify_person_changes(config, paths):
    cfg = presence_config(config, notify_person_changes=True)
    notifier = RecordingNotifier()
    events = arrive() + [Event("person_left", T0, "Guest"), Event("mac_left_home_network", T0)]
    assert hooks.dispatch(events, cfg, paths, notifier, voice_status=lambda: True) == ["notify_person_changes"] * 2
    assert notifier.sent == [("Jarvis", "Kerem eve geldi"), ("Jarvis", "Guest evden çıktı")]
    i18n.set_language("en")
    english = RecordingNotifier()
    hooks.dispatch(events, cfg, paths, english, voice_status=lambda: True)
    assert english.sent == [("Jarvis", "Kerem arrived home"), ("Jarvis", "Guest left home")]
    assert hooks.dispatch(arrive(), cfg, paths, None, voice_status=lambda: True) == [
        "notify_person_changes"
    ]  # no notifier: logged, not raised


def test_hook_failures_are_logged_not_raised(config, paths):
    class Exploding(Notifier):
        name = "mac"  # a Mac channel, so the hook does not swap it for a real MacNotifier

        def notify(self, title, message):
            raise RuntimeError("osascript exploded")

    cfg = presence_config(config, notify_person_changes=True)
    assert hooks.dispatch(arrive(), cfg, paths, Exploding(), voice_status=lambda: True) == []


def test_tick_dispatches_hooks(config, paths, db):
    cfg = presence_config(config, notify_person_changes=True)
    h = Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP}))
    h.tick(T0)
    assert h.notifier.sent == [("Jarvis", "Kerem eve geldi")]


# --- config writer and setup ------------------------------------------------------------------------------


def test_write_presence_config_round_trip(paths):
    people = [
        PresencePerson(name='Kerem "O"', devices=[PHONE, PHONE2], is_me=True),
        PresencePerson(name="Guest", devices=[GUEST_PHONE], track_history=False),
    ]
    shipped = paths.config_file.read_text()
    assert write_presence_config(paths.config_file, [GW_MAC], people) is None  # no overlay yet: nothing to back up
    assert paths.config_file.read_text() == shipped  # the tracked file never receives personal data
    local = paths.local_config_file
    text = local.read_text()
    assert text.startswith("# Personal settings for this Mac") and f'home_gateway_macs = ["{GW_MAC}"]' in text
    assert text.count("\n[[presence.people]]") == 2
    cfg = load_config(paths.config_file)
    assert cfg.presence.home_gateway_macs == [GW_MAC]
    assert [(p.name, p.devices, p.is_me, p.track_history) for p in cfg.presence.people] == [
        ('Kerem "O"', [PHONE, PHONE2], True, True),
        ("Guest", [GUEST_PHONE], False, False),
    ]
    # writing again replaces the real blocks instead of appending more, and keeps a backup of the overlay
    backup = write_presence_config(paths.config_file, [GW_MAC, FOREIGN_GW], people[:1])
    assert backup.is_file() and backup.name == "config.local.toml.bak" and backup.read_text() == text
    again = load_config(paths.config_file)
    assert again.presence.home_gateway_macs == [GW_MAC, FOREIGN_GW] and [p.name for p in again.presence.people] == ['Kerem "O"']
    assert local.read_text().count("\n[[presence.people]]") == 1
    assert "scan_interval_s = 120" in paths.config_file.read_text()  # the rest of [presence] is untouched


def test_write_presence_config_keeps_other_local_settings(paths):
    paths.local_config_file.write_text('[general]\nlog_level = "DEBUG"\n\n[presence]\naway_after_min = 3  # mine\n')
    write_presence_config(paths.config_file, [GW_MAC], [PresencePerson(name="Kerem", devices=[PHONE], is_me=True)])
    cfg = load_config(paths.config_file)
    assert cfg.general.log_level == "DEBUG" and cfg.presence.away_after_min == 3
    assert cfg.presence.home_gateway_macs == [GW_MAC] and cfg.presence.me().name == "Kerem"
    assert "away_after_min = 3  # mine" in paths.local_config_file.read_text()


def test_write_presence_config_restores_overlay_when_result_is_invalid(paths, monkeypatch):
    from jarvis.presence import configwrite

    paths.local_config_file.write_text("[presence]\nhome_gateway_macs = []\n")
    before = paths.local_config_file.read_text()
    monkeypatch.setattr(configwrite, "people_blocks", lambda people: "[[presence.people]]\nname = 7\n")
    with pytest.raises(ConfigError, match="restored from"):
        write_presence_config(paths.config_file, [GW_MAC], [PresencePerson(name="Kerem", devices=[PHONE])])
    assert paths.local_config_file.read_text() == before
    paths.local_config_file.unlink()
    with pytest.raises(ConfigError, match="removed it again"):
        write_presence_config(paths.config_file, [GW_MAC], [PresencePerson(name="Kerem", devices=[PHONE])])
    assert not paths.local_config_file.exists()


def test_setup_wizard_writes_gateway_and_people(config, paths):
    net = FakeNet(arp={PHONE_IP: PHONE, GUEST_IP: GUEST_PHONE, OTHER_IP: OTHER}, hostnames={PHONE_IP: "iphone.local"})
    answers = iter(["2", "Kerem", "Ayşe", "3"])
    confirms = iter([True, True, False])
    console = Console(file=open("/dev/null", "w"), width=120)
    ok = presence_setup.run_setup(
        config,
        paths,
        console,
        run=net.run,
        discover=lambda c, r: discover_devices(c, r, nudge=net.nudge, sleep=lambda s: None),
        ask=lambda q, d: next(answers),
        confirm=lambda q, d: next(confirms),
    )
    assert ok
    cfg = load_config(paths.config_file)
    assert cfg.presence.home_gateway_macs == [GW_MAC]
    assert [(p.name, p.devices, p.is_me) for p in cfg.presence.people] == [("Kerem", [PHONE], True), ("Ayşe", [GUEST_PHONE], False)]


def test_setup_wizard_needs_a_gateway(config, paths):
    console = Console(file=open("/dev/null", "w"))
    assert presence_setup.run_setup(config, paths, console, run=FakeNet(gateway_mac=None).run, discover=lambda c, r: []) is False
    assert load_config(paths.config_file).presence.home_gateway_macs == []


def test_setup_merges_existing_people():
    people = [PresencePerson(name="Kerem", devices=[PHONE], is_me=True), PresencePerson(name="Guest", devices=[GUEST_PHONE])]
    merged = presence_setup._merge_person(people, "kerem", [PHONE2, GUEST_PHONE], is_me=True)
    assert [(p.name, p.devices, p.is_me) for p in merged] == [("Kerem", [PHONE, PHONE2, GUEST_PHONE], True)]  # Guest lost its only device
    assert presence_setup._parse_numbers("1, 3, 9, x, 1", 3) == [0, 2]


# --- doctor and status --------------------------------------------------------------------------------------


def test_health_checks_and_status_lines(config, paths, db):
    names = [c.name for c in health_checks(config, paths)]
    assert names == ["presence home network", "presence people", "presence last scan", "presence remote"]
    details = {c.name: c.detail for c in health_checks(config, paths)}
    assert "jarvis presence setup" in details["presence home network"] and "jarvis scheduler install" in details["presence last scan"]
    assert details["presence remote"].startswith("blocked from Telegram")
    cfg = presence_config(config)
    Harness(cfg, paths, db, FakeNet(arp={PHONE_IP: PHONE}, ping_ok={PHONE_IP})).tick(T0)
    details = {c.name: c.detail for c in health_checks(cfg, paths)}
    assert details["presence people"] == "2 configured, me = Kerem" and "Mac at home: yes" in details["presence last scan"]
    lines = status_lines(cfg, paths, db)  # the test config is Turkish
    assert any("Kerem (ben): evde" in line for line in lines) and any("Guest [geçmiş yok]: dışarıda" in line for line in lines)
    assert any("1. tarama" in line for line in lines)
    i18n.set_language("en")
    lines = status_lines(cfg, paths, db)
    assert any("Kerem (me): home" in line for line in lines) and any("Guest [no history]: away" in line for line in lines)
    assert any("scan #1" in line for line in lines) and "Remote: blocked from Telegram (allow_remote = false)" in lines


def test_person_notifications_stay_on_the_mac_unless_allow_remote(config, paths, monkeypatch):
    """A stolen phone must not learn who left the house: with allow_remote = false the Telegram
    channel of the scheduler's notifier is dropped and a Mac notification is sent instead."""
    from jarvis.scheduler import notify as notify_mod

    class Telegramish(Notifier):
        name = "telegram"

        def __init__(self):
            self.sent = []

        def notify(self, title, message):
            self.sent.append((title, message))

    built: list[tuple[str, str]] = []
    monkeypatch.setattr(notify_mod.MacNotifier, "notify", lambda self, title, message: built.append((title, message)))
    cfg = presence_config(config, notify_person_changes=True)
    mac, tg = RecordingNotifier(), Telegramish()
    hooks.dispatch(arrive(), cfg, paths, notify_mod.MultiNotifier([mac, tg]), voice_status=lambda: True)
    assert mac.sent == [("Jarvis", "Kerem eve geldi")] and tg.sent == []
    hooks.dispatch(arrive(), cfg, paths, tg, voice_status=lambda: True)  # telegram only: a Mac notifier is built instead
    assert tg.sent == [] and built == [("Jarvis", "Kerem eve geldi")]
    cfg = presence_config(config, notify_person_changes=True, allow_remote=True)
    hooks.dispatch(arrive(), cfg, paths, tg, voice_status=lambda: True)
    assert tg.sent == [("Jarvis", "Kerem eve geldi")]


def test_store_refuses_events_for_untracked_people_and_purges_old_rows(config, paths, db):
    store = PresenceStore(db)
    guest = PresencePerson(name="Guest", devices=[GUEST_PHONE], track_history=False)
    assert store.add_event(guest, "arrived", T0) is False and store.history("Guest", "month", T0) == []
    tracked = PresencePerson(name="Guest", devices=[GUEST_PHONE], track_history=True)
    assert store.add_event(tracked, "arrived", T0) is True and len(store.history("Guest", "month", T0)) == 1
    # the flag is switched off later: the next tick removes what was recorded before
    cfg = presence_config(config)
    Harness(cfg, paths, db, FakeNet()).tick(T0)
    assert store.history("Guest", "month", T0) == [] and store.purge_history("Guest") == 0


def test_setup_table_escapes_rich_markup_in_hostnames():
    from io import StringIO

    from rich.console import Console

    from jarvis.presence.setup import _device_table

    devices = [
        Device("192.168.1.9", OTHER, "unknown", "[/bold]", None),
        Device("192.168.1.10", PHONE, "Apple", "[link=https://evil.example]router[/link]", "Kerem"),
    ]
    buffer = StringIO()
    Console(file=buffer, force_terminal=True, width=160).print(_device_table(devices))  # a closing tag must not raise
    out = buffer.getvalue()
    assert "[/bold]" in out and "evil.example" in out and "\x1b]8;" not in out  # no OSC 8 hyperlink emitted


def test_config_writer_drops_comments_inside_a_real_people_block(paths):
    from jarvis.config import load_config
    from jarvis.presence.configwrite import write_presence_config

    paths.local_config_file.write_text(
        '# keep me\n[[presence.people]]\nname = "Old"\n# a note inside the block\ndevices = ["aa:bb:cc:dd:ee:01"]\n'
    )
    write_presence_config(paths.config_file, [GW_MAC], [PresencePerson(name="Kerem", devices=[PHONE], is_me=True)])
    cfg = load_config(paths.config_file)
    assert [p.name for p in cfg.presence.people] == ["Kerem"]
    text = paths.local_config_file.read_text()
    assert "a note inside the block" not in text and text.startswith("# keep me\n")
