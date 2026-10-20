"""API timeouts: the granular timeouts reach the SDK client from `[loop]`, an
`APITimeoutError` is classified by the limit it hit (connect / first_token / stall), a stalled
stream is retried without repeating the text already shown, and the final failure reaches the user
as one short categorised message (Turkish by default) with the request id, spoken in voice mode."""

from __future__ import annotations

import itertools
from io import StringIO
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from rich.console import Console

from jarvis.assistant import Assistant, api_timeout, make_client
from jarvis.brain import Brain
from jarvis.config import LoopConfig, load_config
from jarvis.exceptions import API_ERROR_CATEGORIES, JarvisAPIError
from jarvis.io.text import TerminalOutput
from jarvis.io.voice import VoiceOutputSink, spoken_error
from jarvis.observability.timing import TurnTimer
from jarvis.observability.usage import UsageTracker
from jarvis.tools.registry import ToolRegistry
from tests.conftest import RecordingSink, api_error, connection_error, text_message
from tests.unit.test_voice_sink import FakeTTS

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def timeout_error(cause: Exception | None) -> anthropic.APITimeoutError:
    """What the SDK raises: `APITimeoutError` chained from the httpx timeout that fired."""
    exc = anthropic.APITimeoutError(request=REQUEST)
    exc.__cause__ = cause
    return exc


# --- the configured timeouts reach the client -------------------------------------------------------------


def test_defaults_are_granular_not_the_sdk_five_seconds():
    t = api_timeout(LoopConfig())
    assert (t.connect, t.read, t.write, t.pool) == (10.0, 60.0, 60.0, 10.0)
    assert t.connect > anthropic.DEFAULT_TIMEOUT.connect  # 5 s: the limit every logged timeout hit


def test_config_values_reach_the_sdk_client():
    loop = LoopConfig(api_connect_timeout_s=7.5, api_read_timeout_s=45, api_write_timeout_s=20)
    client = make_client("sk-ant-test", loop=loop)
    assert client.timeout == httpx2.Timeout(connect=7.5, read=45.0, write=20.0, pool=7.5)
    assert client.max_retries == 0  # the Brain owns the retries


def test_config_toml_reads_and_validates_the_timeouts(paths):
    text = paths.config_file.read_text(encoding="utf-8")
    assert "api_connect_timeout_s = 10.0" in text and "api_read_timeout_s = 60.0" in text
    assert load_config(paths.config_file).loop.api_connect_timeout_s == 10.0
    for key, bad in (("api_connect_timeout_s", 0), ("api_connect_timeout_s", 500), ("api_read_timeout_s", -1)):
        with pytest.raises(ValueError):
            LoopConfig(**{key: bad})


def test_assistant_builds_its_client_with_the_config_timeouts(config, paths, monkeypatch):
    seen = {}

    def fake_make_client(api_key=None, *, loop=None):
        seen["loop"] = loop
        from tests.conftest import FakeClient

        return FakeClient([])

    monkeypatch.setattr("jarvis.assistant.make_client", fake_make_client)
    Assistant(config, paths, registry=ToolRegistry())
    assert seen["loop"] is config.loop


# --- timeout phase classification ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cause, ttft, phase",
    [
        (httpx2.ConnectTimeout("connect"), None, "connect"),
        (httpx2.ReadTimeout("read"), None, "first_token"),
        (httpx2.ReadTimeout("read"), 812.0, "stall"),
        (httpx2.WriteTimeout("write"), None, "write"),
        (httpx2.PoolTimeout("pool"), None, "pool"),
        (None, None, "connect"),  # no chained cause: nothing arrived, so the connection is the suspect
        (None, 10.0, "stall"),
    ],
)
def test_timeout_phase(cause, ttft, phase):
    assert Brain.timeout_phase(timeout_error(cause), ttft_ms=ttft) == phase


def test_timeout_phase_is_none_for_other_errors():
    assert Brain.timeout_phase(connection_error(anthropic.APIConnectionError), ttft_ms=None) is None
    assert Brain.timeout_phase(api_error(anthropic.InternalServerError, 500), ttft_ms=None) is None


# --- the Brain: retries, stall detection, the final error ------------------------------------------------------


class ScriptedStream:
    """A stream that yields `events` and then raises `error` (None: ends normally with `message`)."""

    def __init__(self, events: list[Any], error: Exception | None = None, message=None) -> None:
        self.events, self.error, self.message = events, error, message

    def __enter__(self):
        if not self.events and self.error is not None:
            raise self.error  # failed before any byte came back (connect / first token)
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield from self.events
        if self.error is not None:
            raise self.error

    def get_final_message(self):
        return self.message


class ScriptedClient:
    def __init__(self, streams: list[ScriptedStream]) -> None:
        self.streams = list(streams)
        self.calls = 0
        self.messages = self

    def stream(self, **params):
        self.calls += 1
        return self.streams.pop(0)


def text_event(text: str):
    return SimpleNamespace(type="text", text=text, snapshot=text)


_n = itertools.count()


def make_brain(config, db, streams, sleeps):
    client = ScriptedClient(streams)
    brain = Brain(client, config, ToolRegistry(), db, UsageTracker(config, db), sleep=sleeps.append, rng=lambda: 0.0)
    turn = f"to{next(_n)}"
    db.start_session(turn, "m", "d")
    db.start_turn(turn, turn, "hi")
    return brain, client, turn


def run_turn(brain, turn, sink):
    return brain.run_turn(
        system="sys", messages=[{"role": "user", "content": "hi"}], sink=sink, turn_id=turn, timer=TurnTimer(turn, brain.db)
    )


def test_connect_timeouts_are_retried_with_backoff_then_classified(config, db, logs):
    sleeps: list[float] = []
    brain, client, turn = make_brain(config, db, [ScriptedStream([], timeout_error(httpx2.ConnectTimeout("x"))) for _ in range(3)], sleeps)
    sink = RecordingSink()
    with pytest.raises(JarvisAPIError) as exc:
        run_turn(brain, turn, sink)
    err = exc.value
    assert err.category == "timeout" and err.phase == "connect" and err.attempts == 3
    assert err.connect_timeout_s == config.loop.api_connect_timeout_s and err.read_timeout_s == config.loop.api_read_timeout_s
    assert err.request_id is None  # the request never reached the API
    # Never an instant retry: exponential backoff with the configured base.
    assert sleeps == [1.0, 2.0] and client.calls == 3
    assert all("connect sınırı (10 s)" in n and "yeniden deneniyor" in n for n in sink.notices) and len(sink.notices) == 2
    assert "(2/3)" in sink.notices[0] and "(3/3)" in sink.notices[1]
    rows = db.conn.execute("SELECT status, ttft_ms FROM api_calls ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["error:APITimeoutError:connect"] * 3 and all(r["ttft_ms"] is None for r in rows)


def test_stalled_stream_is_detected_retried_and_text_not_repeated(config, db, logs):
    sleeps: list[float] = []
    stalled = ScriptedStream([text_event("Merhaba, "), text_event("bugün ")], timeout_error(httpx2.ReadTimeout("silence")))
    recovered = ScriptedStream(
        [text_event("Merhaba, bugün "), text_event("hava güzel.")], message=text_message("Merhaba, bugün hava güzel.")
    )
    brain, client, turn = make_brain(config, db, [stalled, recovered], sleeps)
    sink = RecordingSink()
    outcome = run_turn(brain, turn, sink)

    assert outcome.text == "Merhaba, bugün hava güzel." and sink.text == "Merhaba, bugün hava güzel."
    assert client.calls == 2 and sleeps == [1.0]
    assert sink.notices == ["API zaman aşımı: stall sınırı (60 s) aşıldı, 1.0 sn sonra yeniden deneniyor (2/3)..."]  # Turkish test config
    rows = db.conn.execute("SELECT status, ttft_ms FROM api_calls ORDER BY id").fetchall()
    assert rows[0]["status"] == "error:APITimeoutError:stall" and rows[0]["ttft_ms"] is not None
    assert rows[1]["status"] == "ok"


def test_read_timeout_before_the_first_token_is_its_own_phase(config, db, logs):
    sleeps: list[float] = []
    brain, _, turn = make_brain(config, db, [ScriptedStream([], timeout_error(httpx2.ReadTimeout("x"))) for _ in range(3)], sleeps)
    sink = RecordingSink()
    with pytest.raises(JarvisAPIError) as exc:
        run_turn(brain, turn, sink)
    assert exc.value.phase == "first_token"
    assert all("first_token sınırı (60 s)" in n for n in sink.notices) and len(sink.notices) == 2
    assert db.conn.execute("SELECT status FROM api_calls").fetchone()["status"] == "error:APITimeoutError:first_token"


def test_timeout_failure_is_logged_with_phase_and_silence(config, db, logs):
    sleeps: list[float] = []
    brain, _, turn = make_brain(config, db, [ScriptedStream([], timeout_error(httpx2.ConnectTimeout("x")))] * 3, sleeps)
    with pytest.raises(JarvisAPIError):
        run_turn(brain, turn, RecordingSink())
    import json

    lines = [json.loads(l) for l in (logs / "jarvis.log").read_text().splitlines() if '"api call failed' in l]
    assert lines and lines[0]["data"]["phase"] == "connect" and lines[0]["data"]["cause"] == "ConnectTimeout"
    assert lines[0]["data"]["timeouts_s"] == {"connect": 10.0, "read": 60.0}
    assert "silent_ms" in lines[0]["data"] and lines[0]["data"]["events"] == 0


# --- categories and the user's message -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, category",
    [
        (dict(error_type="APITimeoutError", phase="connect"), "timeout"),
        (dict(error_type="APIConnectionError"), "network"),
        (dict(status_code=529, error_type="overloaded_error"), "overload"),
        (dict(status_code=429, error_type="rate_limit_error"), "overload"),
        (dict(status_code=401, error_type="authentication_error"), "auth"),
        (dict(status_code=400, error_type="invalid_request_error"), "api"),
        (dict(status_code=500, error_type="api_error"), "api"),
    ],
)
def test_error_category(kwargs, category):
    err = JarvisAPIError("x", attempts=3, **kwargs)
    assert err.category == category and category in API_ERROR_CATEGORIES


def test_user_message_turkish_by_category():
    connect = JarvisAPIError("x", error_type="APITimeoutError", phase="connect", attempts=3, connect_timeout_s=10, read_timeout_s=60)
    msg = connect.user_message("tr")
    assert msg.startswith("Hata (zaman aşımı)") and "10 s" in msg and "3 denemede" in msg and "İstek kimliği" not in msg

    stall = JarvisAPIError("x", error_type="APITimeoutError", phase="stall", attempts=2, connect_timeout_s=10, read_timeout_s=60)
    assert "60 s" in stall.user_message("tr") and "yarıda kesildi" in stall.user_message("tr")

    busy = JarvisAPIError("x", status_code=529, error_type="overloaded_error", request_id="req_42", attempts=3)
    msg = busy.user_message("tr")
    assert msg.startswith("Hata (API yoğun)") and "529" in msg and msg.endswith("İstek kimliği: req_42.")

    assert JarvisAPIError("x", error_type="APIConnectionError", attempts=3).user_message("tr").startswith("Hata (ağ)")
    assert "401" in JarvisAPIError("x", status_code=401, error_type="authentication_error").user_message("tr")
    assert "status 400, invalid_request_error" in JarvisAPIError("x", status_code=400, error_type="invalid_request_error").user_message(
        "tr"
    )


def test_user_message_english_and_unknown_language_falls_back_to_the_main_language():
    from jarvis import i18n

    err = JarvisAPIError("x", error_type="APITimeoutError", phase="connect", attempts=3, connect_timeout_s=10, read_timeout_s=60)
    assert err.user_message("en").startswith("Error (timeout)") and "10 s" in err.user_message("en")
    i18n.set_language("tr")
    assert err.user_message("de") == err.user_message("tr") == err.user_message()
    assert err.spoken_message() == err.spoken_message("tr") == "Sunucuya bağlanamadım, biraz sonra tekrar dener misin?"
    i18n.set_language("en")
    assert err.user_message("de") == err.user_message("en") and err.spoken_message().startswith("I could not reach")


def test_spoken_message_is_one_short_sentence_per_category():
    for category, kwargs in (
        ("timeout", dict(error_type="APITimeoutError")),
        ("network", dict(error_type="APIConnectionError")),
        ("overload", dict(status_code=529)),
        ("auth", dict(status_code=401)),
        ("api", dict(status_code=400)),
    ):
        err = JarvisAPIError("x", **kwargs)
        assert err.category == category
        for lang in ("tr", "en"):
            spoken = err.spoken_message(lang)
            assert spoken and len(spoken) < 90 and spoken.count(".") + spoken.count("?") == 1
    assert JarvisAPIError("x", status_code=400).spoken_message("tr") == spoken_error("tr")


def test_one_line_carries_the_phase_for_the_log():
    err = JarvisAPIError("x", error_type="APITimeoutError", phase="connect", attempts=3)
    assert err.one_line() == "API call failed after 3 attempt(s): APITimeoutError (connect)"


# --- the sinks -------------------------------------------------------------------------------------------------


class ErrorSink(RecordingSink):
    def __init__(self):
        super().__init__()
        self.errors: list[tuple[str, str | None]] = []

    def error(self, text, *, spoken=None):
        self.errors.append((text, spoken))


def test_assistant_reports_the_final_failure_through_sink_error(config, paths, logs):
    from tests.conftest import FakeClient

    client = FakeClient([timeout_error(httpx2.ConnectTimeout("x"))] * config.loop.api_max_attempts)
    assistant = Assistant(config, paths, client=client, registry=ToolRegistry())
    assistant.brain._sleep = lambda s: None
    sink = ErrorSink()
    result = assistant.handle("merhaba", sink)

    assert result.error == "API call failed after 3 attempt(s): APITimeoutError (connect)"
    assert len(sink.errors) == 1
    text, spoken = sink.errors[0]
    assert text.startswith("Hata (zaman aşımı)") and "10 s" in text
    assert spoken == "Sunucuya bağlanamadım, biraz sonra tekrar dener misin?"
    assert sink.finished == 1
    import json

    failed = [json.loads(l) for l in (logs / "jarvis.log").read_text().splitlines() if '"turn failed' in l]
    assert failed and failed[0]["data"]["category"] == "timeout" and failed[0]["data"]["phase"] == "connect"
    assert "request_id" in failed[0]["data"]
    assistant.close()


def test_default_sink_error_degrades_to_a_notice():
    sink = RecordingSink()
    sink.error("Hata (ağ): ...", spoken="İnternet yok.")
    assert sink.notices == ["Hata (ağ): ..."]


def test_terminal_sink_prints_the_error_in_red():
    out = StringIO()
    console = Console(file=out, force_terminal=True, color_system="standard", width=200)
    sink = TerminalOutput(console)
    sink.write_delta("yarım kalan cevap")
    sink.error("Hata (zaman aşımı): bağlanılamadı.", spoken="ignored")
    text = out.getvalue()
    assert "Hata (zaman aşımı): bağlanılamadı." in text and "\x1b[1;31m" in text
    assert text.index("yarım kalan cevap") < text.index("Hata")  # on its own line after the partial reply


def test_voice_sink_speaks_the_category_sentence_once(config):
    tts = FakeTTS()
    events: list[str] = []
    sink = VoiceOutputSink(tts, confirm_phrases=["onaylıyorum"], on_event=events.append, language="tr")
    sink.begin_turn("tr")
    sink.error("Hata (zaman aşımı): ...", spoken="Sunucuya bağlanamadım, biraz sonra tekrar dener misin?")
    assert tts.spoken == ["Sunucuya bağlanamadım, biraz sonra tekrar dener misin?"]
    assert tts.calls[0]["cacheable"] is True  # a constant system phrase: the cloud engine may cache it
    assert events == ["Hata (zaman aşımı): ..."]

    sink.begin_turn("tr")
    sink.error("Error: something went wrong")  # no category sentence: the generic one
    assert tts.spoken[-1] == spoken_error("tr")

    sink.begin_turn("en")
    sink.cancel()
    sink.error("Error: x", spoken="never")
    assert "never" not in tts.spoken  # barge-in: a cancelled turn stays silent
