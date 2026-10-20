"""The cue sounds (`voice_heard`, `voice_ready`): file lookup order and the macOS fallback, afplay
argv with the volume, non-blocking and blocking playback, stop, disabled flags, the doctor
warning. `afplay` is never started: a fake Popen records the argv."""

from __future__ import annotations

import threading
import time

import pytest

from jarvis.config import VoiceSoundsConfig
from jarvis.voice import cues as vc
from jarvis.voice.cues import CUE_EXTENSIONS, CuePlayer, find_cue_file


class FakeProc:
    """A fake afplay: `finish()` ends it; `kill()` ends it and remembers."""

    def __init__(self, argv):
        self.argv = argv
        self.killed = False
        self._done = threading.Event()

    def poll(self):
        return 0 if self._done.is_set() else None

    def kill(self):
        self.killed = True
        self._done.set()

    def finish(self):
        self._done.set()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RealClock:
    """For the tests that wait on another thread: fake time would hit `max_s` instantly."""

    def __call__(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


def make_player(tmp_path, *, settings=None, fallbacks=None, afplay="/usr/bin/afplay", clock=None):
    procs: list[FakeProc] = []

    def popen(argv, **kw):
        proc = FakeProc(argv)
        procs.append(proc)
        return proc

    fallback_dir = tmp_path / "system"
    fallback_dir.mkdir(exist_ok=True)
    if fallbacks is None:
        fallbacks = {}
        for cue, system in vc.FALLBACK_SOUNDS.items():
            path = fallback_dir / system.rsplit("/", 1)[-1]
            path.write_bytes(b"aiff")
            fallbacks[cue] = str(path)
    clock = clock or FakeClock()
    player = CuePlayer(
        tmp_path / "sounds",
        settings or VoiceSoundsConfig(),
        popen=popen,
        which=lambda name: afplay,
        clock=clock,
        sleep=clock.sleep,
        fallbacks=fallbacks,
    )
    return player, procs


# --- lookup -----------------------------------------------------------------------------------------------


def test_lookup_order_m4a_mp3_wav_aiff(tmp_path):
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    assert find_cue_file(sounds, "voice_heard") is None
    for ext in reversed(CUE_EXTENSIONS):  # add the least preferred first: each new file wins
        (sounds / f"voice_heard{ext}").write_bytes(b"x")
        assert find_cue_file(sounds, "voice_heard") == sounds / f"voice_heard{ext}"
    assert find_cue_file(sounds, "voice_heard").suffix == ".m4a"
    (sounds / "voice_heard.m4a").unlink()
    assert find_cue_file(sounds, "voice_heard").suffix == ".mp3"


def test_lookup_ignores_directories_and_missing_folder(tmp_path):
    sounds = tmp_path / "sounds"
    assert find_cue_file(sounds, "voice_ready") is None  # no folder at all
    sounds.mkdir()
    (sounds / "voice_ready.m4a").mkdir()
    assert find_cue_file(sounds, "voice_ready") is None


def test_resolve_prefers_the_custom_file_and_falls_back_to_the_system_sound(tmp_path):
    player, _ = make_player(tmp_path)
    missing = player.resolve("heard")
    assert missing.custom is False and missing.path is not None and missing.path.name == "Bottle.aiff"
    assert player.resolve("ready").path.name == "Purr.aiff"
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "voice_heard.wav").write_bytes(b"x")
    found = player.resolve("heard")
    assert found.custom is True and found.path == tmp_path / "sounds" / "voice_heard.wav"
    assert "voice_heard.wav" in player.describe("heard")
    assert "fallback" in player.describe("ready") and "voice_ready.m4a" in player.describe("ready")


def test_resolve_without_any_file_is_silent_not_an_error(tmp_path):
    player, procs = make_player(tmp_path, fallbacks={"heard": str(tmp_path / "nope.aiff"), "ready": ""})
    assert player.resolve("heard").path is None and player.resolve("ready").path is None
    assert player.play("heard") is None and player.play("ready") is None
    assert player.play_blocking("ready") is True  # nothing to wait for
    assert procs == [] and "silent" in player.describe("ready")


def test_custom_names_come_from_the_config(tmp_path):
    settings = VoiceSoundsConfig(heard="ding", ready="dong")
    player, _ = make_player(tmp_path, settings=settings)
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "dong.mp3").write_bytes(b"x")
    assert player.resolve("ready").path.name == "dong.mp3"
    assert player.resolve("heard").custom is False and player.resolve("heard").name == "ding"


@pytest.mark.parametrize("bad", ["../voice_heard", "voice heard", "voice_heard.m4a", "/tmp/x", ""])
def test_config_rejects_paths_and_extensions_in_names(bad):
    with pytest.raises(ValueError):
        VoiceSoundsConfig(heard=bad)


def test_config_volume_range():
    assert VoiceSoundsConfig(volume=0).volume == 0 and VoiceSoundsConfig(volume=2).volume == 2
    with pytest.raises(ValueError):
        VoiceSoundsConfig(volume=-0.1)
    with pytest.raises(ValueError):
        VoiceSoundsConfig(volume=2.5)


# --- playback -----------------------------------------------------------------------------------------------


def test_play_uses_afplay_argv_with_the_volume(tmp_path):
    player, procs = make_player(tmp_path, settings=VoiceSoundsConfig(volume=0.5))
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "voice_heard.m4a").write_bytes(b"x")
    handle = player.play("heard")
    assert handle is not None and handle.cue == "heard"
    assert procs[0].argv == ["/usr/bin/afplay", "-v", "0.5", str(tmp_path / "sounds" / "voice_heard.m4a")]
    assert player.playing == "heard"
    procs[0].finish()
    assert handle.done() and player.playing is None


def test_play_falls_back_to_the_system_sound_argv(tmp_path):
    player, procs = make_player(tmp_path)
    assert player.play("ready") is not None
    assert procs[0].argv[0] == "/usr/bin/afplay" and procs[0].argv[-1].endswith("Purr.aiff")


def test_play_is_non_blocking_and_stop_kills_it(tmp_path):
    player, procs = make_player(tmp_path)
    handle = player.play("heard")
    assert handle is not None and not handle.done()  # returned while "afplay" still runs
    assert player.stop("ready") is False and not procs[0].killed  # the wrong cue is left alone
    assert player.stop("heard") is True and procs[0].killed and handle.done()
    assert player.stop("heard") is False  # nothing left to stop


def test_a_new_cue_stops_the_previous_one(tmp_path):
    player, procs = make_player(tmp_path)
    player.play("heard")
    player.play("ready")
    assert procs[0].killed and not procs[1].killed and player.playing == "ready"


def test_play_blocking_returns_when_the_cue_finishes(tmp_path):
    player, procs = make_player(tmp_path, clock=RealClock())
    done = {}

    def run():
        done["result"] = player.play_blocking("ready")

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=0.2)
    assert t.is_alive() and "result" not in done  # still waiting for afplay
    procs[0].finish()
    t.join(timeout=2)
    assert done["result"] is True and not procs[0].killed


def test_play_blocking_is_cut_by_should_stop(tmp_path):
    player, procs = make_player(tmp_path)
    flag = {"stop": False}
    calls = []

    def should_stop():
        calls.append(1)
        if len(calls) >= 3:
            flag["stop"] = True
        return flag["stop"]

    assert player.play_blocking("ready", should_stop=should_stop) is False
    assert procs[0].killed


def test_play_blocking_never_waits_longer_than_max(tmp_path):
    clock = FakeClock()
    player, procs = make_player(tmp_path, clock=clock)
    assert player.play_blocking("ready", max_s=1.0) is True  # the fake afplay never ends by itself
    assert procs[0].killed and clock.now >= 1.0


def test_disabled_flags_play_nothing(tmp_path):
    player, procs = make_player(tmp_path, settings=VoiceSoundsConfig(heard_enabled=False, ready_enabled=False))
    assert player.play("heard") is None and player.play("ready") is None
    assert player.play_blocking("ready") is True
    assert procs == [] and player.describe("heard") == "heard: disabled"


def test_missing_afplay_or_failing_popen_never_raises(tmp_path):
    player, procs = make_player(tmp_path, afplay=None)
    assert player.play("heard") is None and procs == []

    def broken_popen(argv, **kw):
        raise OSError("no fork")

    player2 = CuePlayer(
        tmp_path / "sounds",
        VoiceSoundsConfig(),
        popen=broken_popen,
        which=lambda n: "/usr/bin/afplay",
        fallbacks={"heard": str(tmp_path / "f.aiff"), "ready": str(tmp_path / "f.aiff")},
    )
    (tmp_path / "f.aiff").write_bytes(b"x")
    assert player2.play("heard") is None and player2.play_blocking("ready") is True


def test_stop_from_another_thread_while_blocking(tmp_path):
    player, procs = make_player(tmp_path, clock=RealClock())
    result = {}
    t = threading.Thread(target=lambda: result.update(r=player.play_blocking("ready", max_s=5)))
    t.start()
    for _ in range(200):
        if procs:
            break
        threading.Event().wait(0.005)
    assert player.stop() is True  # a barge-in from the hotkey thread
    t.join(timeout=2)
    assert result["r"] is True and procs[0].killed


# --- the doctor ---------------------------------------------------------------------------------------------


def test_doctor_warns_about_missing_cue_files(config, paths, monkeypatch):
    from jarvis.observability.health import format_checklist
    from jarvis.voice import health as vhealth

    checks = vhealth._cue_checks(config, paths)
    by_name = {c.name: c for c in checks}
    assert set(by_name) == {"voice cue heard", "voice cue ready", "voice cue thinking"}
    for cue, c in by_name.items():
        assert c.ok and c.warn and config.voice.sounds.model_dump()[cue.split()[-1]] in c.detail
        assert str(paths.sounds_dir) in c.detail
    assert "[warn] voice cue heard" in format_checklist(checks)
    assert "thinking_sound.m4a" in by_name["voice cue thinking"].detail and "Submarine" in by_name["voice cue thinking"].detail

    paths.sounds_dir.mkdir()
    (paths.sounds_dir / "voice_heard.m4a").write_bytes(b"x")
    config.voice.sounds.ready_enabled = False
    checks = {c.name: c for c in vhealth._cue_checks(config, paths)}
    assert (
        checks["voice cue heard"].ok and not checks["voice cue heard"].warn and checks["voice cue heard"].detail.endswith("voice_heard.m4a")
    )
    assert checks["voice cue ready"].detail.startswith("disabled") and not checks["voice cue ready"].warn


# --- until_silent: the follow-up window opens when the chime is over, not when the file is --------------------


def test_play_blocking_until_silent_returns_after_the_audible_part(tmp_path):
    clock = FakeClock()
    player, procs = make_player(tmp_path, clock=clock)
    measured = []
    player.measure_audible = lambda path: measured.append(path) or 0.45
    assert player.play_blocking("ready", until_silent=True) is True
    assert 0.55 <= clock.now < 1.0  # 0.45 s of sound + the echo margin, not the whole file
    assert not procs[0].killed  # the silent rest plays on unheard
    player.play_blocking("ready", until_silent=True)
    assert len(measured) == 1  # measured once per file
    clock.now = 0.0
    player.measure_audible = None


def test_play_blocking_until_silent_without_a_measurement_waits_as_before(tmp_path):
    clock = FakeClock()
    player, procs = make_player(tmp_path, clock=clock)
    player.measure_audible = lambda path: None
    assert player.play_blocking("ready", max_s=1.0, until_silent=True) is True
    assert procs[0].killed and clock.now >= 1.0


def test_audible_seconds_measures_where_the_sound_ends(tmp_path):
    import subprocess

    import numpy as np

    rate = 16000
    chime = (np.sin(np.arange(int(0.3 * rate)) * 0.2) * 12000).astype(np.int16)
    pcm = np.concatenate([chime, np.zeros(int(1.5 * rate), np.int16)]).tobytes()
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, pcm, b"")  # noqa: E731
    assert vc.audible_seconds(tmp_path / "x.m4a", ffmpeg="/usr/bin/ffmpeg", run=run) == pytest.approx(0.3, abs=0.06)
    silent = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, bytes(3200), b"")  # noqa: E731
    assert vc.audible_seconds(tmp_path / "x.m4a", ffmpeg="/usr/bin/ffmpeg", run=silent) == 0.0
    failed = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"bad file")  # noqa: E731
    assert vc.audible_seconds(tmp_path / "x.m4a", ffmpeg="/usr/bin/ffmpeg", run=failed) is None
