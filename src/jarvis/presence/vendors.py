"""Vendor names from MAC prefixes for the setup table and `list_network_devices`.

The table is deliberately small: a few dozen well-known 24-bit OUI prefixes (Apple, Samsung,
Google, Amazon, the usual router and smart-home makers) so the setup helper can say "that one is
probably your iPhone". It is not the IEEE registry and does not try to be; an unknown prefix is
reported as "unknown". An iPhone with a private (randomized) Wi-Fi address shows no vendor at all,
which is itself a useful hint during setup.
"""

from __future__ import annotations

from jarvis.config import normalize_mac
from jarvis.network.local import is_private_mac

PRIVATE = "Private/randomized address"
UNKNOWN = "unknown"

# 24-bit OUI prefix -> vendor. Keep it short and only add prefixes you are sure about.
OUI: dict[str, str] = {
    # Apple
    "3c:06:30": "Apple",
    "a4:83:e7": "Apple",
    "f0:18:98": "Apple",
    "bc:d0:74": "Apple",
    "8c:85:90": "Apple",
    "ac:bc:32": "Apple",
    "f4:5c:89": "Apple",
    "a8:66:7f": "Apple",
    "d0:03:4b": "Apple",
    "b8:e8:56": "Apple",
    "28:cf:e9": "Apple",
    "00:1c:b3": "Apple",
    "60:f8:1d": "Apple",
    "98:01:a7": "Apple",
    "f0:d1:a9": "Apple",
    # Phones and tablets
    "8c:f5:a3": "Samsung",
    "f4:f5:d8": "Google",
    "3c:5a:b4": "Google",
    "64:09:80": "Xiaomi",
    "28:6c:07": "Xiaomi",
    "f8:a4:5f": "Xiaomi",
    "00:e0:fc": "Huawei",
    # Speakers, TVs, consoles, smart home
    "fc:65:de": "Amazon",
    "40:b4:cd": "Amazon",
    "94:9f:3e": "Sonos",
    "5c:aa:fd": "Sonos",
    "00:17:88": "Philips Hue",
    "98:b6:e9": "Nintendo",
    "fc:0f:e6": "Sony",
    "28:18:78": "Microsoft",
    # Routers and network gear
    "50:c7:bf": "TP-Link",
    "fc:ec:da": "Ubiquiti",
    "24:5a:4c": "Ubiquiti",
    # Computers and boards
    "a4:bf:01": "Intel",
    "18:03:73": "Dell",
    "3c:d9:2b": "HP",
    "b8:27:eb": "Raspberry Pi",
    "dc:a6:32": "Raspberry Pi",
    "e4:5f:01": "Raspberry Pi",
    "24:0a:c4": "Espressif",
    "24:6f:28": "Espressif",
    "30:ae:a4": "Espressif",
    "a4:cf:12": "Espressif",
}


def vendor_for(mac: str | None) -> str:
    """The vendor for a MAC address: the private-address marker, a known vendor, or "unknown"."""
    normalized = normalize_mac(mac or "")
    if normalized is None:
        return UNKNOWN
    if is_private_mac(normalized):
        return PRIVATE
    return OUI.get(normalized[:8], UNKNOWN)
