"""`jarvis presence setup`: run at the Mac, at home.

1. Detect the default gateway and its MAC address, ask whether it is the home network and add it to
   `presence.home_gateway_macs` (existing entries are kept, so a second router can be added).
2. Sweep the subnet and show every device found (IP, MAC, vendor, hostname, known person).
3. Let the user pick their own phone (`is_me = true`) and, optionally, other people's devices by
   number, with names. Existing people are kept and merged by name.
4. Remind about the iPhone "Fixed" private Wi-Fi address setting, then write the config.

Prompts are injectable (`ask`, `confirm`) so tests run without a terminal; the gateway lookup and
the discovery take the same `run` / `discover` fakes as the engine.
"""

from __future__ import annotations

import logging
from typing import Callable

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from jarvis.config import Config, Paths, PresencePerson
from jarvis.i18n import t, t_list
from jarvis.network import local
from jarvis.presence import scanner
from jarvis.presence.configwrite import write_presence_config
from jarvis.presence.scanner import Device
from jarvis.presence.vendors import vendor_for

log = logging.getLogger("jarvis.presence")

Asker = Callable[[str, str], str]  # (question, default) -> answer
Confirmer = Callable[[str, bool], bool]  # (question, default) -> yes/no


def _rich_ask(console: Console) -> Asker:
    return lambda question, default: Prompt.ask(question, default=default or None, console=console) or ""


def _rich_confirm(console: Console) -> Confirmer:
    return lambda question, default: Confirm.ask(question, default=default, console=console)


def _parse_numbers(text: str, count: int) -> list[int]:
    """'1, 3' -> [0, 2]; anything else is ignored."""
    picked: list[int] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= count and int(part) - 1 not in picked:
            picked.append(int(part) - 1)
    return picked


def _device_table(devices: list[Device]) -> Table:
    table = Table(title=t("presence.setup.table_title"))
    for column in t_list("presence.setup.columns"):
        table.add_column(column)
    for i, d in enumerate(devices, start=1):
        # Hostnames are chosen by whoever owns the device: escaped so `[bold]`/`[link=...]` cannot
        # style, hide or hyperlink anything in the terminal (the control characters are gone already).
        table.add_row(str(i), d.ip, d.mac, escape(d.vendor), escape(d.hostname or ""), escape(d.person or ""))
    return table


def _merge_person(people: list[PresencePerson], name: str, macs: list[str], *, is_me: bool) -> list[PresencePerson]:
    """Add `macs` to the person called `name` (created when missing); strip them from everyone else."""
    result: list[PresencePerson] = []
    found = False
    for person in people:
        devices = [m for m in person.devices if m not in macs]
        if person.name.casefold() == name.casefold():
            found = True
            devices = devices + [m for m in macs if m not in devices]
            result.append(
                PresencePerson(name=person.name, devices=devices, is_me=person.is_me or is_me, track_history=person.track_history)
            )
        elif devices:
            result.append(
                PresencePerson(name=person.name, devices=devices, is_me=person.is_me and not is_me, track_history=person.track_history)
            )
    if not found:
        result.append(PresencePerson(name=name, devices=macs, is_me=is_me, track_history=True))
    return result


def run_setup(
    config: Config,
    paths: Paths,
    console: Console,
    *,
    run: local.Runner = local.run_command,
    discover: Callable[..., list[Device]] = scanner.discover_devices,
    ask: Asker | None = None,
    confirm: Confirmer | None = None,
) -> bool:
    """The interactive wizard. Returns True when config.local.toml was written."""
    ask = ask or _rich_ask(console)
    confirm = confirm or _rich_confirm(console)
    presence = config.presence
    console.print(f"[bold]{t('presence.setup.title')}[/] {t('presence.setup.title_hint')}\n")

    gateway, mac = local.gateway_mac(run)
    if gateway is None or mac is None:
        console.print(t("presence.setup.no_gateway"))
        return False
    console.print(t("presence.setup.gateway", ip=gateway.ip, interface=gateway.interface or "?", mac=mac, vendor=vendor_for(mac)))
    gateway_macs = list(presence.home_gateway_macs)
    if confirm(t("presence.setup.is_home_network"), True):
        if mac not in gateway_macs:
            gateway_macs.append(mac)
    elif not gateway_macs:
        console.print(t("presence.setup.no_home"))
        return False

    console.print("\n" + t("presence.setup.scanning"))
    devices = discover(config, run)
    if not devices:
        console.print(t("presence.setup.no_devices"))
        write_presence_config(paths.config_file, gateway_macs, list(presence.people))
        console.print(t("presence.setup.saved_home", path=paths.local_config_file))
        return True
    console.print(_device_table(devices))
    console.print(t("presence.setup.iphone_hint") + "\n")

    people = list(presence.people)
    me = presence.me()
    choice = ask(t("presence.setup.ask_phone"), "")
    picked = _parse_numbers(choice, len(devices))
    if picked:
        name = ask(t("presence.setup.ask_name"), me.name if me else "")
        if name.strip():
            people = _merge_person(people, name.strip(), [devices[i].mac for i in picked], is_me=True)
    while confirm(t("presence.setup.ask_more"), False):
        name = ask(t("presence.setup.ask_their_name"), "")
        numbers = ask(t("presence.setup.ask_numbers"), "")
        picked = _parse_numbers(numbers, len(devices))
        if name.strip() and picked:
            people = _merge_person(people, name.strip(), [devices[i].mac for i in picked], is_me=False)
        else:
            console.print(t("presence.setup.skipped"))

    console.print(f"\n{t('presence.setup.iphone_reminder')}\n")
    backup = write_presence_config(paths.config_file, gateway_macs, people)
    console.print(
        t("presence.setup.wrote_backup", path=paths.local_config_file, backup=backup)
        if backup
        else t("presence.setup.wrote", path=paths.local_config_file)
    )
    console.print(
        t("presence.setup.summary", count=len(gateway_macs), people=", ".join(p.name for p in people) or t("presence.setup.nobody"))
    )
    console.print(t("presence.setup.scans"))
    log.info("presence setup wrote %d gateway MAC(s) and %d people", len(gateway_macs), len(people))
    return True
