"""The announcement request file: atomic write, one-shot take, stale and malformed
files dropped, and the voice service picking it up only while idle and not paused."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.voice import announce
from jarvis.voice import pause as vpause
from jarvis.voice.service import VoiceState
from tests.unit.test_voice_service import parts, run_in_thread, wait_until  # noqa: F401 - the fixture


def test_request_and_take_round_trip(paths):
    path = paths.voice_announce_file
    announce.request(path, kind="say", text="  Hoş geldin   Kerem. ", source="presence")
    assert announce.pending(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["kind"] == "say" and raw["text"] == "Hoş geldin Kerem." and raw["source"] == "presence"
    ann = announce.take(path)
    assert ann is not None and ann.kind == "say" and ann.text == "Hoş geldin Kerem." and ann.age_s() < 5
    assert not path.exists() and announce.take(path) is None  # one shot


def test_newer_request_replaces_the_pending_one(paths):
    path = paths.voice_announce_file
    announce.request(path, kind="say", text="first", source="a")
    announce.request(path, kind="turn", text="second", source="b")
    ann = announce.take(path)
    assert ann.kind == "turn" and ann.text == "second" and ann.source == "b"


def test_request_rejects_bad_input(paths):
    with pytest.raises(ValueError):
        announce.request(paths.voice_announce_file, kind="shout", text="x", source="t")
    with pytest.raises(ValueError):
        announce.request(paths.voice_announce_file, kind="say", text="   ", source="t")
    announce.request(paths.voice_announce_file, kind="say", text="y" * 5000, source="t")
    assert len(announce.take(paths.voice_announce_file).text) == announce.MAX_TEXT_CHARS


def test_stale_and_malformed_files_are_dropped(paths):
    path = paths.voice_announce_file
    old = (datetime.now(timezone.utc) - timedelta(seconds=announce.MAX_AGE_S + 60)).isoformat()
    path.write_text(json.dumps({"kind": "say", "text": "late greeting", "source": "presence", "created_at": old}))
    assert announce.take(path) is None and not path.exists()
    path.write_text("{not json")
    assert announce.take(path) is None and not path.exists()
    path.write_text(json.dumps({"kind": "say"}))
    assert announce.take(path) is None and not path.exists()
    path.write_text(json.dumps(["list"]))
    assert announce.take(path) is None


# --- the voice service -------------------------------------------------------------------------------------


def test_service_speaks_a_say_announcement_while_idle(parts):
    p = parts
    run_in_thread(p.service)
    announce.request(p.paths.voice_announce_file, kind="say", text="Hoş geldin.", source="presence")
    assert wait_until(lambda: "Hoş geldin." in p.tts.spoken)
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "announcement done" for t in p.service.transitions))
    assert not p.paths.voice_announce_file.exists()
    assert p.assistant.handled == []  # no model call for a plain sentence
    assert any("announcement from presence (say)" in line for line in p.lines)
    p.service.request_stop()


def test_service_runs_a_turn_announcement_through_the_assistant(parts):
    p = parts
    run_in_thread(p.service)
    announce.request(p.paths.voice_announce_file, kind="turn", text="System event: the user just arrived home.", source="presence")
    assert wait_until(lambda: p.assistant.handled == ["System event: the user just arrived home."])
    assert wait_until(lambda: p.tts.spoken == ["Yarın saat onda toplantın var."])
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "turn done" for t in p.service.transitions))
    states = [t[1] for t in p.service.transitions]
    assert VoiceState.LISTENING not in states and states[:2] == [VoiceState.THINKING, VoiceState.SPEAKING]
    p.service.request_stop()


def test_announcement_waits_while_paused_and_is_dropped_when_stale(parts):
    p = parts
    vpause.pause(p.paths.voice_pause_file, by="cli")
    run_in_thread(p.service)
    assert p.service.state == VoiceState.PAUSED
    announce.request(p.paths.voice_announce_file, kind="say", text="Hoş geldin.", source="presence")
    time.sleep(0.6)
    assert p.paths.voice_announce_file.exists() and p.tts.spoken == []  # a paused Jarvis stays silent
    vpause.resume(p.paths.voice_pause_file)
    assert wait_until(lambda: "Hoş geldin." in p.tts.spoken, timeout=5)
    p.service.request_stop()
