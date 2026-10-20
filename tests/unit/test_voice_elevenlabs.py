"""ElevenLabsTTS with a mocked HTTP transport, a fake player and a fake `say` runner.
No network, no audio, no Keychain."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from jarvis.io.voice import spoken_choice, spoken_error
from jarvis.voice import elevenlabs as el
from jarvis.voice import health as vhealth
from jarvis.voice.elevenlabs import ElevenLabsTTS
from jarvis.voice.tts import TextToSpeech, details_phrase
from tests.unit.test_voice_sink import FakeTTS, make_sink
from tests.unit.test_voice_tts import VOICES, FakeRunner

KEY = "sk_0123456789abcdef0123456789abcdef"  # gitleaks:allow - a fake key for the tests
VOICE = "v0iceID12"
CHUNK = b"\x01\x00" * 1200  # 50 ms of pcm_24000


class FakePlayer:
    def __init__(self, write_delay: float = 0.0) -> None:
        self.write_delay = write_delay
        self.written: list[bytes] = []
        self.started = self.stopped = self.aborted = self.closed = 0

    def start(self):
        self.started += 1

    def write(self, data):
        self.written.append(bytes(data))
        if self.write_delay:
            time.sleep(self.write_delay)

    def stop(self):
        self.stopped += 1

    def abort(self):
        self.aborted += 1

    def close(self):
        self.closed += 1


class ChunkStream(httpx.SyncByteStream):
    """A streaming body: `first_delay` before the first chunk, `gap` between the others."""

    def __init__(self, chunks, *, first_delay=0.0, gap=0.0):
        self.chunks, self.first_delay, self.gap = list(chunks), first_delay, gap
        self.yielded = 0
        self.closed = False

    def __iter__(self):
        time.sleep(self.first_delay)
        for i, chunk in enumerate(self.chunks):
            if i and self.gap:
                time.sleep(self.gap)
            if self.closed:
                return
            self.yielded += 1
            yield chunk

    def close(self):
        self.closed = True


class Api:
    """Scripted ElevenLabs: `outcomes` are consumed per request (a stream, a Response or an exception)."""

    def __init__(self, outcomes=None, *, default=None):
        self.outcomes = list(outcomes or [])
        self.default = default
        self.requests: list[httpx.Request] = []
        self.streams: list[ChunkStream] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else self.default
        if outcome is None:
            outcome = ChunkStream([CHUNK, CHUNK])
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        self.streams.append(outcome)
        return httpx.Response(200, stream=outcome, headers={"content-type": "audio/pcm"})

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def quota_response(status=401, detail_status="quota_exceeded"):
    return httpx.Response(status, json={"detail": {"status": detail_status, "message": "Out of characters."}})


def make_engine(api: Api, *, paths=None, player=None, runner=None, **kw):
    runner = runner or FakeRunner(duration=0.01)
    say = TextToSpeech(runner=runner, voices=VOICES)
    options = dict(
        voice_id=VOICE,
        first_chunk_timeout_s=0.5,
        api_key=KEY,
        transport=httpx.MockTransport(api),
        player_factory=lambda: player if player is not None else FakePlayer(),
        fallback=say,
    )
    if paths is not None:
        options.update(db_path=paths.db_file, cache_dir=paths.tts_cache_dir)
    options.update(kw)
    engine = ElevenLabsTTS(**options)
    engine.runner = runner  # type: ignore[attr-defined] - for the tests
    return engine


@pytest.fixture
def engine_factory(paths):
    engines = []

    def build(api: Api, **kw):
        kw.setdefault("paths", paths)
        engine = make_engine(api, **kw)
        engine.start()
        engines.append(engine)
        return engine

    yield build
    for engine in engines:
        engine.close()


def said(engine) -> list[str]:
    return [p.argv[-1] for p in engine.runner.processes]


def rows(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM external_calls ORDER BY id").fetchall()]


def audit_lines(logs, decision="tts_fallback"):
    path = logs / "audit.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        data = json.loads(line)
        if data.get("decision") == decision:
            out.append(data)
    return out


# --- streaming playback -----------------------------------------------------------------------------------


def test_playback_starts_on_first_chunk(engine_factory):
    stream = ChunkStream([CHUNK, CHUNK, CHUNK], gap=0.1)
    api = Api([stream])
    player = FakePlayer()
    seen_at_first_audio = []
    engine = engine_factory(api, player=player, on_first_audio=lambda: seen_at_first_audio.append(stream.yielded))
    engine.begin_turn()
    engine.speak("Merhaba dünya.")
    assert engine.wait(timeout=3)
    assert seen_at_first_audio == [1]  # fired after the first chunk, while the stream was still open
    assert player.written == [CHUNK, CHUNK, CHUNK] and player.stopped == 1 and player.aborted == 0
    assert stream.closed
    request = api.requests[0]
    assert request.url.path == f"/v1/text-to-speech/{VOICE}/stream" and request.url.params["output_format"] == "pcm_24000"
    assert request.headers["xi-api-key"] == KEY
    assert api.bodies[0] == {
        "text": "Merhaba dünya.",
        "model_id": "eleven_flash_v2_5",
        "language_code": "tr",
        "apply_text_normalization": "on",
    }
    assert said(engine) == []


def test_odd_sized_http_chunks_are_aligned_to_whole_samples(engine_factory):
    """HTTP chunk boundaries fall anywhere; the int16 player must only ever see an even number of bytes."""
    stream = ChunkStream([b"\x01\x00\x01", b"\x00\x01\x00\x01", b"\x00\x01"])  # 3 + 4 + 2 bytes, odd splits, odd total
    api = Api([stream])
    player = FakePlayer()
    engine = engine_factory(api, player=player)
    engine.speak("Merhaba.")
    assert engine.wait(timeout=3)
    assert all(len(chunk) % 2 == 0 for chunk in player.written) and all(player.written)
    assert b"".join(player.written) == b"\x01\x00" * 4 + b"\x01\x00"  # the truncated last sample is completed with silence
    assert player.stopped == 1 and player.aborted == 0
    assert said(engine) == []  # no fallback


def test_fallback_after_an_http_error_reports_the_say_voice(engine_factory):
    api = Api([httpx.Response(500, text="boom")])
    sentences = []
    engine = engine_factory(api, on_sentence=lambda text, voice: sentences.append((text, voice)))
    engine.speak("Merhaba.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Merhaba."]
    assert sentences[0] == ("Merhaba.", "elevenlabs")  # announced before the request, as before
    assert sentences[-1][0] == "Merhaba." and sentences[-1][1].startswith("Yelda") and "fallback: http_500" in sentences[-1][1]


def test_language_code_is_omitted_for_multilingual_v2(engine_factory):
    api = Api()
    engine = engine_factory(api, model="eleven_multilingual_v2")
    engine.speak("Hello there, how are you today?")
    assert engine.wait(timeout=3)
    assert api.bodies[0] == {
        "text": "Hello there, how are you today?",
        "model_id": "eleven_multilingual_v2",
        "apply_text_normalization": "on",
    }


def test_speak_sends_the_spoken_form_not_digits(engine_factory):
    """Flash v2.5 read "19.00" as garbage: the request carries words (voice/normalize.py), plus language_code
    and apply_text_normalization as the safety net."""
    api = Api()
    engine = engine_factory(api)
    engine.speak("Maç 19.00'da başlıyor, batarya %75 dolu.")
    assert engine.wait(timeout=3)
    assert api.bodies[0] == {
        "text": "Maç saat on dokuzda başlıyor, batarya yüzde yetmiş beş dolu.",
        "model_id": "eleven_flash_v2_5",
        "language_code": "tr",
        "apply_text_normalization": "on",
    }


def test_text_normalization_auto_omits_the_flag_and_off_sends_it(engine_factory):
    api = Api()
    engine = engine_factory(api, text_normalization="auto")
    engine.speak("Merhaba.")
    assert engine.wait(timeout=3)
    assert "apply_text_normalization" not in api.bodies[0]
    api2 = Api()
    engine2 = engine_factory(api2, text_normalization="off")
    engine2.speak("Merhaba.")
    assert engine2.wait(timeout=3)
    assert api2.bodies[0]["apply_text_normalization"] == "off"
    assert engine_factory(Api(), text_normalization="bogus").text_normalization == "auto"


def test_a_400_naming_the_normalization_flag_retries_once_without_it(engine_factory):
    rejected = httpx.Response(
        400, json={"detail": {"status": "invalid_request", "message": "apply_text_normalization is not available for this model"}}
    )
    api = Api([rejected, ChunkStream([CHUNK, CHUNK])])
    player = FakePlayer()
    engine = engine_factory(api, player=player)
    engine.speak("Merhaba dünya.")
    assert engine.wait(timeout=3)
    assert len(api.requests) == 2
    assert api.bodies[0]["apply_text_normalization"] == "on" and "apply_text_normalization" not in api.bodies[1]
    assert engine.text_normalization == "auto"
    assert player.written == [CHUNK, CHUNK] and said(engine) == []  # spoken by ElevenLabs, no fallback
    assert engine.circuit_open is False


def test_barge_in_aborts_audio_and_cancels_pending_requests(engine_factory):
    first = ChunkStream([CHUNK] * 30, gap=0.05)
    second = ChunkStream([CHUNK] * 30, gap=0.05)
    api = Api([first, second])
    player = FakePlayer()
    engine = engine_factory(api, player=player)
    engine.speak("Birinci cümle.")
    engine.speak("İkinci cümle.")
    engine.speak("Üçüncü cümle.")
    deadline = time.time() + 3
    while len(player.written) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(player.written) >= 2 and engine.is_speaking
    generation = engine.generation
    engine.stop()
    assert engine.generation == generation + 1
    assert engine.wait(timeout=3)
    assert player.aborted >= 1
    assert first.closed
    assert len(first.chunks) > len(player.written)  # playback stopped mid-stream
    assert len(api.requests) <= 2  # the third sentence was never requested
    time.sleep(0.1)
    if len(api.requests) == 2:
        assert second.closed and second.yielded <= 1  # the prefetched request was closed, never played
    # the engine keeps working after a barge-in
    engine.speak("Yeniden.")
    assert engine.wait(timeout=3)
    assert json.loads(api.requests[-1].content)["text"] == "Yeniden."


def test_prefetch_starts_the_next_request_while_playing(engine_factory):
    first = ChunkStream([CHUNK] * 6, gap=0.05)
    second = ChunkStream([CHUNK] * 2)
    api = Api([first, second])
    engine = engine_factory(api)
    engine.speak("Bir.")
    engine.speak("İki.")
    deadline = time.time() + 3
    while first.yielded < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert len(api.requests) == 2  # the second request is in flight before the first finished playing
    assert engine.wait(timeout=3)
    assert [b["text"] for b in api.bodies] == ["Bir.", "İki."]


# --- fallbacks -------------------------------------------------------------------------------------------


def test_fallback_on_http_error(engine_factory, db, logs):
    api = Api([httpx.Response(500, text="boom")])
    engine = engine_factory(api)
    engine.speak("Merhaba.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Merhaba."]
    (row,) = rows(db)
    assert row["service"] == "elevenlabs" and row["status"] == "error" and row["units"] == 8 and row["cached"] == 0
    assert "http_500" in row["detail"]
    (entry,) = audit_lines(logs)
    assert entry["reason"] == "http_500" and entry["chars"] == 8 and entry["tool"] == "voice"
    assert json.loads(db.kv_get("elevenlabs:last_fallback"))["reason"] == "http_500"
    assert not engine.circuit_open  # one failure does not trip the breaker


def test_fallback_on_first_chunk_timeout(engine_factory, db):
    slow = ChunkStream([CHUNK], first_delay=0.4)
    api = Api([slow])
    player = FakePlayer()
    engine = engine_factory(api, player=player, first_chunk_timeout_s=0.05)
    engine.speak("Yavaş cümle.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Yavaş cümle."] and player.written == []
    (row,) = rows(db)
    assert row["status"] == "timeout"
    time.sleep(0.5)
    assert slow.closed  # the late response was closed, not played


def test_fallback_on_quota_trips_the_breaker(engine_factory, db, logs):
    api = Api([quota_response()])
    engine = engine_factory(api)
    engine.speak("Bir.")
    engine.speak("İki.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Bir.", "İki."]
    assert len(api.requests) == 1 and engine.circuit_open
    reasons = [e["reason"] for e in audit_lines(logs)]
    assert reasons == ["quota", "circuit_open"]
    assert [r["status"] for r in rows(db)] == ["error", "fallback"]


def test_fallback_on_429(engine_factory):
    api = Api([httpx.Response(429, json={"detail": {"status": "too_many_concurrent_requests", "message": "slow down"}})])
    engine = engine_factory(api)
    engine.speak("Bir.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Bir."] and engine.circuit_open


def test_fallback_when_offline(engine_factory, logs):
    api = Api([httpx.ConnectError("name resolution failed")])
    engine = engine_factory(api)
    engine.speak("Çevrimdışı.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Çevrimdışı."]
    assert audit_lines(logs)[0]["reason"] == "offline"


def test_fallback_without_key(engine_factory, logs):
    api = Api()
    engine = engine_factory(api, api_key="")
    engine.speak("Anahtar yok.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Anahtar yok."] and api.requests == []
    assert audit_lines(logs)[0]["reason"] == "no_key"


def test_fallback_without_voice_id(engine_factory, logs):
    api = Api()
    engine = engine_factory(api, voice_id="")
    engine.speak("Ses yok.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Ses yok."] and api.requests == []
    assert audit_lines(logs)[0]["reason"] == "no_voice_id"


def test_local_only_routes_to_say(engine_factory, db):
    api = Api()
    engine = engine_factory(api)
    engine.speak("Gizli e-posta içeriği.", local_only=True)
    engine.speak("Normal cümle.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Gizli e-posta içeriği."]
    assert [b["text"] for b in api.bodies] == ["Normal cümle."]
    assert [r["status"] for r in rows(db)] == ["fallback", "ok"]


def test_first_audio_fires_for_a_say_fallback(engine_factory):
    fired = threading.Event()
    engine = engine_factory(Api([httpx.Response(503)]), on_first_audio=fired.set)
    engine.begin_turn()
    engine.speak("Merhaba.")
    assert engine.wait(timeout=3) and fired.is_set()


def test_circuit_breaker_opens_after_three_failures_and_recovers(engine_factory):
    clock = {"t": 1000.0}
    api = Api([httpx.Response(500), httpx.Response(500), httpx.Response(500)])
    engine = engine_factory(api, clock=lambda: clock["t"])
    for text in ("Bir.", "İki.", "Üç.", "Dört."):
        engine.speak(text)
    assert engine.wait(timeout=5)
    assert said(engine) == ["Bir.", "İki.", "Üç.", "Dört."]
    assert len(api.requests) == 3 and engine.circuit_open  # "Dört." never reached the API
    clock["t"] += ElevenLabsTTS.COOLDOWN_S + 1
    assert not engine.circuit_open
    engine.speak("Beş.")
    assert engine.wait(timeout=3)
    assert len(api.requests) == 4 and said(engine) == ["Bir.", "İki.", "Üç.", "Dört."]


def test_player_failure_falls_back(engine_factory):
    def broken():
        raise RuntimeError("no output device")

    api = Api()
    engine = engine_factory(api, player_factory=broken)
    engine.speak("Merhaba.")
    assert engine.wait(timeout=3)
    assert said(engine) == ["Merhaba."]


# --- accounting, budget, cache ------------------------------------------------------------------------------


def test_accounting_rows_and_usage_lines(engine_factory, db, logs):
    api = Api()
    engine = engine_factory(api)
    engine.speak("On karakter")
    assert engine.wait(timeout=3)
    (row,) = rows(db)
    assert row == {
        **row,
        "service": "elevenlabs",
        "operation": "eleven_flash_v2_5",
        "units": 11,
        "cached": 0,
        "status": "ok",
        "detail": VOICE,
    }
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    lines = [json.loads(line) for line in (logs / "usage.jsonl").read_text().splitlines()]
    (usage,) = [line for line in lines if line.get("kind") == "elevenlabs"]
    assert usage["chars"] == 11 and usage["status"] == "ok" and usage["cached"] is False
    assert usage["model"] == "eleven_flash_v2_5" and usage["voice_id"] == VOICE and usage["first_chunk_ms"] >= 0
    assert db.external_units_since("elevenlabs", el.start_of_month_utc()) == 11


def test_monthly_budget_switches_to_say_and_says_so_once(engine_factory, db, logs):
    api = Api()
    engine = engine_factory(api, monthly_char_budget=12)
    engine.speak("Sekiz kr")  # 8 chars: fits
    engine.speak("Sekiz kr")  # 16 > 12: budget
    engine.speak("Sekiz kr")
    assert engine.wait(timeout=3)
    assert len(api.requests) == 1
    assert said(engine) == [el.budget_notice("tr"), "Sekiz kr", "Sekiz kr"]
    assert [r["status"] for r in rows(db)] == ["ok", "fallback", "fallback"]
    assert [e["reason"] for e in audit_lines(logs)] == ["budget", "budget"]
    assert db.external_units_since("elevenlabs", el.start_of_month_utc()) == 8


def test_budget_notice_in_english(engine_factory):
    engine = engine_factory(Api(), monthly_char_budget=5)
    engine.speak("This is an English sentence.")
    assert engine.wait(timeout=3)
    assert said(engine)[0] == el.budget_notice("en")


def test_phrase_cache_hit_skips_the_request(engine_factory, db, paths):
    api = Api()
    player = FakePlayer()
    engine = engine_factory(api, player=player)
    engine.speak(details_phrase("tr"), cacheable=True)
    assert engine.wait(timeout=3)
    path = paths.tts_cache_dir / f"{el.cache_key('eleven_flash_v2_5', VOICE, details_phrase('tr'))}.pcm"
    assert path.is_file() and path.read_bytes() == CHUNK * 2
    assert not list(paths.tts_cache_dir.glob("*.tmp"))
    engine.speak(details_phrase("tr"), cacheable=True)
    assert engine.wait(timeout=3)
    assert len(api.requests) == 1
    assert b"".join(player.written) == CHUNK * 4
    assert [(r["cached"], r["status"]) for r in rows(db)] == [(0, "ok"), (1, "ok")]


def test_normal_replies_are_never_cached(engine_factory, paths):
    engine = engine_factory(Api())
    engine.speak("Yarın saat onda toplantın var.")
    assert engine.wait(timeout=3)
    assert not paths.tts_cache_dir.is_dir() or not list(paths.tts_cache_dir.glob("*.pcm"))


def test_cached_phrase_is_played_by_say_when_local_only(engine_factory, paths):
    engine = engine_factory(Api())
    engine.speak(details_phrase("tr"), cacheable=True)
    assert engine.wait(timeout=3)
    engine.speak(details_phrase("tr"), cacheable=True, local_only=True)
    assert engine.wait(timeout=3)
    assert said(engine) == [details_phrase("tr")]


# --- reports and CLI -------------------------------------------------------------------------------------------


def test_usage_report(db):
    assert el.usage_report(db, 7) == ""
    db.add_external_call(service="elevenlabs", operation="m", units=100, status="ok", detail=VOICE)
    db.add_external_call(service="elevenlabs", operation="m", units=30, cached=True, status="ok", detail=VOICE)
    db.add_external_call(service="elevenlabs", operation="m", units=40, status="error", detail=VOICE)
    db.add_external_call(service="elevenlabs", operation="m", units=50, status="fallback", detail=VOICE)
    db.add_external_call(service="google_maps", operation="directions", units=1, status="ok")
    text = el.usage_report(db, 7, budget=1000)
    assert text.startswith("ElevenLabs")
    day_line = [line for line in text.splitlines() if line.startswith("20")][0]
    assert day_line.split() == [day_line.split()[0], "2", "100", "1", "2"]
    assert "This month: 100 characters billed of the 1,000 budget (10%)." in text
    assert "budget" not in el.usage_report(db, 7)


def voices_api(pages):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == KEY and request.url.path == "/v2/voices"
        token = request.url.params.get("page_token")
        index = int(token) if token else 0
        page = pages[index]
        has_more = index + 1 < len(pages)
        return httpx.Response(
            200, json={"voices": page, "has_more": has_more, "next_page_token": str(index + 1) if has_more else None, "total_count": 3}
        )

    return httpx.MockTransport(handler)


def test_list_voices_cli_prints_every_voice(monkeypatch):
    transport = voices_api(
        [
            [
                {"voice_id": "abc123", "name": "Ayşe", "category": "premade", "labels": {"language": "tr", "gender": "female"}},
                {"voice_id": "def456", "name": "Daniel", "category": "premade", "labels": {"language": "en", "accent": "british"}},
            ],
            [{"voice_id": "ghi789", "name": "Custom", "category": "cloned", "labels": {}}],
        ]
    )
    out: list[str] = []
    assert el.list_voices_cli(out.append, api_key=KEY, transport=transport) == 0
    text = "\n".join(out)
    for needle in (
        "Ayşe",
        "abc123",
        "language=tr",
        "Daniel",
        "def456",
        "accent=british",
        "Custom",
        "ghi789",
        "cloned",
        "3 voice(s)",
        "elevenlabs_voice_id",
    ):
        assert needle in text
    assert KEY not in text


def test_list_voices_cli_without_key(monkeypatch):
    monkeypatch.setattr(el, "get_api_key", lambda: None)
    out: list[str] = []
    assert el.list_voices_cli(out.append) == 1
    assert "set-elevenlabs-key" in out[0]


def test_list_voices_cli_reports_api_errors():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "nope"}}))
    out: list[str] = []
    assert el.list_voices_cli(out.append, api_key=KEY, transport=transport) == 1
    assert "401" in out[0] and "invalid_api_key" in out[0]


def test_set_key_cli(monkeypatch):
    stored = {}
    monkeypatch.setattr(el.secrets, "set_secret", lambda name, value: stored.__setitem__(name, value))
    out: list[str] = []
    assert el.set_key_cli(prompt=lambda _: f"  {KEY} ", out=out.append) == 0
    assert stored == {"elevenlabs_api_key": KEY}
    assert not any(KEY in line for line in out) and "list-voices" in out[-1]
    assert el.set_key_cli(prompt=lambda _: "", out=out.append) == 1
    assert len(stored) == 1


def test_fetch_subscription(monkeypatch):
    monkeypatch.undo()  # conftest stubs fetch_subscription for the doctor tests; test the real one
    data = {"tier": "starter", "character_count": 1200, "character_limit": 30000, "next_character_count_reset_unix": 1761000000}
    ok = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    assert el.fetch_subscription(KEY, transport=ok) == data
    bad = httpx.MockTransport(lambda request: httpx.Response(401, json={"detail": {"status": "invalid_api_key"}}))
    assert el.fetch_subscription(KEY, transport=bad) is None

    def offline(request):
        raise httpx.ConnectError("offline")

    assert el.fetch_subscription(KEY, transport=httpx.MockTransport(offline)) is None


# --- the sink ---------------------------------------------------------------------------------------------------


def test_sink_routes_tainted_turn_to_say_when_cloud_not_allowed():
    tts = FakeTTS()
    sink, _, _ = make_sink(tts, use_cloud_for_tainted=False)
    sink.begin_turn("tr")
    sink.write_delta("Önce temiz cümle. ")
    sink.taint_changed(["email"])
    sink.write_delta("Sonra e-postadan gelen cümle. ")
    sink.finish()
    assert [(c["text"], c["local_only"]) for c in tts.calls] == [("Önce temiz cümle.", False), ("Sonra e-postadan gelen cümle.", True)]
    sink.begin_turn("tr")  # a new turn starts clean
    sink.write_delta("Yeni tur.")
    sink.finish()
    assert tts.calls[-1]["local_only"] is False and sink.tainted is False


def test_sink_keeps_cloud_for_tainted_turn_by_default():
    tts = FakeTTS()
    sink, _, _ = make_sink(tts)
    sink.begin_turn("tr")
    sink.taint_changed(["web"])
    sink.write_delta("Web'den gelen cümle.")
    sink.finish()
    assert sink.tainted and all(c["local_only"] is False for c in tts.calls)


def test_sink_marks_system_phrases_cacheable_and_replies_not():
    tts = FakeTTS()
    sink, _, _ = make_sink(tts)
    sink.begin_turn("tr")
    sink.write_delta("Normal cümle. Bak: https://example.com/x")
    sink.finish()
    sink.notice("Error: boom")
    sink.ask_choice("Hangisi?", [("a", "A"), ("b", "B")], "a")
    flags = {c["text"]: c["cacheable"] for c in tts.calls}
    assert flags["Normal cümle."] is False
    assert flags[details_phrase("tr")] is True and flags[spoken_error("tr")] is True and flags[spoken_choice("tr")] is True


def test_sink_confirmation_question_is_local_in_tainted_turn():
    from jarvis.io.base import ConfirmRequest

    tts = FakeTTS()
    sink, _, _ = make_sink(tts, use_cloud_for_tainted=False)
    sink.begin_turn("tr")
    sink.taint_changed(["email"])
    sink.confirm(ConfirmRequest(tool="send_email", args={"to": "a@b.c"}, risk="confirm", tainted=True))
    assert tts.calls and tts.calls[0]["local_only"] is True and tts.calls[0]["cacheable"] is False


def test_say_engine_accepts_the_shared_signature():
    say = TextToSpeech(runner=FakeRunner(duration=0.01), voices=VOICES)
    say.start()
    try:
        say.speak("Merhaba.", cacheable=True, local_only=True)
        assert say.wait(timeout=3)
    finally:
        say.close()


# --- doctor -----------------------------------------------------------------------------------------------------


def names(checks):
    return {c.name: c for c in checks}


def test_health_checks_cover_elevenlabs_and_never_raise(config, paths, db, monkeypatch):
    sub = {"tier": "starter", "character_count": 1200, "character_limit": 30000, "next_character_count_reset_unix": 1761000000}
    monkeypatch.setattr(el, "get_api_key", lambda: KEY)
    monkeypatch.setattr(el, "fetch_subscription", lambda key, **kw: sub)
    config.voice.tts_engine = "elevenlabs"
    config.voice.elevenlabs_voice_id = ""
    checks = names(vhealth._elevenlabs_checks(config, paths))
    assert list(checks) == [
        "ElevenLabs key",
        "ElevenLabs voice id",
        "ElevenLabs model",
        "ElevenLabs quota",
        "ElevenLabs usage",
        "ElevenLabs cache",
    ]
    assert checks["ElevenLabs key"].ok and not checks["ElevenLabs voice id"].ok and "list-voices" in checks["ElevenLabs voice id"].detail
    assert checks["ElevenLabs model"].ok and "eleven_flash_v2_5" in checks["ElevenLabs model"].detail
    assert (
        checks["ElevenLabs quota"].ok
        and "1,200 of 30,000" in checks["ElevenLabs quota"].detail
        and "starter" in checks["ElevenLabs quota"].detail
    )
    assert checks["ElevenLabs usage"].ok and "0 characters" in checks["ElevenLabs usage"].detail
    assert "0 cached phrase" in checks["ElevenLabs cache"].detail

    # usage, last fallback and the cache are read from disk
    db.add_external_call(service="elevenlabs", operation="m", units=150000, status="ok", detail=VOICE)
    db.kv_set(el.LAST_FALLBACK_KEY, json.dumps({"ts": "2026-10-14T10:00:00.000Z", "reason": "timeout"}))
    paths.tts_cache_dir.mkdir(parents=True, exist_ok=True)
    (paths.tts_cache_dir / "abc.pcm").write_bytes(b"\x00\x00")
    checks = names(vhealth._elevenlabs_checks(config, paths))
    assert not checks["ElevenLabs usage"].ok and "BUDGET REACHED" in checks["ElevenLabs usage"].detail
    assert "last fallback 2026-10-14T10:00:00 (timeout)" in checks["ElevenLabs usage"].detail
    assert "1 cached phrase" in checks["ElevenLabs cache"].detail

    # no key, exhausted quota, subscription fetch failing: reported, never raised
    monkeypatch.setattr(el, "get_api_key", lambda: None)
    checks = names(vhealth._elevenlabs_checks(config, paths))
    assert not checks["ElevenLabs key"].ok and "set-elevenlabs-key" in checks["ElevenLabs key"].detail
    assert checks["ElevenLabs quota"].ok and "skipped" in checks["ElevenLabs quota"].detail
    monkeypatch.setattr(el, "get_api_key", lambda: KEY)
    monkeypatch.setattr(el, "fetch_subscription", lambda key, **kw: {**sub, "character_count": 30000})
    assert not names(vhealth._elevenlabs_checks(config, paths))["ElevenLabs quota"].ok

    def boom(key, **kw):
        raise RuntimeError("no network")

    monkeypatch.setattr(el, "fetch_subscription", boom)
    assert not names(vhealth._elevenlabs_checks(config, paths))["ElevenLabs quota"].ok

    # with `say` as the engine every ElevenLabs line is informational
    config.voice.tts_engine = "say"
    monkeypatch.setattr(el, "get_api_key", lambda: None)
    assert all(c.ok for c in vhealth._elevenlabs_checks(config, paths))


def test_voice_health_includes_elevenlabs_lines(config, paths, monkeypatch):
    monkeypatch.setattr(el, "get_api_key", lambda: None)
    monkeypatch.setattr(vhealth, "_agent_check", lambda enabled: [])
    checks = vhealth.health_checks(config, paths)
    assert any(c.name == "ElevenLabs key" for c in checks)


def test_service_builds_the_engine_from_config(config, paths):
    from jarvis.voice.service import VoiceService

    config.voice.tts_engine = "elevenlabs"
    service = VoiceService(config, paths)
    engine = service._build_tts()
    assert isinstance(engine, ElevenLabsTTS) and isinstance(engine.fallback, TextToSpeech)
    assert engine.db_path == paths.db_file and engine.cache_dir == paths.tts_cache_dir
    assert engine.monthly_char_budget == config.voice.elevenlabs_monthly_char_budget
    config.voice.tts_engine = "say"
    assert isinstance(VoiceService(config, paths)._build_tts(), TextToSpeech)
