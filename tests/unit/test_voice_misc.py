"""Smaller voice pieces: transcript cleaning and language fallback in SpeechToText, the voice
launchd agent, the doctor checks, the stats latency section, the CLI parser, the channel note."""

from __future__ import annotations

import plistlib
import subprocess

import numpy as np
import pytest

from jarvis import context, services
from jarvis.io import voice as iov
from jarvis.main import build_parser
from jarvis.observability import reports
from jarvis.observability.health import Check
from jarvis.voice import health as vhealth
from jarvis.voice import launchd

# --- speech to text ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", ".", "a", "Altyazı M.K.", "altyazı mk", "Thanks for watching!", "thank you", "♪ ♪"])
def test_clean_transcript_drops_empty_and_hallucinations(text):
    assert iov.clean_transcript(text, ["Altyazı M.K.", "Thanks for watching!", "Thank you."]) is None


def test_clean_transcript_keeps_real_speech():
    assert iov.clean_transcript("  Yarın ne var?  ", ["Thank you."]) == "Yarın ne var?"
    assert iov.clean_transcript("Thank you for the agenda", ["Thank you."]) == "Thank you for the agenda"


class FakeWhisper:
    def __init__(self, language="tr", text=" Yarın ne var?"):
        self.language, self.text = language, text
        self.calls: list[dict] = []

    def transcribe(self, audio, **options):
        self.calls.append(options)
        lang = options.get("language") or self.language
        return {"text": self.text, "language": lang}


def test_transcribe_array_restricts_languages(monkeypatch):
    stt = iov.SpeechToText("m", languages=["tr", "en"], blocklist=["Thank you."])
    fake = FakeWhisper(language="de")
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake)
    text, language = stt.transcribe_array(np.zeros(16000, dtype=np.int16))
    assert text == "Yarın ne var?" and language == "tr"
    assert len(fake.calls) == 2 and fake.calls[1]["language"] == "tr"

    fake_en = FakeWhisper(language="en", text=" What is on tomorrow?")
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake_en)
    assert stt.transcribe_array(np.zeros(16000, dtype=np.float32)) == ("What is on tomorrow?", "en")
    assert len(fake_en.calls) == 1
    assert stt.transcribe_array(np.zeros(0, dtype=np.float32)) == ("", None)
    with pytest.raises(ValueError):
        stt.transcribe_array(np.zeros(100, dtype=np.float32), sample_rate=8000)


def test_warm_up_loads_once(monkeypatch):
    stt = iov.SpeechToText("m")
    fake = FakeWhisper()
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake)
    assert not stt.warmed_up
    stt.warm_up()
    assert stt.warmed_up and len(fake.calls) == 1


def test_transcribe_without_mlx_whisper_raises_voice_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mlx_whisper":
            raise ImportError("no mlx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(iov.VoiceUnavailable):
        iov.SpeechToText("m").transcribe(b"abc")


# --- launchd ------------------------------------------------------------------------------------------------


NOT_FOUND = (113, "", f'Could not find service "{launchd.LABEL}" in domain for user gui: 501')
IO_ERROR = (5, "", "Bootstrap failed: 5: Input/output error")


class FakeLaunchctl:
    """A launchd that remembers whether the label is loaded.

    `bootstrap_script` drives the bootstrap attempts in order: a result tuple is returned as a
    failure, "ghost" returns 0 without loading (the plist wiped with the previous instance), and
    once the script is used up every bootstrap succeeds. `teardown_polls` is how many `print`
    calls after a `bootout` still see the old instance; `load_lies` makes `load -w` return 0
    without loading. `results` overrides the answer for a command outright.
    """

    def __init__(self, results=None, *, bootstrap_script=(), teardown_polls=0, load_lies=False):
        self.calls = []
        self.results = results or {}
        self.loaded = False
        self.bootstrap_script = list(bootstrap_script)
        self.teardown_polls = teardown_polls
        self.load_lies = load_lies
        self._teardown_left = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        cmd = argv[0]
        if cmd in self.results:
            code, out, err = self.results[cmd]
        elif cmd == "bootout":
            code, out, err = (0, "", "") if self.loaded else (3, "", "not loaded")
            self._teardown_left = self.teardown_polls if self.loaded else 0
            self.loaded = False
        elif cmd == "bootstrap":
            step = self.bootstrap_script.pop(0) if self.bootstrap_script else "ok"
            if isinstance(step, tuple):
                code, out, err = step
            else:
                code, out, err = 0, "", ""
                self.loaded = step != "ghost"
        elif cmd == "load":
            code, out, err = 0, "", ""
            self.loaded = not self.load_lies
        elif cmd == "print":
            if self._teardown_left > 0:
                self._teardown_left -= 1
                code, out, err = 0, "state = running\npid = 1\n", ""
            else:
                code, out, err = (0, "state = running\npid = 77\n", "") if self.loaded else NOT_FOUND
        else:
            code, out, err = 0, "", ""
        return subprocess.CompletedProcess(["launchctl", *argv], code, out, err)


@pytest.fixture
def agent(tmp_path, monkeypatch):
    target = tmp_path / "LaunchAgents" / f"{launchd.LABEL}.plist"
    exe = tmp_path / "venv" / "bin" / "jarvis"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchd, "plist_path", lambda: target)
    monkeypatch.setattr(launchd, "jarvis_executable", lambda: exe)
    monkeypatch.setattr(launchd, "_uid", lambda: 501)
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    monkeypatch.setattr(launchd, "_sleep", lambda s: None)
    return target, exe, fake


def test_voice_plist_and_install(paths, agent):
    target, exe, fake = agent
    data = launchd.build_plist(paths)
    assert data["Label"] == launchd.LABEL == f"{services.LABEL_PREFIX}.voice" and data["ProgramArguments"] == [str(exe), "voice", "run"]
    assert data["ProcessType"] == "Interactive" and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"]["JARVIS_HOME"] == str(paths.home.resolve())
    assert launchd.install(paths) == target
    assert plistlib.loads(target.read_bytes())["Label"] == launchd.LABEL
    assert [c[0] for c in fake.calls] == ["bootout", "print", "bootstrap", "print"] and fake.calls[2][1] == "gui/501"
    assert fake.loaded
    assert launchd.uninstall() is True and not target.exists()
    assert not fake.loaded


def test_voice_status_never_raises(agent, monkeypatch):
    target, _, fake = agent
    assert launchd.status().installed is False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    fake.results["print"] = (0, "state = running\npid = 77\n", "")
    status = launchd.status()
    assert status.running and status.pid == 77
    monkeypatch.setattr(launchd, "_run_launchctl", lambda argv: (_ for _ in ()).throw(OSError("boom")))
    assert "status unknown" in launchd.status().detail


# --- doctor -------------------------------------------------------------------------------------------------


def names_ok(checks: list[Check]) -> dict[str, bool]:
    return {c.name: c.ok for c in checks}


@pytest.fixture(autouse=True)
def _no_elevenlabs_account(monkeypatch):
    """The ElevenLabs doctor lines must never touch the real Keychain or the network in tests."""
    from jarvis.voice import elevenlabs

    monkeypatch.setattr(elevenlabs, "get_api_key", lambda: None)
    monkeypatch.setattr(elevenlabs, "fetch_subscription", lambda key, **kw: None)


def test_voice_health_checks_disabled_is_informational(config, paths, monkeypatch):
    config.general.use_voice = False
    monkeypatch.setattr(vhealth, "_agent_check", lambda enabled: [Check("voice agent", not enabled, "not installed")])
    checks = vhealth.health_checks(config, paths)
    result = names_ok(checks)
    assert result["voice enabled"] and result["voice agent"]
    assert any(c.name == "voice Turkish voice" for c in checks)


def test_voice_health_checks_report_missing_packages(config, paths, monkeypatch):
    from jarvis.voice.service import VoiceService

    config.general.use_voice = True
    monkeypatch.setattr(VoiceService, "missing_dependencies", staticmethod(lambda: ["openwakeword"]))
    monkeypatch.setattr(vhealth, "_agent_check", lambda enabled: [])
    checks = vhealth.health_checks(config, paths)
    pkg = next(c for c in checks if c.name == "voice packages")
    assert not pkg.ok and "'.[voice]'" in pkg.detail
    assert not any(c.name == "voice microphone" for c in checks)  # stops early


def test_voice_health_voice_quality_warning(config, paths, monkeypatch):
    from jarvis.voice import tts

    config.general.use_voice = True
    monkeypatch.setattr(tts, "list_voices", lambda run=None: {"Yelda": "tr_TR", "Samantha": "en_US"})
    monkeypatch.setattr(vhealth, "_agent_check", lambda enabled: [])
    checks = {c.name: c for c in vhealth._voice_checks(config.voice, True)}
    assert checks["voice Turkish voice"].ok and "Enhanced" in checks["voice Turkish voice"].detail
    config.voice.tts_voice_tr = "Nobody"
    checks = {c.name: c for c in vhealth._voice_checks(config.voice, True)}
    assert not checks["voice Turkish voice"].ok


def test_doctor_runs_voice_module(config, paths):
    from jarvis.observability import health

    checks = health.integration_checks(config, paths)
    assert any(c.name.startswith("voice ") for c in checks)


# --- stats --------------------------------------------------------------------------------------------------


def test_voice_latency_report(db):
    db.start_session("s1", "m", "d")
    for i, (first, total) in enumerate([(900, 2500), (1100, 3000), (700, 2000)]):
        tid = f"t{i}"
        db.start_turn(tid, "s1", "hi")
        db.add_timing(tid, "stt", 300 + i)
        db.add_timing(tid, "ttft", 500)
        db.add_timing(tid, "first_audio", first)
        db.add_timing(tid, "tts_total", total)
        db.finish_turn(tid, final_reply="ok", tainted=False, total_ms=1000, error=None)
    db.start_turn("text", "s1", "typed")  # a text turn: no first_audio, excluded
    db.add_timing("text", "ttft", 9999)
    db.finish_turn("text", final_reply="ok", tainted=False, total_ms=1, error=None)
    text = reports.voice_latency_report(db, 7)
    assert "3 spoken turn(s)" in text and "first_audio" in text and "9999" not in text
    row = next(line for line in text.splitlines() if line.startswith("first_audio"))
    assert "900" in row and "1100" in row  # p50 and p90 (nearest rank over three values)
    assert "Voice latency" in reports.stats_report(db, 7)
    assert reports.voice_latency_report(db, 0) == "" or "spoken" in reports.voice_latency_report(db, 0)


def test_follow_up_report_counts_outcomes(db):
    import pytest

    assert reports.follow_up_report(db, 7) == "" and "Voice follow-ups" not in reports.stats_report(db, 7)
    db.start_session("s1", "m", "d")
    db.start_turn("t1", "s1", "hi")
    db.add_follow_up("turn", window_s=6.0, turn_id="t1", speech_after_ms=1200)
    db.add_follow_up("silence", window_s=6.0)
    db.add_follow_up("noise", window_s=6.0, speech_after_ms=800)
    db.add_follow_up("pause", window_s=6.0, speech_after_ms=400)
    db.add_follow_up("cancelled", window_s=6.0)
    with pytest.raises(ValueError):
        db.add_follow_up("bogus", window_s=6.0)
    assert db.follow_up_counts(reports._since(7)) == {"turn": 1, "silence": 1, "noise": 1, "pause": 1, "cancelled": 1}
    text = reports.follow_up_report(db, 7)
    assert text.startswith("Voice follow-ups: 5 window(s) opened, 2 used (40%), 1 silent, 1 noise (20%), 1 cancelled")
    assert "speech started 0.8 s after the window opened on average" in text
    assert "Voice follow-ups" in reports.stats_report(db, 7)
    row = db.conn.execute("SELECT turn_id FROM voice_follow_ups WHERE outcome = 'turn'").fetchone()
    assert row["turn_id"] == "t1"  # the flag that marks a follow-up turn


def test_percentile_nearest_rank():
    assert reports.percentile([], 0.5) == 0.0
    assert reports.percentile([5.0], 0.9) == 5.0
    assert reports.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 3.0  # round(0.5 * 3) = 2 -> 3.0
    assert reports.percentile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], 0.9) == 9.0


# --- CLI and prompt -----------------------------------------------------------------------------------------


def test_voice_cli_parser():
    parser = build_parser()
    args = parser.parse_args(["voice", "run", "--foreground"])
    assert args.command == "voice" and args.voice_command == "run" and args.foreground
    assert parser.parse_args(["voice"]).voice_command == "status"  # bare `jarvis voice` must not start a second service
    assert parser.parse_args(["voice", "calibrate", "--seconds", "10"]).seconds == 10.0
    assert parser.parse_args(["voice", "install"]).voice_command == "install"


def test_channel_note_in_system_prompt():
    for channel in ("cli", "telegram", "voice"):
        system = context.build_system("P", "R", None, "", channel=channel)
        assert f"Input channel: {channel}" in system[2]["text"]
    assert "short" in context.build_system("P", "R", None, "", channel="voice")[2]["text"]
    assert "Input channel: cli" in context.build_system("P", "R", None, "")[2]["text"]


def test_assistant_passes_channel(config, paths):
    from jarvis.assistant import Assistant
    from jarvis.tools.base import ToolContext
    from jarvis.tools.registry import ToolRegistry
    from tests.conftest import FakeClient

    registry = ToolRegistry.discover(ToolContext.build(config, paths))
    a = Assistant(config, paths, client=FakeClient([]), registry=registry, channel="voice")
    try:
        assert "Input channel: voice" in a.build_system()[2]["text"]
    finally:
        a.close()


def test_voice_input_source_wraps_callable():
    answers = iter(["merhaba", None])
    source = iov.VoiceInputSource(lambda: next(answers))
    assert source.read() == "merhaba" and source.read() is None


def test_install_waits_for_the_old_instance_to_be_torn_down_before_bootstrapping(paths, agent, monkeypatch):
    """2026-10-16 00:09: bootstrap right after bootout returned 0, then the exiting old instance took
    the label with it and `jarvis voice status` said "not loaded"."""
    target, _exe, fake = agent
    naps: list[float] = []
    monkeypatch.setattr(launchd, "_sleep", naps.append)
    fake.loaded = True
    fake.teardown_polls = 3
    assert launchd.install(paths) == target
    cmds = [c[0] for c in fake.calls]
    assert cmds == ["bootout", "print", "print", "print", "print", "bootstrap", "print"]
    assert naps == [launchd.UNLOAD_POLL_S] * 3 and fake.loaded


def test_install_retries_bootstrap_on_the_io_error_and_on_a_ghost_success(paths, agent, monkeypatch):
    target, _exe, fake = agent
    naps: list[float] = []
    monkeypatch.setattr(launchd, "_sleep", naps.append)
    fake.bootstrap_script = [IO_ERROR, "ghost"]
    assert launchd.install(paths) == target
    cmds = [c[0] for c in fake.calls]
    assert cmds == ["bootout", "print", "bootstrap", "print", "bootstrap", "print", "bootstrap", "print"]
    assert naps == [launchd.LOAD_RETRY_S] * 2 and fake.loaded and "load" not in cmds


def test_install_fails_loudly_when_load_w_returns_0_without_loading(paths, agent):
    """2026-10-15 23:58: `load -w` exited 0, the agent was never loaded, "Hey Jarvis" stayed silent."""
    target, _exe, fake = agent
    fake.bootstrap_script = [IO_ERROR] * launchd.LOAD_ATTEMPTS
    fake.load_lies = True
    with pytest.raises(launchd.VoiceInstallError, match="not loaded"):
        launchd.install(paths)
    cmds = [c[0] for c in fake.calls]
    assert cmds.count("bootstrap") == launchd.LOAD_ATTEMPTS and cmds[-2:] == ["load", "print"]


def test_install_accepts_load_w_when_the_agent_is_really_loaded(paths, agent):
    target, _exe, fake = agent
    fake.bootstrap_script = [IO_ERROR] * launchd.LOAD_ATTEMPTS
    assert launchd.install(paths) == target
    assert [c[0] for c in fake.calls][-2:] == ["load", "print"] and fake.loaded


# --- fixed STT language (voice.stt_language) ------------------------------------------------------------------


def test_fixed_language_skips_detection(monkeypatch):
    stt = iov.SpeechToText("m", languages=["tr", "en"], language="tr")
    fake = FakeWhisper(language="en", text=" Saat kaç?")  # detection would have said English
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake)
    assert stt.transcribe_array(np.zeros(16000, dtype=np.int16)) == ("Saat kaç?", "tr")
    assert len(fake.calls) == 1 and fake.calls[0]["language"] == "tr" and fake.calls[0]["path_or_hf_repo"] == "m"
    assert stt.describe() == "m, language fixed to tr"


def test_model_by_language_and_auto(monkeypatch):
    stt = iov.SpeechToText("m", language="EN", model_by_language={"en": "m-en"})
    fake = FakeWhisper()
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake)
    assert stt.model == "m-en" and stt.language == "en"
    stt.warm_up()
    assert fake.calls[0]["path_or_hf_repo"] == "m-en"
    assert stt.transcribe_array(np.zeros(16000, dtype=np.float32)) == ("Yarın ne var?", "en")
    assert fake.calls[1]["language"] == "en"
    auto = iov.SpeechToText("m", language="auto", languages=["tr", "en"], model_by_language={"en": "m-en"})
    assert auto.language is None and auto.model == "m" and "auto-detected" in auto.describe()
    fake_de = FakeWhisper(language="de")
    monkeypatch.setattr(auto, "_load_whisper", lambda: fake_de)
    assert auto.transcribe_array(np.zeros(16000, dtype=np.int16))[1] == "tr" and len(fake_de.calls) == 2


def test_stt_from_config_and_validation(config):
    stt = iov.SpeechToText.from_config(config.voice)
    assert stt.language == "tr" and stt.model == config.voice.stt_model and stt.blocklist == list(config.voice.stt_hallucination_blocklist)
    from jarvis.config import VoiceConfig

    v = VoiceConfig(stt_language="en", stt_model_by_language={"EN": " x/y "})
    assert v.stt_model_by_language == {"en": "x/y"} and iov.SpeechToText.from_config(v).model == "x/y"
    with pytest.raises(ValueError):
        VoiceConfig(stt_language="tr-TR")
    with pytest.raises(ValueError):
        VoiceConfig(stt_model_by_language={"english": "m"})
    from jarvis.interfaces.telegram_bot import _default_stt_factory

    assert _default_stt_factory(config).language == "tr"  # Telegram voice notes follow [voice] too


# --- the engine choice, the Whisper fallback, hints and corrections (voice.stt_engine) ---------------------


class FakePrimary:
    def __init__(self, text="Sezen Aksa'dan çal", fail=False, confidence=0.9):
        self.text, self.fail, self.confidence = text, fail, confidence
        self.hints: list[str] = []
        self.name = "apple"
        self.checked = 0

    def check(self):
        self.checked += 1
        if self.fail:
            raise RuntimeError("not authorized")

    def transcribe_array(self, audio):
        from jarvis.voice.stt_engines import SttResult

        if self.fail:
            raise RuntimeError("helper crashed")
        return SttResult(self.text, "tr", self.confidence, 5.0, self.name)


def test_primary_engine_with_hints_and_corrections(monkeypatch, tmp_path):
    from jarvis.voice.corrections import CorrectionStore
    from jarvis.voice.vocabulary import Vocabulary

    store = CorrectionStore(tmp_path / "corrections.md")
    (tmp_path / "corrections.md").write_text("- Sezen Aksa → Sezen Aksu\n", encoding="utf-8")
    primary = FakePrimary()
    stt = iov.SpeechToText(
        "m",
        language="tr",
        engine="apple",
        primary=primary,
        vocabulary=lambda: Vocabulary(user=["Sezen Aksu", "Kadıköy"]),
        corrections=store,
    )
    fake = FakeWhisper()
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake)
    assert stt.transcribe_array(np.zeros(16000, dtype=np.int16)) == ("Sezen Aksu'dan çal", "tr")
    assert primary.hints == ["Sezen Aksu", "Kadıköy"] and fake.calls == [] and stt.last_engine == "apple"
    assert stt.last_confidence == 0.9 and "apple" in stt.describe() and "fallback m" in stt.describe()


def test_primary_failure_falls_back_to_whisper_with_the_prompt(monkeypatch):
    from jarvis.voice.vocabulary import Vocabulary

    stt = iov.SpeechToText(
        "m", language="tr", engine="apple", primary=FakePrimary(fail=True), vocabulary=lambda: Vocabulary(user=["Sezen Aksu"])
    )
    fake = FakeWhisper(text=" Sezen Aksu çal")
    monkeypatch.setattr(stt, "_load_whisper", lambda: fake)
    assert stt.transcribe_array(np.zeros(16000, dtype=np.int16)) == ("Sezen Aksu çal", "tr")
    assert stt.last_engine == "whisper (fallback)" and stt.fallbacks == 1 and "helper crashed" in stt.last_error
    assert fake.calls[0]["initial_prompt"] == "Sezen Aksu." and fake.calls[0]["language"] == "tr"
    stt.warm_up()  # the failed check is reported, not raised
    assert stt.warmed_up and "not authorized" in stt.last_error


def test_whisper_prompt_echo_and_repetition_loops_are_dropped(monkeypatch):
    from jarvis.voice.vocabulary import Vocabulary

    stt = iov.SpeechToText("m", language="tr", vocabulary=lambda: Vocabulary(user=["Tarkan", "Gülümse", "Kadıköy"]))
    monkeypatch.setattr(stt, "_load_whisper", lambda: FakeWhisper(text=" Tarkan, Gülümse, Kadıköy."))
    assert stt.transcribe_array(np.zeros(16000, dtype=np.int16))[0] == ""
    assert iov.is_repetition_loop("Gülümse, Gülümse, Gülümse, Gülümse, Gülümse")
    assert iov.is_repetition_loop("a b " * 10)
    assert not iov.is_repetition_loop("Kuzu kuzu şarkısını aç")
    assert iov.clean_transcript("Gülümse Gülümse Gülümse Gülümse Gülümse") is None
    assert not iov.is_prompt_echo("Sezen Aksu'dan Gülümse çal", "Sezen Aksu, Gülümse.")


def test_build_primary_by_engine_and_language(paths, monkeypatch):
    from jarvis.config import VoiceConfig
    from jarvis.voice import elevenlabs, stt_engines

    assert iov.build_primary("whisper", VoiceConfig(), paths, "tr") is None
    assert iov.build_primary("apple", VoiceConfig(), paths, "auto") is None  # Apple needs a fixed language
    apple = iov.build_primary("apple", VoiceConfig(), paths, "en")
    assert isinstance(apple, stt_engines.AppleSpeech) and apple.locale == "en-US" and apple.timeout_s == iov.APPLE_TIMEOUT_S
    monkeypatch.setattr(elevenlabs, "get_api_key", lambda: None)
    assert isinstance(iov.build_primary("hybrid", VoiceConfig(), paths, "tr"), stt_engines.AppleSpeech)  # no key: Apple alone
    monkeypatch.setattr(elevenlabs, "get_api_key", lambda: "sk_test")
    assert isinstance(iov.build_primary("elevenlabs", VoiceConfig(), paths, "tr"), stt_engines.ElevenLabsStt)
    hybrid = iov.build_primary("hybrid", VoiceConfig(stt_hybrid_threshold=0.7), paths, "tr")
    assert isinstance(hybrid, stt_engines.HybridStt) and hybrid.threshold == 0.7
    target = FakePrimary()
    cloud = stt_engines.ElevenLabsStt("k")
    from jarvis.voice.vocabulary import Vocabulary

    iov.set_hints(stt_engines.HybridStt(target, cloud), Vocabulary(user=["A b"]))
    assert target.hints == ["A b"] and cloud.keyterms == ["A b"]


def test_from_config_wires_the_engine_vocabulary_and_corrections(config, paths, monkeypatch):
    from jarvis.voice import vocabulary

    monkeypatch.setattr(vocabulary, "calendar_terms", lambda *a, **k: [])  # no calendar access in tests
    config.voice.stt_engine = "apple"
    stt = iov.SpeechToText.from_config(config.voice, paths, config=config)
    assert stt.engine == "apple" and stt.primary is not None and stt.corrections is not None
    assert stt.vocabulary is not None and stt.vocabulary_cache is not None
    assert "Fenerbahçe" in stt.vocabulary().terms
    bare = iov.SpeechToText.from_config(config.voice)
    assert bare.primary is None and bare.vocabulary is None


def test_config_engine_fields():
    from jarvis.config import VoiceConfig

    v = VoiceConfig()
    assert v.stt_engine == "whisper" and v.preroll_ms == 1000 and v.stt_hybrid_threshold == 0.6
    with pytest.raises(ValueError):
        VoiceConfig(stt_engine="siri")
    with pytest.raises(ValueError):
        VoiceConfig(preroll_ms=5000)


def test_repo_config_uses_apple(config):
    assert config.voice.stt_engine == "apple" and config.voice.stt_language == "tr"
