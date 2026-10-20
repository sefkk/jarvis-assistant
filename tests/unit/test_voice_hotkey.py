"""Push-to-talk hotkey: edge-triggered press/release over canonical pynput keys, no real listener."""

from __future__ import annotations

import sys

import pytest
from pynput import keyboard

from jarvis.io.voice import VoiceUnavailable
from jarvis.voice import hotkey
from jarvis.voice.hotkey import HotkeyListener, parse_hotkey

ALT = keyboard.Key.alt
SPACE = keyboard.KeyCode.from_vk(49)


class FakeListener:
    instances: list["FakeListener"] = []

    def __init__(self, on_press, on_release) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def canonical(self, key):
        if key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
            return keyboard.Key.alt
        if key == keyboard.Key.space:
            return SPACE
        return key


@pytest.fixture
def events():
    return []


@pytest.fixture
def listener(events):
    FakeListener.instances.clear()
    return HotkeyListener(
        "<alt>+<space>",
        on_press=lambda: events.append("press"),
        on_release=lambda: events.append("release"),
        listener_factory=FakeListener,
    )


def test_parse_hotkey():
    assert parse_hotkey("<alt>+<space>") == {ALT, SPACE}
    assert parse_hotkey("<cmd>+<shift>+j") == {keyboard.Key.cmd, keyboard.Key.shift, keyboard.KeyCode.from_char("j")}
    for bad in ("", "   ", "<nope>+x", "alt+", "<alt>+<alt>"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)


def test_press_fires_once_when_the_whole_combo_is_down(listener, events):
    listener.handle_press(ALT)
    assert events == [] and not listener.is_held
    listener.handle_press(SPACE)
    assert events == ["press"] and listener.is_held
    listener.handle_press(SPACE)  # key repeat while held
    listener.handle_press(ALT)
    assert events == ["press"]


def test_release_fires_once_when_any_combo_key_goes_up(listener, events):
    listener.handle_press(ALT)
    listener.handle_press(SPACE)
    listener.handle_release(SPACE)
    assert events == ["press", "release"] and not listener.is_held
    listener.handle_release(ALT)
    assert events == ["press", "release"]
    # hold again, release the modifier first this time
    listener.handle_press(SPACE)
    listener.handle_press(ALT)
    assert events[-1] == "press" and listener.is_held
    listener.handle_release(ALT)
    assert events[-1] == "release" and not listener.is_held
    listener.handle_press(ALT)  # space is still down, so the combo is complete again
    assert events[-1] == "press"


def test_unrelated_keys_are_ignored(listener, events):
    listener.handle_press(keyboard.KeyCode.from_char("x"))
    listener.handle_release(keyboard.KeyCode.from_char("x"))
    listener.handle_release(SPACE)  # release without a prior press
    assert events == [] and not listener.is_held


def test_start_wires_pynput_callbacks_through_canonical(listener, events):
    listener.start()
    fake = FakeListener.instances[0]
    assert fake.started
    fake.on_press(keyboard.Key.alt_l)
    fake.on_press(keyboard.Key.space)
    assert events == ["press"]
    fake.on_release(keyboard.Key.alt_r)
    assert events == ["press", "release"]
    listener.start()  # idempotent
    assert len(FakeListener.instances) == 1
    listener.stop()
    assert fake.stopped and not listener.is_held
    listener.stop()  # idempotent


def test_callback_exceptions_do_not_escape(listener):
    listener.on_press = lambda: 1 / 0
    listener.start()
    fake = FakeListener.instances[0]
    fake.on_press(ALT)
    fake.on_press(SPACE)  # on_press raises; must be swallowed and logged
    assert listener.is_held


def test_bad_combo_raises_at_construction():
    with pytest.raises(ValueError):
        HotkeyListener("<bogus>", lambda: None, lambda: None, listener_factory=FakeListener)


def test_without_pynput(monkeypatch):
    monkeypatch.setitem(sys.modules, "pynput", None)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", None)
    monkeypatch.setattr(hotkey, "pynput_available", lambda: False)
    lst = HotkeyListener("<alt>+<space>", lambda: None, lambda: None)  # parsing is skipped
    with pytest.raises(VoiceUnavailable):
        lst.start()
    with pytest.raises(VoiceUnavailable):
        parse_hotkey("<alt>+<space>")


def test_permission_helpers_never_raise(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert hotkey.input_monitoring_granted() is None
    assert hotkey.request_input_monitoring() is False
