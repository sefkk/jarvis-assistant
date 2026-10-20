"""Global push-to-talk hotkey with pynput: hold to talk, release to stop.

`HotkeyListener` keeps the set of combo keys currently down and fires `on_press` once when the
whole combination is held and `on_release` once when any of its keys goes up. The pynput
listener thread only canonicalises keys and calls `handle_press` / `handle_release`, which are
plain methods tests drive directly. Keyboard monitoring needs the Input Monitoring permission
(`input_monitoring_granted`), which under launchd belongs to the Python executable.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any, Callable

log = logging.getLogger("jarvis.voice.hotkey")


def pynput_available() -> bool:
    return importlib.util.find_spec("pynput") is not None


def _voice_unavailable(detail: str) -> Exception:
    from jarvis.io.voice import VOICE_UNAVAILABLE, VoiceUnavailable  # noqa: PLC0415

    return VoiceUnavailable(f"{VOICE_UNAVAILABLE} ({detail})")


def parse_hotkey(combo: str) -> set:
    """`"<alt>+<space>"` -> set of canonical pynput keys. ValueError for a bad string."""
    try:
        from pynput import keyboard  # noqa: PLC0415
    except ImportError:
        raise _voice_unavailable("pynput is not installed") from None
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError("hotkey must be a non-empty string such as '<alt>+<space>'")
    try:
        keys = keyboard.HotKey.parse(combo.strip())
    except ValueError as exc:
        raise ValueError(f"invalid hotkey {combo!r}: use pynput syntax such as '<alt>+<space>' or '<cmd>+<shift>+j'") from exc
    return set(keys)


class HotkeyListener:
    def __init__(
        self,
        combo: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        *,
        listener_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.combo = combo
        self.on_press = on_press
        self.on_release = on_release
        self._listener_factory = listener_factory
        self._listener: Any = None
        self._keys: set = parse_hotkey(combo) if pynput_available() else set()
        self._down: set = set()
        self._held = False
        self._lock = threading.Lock()

    # --- testable core -------------------------------------------------------------------------

    @property
    def is_held(self) -> bool:
        return self._held

    def handle_press(self, key) -> None:
        """A canonical key went down. Fires on_press once when the whole combo is held."""
        if key not in self._keys:
            return
        fire = False
        with self._lock:
            self._down.add(key)
            if not self._held and self._down >= self._keys:
                self._held = True
                fire = True
        if fire:
            log.debug("hotkey %s pressed", self.combo)
            self.on_press()

    def handle_release(self, key) -> None:
        """A canonical key went up. Fires on_release once when the combo stops being held."""
        if key not in self._keys:
            return
        fire = False
        with self._lock:
            self._down.discard(key)
            if self._held:
                self._held = False
                fire = True
        if fire:
            log.debug("hotkey %s released", self.combo)
            self.on_release()

    # --- pynput wiring ---------------------------------------------------------------------------

    def _on_press(self, key) -> None:
        try:
            self.handle_press(self._listener.canonical(key))
        except Exception:  # the listener thread must never die
            log.exception("hotkey press handler failed")

    def _on_release(self, key) -> None:
        try:
            self.handle_release(self._listener.canonical(key))
        except Exception:
            log.exception("hotkey release handler failed")

    def start(self) -> None:
        if self._listener is not None:
            return
        if not self._keys:
            self._keys = parse_hotkey(self.combo)  # raises VoiceUnavailable when pynput is missing
        if self._listener_factory is not None:
            self._listener = self._listener_factory(on_press=self._on_press, on_release=self._on_release)
        else:
            try:
                from pynput import keyboard  # noqa: PLC0415
            except ImportError:
                raise _voice_unavailable("pynput is not installed") from None
            self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()
        log.info("hotkey listener started (%s)", self.combo)

    def stop(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception as exc:
            log.debug("hotkey listener stop failed: %s", exc)
        self._listener = None
        with self._lock:
            self._down.clear()
            self._held = False
        log.info("hotkey listener stopped")


# --- permission --------------------------------------------------------------------------------


def input_monitoring_granted() -> bool | None:
    """Input Monitoring for this process (Quartz preflight). None when Quartz is unavailable."""
    try:
        import Quartz  # noqa: PLC0415

        return bool(Quartz.CGPreflightListenEventAccess())
    except Exception as exc:
        log.debug("input monitoring preflight failed: %s", exc)
        return None


def request_input_monitoring() -> bool:
    """Ask macOS to prompt for Input Monitoring. False when it cannot be requested."""
    try:
        import Quartz  # noqa: PLC0415

        return bool(Quartz.CGRequestListenEventAccess())
    except Exception as exc:
        log.debug("input monitoring request failed: %s", exc)
        return False
