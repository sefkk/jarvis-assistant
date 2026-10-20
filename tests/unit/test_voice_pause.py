"""The pause file under JARVIS_HOME, durations, the CLI commands, the `pause_listening` tool, Telegram
/mute and /unmute, and the doctor check (the service side lives in test_voice_service.py)."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from jarvis.main import build_parser, cmd_voice
from jarvis.voice import pause

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)


# --- state file ---------------------------------------------------------------------------------------------


def test_state_lives_under_jarvis_home_and_survives_restarts(paths):
    f = paths.voice_pause_file
    assert f.parent == paths.data_dir and paths.data_dir.is_relative_to(paths.home)
    assert not pause.is_paused(f) and pause.describe(pause.load_state(f)) == "listening"
    state = pause.pause(f, by="cli", now=NOW)
    assert state.paused and state.by == "cli" and state.until is None
    again = pause.load_state(f, now=NOW + timedelta(days=3))  # a reboot later: still paused, no expiry
    assert again.paused and again.paused_at == state.paused_at and not again.expired
    assert pause.resume(f) is True and not f.exists()
    assert pause.resume(f) is False


def test_timed_pause_expires(paths):
    f = paths.voice_pause_file
    state = pause.pause(f, by="telegram", minutes=60, now=NOW)
    assert state.remaining_minutes(NOW) == 60 and state.remaining_minutes(NOW + timedelta(minutes=45)) == 15
    assert pause.load_state(f, now=NOW + timedelta(minutes=59)).paused
    late = pause.load_state(f, now=NOW + timedelta(minutes=60))
    assert not late.paused and late.expired and f.is_file()  # the service deletes it when it resumes
    assert "pause time is over" in pause.describe(late)


def test_repause_keeps_paused_at_but_takes_the_new_duration_and_source(paths):
    f = paths.voice_pause_file
    first = pause.pause(f, by="cli", now=NOW)
    second = pause.pause(f, by="voice", minutes=30, now=NOW + timedelta(minutes=5))
    assert second.paused_at == first.paused_at and second.by == "voice"
    assert second.remaining_minutes(NOW + timedelta(minutes=5)) == 30
    with pytest.raises(ValueError):
        pause.pause(f, by="cli", minutes=0)
    with pytest.raises(ValueError):
        pause.pause(f, by="cli", minutes=pause.MAX_PAUSE_MINUTES + 1)


def test_unreadable_pause_file_fails_closed(paths):
    f = paths.voice_pause_file
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("not json")
    assert pause.is_paused(f)
    assert pause.resume(f) and not pause.is_paused(f)


def test_describe_shows_until_and_why(config, paths):
    f = paths.voice_pause_file
    pause.pause(f, by="telegram", minutes=90, now=NOW)
    text = pause.describe(pause.load_state(f, now=NOW), now=NOW)
    assert "paused since" in text and "until" in text and "90 dk left" in text and "(by /mute)" in text
    pause.pause(f, by="voice")
    assert "until resumed (by spoken phrase)" in pause.describe(pause.load_state(f))


# --- durations and phrases -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, minutes",
    [
        ("60m", 60),
        ("1h", 60),
        ("1h30m", 90),
        ("90", 90),
        ("1 saat", 60),
        ("30 dk", 30),
        ("1,5 saat", 90),
        ("2 hours", 120),
        ("45 min", 45),
        ("10 dakika", 10),
        ("", None),
        ("soon", None),
        ("0", None),
        ("1h x", None),
    ],
)
def test_parse_duration(text, minutes):
    assert pause.parse_duration(text) == minutes


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("Sesi kapat.", (True, None)),
        ("1 saat sesi kapat", (True, 60)),
        ("sesi kapat 30 dakika", (True, 30)),
        ("Dinlemeyi durdur", (True, None)),
        ("stop listening for 2 hours", (False, None)),  # "for" is not in the phrase: the model + tool handle it
        ("Stop listening", (True, None)),
        ("sesi biraz kapat", (False, None)),
        ("yarın ne var", (False, None)),
    ],
)
def test_match_pause_phrase(heard, expected):
    phrases = ["sesi kapat", "dinlemeyi durdur", "stop listening"]
    assert pause.match_pause_phrase(heard, phrases) == expected


def test_labels_and_notifications(config):
    assert pause.duration_label(60) == "60 dk" and pause.duration_label(90) == "90 dk" and pause.duration_label(150) == "2 sa 30 dk"
    assert pause.spoken_label_tr(90) == "1 saat 30 dakika" and pause.spoken_pause_ack("tr", 60) == "Tamam, 1 saat dinlemiyorum."
    assert pause.spoken_pause_ack("tr", None) == "Tamam, dinlemeyi durduruyorum."
    assert pause.spoken_pause_ack("en", 90) == "Okay, not listening for 1 hour 30 minutes."
    state = pause.PauseState(paused=True, until=pause._iso(datetime.now(timezone.utc) + timedelta(minutes=60)))
    assert pause.paused_notification(state) == "🔇 Jarvis dinlemiyor (60 dk)"
    assert pause.paused_notification(pause.PauseState(paused=True)) == "🔇 Jarvis dinlemiyor"
    assert pause.resumed_notification() == "🎙️ Jarvis tekrar dinliyor"
    assert pause.resume_options() == [("no", "Hayır"), ("yes", "Evet")]


def test_labels_and_notifications_in_english():
    assert pause.duration_label(60, "en") == "60 min" and pause.duration_label(150, "en") == "2 h 30 min"
    assert pause.duration_label(120, "en") == "2 h" and pause.duration_label(120, "tr") == "2 sa"
    assert pause.spoken_label(61, "en") == "1 hour 1 minute" and pause.spoken_pause_ack("en", None) == "Okay, I stopped listening."
    state = pause.PauseState(paused=True, until=pause._iso(datetime.now(timezone.utc) + timedelta(minutes=60)))
    assert pause.paused_notification(state, "en") == "🔇 Jarvis is not listening (60 min)"
    assert pause.resumed_notification("en") == "🎙️ Jarvis is listening again"
    assert pause.resume_question("en") == "Listening is paused. Should I start listening again?"
    assert pause.resume_options("en") == [("no", "No"), ("yes", "Yes")]


# --- CLI ------------------------------------------------------------------------------------------------------


def _run(cmd, config, paths, monkeypatch, *, running=True, pause_for=None):
    from types import SimpleNamespace

    from jarvis.voice import launchd

    monkeypatch.setattr(launchd, "status", lambda: SimpleNamespace(installed=True, running=running, pid=1 if running else None, detail="x"))
    console = Console(record=True, width=250)
    code = cmd_voice(argparse.Namespace(voice_command=cmd, foreground=False, seconds=1.0, pause_for=pause_for), config, paths, console)
    return code, console.export_text()


def test_cli_parser_accepts_pause_resume_and_for():
    parser = build_parser()
    assert parser.parse_args(["voice", "pause"]).pause_for is None
    assert parser.parse_args(["voice", "pause", "--for", "60m"]).pause_for == "60m"
    assert parser.parse_args(["voice", "resume"]).voice_command == "resume"


def test_cli_pause_for_resume_status(config, paths, monkeypatch):
    from jarvis.i18n import t

    code, text = _run("pause", config, paths, monkeypatch, running=False, pause_for="60m")
    assert code == 0 and "60 dk" in text and t("cli.voice.not_running") in text  # the test config is Turkish
    state = pause.load_state(paths.voice_pause_file)
    assert state.by == "cli" and 59 <= state.remaining_minutes() <= 60
    code, text = _run("status", config, paths, monkeypatch)
    assert (
        code == 0 and t("cli.voice.listening", state="paused since") in text and "auto-resume" in text and "(by jarvis voice pause)" in text
    )
    code, text = _run("pause", config, paths, monkeypatch, pause_for="soon")
    assert code == 1 and t("cli.voice.bad_duration", value="'soon'") in text
    code, text = _run("resume", config, paths, monkeypatch)
    assert code == 0 and t("cli.voice.resumed") in text and not paths.voice_pause_file.exists()
    code, text = _run("resume", config, paths, monkeypatch)
    assert t("cli.voice.not_paused") in text
    code, text = _run("status", config, paths, monkeypatch)
    assert t("cli.voice.listening", state="listening") in text


# --- the tool (any channel) ------------------------------------------------------------------------------------


def test_pause_listening_tool_is_safe_and_records_the_channel(config, paths):
    from jarvis.tools.base import RiskLevel, Runtime, ToolContext
    from jarvis.tools.listening import PauseListening, PauseListeningInput

    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, channel="telegram")
    tool = PauseListening(ctx)
    assert tool.risk == RiskLevel.SAFE and tool.taints is False and tool.delegate_ok is False
    result = tool.run(PauseListeningInput(minutes=45))
    assert not result.is_error and "45 min" in result.content  # model-facing: English
    state = pause.load_state(paths.voice_pause_file)
    assert state.by == "telegram" and 44 <= state.remaining_minutes() <= 45
    result = tool.run(PauseListeningInput())
    assert "until resumed" in result.content and pause.load_state(paths.voice_pause_file).until is None
    with pytest.raises(ValueError):
        PauseListeningInput(minutes=0)


def test_pause_listening_is_allowed_from_telegram(config):
    from pathlib import Path

    from jarvis.config import load_config

    real = load_config(Path(__file__).resolve().parents[2] / "config.example.toml")
    assert "listening.pause_listening" in real.telegram.remote_allowed_tools


# --- doctor ---------------------------------------------------------------------------------------------------------


def test_health_check_reports_pause_as_info(config, paths):
    from jarvis.voice import health

    config.general.use_voice = False  # keeps the heavy checks short
    names = {c.name: c for c in health.health_checks(config, paths)}
    assert names["voice listening"].ok and names["voice listening"].detail == "listening"
    pause.pause(paths.voice_pause_file, by="voice", minutes=20)
    names = {c.name: c for c in health.health_checks(config, paths)}
    assert names["voice listening"].ok and "paused since" in names["voice listening"].detail
