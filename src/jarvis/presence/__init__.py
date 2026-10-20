"""Presence: "Am I home?" and "Who is home?".

Two signals, both needed because Jarvis runs on a MacBook that travels:

- the Mac is at home when its default gateway's MAC address is one of
  `presence.home_gateway_macs` (`engine.tick` checks it every scan);
- a person is at home when one of the devices listed for them in `presence.people` answers on
  the home network (`scanner.scan_devices`), with a debounce so a sleeping phone does not flap
  (`engine.evaluate`).

Privacy rules (a reviewer audits these):

- Presence data stays on this Mac: states and events live in the local SQLite database
  (`store.PresenceStore`); nothing is uploaded and nothing is sent to Telegram unless
  `presence.allow_remote` / `allow_remote_self` say so (`tools.presence`).
- Only devices listed in the config are probed. The periodic "sweep" is one empty UDP datagram
  per subnet host so the kernel fills the ARP table; no port scanning, no root privileges.
- Logs carry names and states only: never ARP dumps, never the MAC or IP of an unregistered
  device, never hostnames.
- History (arrivals and departures) is kept only for people with `track_history = true`;
  everyone else has just a current state.
- Hostnames and mDNS names are chosen by whoever owns the device: `list_network_devices` is a
  tainting tool and sanitizes them before they reach the model.
"""
