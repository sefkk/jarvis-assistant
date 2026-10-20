"""The ElevenLabs API key only ever travels in the `xi-api-key` header: it never reaches
jarvis.log, audit.jsonl, usage.jsonl, an exception message or the terminal."""

from __future__ import annotations

import logging

import httpx

from jarvis.voice import elevenlabs as el
from tests.unit.test_voice_elevenlabs import KEY, Api, make_engine, quota_response


def log_text(logs) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in logs.glob("*.jsonl")) + "".join(
        path.read_text(encoding="utf-8") for path in logs.glob("jarvis.log*")
    )


def test_key_never_reaches_the_logs(paths, logs):
    """DEBUG logging, a network error whose message quotes the key, a quota error and a normal
    request: afterwards no log channel contains the key."""
    logging.getLogger("jarvis").setLevel(logging.DEBUG)
    api = Api([httpx.ConnectError(f"connection failed while sending xi-api-key: {KEY}"), quota_response(), None])
    engine = make_engine(api, paths=paths)
    engine.start()
    try:
        engine.speak("Birinci cümle.")  # network error quoting the key
        engine.speak("İkinci cümle.")  # quota error (trips the breaker)
        assert engine.wait(timeout=3)
        engine._open_until = 0.0  # let the next request through
        engine.speak("Üçüncü cümle.")  # a normal streamed request
        assert engine.wait(timeout=3)
        assert len(api.requests) == 3
        logging.getLogger("jarvis.voice.elevenlabs").debug("request headers %s", dict(engine._client.headers))
    finally:
        engine.close()
    text = log_text(logs)
    assert "tts_fallback" in text and "elevenlabs" in text  # the channels were written
    assert KEY not in text
    assert "sk_***" in text  # the redaction, not the key, is what the debug line left behind


def test_api_error_message_never_contains_the_key():
    response = httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": f"bad key {KEY}"}})
    exc = el._error_from_response(response)
    assert KEY not in str(exc) and "invalid_api_key" in str(exc) and exc.status_code == 401
    assert KEY not in el._safe_error(RuntimeError(f"leak {KEY}"))


def test_set_key_cli_never_echoes_the_key(monkeypatch):
    stored = {}
    monkeypatch.setattr(el.secrets, "set_secret", lambda name, value: stored.__setitem__(name, value))
    out: list[str] = []
    assert el.set_key_cli(prompt=lambda _: KEY, out=out.append) == 0
    assert stored[el.secrets.ELEVENLABS_API_KEY] == KEY
    assert all(KEY not in line for line in out)


def test_subscription_and_voice_errors_do_not_leak_the_key(monkeypatch, logs):
    def leaky(request):
        raise httpx.ConnectError(f"refused {request.headers['xi-api-key']}")

    transport = httpx.MockTransport(leaky)
    assert el.fetch_subscription(KEY, transport=transport) is None
    out: list[str] = []
    assert el.list_voices_cli(out.append, api_key=KEY, transport=transport) == 1
    assert all(KEY not in line for line in out)
    assert KEY not in log_text(logs)
