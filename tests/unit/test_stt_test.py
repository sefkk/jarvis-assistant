"""`jarvis voice test-stt` (voice/stt_test.py): scoring, the recording session and the comparison, with fakes."""

from __future__ import annotations

import json

import numpy as np
import pytest

from jarvis.main import build_parser
from jarvis.voice import stt_test as st
from jarvis.voice.audio import SAMPLE_RATE, WAKE_FRAME_SAMPLES
from jarvis.voice.stt_engines import EngineSpec, SttResult
from jarvis.voice.vad import SileroVad

# --- normalization and scoring -------------------------------------------------------------------------------------


def test_tokens_turkish_lower_apostrophes_and_numbers():
    assert st.tokens("Kadıköy'den Taksim'e") == ["kadıköy", "den", "taksim", "e"]
    assert st.tokens("İZMİR'de ILIK") == ["izmir", "de", "ılık"]
    assert st.tokens("saat 15:30'da") == ["saat", "15", "30", "da"]
    assert st.tokens("yirmi beş dakika") == ["25", "dakika"]
    assert st.tokens("on beş otuz") == ["15", "30"]
    assert st.tokens("yüz yirmi iki kilo, üç tekrar") == ["122", "kilo", "3", "tekrar"]
    assert st.tokens("sabah 7:45’e") == ["sabah", "7", "45", "e"]
    assert st.tokens("saat on beş otuzda, cumartesi dokuza, yedi kırk beşe") == ["saat", "15", "30", "cumartesi", "9", "7", "45"]
    assert st.tokens("yirmi beş dakikalık") == ["25", "dakikalık"] and st.tokens("bir dakika") == ["1", "dakika"]
    assert st.tokens("") == []


def test_names_plain_and_hints():
    s = "{Sezen Aksu}'dan {Gülümse} çal."
    assert st.plain(s) == "Sezen Aksu'dan Gülümse çal." and st.names_in(s) == ["Sezen Aksu", "Gülümse"]
    assert st.hint_terms(st.SENTENCES)[:2] == ["Sezen Aksu", "Gülümse"] and len(set(st.hint_terms(st.SENTENCES))) == len(
        st.hint_terms(st.SENTENCES)
    )
    assert len(st.SENTENCES) == 15 and all(st.plain(s) for s in st.SENTENCES)


def test_english_sentences_tokens_and_engines(config, paths):
    assert len(st.SENTENCES_EN) == 15 and all(st.plain(s) for s in st.SENTENCES_EN)
    assert len(set(st.hint_terms(st.SENTENCES_EN))) == len(st.hint_terms(st.SENTENCES_EN)) >= 20
    assert st.tokens("Twenty-five minutes at seven forty five", "en") == ["25", "minutes", "at", "7", "45"]
    assert st.tokens("King's Cross", "en") == ["king", "s", "cross"] and st.tokens("I", "en") == ["i"]
    config.voice.stt_language = "en"
    assert st.session_language(config) == "en" and st.sentences_for("en") == st.SENTENCES_EN
    specs = st.engine_specs(config, paths, hints=["Arsenal"], cloud=False, api_key=None, language="en")
    keys = [s.key for s in specs]
    assert "turkish" not in keys and "apple" in keys
    apple = next(s for s in specs if s.key == "apple").build()
    assert apple.locale == "en-US"
    current = next(s for s in specs if s.key == "current").build()
    assert current.language == "en" and current.model == config.voice.stt_model_by_language["en"]
    score = st.score_sentence(
        1, "Play {Hello} by {Adele} for 25 minutes.", SttResult("play hello by Adele for twenty five minutes", "en", None, 1.0, "x"), "en"
    )
    assert score.errors == 0 and score.names_found == 2
    config.voice.stt_language = "tr"
    assert "turkish" in [s.key for s in st.engine_specs(config, paths, hints=[], cloud=False, api_key=None, language="tr")]


def test_levenshtein_and_sequence():
    assert st.levenshtein([], ["a"]) == 1 and st.levenshtein(["a", "b"], ["a", "b"]) == 0
    assert st.levenshtein(["a", "b", "c"], ["a", "x", "c", "d"]) == 2
    assert st.contains_sequence(["a", "b", "c"], ["b", "c"]) and not st.contains_sequence(["a", "c", "b"], ["b", "c"])
    assert st.contains_sequence([], [])


def test_score_sentence_and_report_aggregates():
    sentence = "{Sezen Aksu}'dan {Gülümse} çal."
    perfect = st.score_sentence(1, sentence, SttResult("Sezen Aksu'dan Gülümse çal", "tr", 0.9, 120.0, "x"))
    assert perfect.errors == 0 and perfect.word_accuracy == 1.0 and perfect.names_found == 2
    bad = st.score_sentence(2, sentence, SttResult("Sezen Aksu'dan gülüşme çal", "tr", 0.4, 80.0, "x", cost_usd=0.002))
    assert bad.errors == 1 and bad.names_found == 1 and bad.words == 5
    report = st.EngineReport("k", "label", True, False, 0.0, [perfect, bad])
    assert report.word_accuracy == pytest.approx(1 - 1 / 10) and report.name_accuracy == pytest.approx(3 / 4)
    assert report.avg_ms == 100.0 and report.cost_usd == 0.002
    assert st.EngineReport("k", "l", True, False, 0.0).word_accuracy == 0.0


# --- recording session --------------------------------------------------------------------------------------------


def fake_vad_model(chunk):
    return 0.95 if float(np.abs(chunk).mean()) > 0.01 else 0.0


class FakeMic:
    def __init__(self, frames):
        self.frames = list(frames)
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def flush(self):
        pass

    def read(self, timeout=None):
        return self.frames.pop(0) if self.frames else None


def utterance(seconds=0.8, lead=0.3, tail=1.5, amplitude=5000, seed=0):
    rng = np.random.default_rng(seed)
    parts = [
        np.zeros(int(lead * SAMPLE_RATE), np.int16),
        (rng.standard_normal(int(seconds * SAMPLE_RATE)) * amplitude).astype(np.int16),
        np.zeros(int(tail * SAMPLE_RATE), np.int16),
    ]
    audio = np.concatenate(parts)
    return [audio[i : i + WAKE_FRAME_SAMPLES] for i in range(0, len(audio), WAKE_FRAME_SAMPLES)]


def test_record_session_saves_wavs_and_manifest(config, paths, tmp_path):
    silence = [np.zeros(WAKE_FRAME_SAMPLES, np.int16)] * int(config.voice.no_speech_timeout_s * SAMPLE_RATE / WAKE_FRAME_SAMPLES + 2)
    mic = FakeMic(utterance(seed=1) + silence + utterance(seed=2))  # sentence 1, nothing heard, sentence 2 again
    lines, prompts, sounds = [], [], []
    session = st.record_session(
        config,
        paths,
        out=lines.append,
        wait=lambda p: prompts.append(p) or "",
        mic=mic,
        vad=SileroVad(model=fake_vad_model),
        play_sound=sounds.append,
        sentences=["{Ali} ile buluş.", "Saat 3'te."],
        directory=tmp_path / "s1",
    )
    assert mic.started and mic.stopped and len(prompts) == 3 and len(sounds) == 3
    assert [f.name for f in session.files] == ["01.wav", "02.wav"] and all(f.is_file() for f in session.files)
    assert any("bir şey duyulmadı" in line for line in lines)  # the test config is Turkish
    manifest = json.loads(session.manifest.read_text(encoding="utf-8"))
    assert manifest["items"][1] == {"index": 2, "sentence": "Saat 3'te.", "file": "02.wav"} and manifest["language"] == "tr"
    loaded = st.Session.load(tmp_path / "s1")
    assert loaded.sentences == session.sentences and loaded.files == session.files and loaded.language == "tr"
    assert loaded.total_seconds() > 1.0


# --- comparison --------------------------------------------------------------------------------------------------


class Scripted:
    def __init__(self, name, texts, confidence=0.9, cost=0.0, local=True):
        self.name, self.texts, self.confidence, self.cost, self.local = name, list(texts), confidence, cost, local
        self.paid = not local
        self.calls = 0

    def warm_up(self):
        return 5.0

    def transcribe_array(self, audio):
        text = self.texts[self.calls % len(self.texts)]
        self.calls += 1
        return SttResult(text, "tr", self.confidence, 50.0, self.name, cost_usd=self.cost)


def make_session(tmp_path):
    sentences = ["{Sezen Aksu} çal.", "Bana 25 dakika kur."]
    files = []
    for i in range(2):
        path = tmp_path / f"0{i + 1}.wav"
        from jarvis.voice.audio import write_wav_int16

        write_wav_int16(path, np.zeros(SAMPLE_RATE, np.int16))
        files.append(path)
    session = st.Session(tmp_path, sentences, files)
    session.save()
    return session


def test_compare_scores_every_option_skips_missing_models_and_adds_the_hybrid(config, paths, tmp_path, monkeypatch):
    session = make_session(tmp_path)
    good = Scripted("local-good", ["Sezen Aksu çal", "Bana 25 dakika kur"], confidence=0.9)
    weak = Scripted("local-weak", ["Sezen akşu çal", "Bana yirmi beş dakika kur"], confidence=0.3)
    cloud = Scripted("elevenlabs", ["Sezen Aksu çal", "Bana 25 dakika kur"], confidence=0.99, cost=0.001, local=False)
    specs = [
        EngineSpec("current", "current", lambda: weak, True, False, requires_model="cached/model"),
        EngineSpec("large-v3", "large", lambda: good, True, False, requires_model="missing/model"),
        EngineSpec("apple", "apple", lambda: good, True, False),
        EngineSpec("elevenlabs", "cloud", lambda: cloud, False, True, 0.0037),
    ]
    monkeypatch.setattr(st, "model_available", lambda model: model == "cached/model")
    lines = []
    reports = st.compare(config, paths, session, out=lines.append, specs=specs, verbose=True)
    by_key = {r.key: r for r in reports}
    assert by_key["large-v3"].error and "--download" in by_key["large-v3"].error
    assert by_key["current"].word_accuracy == pytest.approx(1 - 1 / 7) and by_key["current"].name_accuracy == 0.0
    assert by_key["apple"].word_accuracy == 1.0 and by_key["apple"].name_accuracy == 1.0 and by_key["apple"].load_ms == 5.0
    hybrid = by_key["hybrid"]
    assert hybrid.sentences[0].routed_to == "apple" and hybrid.cost_usd == 0.0  # the best local was confident: no cloud call
    assert "current" in lines[-4] or any("option" in line for line in lines)
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert {e["key"] for e in results["engines"]} == {"current", "large-v3", "apple", "elevenlabs", "hybrid"}
    assert any(line.startswith("== ") for line in lines)  # verbose details


class Flaky(Scripted):
    def transcribe_array(self, audio):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("no speech")
        return SttResult("Bana 25 dakika kur", "tr", 0.9, 50.0, self.name)


def test_run_engine_survives_one_bad_sentence_and_results_merge(config, paths, tmp_path):
    session = make_session(tmp_path)
    lines = []
    report = st.run_engine(EngineSpec("apple", "apple", lambda: Flaky("apple", [""]), True, False), session, lines.append)
    assert report.error is None and len(report.sentences) == 2
    assert report.sentences[0].hypothesis == "" and report.sentences[0].errors == 3 and report.sentences[1].errors == 0
    assert any("cümle 1: RuntimeError: no speech" in line for line in lines)
    dead = st.run_engine(EngineSpec("x", "x", lambda: Scripted("x", [""]), True, False), session, lines.append)
    assert dead.error is None  # empty transcripts are results, not failures
    # results.json keeps rows of options that were not part of this run
    st.save_results(
        session, [st.EngineReport("elevenlabs", "cloud", False, True, 0.0037, [st.SentenceScore(1, "r", "r", 1, 0, 0, 0, 1.0, 1.0, 0.01)])]
    )
    st.save_results(session, [report])
    data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [e["key"] for e in data["engines"]] == ["elevenlabs", "apple"] and data["engines"][0]["cost_usd"] == 0.01
    st.save_results(session, [st.EngineReport("elevenlabs", "cloud", False, True, 0.0037, [])])
    data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [e["key"] for e in data["engines"]] == ["elevenlabs", "apple"] and data["engines"][0]["cost_usd"] == 0.0


def test_simulate_hybrid_routes_low_confidence_to_the_cloud():
    a = st.SentenceScore(1, "r", "slm", 1, 1, 0, 0, 100.0, 0.2)
    b = st.SentenceScore(1, "r", "selam", 1, 0, 0, 0, 400.0, 0.99, 0.002)
    local = st.EngineReport("turkish", "t", True, False, 0.0, [a])
    cloud = st.EngineReport("elevenlabs", "e", False, True, 0.0037, [b])
    hybrid = st.simulate_hybrid(local, cloud, threshold=0.6)
    s = hybrid.sentences[0]
    assert s.hypothesis == "selam" and s.routed_to == "elevenlabs" and s.ms == 500.0 and hybrid.cost_usd == 0.002


def test_compare_filters_engines_and_downloads_on_request(config, paths, tmp_path, monkeypatch):
    session = make_session(tmp_path)
    good = Scripted("x", ["Sezen Aksu çal", "Bana 25 dakika kur"])
    specs = [
        EngineSpec("large-v3", "large", lambda: good, True, False, requires_model="missing/model"),
        EngineSpec("apple", "apple", lambda: good, True, False),
    ]
    monkeypatch.setattr(st, "model_available", lambda model: False)
    downloaded = []
    monkeypatch.setattr(st, "download_model", lambda model, out: downloaded.append(model))
    reports = st.compare(config, paths, session, out=lambda _l: None, specs=specs, engines=["large-v3"], download=True)
    assert [r.key for r in reports] == ["large-v3"] and downloaded == ["missing/model"] and not reports[0].error


def test_format_table_marks_cost_and_place():
    ok = st.EngineReport("a", "Apple", True, False, 0.0, [st.SentenceScore(1, "r", "r", 1, 0, 0, 0, 300.0, 0.7)])
    paid = st.EngineReport("e", "Scribe", False, True, 0.0045, [st.SentenceScore(1, "r", "r", 1, 0, 0, 0, 900.0, 0.9, 0.001)])
    broken = st.EngineReport("b", "Broken", True, False, 0.0, error="model not downloaded")
    lines = st.format_table([ok, paid, broken], 12.0)
    assert "free  local" in lines[2] and "paid  cloud" in lines[3] and "0.0045" in lines[3] and "model not downloaded" in lines[4]


def test_voice_cli_parser_test_stt():
    args = build_parser().parse_args(
        ["voice", "test-stt", "--compare", "/x", "--engines", "current,apple", "--cloud", "--download", "--verbose"]
    )
    assert args.voice_command == "test-stt" and args.compare == "/x" and args.engines == "current,apple"
    assert args.cloud and args.download and args.verbose
    assert not build_parser().parse_args(["voice", "test-stt"]).cloud


def test_run_test_stt_needs_the_voice_extra(config, paths, monkeypatch):
    from jarvis.voice import service

    monkeypatch.setattr(service.VoiceService, "missing_dependencies", staticmethod(lambda: ["mlx-whisper"]))
    lines = []
    assert st.run_test_stt(config, paths, out=lines.append) == 1 and "mlx-whisper" in lines[0]
