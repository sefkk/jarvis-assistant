"""STT engines (voice/stt_engines.py) with fakes: Whisper options and confidence, the Apple helper, Scribe, hybrid."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest

from jarvis.exceptions import JarvisError
from jarvis.voice import stt_engines as se


def speech(seconds=1.0):
    return (np.random.default_rng(0).standard_normal(int(16000 * seconds)) * 3000).astype(np.int16)


class FakeWhisper:
    def __init__(self, text=" Merhaba dünya", segments=None):
        self.text, self.segments, self.calls = text, segments, []

    def transcribe(self, audio, **options):
        self.calls.append(options)
        out = {"text": self.text, "language": options.get("language", "xx")}
        if self.segments is not None:
            out["segments"] = self.segments
        return out


def test_whisper_engine_passes_language_prompt_and_confidence():
    fake = FakeWhisper(segments=[{"avg_logprob": -0.1}, {"avg_logprob": -0.3}])
    engine = se.WhisperMlx("org/model-x", language="tr", initial_prompt="Sezen Aksu, Kadıköy", loader=lambda: fake)
    assert engine.name == "model-x" and engine.local and not engine.paid
    engine.warm_up()
    result = engine.transcribe_array(speech())
    assert result.text == "Merhaba dünya" and result.language == "tr" and result.engine == "model-x"
    assert result.confidence == pytest.approx(math.exp(-0.2))
    for call in fake.calls:
        assert call["path_or_hf_repo"] == "org/model-x" and call["language"] == "tr" and call["initial_prompt"] == "Sezen Aksu, Kadıköy"
    bare = se.WhisperMlx("m", language=None, loader=lambda: FakeWhisper())
    assert "language" not in bare._options() and bare.transcribe_array(speech()).confidence is None


def test_whisper_confidence_edge_cases():
    assert se.whisper_confidence({}) is None and se.whisper_confidence({"segments": []}) is None
    assert se.whisper_confidence({"segments": [{"avg_logprob": 0.5}]}) == 1.0
    assert se.whisper_confidence("nope") is None


# --- Apple ---------------------------------------------------------------------------------------------------------


def test_ensure_helper_compiles_once(tmp_path, monkeypatch):
    source = tmp_path / "s.swift"
    source.write_text("print(1)")
    monkeypatch.setattr(se.shutil, "which", lambda name: "/usr/bin/swiftc")
    runs = []

    def fake_run(argv, **kw):
        runs.append(argv)
        Path(argv[argv.index("-o") + 1]).write_text("bin")
        return subprocess.CompletedProcess(argv, 0, "", "")

    first = se.ensure_helper(tmp_path / "bin", source=source, run=fake_run)
    second = se.ensure_helper(tmp_path / "bin", source=source, run=fake_run)
    assert first == second and first.is_file() and len(runs) == 1
    assert runs[0][0] == "/usr/bin/swiftc" and "-parse-as-library" in runs[0] and str(source) in runs[0]
    assert "__info_plist" in runs[0]  # the usage description macOS needs before asking for Speech Recognition
    plist = Path(runs[0][-1])
    assert "NSSpeechRecognitionUsageDescription" in plist.read_text()
    source.write_text("print(2)")  # a new source version compiles again under another name
    third = se.ensure_helper(tmp_path / "bin", source=source, run=fake_run)
    assert third != first and len(runs) == 2
    assert not first.exists() and third.is_file()  # the old version is removed


def test_ensure_helper_without_swiftc_or_with_a_failed_compile(tmp_path, monkeypatch):
    source = tmp_path / "s.swift"
    source.write_text("x")
    monkeypatch.setattr(se.shutil, "which", lambda name: None)
    with pytest.raises(JarvisError, match="swiftc"):
        se.ensure_helper(tmp_path / "bin", source=source)
    monkeypatch.setattr(se.shutil, "which", lambda name: "/usr/bin/swiftc")
    bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "error: boom")  # noqa: E731
    with pytest.raises(JarvisError, match="boom"):
        se.ensure_helper(tmp_path / "bin", source=source, run=bad)


def test_apple_engine_runs_the_helper_with_hints(tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        assert Path(argv[2]).is_file()  # the WAV exists while the helper runs
        return subprocess.CompletedProcess(argv, 0, json.dumps({"text": "Yarın hava nasıl", "confidence": 0.8, "ms": 300}), "")

    engine = se.AppleSpeech(tmp_path, locale="tr-TR", hints=["Kadıköy", " ", "Sezen Aksu"], run=fake_run, helper=tmp_path / "helper")
    result = engine.transcribe_array(speech())
    assert result.text == "Yarın hava nasıl" and result.language == "tr" and result.confidence == 0.8 and result.engine == "apple"
    assert calls[0][0] == str(tmp_path / "helper") and calls[0][1] == "tr-TR" and calls[0][3:] == ["Kadıköy", "Sezen Aksu"]
    failing = se.AppleSpeech(
        tmp_path,
        run=lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, json.dumps({"error": "recognition failed"}), "kAFAssistantErrorDomain 1101"
        ),
        helper=tmp_path / "helper",
    )
    with pytest.raises(JarvisError, match="recognition failed kAFAssistantErrorDomain 1101"):
        failing.transcribe_array(speech())
    silent = se.AppleSpeech(
        tmp_path,
        run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, json.dumps({"error": "recognition failed"}), "No speech detected\n"),
        helper=tmp_path / "helper",
    )
    empty = silent.transcribe_array(speech())
    assert empty.text == "" and empty.confidence is None and empty.engine == "apple"


# --- ElevenLabs ------------------------------------------------------------------------------------------------------


def test_elevenlabs_stt_request_and_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        body = request.read()
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "text": "Kadıköy'den Taksim'e",
                "language_code": "tur",
                "language_probability": 0.99,
                "words": [
                    {"text": "Kadıköy'den", "type": "word", "logprob": -0.1},
                    {"text": " ", "type": "spacing", "logprob": 0.0},
                    {"text": "Taksim'e", "type": "word", "logprob": -0.3},
                ],
            },
        )

    engine = se.ElevenLabsStt("sk_test", language="tr", keyterms=["Kadıköy", "Taksim"], transport=httpx.MockTransport(handler))
    result = engine.transcribe_array(speech(6.0))
    assert result.text == "Kadıköy'den Taksim'e" and result.language == "tr" and result.engine == "elevenlabs"
    assert result.confidence == pytest.approx(math.exp(-0.2))
    assert seen["url"].endswith("/v1/speech-to-text") and seen["key"] == "sk_test"
    body = seen["body"]
    assert b'name="model_id"' in body and b"scribe_v2" in body and b'name="language_code"' in body and b"tr" in body
    assert body.count(b'name="keyterms"') == 2 and b'filename="input.wav"' in body
    assert engine.usd_per_minute == pytest.approx((0.22 + 0.05) / 60)
    assert result.cost_usd == pytest.approx(6.0 / 60 * engine.usd_per_minute)
    assert se.ElevenLabsStt("k").usd_per_minute == pytest.approx(0.22 / 60)


def test_elevenlabs_stt_errors_do_not_leak_the_key():
    engine = se.ElevenLabsStt(
        "sk_secret",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "nope"}})),
    )
    with pytest.raises(JarvisError) as info:
        engine.transcribe_array(speech())
    assert "sk_secret" not in str(info.value)

    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(JarvisError, match="failed"):
        se.ElevenLabsStt("sk_secret", transport=httpx.MockTransport(boom)).transcribe_array(speech())


# --- hybrid -----------------------------------------------------------------------------------------------------------


class Scripted:
    local = True
    paid = False

    def __init__(self, name, text, confidence, fail=False, cost=0.0):
        self.name, self.text, self.confidence, self.fail, self.cost = name, text, confidence, fail, cost

    def transcribe_array(self, audio):
        if self.fail:
            raise JarvisError("cloud down")
        return se.SttResult(self.text, "tr", self.confidence, 100.0, self.name, cost_usd=self.cost)


def test_hybrid_routes_on_confidence_and_survives_cloud_failures():
    sure = se.HybridStt(Scripted("local", "selam", 0.9), Scripted("cloud", "merhaba", 0.99, cost=0.01), threshold=0.6)
    r = sure.transcribe_array(speech())
    assert r.text == "selam" and r.routed_to == "local" and r.cost_usd == 0.0 and r.engine == "hybrid"
    unsure = se.HybridStt(Scripted("local", "slm", 0.3), Scripted("cloud", "selam", 0.99, cost=0.01), threshold=0.6)
    r = unsure.transcribe_array(speech())
    assert r.text == "selam" and r.routed_to == "cloud" and r.cost_usd == 0.01 and r.ms == 200.0
    offline = se.HybridStt(Scripted("local", "slm", None), Scripted("cloud", "", 0, fail=True))
    r = offline.transcribe_array(speech())
    assert r.text == "slm" and r.routed_to == "local"


def test_engine_language_defaults_follow_the_main_language(tmp_path):
    from jarvis import i18n

    i18n.set_language("en")
    assert se.AppleSpeech(tmp_path).locale == "en-US" and se.ElevenLabsStt("k").language == "en"
    assert se.WhisperMlx("m").language == "en" and se.WhisperMlx("m", language=None).language is None
    i18n.set_language("tr")
    assert se.AppleSpeech(tmp_path).locale == "tr-TR" and se.ElevenLabsStt("k").language == "tr"
    assert se.locale_for("en") == "en-US" and se.locale_for("xx") == "tr-TR"
