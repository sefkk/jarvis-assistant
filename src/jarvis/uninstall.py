"""`jarvis uninstall`: remove what Jarvis put on this Mac outside the repository folder, step by step.

1. The launchd agents (scheduler, Telegram, voice): always, they would keep running otherwise.
2. The Keychain entries (API keys, tokens, the Google login): only after a yes.
3. The local data (config.toml, config.local.toml, data/ with the profile, memory and transcripts, logs/):
   only after a second yes. Every default answer is no.

The `jarvis` command itself and the repository stay: the closing lines say how to remove them.
"""

from __future__ import annotations

import importlib
import shutil
from typing import Any, Callable

from rich.console import Console

from jarvis.i18n import t
from jarvis.utils import secrets


def remove_agents(console: Console) -> list[str]:
    """Unload and delete every installed agent; returns the names that were installed."""
    from jarvis import services

    removed = []
    for agent in services.AGENTS:
        module = importlib.import_module(agent.module)
        try:
            if module.uninstall():
                removed.append(agent.name)
        except Exception as exc:  # one agent must not stop the others
            console.print(f"[red]{agent.name}: {type(exc).__name__}: {exc}[/]")
    return removed


def stored_secrets() -> list[str]:
    from jarvis.setup_wizard import SECRET_NAMES

    return [name for name in SECRET_NAMES if secrets.get_secret(name)]


def remove_secrets() -> list[str]:
    removed = stored_secrets()
    for name in removed:
        secrets.delete_secret(name)
    return removed


def local_data_targets(paths: Any) -> list:
    return [p for p in (paths.config_file, paths.local_config_file, paths.data_dir, paths.logs_dir) if p.exists()]


def remove_local_data(paths: Any) -> list:
    removed = []
    for target in local_data_targets(paths):
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(target)
    return removed


def run_uninstall(paths: Any, console: Console, confirm: Callable[[str], bool]) -> int:
    console.print(f"[bold]{t('uninstall.title')}[/]")
    agents = remove_agents(console)
    console.print(t("uninstall.agents_removed", names=", ".join(agents)) if agents else t("uninstall.no_agents"))

    present = stored_secrets()
    if present:
        console.print(t("uninstall.secrets_found", names=", ".join(present)))
        if confirm(t("uninstall.secrets_ask")):
            console.print(t("uninstall.secrets_removed", names=", ".join(remove_secrets())))
        else:
            console.print(t("uninstall.kept"))

    targets = local_data_targets(paths)
    if targets:
        shown = ", ".join(str(p.relative_to(paths.home)) if p.is_relative_to(paths.home) else str(p) for p in targets)
        console.print(t("uninstall.data_found", paths=shown))
        if confirm(t("uninstall.data_ask")):
            remove_local_data(paths)
            console.print(t("uninstall.data_removed"))
        else:
            console.print(t("uninstall.kept"))

    console.print(f"\n{t('uninstall.finish', home=paths.home)}")
    return 0
