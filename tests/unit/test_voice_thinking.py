"""The thinking sound (`sounds/thinking_sound.*`, `cues.ThinkingLoop`) and its text-chat counterpart,
the "Düşünüyorum…" spinner. `afplay` is never started: a fake Popen records the argv. The loop is
armed after the heard cue, starts only after `thinking_delay_ms` without a spoken reply, loops with
the optional gap, and stops for the first audio, a confirmation, a choice, an error, a barge-in, a
pause, a cancel and a dropped transcript; it never plays over Jarvis's voice."""

from __future__ import annotations

import time
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from jarvis.config import VoiceSoundsConfig
from jarvis.io.base import ConfirmRequest
from jarvis.io.text import TerminalOutput, thinking_text
from jarvis.voice import cues as vc
from jarvis.voice.cues import CUE_EXTENSIONS, CuePlayer
from jarvis.voice.service import VoiceState
from tests.unit.test_voice_cues import FakeProc, RealClock, make_player
from tests.unit.test_voice_service import (  # noqa: F401 - `parts` is a fixture
    FRAME,
    HEARD,
    LOUD,
    FakeAssistant,
    parts,
    run_in_thread,
    speak_utterance,
    wait_until,
)
from tests.unit.test_voice_sink import FakeDialog

THINKING = "thinking_sound.m4a"


def settle(seconds: float) -> None:
    time.sleep(seconds)


# --- lookup and config ------------------------------------------------------------------------------------


def test_thinking_follows_the_same_lookup_order_and_has_a_fallback(tmp_path):
    player, _ = make_player(tmp_path)
    missing = player.resolve("thinking")
    assert missing.custom is False and missing.path is not None and missing.path.name == "Submarine.aiff"
    assert "thinking_sound.m4a, thinking_sound.mp3, thinking_sound.wav, thinking_sound.aiff" in player.describe("thinking")
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    for ext in reversed(CUE_EXTENSIONS):
        (sounds / f"thinking_sound{ext}").write_bytes(b"x")
        found = player.resolve("thinking")
        assert found.custom and found.path == sounds / f"thinking_sound{ext}"
    assert player.describe("thinking") == f"thinking: {sounds / 'thinking_sound.m4a'}"
    assert vc.CUES == ("heard", "ready", "thinking") and "thinking" in vc.FALLBACK_SOUNDS


def test_thinking_config_defaults_and_validation():
    s = VoiceSoundsConfig()
    assert (s.thinking, s.thinking_enabled, s.thinking_volume, s.thinking_delay_ms, s.thinking_gap_ms) == (
        "thinking_sound",
        True,
        0.4,
        700,
        0,
    )
    assert VoiceSoundsConfig(thinking="hum", thinking_volume=0, thinking_delay_ms=0, thinking_gap_ms=2000).thinking == "hum"
    for bad in ({"thinking": "../x"}, {"thinking": "a.m4a"}, {"thinking_volume": 2.5}, {"thinking_delay_ms": -1}, {"thinking_gap_ms": -5}):
        with pytest.raises(ValueError):
            VoiceSoundsConfig(**bad)


def test_config_toml_has_the_thinking_keys(paths):
    from jarvis.config import load_config

    text = paths.config_file.read_text(encoding="utf-8")
    for key in (
        'thinking = "thinking_sound"',
        "thinking_enabled = true",
        "thinking_volume = ",
        "thinking_delay_ms = ",
        "thinking_gap_ms = ",
    ):
        assert key in text
    assert 0 < load_config(paths.config_file).voice.sounds.thinking_volume <= 1  # the user tunes the level in config.toml


# --- the loop itself ----------------------------------------------------------------------------------------


def loop_player(tmp_path, **settings):
    cfg = VoiceSoundsConfig(**{"thinking_delay_ms": 100, **settings})
    player, procs = make_player(tmp_path, settings=cfg, clock=RealClock())
    (tmp_path / "sounds").mkdir(exist_ok=True)
    (tmp_path / "sounds" / THINKING).write_bytes(b"m4a")
    (tmp_path / "sounds" / HEARD).write_bytes(b"m4a")
    return player, procs


def thinking_runs(procs):
    return [p for p in procs if p.argv[-1].endswith(THINKING)]


def test_loop_waits_for_the_delay_then_plays_at_its_own_volume(tmp_path):
    player, procs = loop_player(tmp_path, thinking_volume=0.25)
    t0 = time.monotonic()
    assert player.thinking.start() is True
    settle(0.04)
    assert thinking_runs(procs) == [] and player.thinking.armed and not player.thinking.playing  # still in the delay
    assert wait_until(lambda: thinking_runs(procs), timeout=2)
    assert time.monotonic() - t0 >= 0.09
    run = thinking_runs(procs)[0]
    assert run.argv == ["/usr/bin/afplay", "-v", "0.25", str(tmp_path / "sounds" / THINKING)]
    assert player.thinking.playing and player.thinking.loops == 1
    assert player.thinking.stop() is True and run.killed
    assert wait_until(lambda: not player.thinking.armed)


def test_loop_waits_for_the_heard_cue_to_finish_first(tmp_path):
    player, procs = loop_player(tmp_path, thinking_delay_ms=0)
    heard = player.play("heard")
    assert player.thinking.start(after=heard) is True
    settle(0.15)
    assert thinking_runs(procs) == []  # the heard cue is still playing: never over it
    procs[0].finish()
    assert wait_until(lambda: thinking_runs(procs), timeout=2)
    player.thinking.stop()


def test_loop_repeats_and_honours_the_gap(tmp_path):
    player, procs = loop_player(tmp_path, thinking_delay_ms=0, thinking_gap_ms=150)
    player.thinking.start()
    assert wait_until(lambda: len(thinking_runs(procs)) == 1, timeout=2)
    started_first = time.monotonic()
    thinking_runs(procs)[0].finish()  # the file ended: the next run comes after the gap
    settle(0.05)
    assert len(thinking_runs(procs)) == 1
    assert wait_until(lambda: len(thinking_runs(procs)) == 2, timeout=2)
    assert time.monotonic() - started_first >= 0.14 and player.thinking.loops == 2
    thinking_runs(procs)[1].finish()
    assert wait_until(lambda: len(thinking_runs(procs)) == 3, timeout=2)  # and again, until stopped
    player.thinking.stop()
    settle(0.1)
    assert len(thinking_runs(procs)) == 3  # nothing after stop
    assert all(p.killed or p.poll() is not None for p in thinking_runs(procs))


def test_stop_during_the_delay_means_the_sound_is_never_heard(tmp_path):
    player, procs = loop_player(tmp_path, thinking_delay_ms=300)
    player.thinking.start()
    settle(0.05)
    assert player.thinking.stop() is True  # the reply's first audio came in time
    settle(0.4)
    assert thinking_runs(procs) == [] and not player.thinking.armed
    assert player.thinking.stop() is False  # nothing left


def test_stop_kills_the_running_afplay_at_once_and_the_loop_can_be_armed_again(tmp_path):
    player, procs = loop_player(tmp_path, thinking_delay_ms=0)
    player.thinking.start()
    assert wait_until(lambda: thinking_runs(procs), timeout=2)
    run = thinking_runs(procs)[0]
    player.thinking.stop()
    assert run.killed  # synchronously, on the caller's thread
    assert wait_until(lambda: not player.thinking.armed)
    assert player.thinking.start() is True  # after a confirmation the service arms it again
    assert wait_until(lambda: len(thinking_runs(procs)) == 2, timeout=2)
    player.thinking.stop()


def test_start_is_a_noop_while_armed_or_looping(tmp_path):
    player, procs = loop_player(tmp_path, thinking_delay_ms=200)
    assert player.thinking.start() is True
    assert player.thinking.start() is False  # armed: one loop at a time
    assert wait_until(lambda: thinking_runs(procs), timeout=2)
    assert player.thinking.start() is False  # looping
    player.thinking.stop()
    settle(0.05)
    assert len(thinking_runs(procs)) == 1


def test_player_stop_all_stops_the_loop_but_stop_heard_does_not(tmp_path):
    player, procs = loop_player(tmp_path, thinking_delay_ms=0)
    player.thinking.start()
    assert wait_until(lambda: thinking_runs(procs), timeout=2)
    assert player.stop("heard") is False and not thinking_runs(procs)[0].killed  # the wrong cue is left alone
    assert player.stop() is True and thinking_runs(procs)[0].killed  # barge-in / cancel / pause / close
    assert player.stop("thinking") is False
    player.thinking.start()
    assert wait_until(lambda: len(thinking_runs(procs)) == 2, timeout=2)
    assert player.stop("thinking") is True and thinking_runs(procs)[1].killed


def test_disabled_or_unplayable_never_starts(tmp_path):
    player, procs = loop_player(tmp_path, thinking_enabled=False)
    assert player.thinking.start() is False and procs == [] and player.describe("thinking") == "thinking: disabled"

    (tmp_path / "other").mkdir()
    silent, procs2 = make_player(
        tmp_path / "other", settings=VoiceSoundsConfig(thinking_delay_ms=0), fallbacks={"heard": "", "ready": "", "thinking": ""}
    )
    assert silent.thinking.start() is False and procs2 == []  # no file, no fallback

    (tmp_path / "third").mkdir()
    missing_afplay, procs3 = make_player(tmp_path / "third", afplay=None)
    assert missing_afplay.thinking.start() is False and procs3 == []


def test_loop_survives_a_failing_popen(tmp_path):
    def broken_popen(argv, **kw):
        raise OSError("no fork")

    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / THINKING).write_bytes(b"x")
    player = CuePlayer(
        tmp_path / "sounds",
        VoiceSoundsConfig(thinking_delay_ms=0),
        popen=broken_popen,
        which=lambda n: "/usr/bin/afplay",
        clock=time.monotonic,
        sleep=time.sleep,
    )
    assert player.thinking.start() is True
    assert wait_until(lambda: not player.thinking.armed)  # gave up quietly
    assert not player.thinking.playing


# --- in the voice service -----------------------------------------------------------------------------------


class SlowAssistant(FakeAssistant):
    """`before(sink)` runs after `delay` (the thinking sound is playing by then), then the reply."""

    def __init__(self, *, delay=0.5, before=None, reply="Yarın saat onda toplantın var.", after_delay=0.0):
        super().__init__(reply=reply, delay=delay)
        self.before = before
        self.after_delay = after_delay

    def handle(self, text, sink):
        self.handled.append(text)
        time.sleep(self.delay)
        if self.before is not None:
            self.before(sink)
            time.sleep(self.after_delay)
        if self.reply:
            sink.write_delta(self.reply)
        sink.finish()
        return SimpleNamespace(turn_id=f"t{len(self.handled)}", reply=self.reply)


@pytest.fixture
def think(parts):
    """`parts` with the thinking sound on (150 ms delay) and a fake afplay where the one-shot cues end
    at once while a thinking run "plays" until it is killed. `spoken_while` records which cues were
    running when each sentence reached the TTS."""
    p = parts
    p.config.voice.sounds.thinking_enabled = True
    p.config.voice.sounds.thinking_delay_ms = 150
    (p.paths.sounds_dir / THINKING).write_bytes(b"m4a")
    p.started_at: dict[str, list[float]] = {}

    def popen(argv, **kw):
        proc = FakeProc(argv)
        name = argv[-1].rsplit("/", 1)[-1]
        if name != THINKING:
            proc.finish()
        p.cues.procs.append(proc)
        p.started_at.setdefault(name, []).append(time.monotonic())
        return proc

    p.service.cues._popen = popen
    p.spoken_while: list[tuple[str, list[str]]] = []
    original_speak = p.tts.speak

    def speak(text, language=None, **kw):
        # The real engines fire on_first_audio / on_sentence as the `say` process starts, i.e. at the
        # moment the audio begins; what is still running right after that is what would overlap.
        original_speak(text, language, **kw)
        if p.tts.on_sentence is not None:
            p.tts.on_sentence(text, None)
        p.spoken_while.append((text, p.cues.running()))

    p.tts.speak = speak
    p.thinking_runs = lambda: [proc for proc in p.cues.procs if proc.argv[-1].endswith(THINKING)]
    return p


def use_assistant(p, assistant):
    p.assistant = assistant
    p.service.assistant_factory = lambda c, paths: assistant


def live_sink(p):
    """The service builds its sink in run(); the dialogs must be faked before anything can ask."""
    assert wait_until(lambda: p.service._sink is not None)
    return p.service._sink


def test_thinking_sound_starts_after_heard_and_delay_and_stops_at_the_first_audio(think):
    p = think
    use_assistant(p, SlowAssistant(delay=0.8))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    run = p.thinking_runs()[0]
    assert run.argv[:3] == ["/usr/bin/afplay", "-v", str(p.config.voice.sounds.thinking_volume)] and run.argv[-1].endswith(THINKING)
    # After the heard cue (which ended at once) plus the delay, while the Assistant is still working.
    assert p.started_at[THINKING][0] - p.started_at[HEARD][0] >= 0.14
    assert p.service.state == VoiceState.THINKING and p.cues.running() == [THINKING]
    assert wait_until(lambda: p.assistant.handled and p.tts.spoken, timeout=3)
    # The first audio killed it; nothing started after the reply began speaking.
    assert wait_until(lambda: run.killed)
    assert p.spoken_while[0] == ("Yarın saat onda toplantın var.", [])
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "turn done" for t in p.service.transitions))
    assert len(p.thinking_runs()) == 1 and not p.service.cues.thinking.playing
    assert any("thinking sound armed" in line for line in p.lines) and any("thinking sound started" in line for line in p.lines)
    assert any("thinking sound stopped (first audio)" in line for line in p.lines)
    p.service.request_stop()


def test_a_fast_reply_never_hears_the_thinking_sound(think):
    p = think
    p.config.voice.sounds.thinking_delay_ms = 400
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.service.state == VoiceState.IDLE and any(t[2] == "turn done" for t in p.service.transitions))
    settle(0.5)
    assert p.thinking_runs() == [] and p.cues.played == [HEARD]
    assert any("thinking sound armed" in line for line in p.lines) and not any("thinking sound started" in line for line in p.lines)
    p.service.request_stop()


def test_thinking_sound_loops_through_a_long_turn(think):
    p = think
    use_assistant(p, SlowAssistant(delay=1.2))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    p.thinking_runs()[0].finish()  # the file ended: the loop starts it again at once (gap 0)
    assert wait_until(lambda: len(p.thinking_runs()) == 2, timeout=2)
    assert wait_until(lambda: p.tts.spoken, timeout=3)
    assert wait_until(lambda: all(r.poll() is not None for r in p.thinking_runs()))
    p.service.request_stop()


def test_confirmation_stops_the_thinking_sound_and_it_returns_afterwards(think):
    from jarvis.voice import confirm as vconfirm

    p = think
    request = ConfirmRequest(tool="send_email", args={"to": "a@b.c"}, risk="CONFIRM", tainted=True)  # tainted: dialog only
    decisions: list[bool] = []
    use_assistant(p, SlowAssistant(delay=0.5, before=lambda sink: decisions.append(sink.confirm(request)), after_delay=0.6))
    run_in_thread(p.service)
    live_sink(p)._dialog_starter = lambda text, *, timeout_s: FakeDialog(result=vconfirm.confirm_label("tr"), result_after=1)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    first = p.thinking_runs()[0]
    assert wait_until(lambda: decisions == [True], timeout=5)
    # The spoken question found no thinking sound playing; it was killed on `on_attention("confirm")`.
    question = p.spoken_while[0]
    assert "send email" in question[0] and question[1] == [] and first.killed
    assert any("thinking sound stopped (confirm)" in line for line in p.lines)
    # The turn went on working after the answer: armed again, and looping before the reply.
    assert wait_until(lambda: len(p.thinking_runs()) == 2, timeout=3)
    assert wait_until(lambda: p.tts.spoken[-1] == "Yarın saat onda toplantın var.", timeout=3)
    assert p.spoken_while[-1][1] == [] and p.thinking_runs()[1].killed  # the reply never overlaps it either
    assert any("thinking sound stopped (speech)" in line for line in p.lines)
    p.service.request_stop()


def test_choice_dialog_stops_the_thinking_sound(think):
    p = think
    answers: list[str] = []
    use_assistant(
        p, SlowAssistant(delay=0.5, before=lambda sink: answers.append(sink.ask_choice("Hangisi?", [("a", "A"), ("b", "B")], "a")))
    )
    run_in_thread(p.service)
    live_sink(p)._choice_dialog = lambda question, options, default, *, timeout_s: "b"
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    assert wait_until(lambda: answers == ["b"], timeout=5)
    assert p.spoken_while[0][1] == [] and p.thinking_runs()[0].killed
    assert any("thinking sound stopped (choice)" in line for line in p.lines)
    p.service.request_stop()


def test_error_stops_the_thinking_sound_then_the_sentence_is_spoken(think):
    p = think

    def fail(sink):
        sink.error("Hata (zaman aşımı): Anthropic API'ye bağlanılamadı.", spoken="Sunucuya bağlanamadım, biraz sonra tekrar dener misin?")

    use_assistant(p, SlowAssistant(delay=0.5, before=fail, reply=""))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    assert wait_until(lambda: p.tts.spoken, timeout=3)
    assert p.spoken_while == [("Sunucuya bağlanamadım, biraz sonra tekrar dener misin?", [])]
    assert p.thinking_runs()[0].killed and any("thinking sound stopped (error)" in line for line in p.lines)
    assert any("ℹ️ Hata (zaman aşımı)" in line for line in p.lines)
    assert wait_until(lambda: p.service.state == VoiceState.IDLE)
    assert len(p.thinking_runs()) == 1
    p.service.request_stop()


def test_barge_in_kills_the_thinking_sound_immediately(think):
    p = think
    use_assistant(p, SlowAssistant(delay=2.0))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    p.hotkey.is_held = True
    p.service._on_hotkey_press()  # the hotkey thread, while THINKING
    assert p.thinking_runs()[0].killed  # synchronously
    assert wait_until(lambda: p.service.state == VoiceState.LISTENING, timeout=5)
    p.mic.push(*([LOUD] * 5))
    settle(0.1)
    p.hotkey.is_held = False
    assert wait_until(lambda: len(p.assistant.handled) == 2, timeout=5)
    assert len(p.thinking_runs()) >= 1 and all(r.poll() is not None for r in p.thinking_runs() if r is not p.thinking_runs()[-1])
    p.service.request_stop()


def test_pause_kills_the_thinking_sound(think):
    from jarvis.voice import pause as vpause

    p = think
    use_assistant(p, SlowAssistant(delay=1.5))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    vpause.pause(p.service._pause_file, by="cli")
    p.service._sync_pause()  # what the audio thread does within a second
    assert p.thinking_runs()[0].killed and p.service.paused
    assert wait_until(lambda: p.service.state == VoiceState.PAUSED, timeout=5)
    assert len(p.thinking_runs()) == 1
    p.service.request_stop()


def test_cancel_kills_the_thinking_sound(think):
    p = think
    use_assistant(p, SlowAssistant(delay=1.5))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.thinking_runs(), timeout=3)
    p.service.cancel("test")
    assert p.thinking_runs()[0].killed
    assert wait_until(lambda: p.service.state == VoiceState.IDLE, timeout=5)
    p.service.request_stop()


def test_dropped_transcript_disarms_the_thinking_sound(think):
    p = think
    p.stt.text, p.stt.blocklist = "Altyazı M.K.", ["Altyazı M.K."]
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: any(t[2] == "empty transcript" for t in p.service.transitions))
    settle(0.4)
    assert p.thinking_runs() == [] and not p.service.cues.thinking.armed
    assert any("thinking sound armed" in line for line in p.lines)
    p.service.request_stop()


def test_disabled_flag_plays_nothing_in_the_service(think):
    p = think
    p.config.voice.sounds.thinking_enabled = False
    use_assistant(p, SlowAssistant(delay=0.6))
    run_in_thread(p.service)
    speak_utterance(p)
    assert wait_until(lambda: p.tts.spoken, timeout=3)
    assert p.thinking_runs() == [] and not any("thinking sound" in line for line in p.lines if "cue thinking" not in line)
    assert any("cue thinking: disabled" in line for line in p.lines)
    p.service.request_stop()


def test_start_reports_the_thinking_cue_like_the_others(think):
    p = think
    run_in_thread(p.service)
    assert any(line.endswith(f"cue thinking: {p.paths.sounds_dir / THINKING}") for line in p.lines)
    p.service.request_stop()


# --- the text chat: "Düşünüyorum…" spinner -------------------------------------------------------------------


class FakeStatus:
    def __init__(self, text):
        self.text = text
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def make_terminal(**kw):
    statuses: list[FakeStatus] = []

    def factory(text):
        status = FakeStatus(text)
        statuses.append(status)
        return status

    console = Console(file=StringIO(), width=120, force_terminal=False)
    return TerminalOutput(console, status_factory=factory, **kw), statuses, console


def test_spinner_shows_until_the_first_text(config):
    sink, statuses, console = make_terminal()
    assert not sink.thinking
    sink.begin_turn()
    assert sink.thinking and statuses[0].text == "Düşünüyorum…" and statuses[0].started == 1
    sink.write_delta("Merhaba")
    assert not sink.thinking and statuses[0].stopped == 1
    sink.write_delta(" Kerem.")  # later text: nothing more to stop
    sink.finish()
    assert statuses[0].stopped == 1 and "Merhaba Kerem." in console.file.getvalue()


@pytest.mark.parametrize("event", ["notice", "error", "finish", "debug", "choice"])
def test_spinner_is_removed_by_every_kind_of_output(event):
    sink, statuses, console = make_terminal(debug=True)
    sink.begin_turn()
    if event == "notice":
        sink.notice("• get_time")
    elif event == "error":
        sink.error("Hata (ağ): ...", spoken="x")
    elif event == "finish":
        sink.finish()
    elif event == "debug":
        sink.debug("thinking...")
    else:
        sink.console.input = lambda *a, **k: "a"  # the prompt reads from the console
        sink.ask_choice("Hangisi?", [("a", "A"), ("b", "B")], "a")
    assert not sink.thinking and statuses[0].stopped == 1


def test_spinner_text_follows_the_language():
    assert make_terminal(language="tr")[0].thinking_text == "Düşünüyorum…"
    assert make_terminal(language="en")[0].thinking_text == "Thinking…"
    assert make_terminal(language="de")[0].thinking_text == thinking_text()


def test_spinner_failure_never_breaks_the_chat():
    def broken(text):
        raise RuntimeError("no tty")

    sink = TerminalOutput(Console(file=StringIO(), force_terminal=False), status_factory=broken)
    sink.begin_turn()
    assert not sink.thinking
    sink.write_delta("ok")
    sink.finish()


def test_chat_loop_starts_the_spinner_before_every_turn(config, paths, monkeypatch):
    from jarvis import main as main_mod
    from jarvis.main import cmd_chat
    from jarvis.observability import health
    from tests.unit.test_global_command import ScriptedInput

    monkeypatch.setattr(health, "run_checks", lambda config, paths, **kw: [])
    monkeypatch.setattr(main_mod, "startup_health_sync", lambda config, paths: None)
    console = Console(file=StringIO(), width=200, force_terminal=False)
    statuses: list[FakeStatus] = []

    def status(text, **kw):
        s = FakeStatus(text)
        statuses.append(s)
        return s

    console.status = status  # what TerminalOutput's default factory calls
    seen: list[bool] = []

    def handle(text, sink):
        seen.append(sink.thinking)  # the spinner is up when the Assistant starts
        sink.write_delta("cevap")
        seen.append(sink.thinking)  # and gone with the first text
        sink.finish()
        return SimpleNamespace(
            turn_id="t",
            total_ms=1.0,
            timings={},
            compacted=False,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0),
        )

    fake = SimpleNamespace(session_id="s", registry=SimpleNamespace(names=lambda: []), handle=handle, close=lambda: None)
    rc = cmd_chat(config, paths, console, debug=False, source=ScriptedInput(["merhaba", "tekrar", None]), assistant_factory=lambda: fake)
    assert rc == 0 and seen == [True, False, True, False]
    assert [s.text for s in statuses] == ["Düşünüyorum…", "Düşünüyorum…"] and all(s.stopped == 1 for s in statuses)
