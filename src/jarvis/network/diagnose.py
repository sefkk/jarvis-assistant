"""The plain-language verdict, computed in code from simple rules so the model only
has to explain it. The rules run in order and the first match wins: offline, router unreachable,
no internet behind the router, DNS, weak Wi-Fi, slow, ok. The `likely_cause` strings are English for
the model and the history (the model puts them into the user's language); the outage notification takes
its wording from the locale by verdict key. The verdict keys are stable identifiers for the history.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.network.probes import Diagnostics
from jarvis.network.speedtest import SpeedResult

# Thresholds (first match wins, see `likely_cause`). Wi-Fi: below -75 dBm or under 15 dB above the
# noise floor most clients drop to low rates. Speed: under 10 Mbit/s down, under 50 RPM or more
# than 300 ms idle RTT feels slow for video calls. Quick check (no speed test): a router that takes
# over 100 ms on the LAN or a public host over 400 ms away means the link is struggling.
RSSI_WEAK_DBM = -75
SNR_WEAK_DB = 15
SLOW_DOWNLOAD_MBPS = 10.0
SLOW_RESPONSIVENESS_RPM = 50
SLOW_RTT_MS = 300.0
SLOW_GATEWAY_MS = 100.0
SLOW_PUBLIC_PING_MS = 400.0

OFFLINE = "offline"
GATEWAY_UNREACHABLE = "gateway_unreachable"
NO_INTERNET = "no_internet"
DNS = "dns"
WEAK_WIFI = "weak_wifi"
SLOW = "slow"
OK = "ok"

# Verdicts that mean "the internet is not usable" (periodic down/up transitions, history failures).
FAILURE_VERDICTS = frozenset({OFFLINE, GATEWAY_UNREACHABLE, NO_INTERNET, DNS})

CAUSES = {
    OFFLINE: "Wi-Fi connection or router problem",
    GATEWAY_UNREACHABLE: "Wi-Fi connection or router problem",
    NO_INTERNET: "The router is not connected to the internet / internet provider problem",
    DNS: "DNS problem",
    WEAK_WIFI: "Weak Wi-Fi signal, move closer to the router",
    SLOW: "The connection works but is slow",
    OK: "The connection is normal",
}


@dataclass(frozen=True)
class Verdict:
    key: str
    cause: str  # English, for the model and the history

    @property
    def failure(self) -> bool:
        return self.key in FAILURE_VERDICTS


def verdict(key: str) -> Verdict:
    return Verdict(key, CAUSES[key])


def _weak_wifi(diag: Diagnostics) -> bool:
    wifi = diag.wifi
    if wifi is None or wifi.rssi is None or diag.interface_kind not in (None, "wifi"):
        return False  # on Ethernet the Wi-Fi signal does not matter
    if wifi.rssi <= RSSI_WEAK_DBM:
        return True
    return wifi.snr is not None and wifi.snr < SNR_WEAK_DB


def _slow(diag: Diagnostics, speed: SpeedResult | None) -> bool:
    if speed is not None:
        return (
            (speed.download_mbps is not None and speed.download_mbps < SLOW_DOWNLOAD_MBPS)
            or (speed.responsiveness_rpm is not None and speed.responsiveness_rpm < SLOW_RESPONSIVENESS_RPM)
            or (speed.latency_ms is not None and speed.latency_ms > SLOW_RTT_MS)
        )
    gateway_ms = diag.gateway_latency_ms
    public_ms = diag.best_public_ping_ms
    return (gateway_ms is not None and gateway_ms > SLOW_GATEWAY_MS) or (public_ms is not None and public_ms > SLOW_PUBLIC_PING_MS)


def likely_cause(diag: Diagnostics, speed: SpeedResult | None = None) -> Verdict:
    if diag.gateway_ip is None or (diag.interface is not None and not diag.interface_active and diag.interface_kind is not None):
        return verdict(OFFLINE)
    if not diag.gateway_reachable:
        return verdict(GATEWAY_UNREACHABLE)
    if not diag.online:
        return verdict(NO_INTERNET)
    if not diag.dns_ok:
        return verdict(DNS)
    if _weak_wifi(diag):
        return verdict(WEAK_WIFI)
    if _slow(diag, speed):
        return verdict(SLOW)
    return verdict(OK)
