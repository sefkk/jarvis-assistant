"""Secrets live in the macOS Keychain via `keyring`; .env is a development fallback."""

from __future__ import annotations

import logging
import os

KEYCHAIN_SERVICE = "jarvis"
ANTHROPIC_API_KEY = "anthropic_api_key"
ELEVENLABS_API_KEY = "elevenlabs_api_key"  # `jarvis voice set-elevenlabs-key`
GOOGLE_MAPS_API_KEY = "google_maps_api_key"  # `jarvis key set google_maps_api_key`

# Keychain name -> environment variable accepted as a fallback.
ENV_FALLBACKS = {ANTHROPIC_API_KEY: "ANTHROPIC_API_KEY"}

log = logging.getLogger("jarvis.secrets")


def get_secret(name: str) -> str | None:
    """Keychain first, then the environment (which `load_dotenv` may have filled from .env)."""
    try:
        import keyring

        value = keyring.get_password(KEYCHAIN_SERVICE, name)
        if value:
            return value
    except Exception as exc:  # keyring backend problems must never crash Jarvis
        log.warning("keychain lookup failed for %s: %s", name, type(exc).__name__)
    env_name = ENV_FALLBACKS.get(name)
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYCHAIN_SERVICE, name, value)


def delete_secret(name: str) -> None:
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(KEYCHAIN_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass
