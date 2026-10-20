"""Stopping a ringing alarm from the voice service (the hotkey, a spoken word without the wake word)
and from Telegram (/stop_alarm, /snooze, a typed word). The ring itself is a state file written by the
test; the requests land in the command file the scheduler's ringer would read."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from jarvis.scheduler import alarm as A
from jarvis.utils.timeutil import local_tz
from jarvis.voice.service import VoiceState
from tests.unit.test_telegram_bot import FakeAssistant, audit_lines, make_bot, message_update
from tests.unit.test_voice_service import FRAME, LOUD, parts, run_in_thread, wait_until  # noqa: F401 - fixtures

TZ = local_tz()


def write_ring_file(paths, alarm_id=1, label="Kalk"):
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.alarm_ring_file.write_text(
        json.dumps(
            {
                "alarm_id": alarm_id,
                "label": label,
                "fire_at": datetime.now(TZ).isoformat(),
                "started_at": datetime.now(TZ).isoformat(),
                "pid": os.getpid(),
                "max_ring_s": 600.0,
            }
        )
    )


def read_command(paths) -> dict | None:
    return json.loads(paths.alarm_command_file.read_text()) if paths.alarm_command_file.is_file() else None


# --- voice ------------------------------------------------------------------------------------------------------


def test_hotkey_stops_the_ringing_alarm_without_starting_a_turn(parts, logs):
    p = parts
    run_in_thread(p.service)
    write_ring_file(p.paths)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()
    command = read_command(p.paths)
    assert command is not None and command["action"] == "stop" and command["by"] == "hotkey"
    p.paths.alarm_ring_file.unlink()  # the ringer took the request down
    p.hotkey.is_held = False
    time.sleep(0.2)
    assert p.assistant.handled == [] and p.service.state in (VoiceState.IDLE, VoiceState.LISTENING)
    assert any("alarm stopped (hotkey)" in line for line in p.lines)
    assert any(a.get("decision") == "alarm_stopped" and a.get("by") == "hotkey" for a in audit_lines(logs))
    p.service.request_stop()


def test_spoken_stop_word_stops_the_alarm_without_the_wake_word(parts):
    p = parts
    p.stt.text = "tamam"
    run_in_thread(p.service)
    write_ring_file(p.paths)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)  # no wake word: the ring itself opens the microphone
    assert p.sounds == []  # no listen cue over the alarm
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: read_command(p.paths) is not None)
    command = read_command(p.paths)
    assert command["action"] == "stop" and command["by"] == "voice"
    p.paths.alarm_ring_file.unlink()
    assert wait_until(lambda: p.service.state == VoiceState.IDLE)
    assert p.assistant.handled == [] and not any("heard" in s for s in p.cues.played)
    assert any("-> stop" in line for line in p.lines)
    p.service.request_stop()


def test_spoken_snooze_word_snoozes(parts):
    p = parts
    p.stt.text = "ertele biraz"
    run_in_thread(p.service)
    write_ring_file(p.paths)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: read_command(p.paths) is not None)
    assert read_command(p.paths)["action"] == "snooze"
    p.paths.alarm_ring_file.unlink()
    assert wait_until(lambda: p.service.state == VoiceState.IDLE)
    p.service.request_stop()


def test_unrelated_speech_while_ringing_is_ignored_and_listening_goes_on(parts):
    p = parts
    p.stt.text = "yarın ne var"
    run_in_thread(p.service)
    write_ring_file(p.paths)
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)
    p.mic.push(*([LOUD] * 6), *([FRAME] * 8))
    assert wait_until(lambda: any("-> ignored" in line for line in p.lines))
    assert read_command(p.paths) is None and p.assistant.handled == []
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING)  # still ringing: another round
    p.paths.alarm_ring_file.unlink()
    assert wait_until(lambda: p.service.state == VoiceState.IDLE, timeout=8.0)
    p.service.request_stop()


def test_paused_service_does_not_listen_but_the_hotkey_still_stops(parts):
    from jarvis.voice import pause as vpause

    p = parts
    vpause.pause(p.paths.voice_pause_file, by="cli")
    run_in_thread(p.service)
    assert p.service.state == VoiceState.PAUSED
    write_ring_file(p.paths)
    time.sleep(0.3)
    assert p.service.state == VoiceState.PAUSED  # the microphone stays closed
    p.service._on_hotkey_press()
    assert read_command(p.paths)["by"] == "hotkey"
    p.paths.alarm_ring_file.unlink()
    p.service.request_stop()


# --- telegram ---------------------------------------------------------------------------------------------------


def test_help_lists_the_alarm_commands(config):
    from jarvis.interfaces.telegram_bot import help_text

    assert "/stop_alarm" in help_text(config) and "/snooze" in help_text(config)


def test_stop_alarm_command_when_nothing_rings(config, paths):
    bot, api = make_bot(config, paths)
    api.batches.append([message_update("/stop_alarm")])
    bot.run()
    assert api.sent() == [A.NOT_RINGING_TEXT]


def test_stop_alarm_command_writes_the_stop_request(config, paths, logs, monkeypatch):
    monkeypatch.setattr(A, "ACK_WAIT_S", 0.0)
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    write_ring_file(paths)
    api.batches.append([message_update("/stop_alarm")])
    bot.run()
    assert api.sent() == [A.STOPPED_TEXT]
    command = read_command(paths)
    assert command["action"] == "stop" and command["by"] == "telegram" and assistant.calls == []
    assert any(a.get("decision") == "alarm_command" for a in audit_lines(logs))


def test_snooze_command_with_and_without_minutes(config, paths, monkeypatch):
    monkeypatch.setattr(A, "ACK_WAIT_S", 0.0)
    bot, api = make_bot(config, paths)
    write_ring_file(paths)
    api.batches.extend([[message_update("/snooze")], [message_update("/snooze 15")], [message_update("/snooze x")]])
    bot.run()
    replies = api.sent()
    assert replies[0].startswith("Alarm ertelendi: 9 dakika sonra") and replies[1].startswith("Alarm ertelendi: 15 dakika sonra")
    assert replies[2].startswith("Kullanım: /snooze")
    assert read_command(paths)["minutes"] == 15


def test_typed_word_stops_the_alarm_only_while_it_rings(config, paths, monkeypatch):
    monkeypatch.setattr(A, "ACK_WAIT_S", 0.0)
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.append([message_update("tamam")])  # nothing rings: the model gets it
    bot.run()
    assert assistant.calls and api.sent() != [A.STOPPED_TEXT]
    assistant.calls.clear()
    bot2, api2 = make_bot(config, paths, assistant=assistant)
    write_ring_file(paths)
    api2.batches.extend([[message_update("Tamam!")], [message_update("ertele")]])
    bot2.run()
    assert api2.sent()[0] == A.STOPPED_TEXT and api2.sent()[1].startswith("Alarm ertelendi") and assistant.calls == []
    assert read_command(paths)["action"] == "snooze"
