"""ElevenLabs text to speech: the cloud voice in front of the `say` engine.

`ElevenLabsTTS` has the same interface as `TextToSpeech` (voice/tts.py), so `VoiceService` and
`VoiceOutputSink` work with either engine. One worker thread takes sentences from a queue; for each
sentence it streams `POST /v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000` and writes
the chunks to a `sounddevice.RawOutputStream` as they arrive, so speech starts on the first chunk.
The request for the next queued sentence is started while the current one plays (one ahead), which
hides the round trip between sentences.

Why HTTP streaming per sentence and not the websocket `stream-input` endpoint: the websocket
buffers text until its chunk_length_schedule (120-290 characters by default) fills, which is a
poor fit when whole sentences are already available from the sentence splitter; one HTTP request
per sentence keeps the sentence pipeline, barge-in (close the response, abort the player) and the
per-sentence accounting simple.

Fallback to `say` (never silence): no key, no voice id, an HTTP or network error, no first chunk
within `elevenlabs_first_chunk_timeout_s`, quota exhausted, the monthly character budget, a
tainted turn with `elevenlabs_use_for_tainted = false` (`local_only=True`), or an open circuit
breaker (`FAILURE_THRESHOLD` consecutive failures keep `say` for `COOLDOWN_S`; quota and an
invalid key trip it at once). Every fallback is logged, audited and remembered in the kv table so
`jarvis doctor` shows it.

Text: every sentence is rewritten in its spoken form by `voice/normalize.py`
in `speak()` (numbers, times, dates, units, symbols as words; Flash v2.5 read Turkish digits as
garbage), the request carries `language_code` for the flash/turbo models and
`apply_text_normalization` from `voice.elevenlabs_text_normalization` as the safety net. A 400 that
names the normalization flag turns it off for the rest of the process and retries the sentence once.

Privacy: reply text is sent to ElevenLabs (README). The API key lives in the Keychain, only ever
travels in the `xi-api-key` header and is never logged (the log channels also mask `sk_...`).

# TODO(setup): Telegram voice replies with ElevenLabs (MANUAL_STEPS.md, Optional). Not implemented yet.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from jarvis.i18n import t
from jarvis.observability import logging_setup
from jarvis.utils import secrets
from jarvis.utils.timeutil import now_iso, now_local
from jarvis.voice.normalize import normalize_for_speech
from jarvis.voice.tts import TextToSpeech, detect_language

log = logging.getLogger("jarvis.voice.elevenlabs")

API_BASE = "https://api.elevenlabs.io"
SERVICE = "elevenlabs"
DEFAULT_MODEL = "eleven_flash_v2_5"
OUTPUT_FORMAT = "pcm_24000"  # 16-bit signed little-endian mono 24 kHz (pcm_44100 needs the Pro tier)
SAMPLE_RATE = 24000
PLAY_CHUNK_BYTES = 4800  # 100 ms of audio: cached playback stays responsive to barge-in
SAMPLE_BYTES = 2  # pcm_24000 is 16-bit mono
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
LAST_FALLBACK_KEY = "elevenlabs:last_fallback"
# `language_code` is accepted by the flash/turbo models; multilingual_v2 rejects it.
MODELS_WITHOUT_LANGUAGE_CODE = {"eleven_multilingual_v2", "eleven_multilingual_v1", "eleven_monolingual_v1"}


def budget_notice(language: str | None = None) -> str:
    """Spoken once when the monthly character budget is used up and `say` takes over."""
    return t("voice.speech.elevenlabs_budget", language)


def get_api_key() -> str | None:
    """The key from the Keychain (`jarvis voice set-elevenlabs-key`); None when not set."""
    return secrets.get_secret(secrets.ELEVENLABS_API_KEY)


def start_of_month_utc() -> str:
    """The first of the local calendar month as the UTC ISO prefix used in external_calls.created_at."""
    first = now_local().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def cache_key(model: str, voice_id: str, text: str) -> str:
    return hashlib.sha256(f"{model}|{voice_id}|{OUTPUT_FORMAT}|{text}".encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> str:
    """An exception as text with every secret pattern masked (httpx errors carry the URL, not headers)."""
    return logging_setup.redact(f"{type(exc).__name__}: {exc}"[:300])


class ElevenLabsError(Exception):
    """A non-200 answer. Carries the status code and the API's `detail.status`; never the key."""

    def __init__(self, status_code: int, detail_status: str = "", message: str = "") -> None:
        self.status_code = status_code
        self.detail_status = detail_status
        text = f"HTTP {status_code}"
        if detail_status:
            text += f" {detail_status}"
        if message:
            text += f": {message}"
        super().__init__(logging_setup.redact(text))

    @property
    def quota(self) -> bool:
        return self.status_code == 429 or "quota" in self.detail_status.lower()

    @property
    def reason(self) -> str:
        if self.quota:
            return "quota"
        if self.status_code == 401:
            return "auth"
        return f"http_{self.status_code}"


def _error_from_response(response: httpx.Response) -> ElevenLabsError:
    status, message = "", ""
    try:
        response.read()
        detail = response.json().get("detail")
        if isinstance(detail, dict):
            status = str(detail.get("status") or "")
            message = str(detail.get("message") or "")[:200]
        elif isinstance(detail, str):
            message = detail[:200]
    except Exception:  # not JSON: the status code is enough
        pass
    return ElevenLabsError(response.status_code, status, message)


def _default_player():
    import sounddevice  # noqa: PLC0415 - optional `voice` extra

    return sounddevice.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16")


@dataclass
class _Item:
    text: str
    language: str
    generation: int
    cacheable: bool
    local_only: bool


class _Fetch:
    """One streaming request on its own thread. The worker waits for `ready` (first chunk or error)
    with the first-chunk deadline, then iterates the rest of the audio itself. `cancel()` closes the
    response from any thread (barge-in, timeout)."""

    def __init__(self, client: httpx.Client, request: httpx.Request, item: _Item) -> None:
        self.item = item
        self.started = time.monotonic()
        self.first_chunk_ms: float | None = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self._response: httpx.Response | None = None
        self._first = b""
        self._rest: Iterator[bytes] | None = None
        self._cancelled = False
        self._lock = threading.Lock()
        self._client, self._request = client, request
        threading.Thread(target=self._run, name="jarvis-elevenlabs-fetch", daemon=True).start()

    def _run(self) -> None:
        try:
            response = self._client.send(self._request, stream=True)
            with self._lock:
                self._response = response
                if self._cancelled:
                    response.close()
                    return
            if response.status_code != 200:
                self.error = _error_from_response(response)
                response.close()
                return
            self._rest = response.iter_bytes()
            for chunk in self._rest:
                if chunk:
                    self._first = chunk
                    break
            self.first_chunk_ms = (time.monotonic() - self.started) * 1000.0
        except BaseException as exc:  # noqa: BLE001 - reported to the worker, which falls back
            self.error = exc
        finally:
            self.ready.set()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # already closed
                pass

    def chunks(self) -> Iterator[bytes]:
        if self._first:
            yield self._first
        if self._rest is not None:
            for chunk in self._rest:
                if chunk:
                    yield chunk
        self.cancel()  # closes the response (the stream is consumed)


class ElevenLabsTTS:
    """ElevenLabs streaming TTS with `say` as the fallback; same interface as `TextToSpeech`."""

    FAILURE_THRESHOLD = 3
    COOLDOWN_S = 300.0

    def __init__(
        self,
        *,
        voice_id: str,
        model: str = DEFAULT_MODEL,
        first_chunk_timeout_s: float = 3.0,
        monthly_char_budget: int = 0,
        default_language: str = "tr",
        text_normalization: str = "on",
        fallback: TextToSpeech | None = None,
        api_key: str | None | Callable[[], str | None] = None,
        db_path: Path | str | None = None,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        player_factory: Callable[[], Any] = _default_player,
        on_first_audio: Callable[[], None] | None = None,
        on_sentence: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.voice_id = voice_id.strip()
        self.model = model.strip() or DEFAULT_MODEL
        self.first_chunk_timeout_s = float(first_chunk_timeout_s)
        self.monthly_char_budget = int(monthly_char_budget)
        self.default_language = default_language
        # ElevenLabs' own `apply_text_normalization` ("on" | "auto" | "off"): the safety net behind
        # voice/normalize.py, which already turns digits into words before the request is built.
        self.text_normalization = text_normalization if text_normalization in ("on", "auto", "off") else "auto"
        self.fallback = fallback or TextToSpeech(default_language=default_language)
        self.fallback.on_first_audio = self._fire_first_audio
        self.fallback.on_sentence = None  # this engine reports the sentence itself
        self._key_source = api_key if callable(api_key) else (lambda value=api_key: value if value is not None else get_api_key())
        self._api_key: str | None = None
        self.db_path = Path(db_path) if db_path else None
        self.cache_dir = cache_dir
        self._transport = transport
        self.player_factory = player_factory
        self.on_first_audio = on_first_audio
        self.on_sentence = on_sentence
        self.clock = clock

        self._client: httpx.Client | None = None
        self._db: Any = None  # opened on the worker thread (SQLite connections do not cross threads)
        self._player_obj: Any = None
        self._queue: deque[_Item] = deque()
        self._cond = threading.Condition()
        self._current: _Item | None = None
        self._fetch: _Fetch | None = None
        self._prefetch: _Fetch | None = None
        self._inflight: set[_Fetch] = set()  # started, not yet recorded: counts against the budget
        self._generation = 0
        self._first_audio_pending = True
        self._closing = False
        self._worker: threading.Thread | None = None
        self._failures = 0
        self._open_until = 0.0
        self._budget_announced = False

    # -- lifecycle --

    def start(self) -> None:
        if self._worker is not None:
            return
        self._api_key = self._key_source()
        if not self._api_key:
            log.warning("no ElevenLabs API key in the Keychain; every sentence falls back to say (`jarvis voice set-elevenlabs-key`)")
        if not self.voice_id:
            log.warning("elevenlabs_voice_id is empty; every sentence falls back to say (`jarvis voice list-voices`)")
        self._client = httpx.Client(
            base_url=API_BASE, headers={"xi-api-key": self._api_key or ""}, timeout=HTTP_TIMEOUT, transport=self._transport
        )
        self.fallback.start()
        self._closing = False
        self._worker = threading.Thread(target=self._run, name="jarvis-elevenlabs", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self.stop()
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None
        self.fallback.close()
        if self._client is not None:
            self._client.close()
            self._client = None

    def begin_turn(self) -> None:
        with self._cond:
            self._first_audio_pending = True
        self.fallback.begin_turn()

    # -- speaking --

    def speak(self, text: str, language: str | None = None, *, cacheable: bool = False, local_only: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        language = language or detect_language(text, self.default_language)
        text = normalize_for_speech(text, language)  # digits and symbols as words (voice/normalize.py)
        if not text:
            return
        with self._cond:
            self._queue.append(_Item(text, language, self._generation, cacheable, local_only))
            self._cond.notify_all()

    def stop(self) -> None:
        """Barge-in: drop the queue, abort playback, close the in-flight and prefetched responses."""
        with self._cond:
            self._generation += 1
            self._queue.clear()
            fetches = [f for f in (self._fetch, self._prefetch) if f is not None]
            if self._prefetch is not None:
                self._inflight.discard(self._prefetch)
            self._prefetch = None
            player = self._player_obj
            self._cond.notify_all()
        for fetch in fetches:
            fetch.cancel()
        if player is not None:
            try:
                player.abort()
            except Exception as exc:  # the stream may already be stopped
                log.debug("player abort failed: %s", exc)
        self.fallback.stop()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._cond:
                busy = bool(self._queue) or self._current is not None
            if not busy and not self.fallback.is_speaking:
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            time.sleep(0.01 if remaining is None else min(0.01, remaining))

    @property
    def is_speaking(self) -> bool:
        with self._cond:
            busy = bool(self._queue) or self._current is not None
        return busy or self.fallback.is_speaking

    @property
    def generation(self) -> int:
        return self._generation

    def set_default_language(self, language: str) -> None:
        self.default_language = language
        self.fallback.set_default_language(language)

    @property
    def circuit_open(self) -> bool:
        return self.clock() < self._open_until

    # -- worker --

    def _run(self) -> None:
        self._open_db()
        self._open_player()
        try:
            while True:
                with self._cond:
                    while not self._queue and not self._closing:
                        self._cond.wait(timeout=0.5)
                    if self._closing and not self._queue:
                        return
                    item = self._queue.popleft()
                    if item.generation != self._generation:
                        self._cond.notify_all()
                        continue
                    self._current = item
                try:
                    self._speak_item(item)
                except Exception as exc:  # the last net: the user still gets the sentence
                    log.exception("ElevenLabs sentence failed unexpectedly")
                    self._note_fallback("internal", item, _safe_error(exc))
                    self._speak_with_say(item)
                finally:
                    with self._cond:
                        self._current = None
                        self._cond.notify_all()
        finally:
            with self._cond:
                prefetch, self._prefetch = self._prefetch, None
            if prefetch is not None:
                prefetch.cancel()
            self._close_player()
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    def _open_db(self) -> None:
        if self.db_path is None:
            return
        try:
            from jarvis.observability.db import Database  # noqa: PLC0415

            self._db = Database(self.db_path)
        except Exception as exc:
            log.warning("ElevenLabs accounting disabled, cannot open the database: %s", _safe_error(exc))
            self._db = None

    def _open_player(self) -> None:
        if self._player_obj is not None:
            return
        try:
            self._player_obj = self.player_factory()
        except Exception as exc:  # no sounddevice or no output device: every sentence falls back
            log.warning("audio output unavailable, falling back to say: %s", _safe_error(exc))
            self._player_obj = None

    def _close_player(self) -> None:
        player, self._player_obj = self._player_obj, None
        if player is not None:
            try:
                player.close()
            except Exception as exc:
                log.debug("player close failed: %s", exc)

    def _player(self):
        self._open_player()
        if self._player_obj is None:
            raise RuntimeError("no audio output")
        self._player_obj.start()
        return self._player_obj

    def _stale(self, item: _Item) -> bool:
        return item.generation != self._generation

    def _fire_first_audio(self) -> None:
        with self._cond:
            fire, self._first_audio_pending = self._first_audio_pending, False
        if fire and self.on_first_audio is not None:
            try:
                self.on_first_audio()
            except Exception:
                log.exception("on_first_audio hook failed")

    def _report_sentence(self, item: _Item, voice: str) -> None:
        if self.on_sentence is not None:
            try:
                self.on_sentence(item.text, voice)
            except Exception:
                log.exception("on_sentence hook failed")

    def _speak_item(self, item: _Item) -> None:
        reason = self._precheck(item)
        if reason is not None:
            self._report_sentence(item, self.fallback.voice_for(item.language) or "say")
            self._record(item, status="fallback", reason=reason)
            self._note_fallback(reason, item)
            if reason == "budget" and not self._budget_announced:
                self._budget_announced = True
                self.fallback.speak(budget_notice(item.language), item.language)
            self._speak_with_say(item)
            return
        if item.cacheable:
            audio = self._cache_read(item)
            if audio:
                self._report_sentence(item, "elevenlabs (cached)")
                started = time.monotonic()
                self._play_bytes(item, audio)
                self._record(item, status="ok", cached=True, duration_ms=(time.monotonic() - started) * 1000.0)
                return
        self._report_sentence(item, "elevenlabs")
        fetch = self._take_fetch(item)
        status, reason, audio = self._play_fetch(item, fetch)
        if status == "error" and self._normalization_rejected(fetch.error):
            with self._cond:
                self._fetch = None
                self._inflight.discard(fetch)
            fetch = self._take_fetch(item)  # once more, without the flag
            status, reason, audio = self._play_fetch(item, fetch)
        duration_ms = (time.monotonic() - fetch.started) * 1000.0
        with self._cond:
            self._fetch = None
            self._inflight.discard(fetch)
        if status in ("ok", "cancelled"):
            self._failures = 0
            self._record(
                item,
                status="ok",
                duration_ms=duration_ms,
                first_chunk_ms=fetch.first_chunk_ms,
                reason="cancelled" if status == "cancelled" else None,
            )
            if status == "ok" and item.cacheable and audio:
                self._cache_write(item, audio)
            return
        self._record(item, status=status, duration_ms=duration_ms, first_chunk_ms=fetch.first_chunk_ms, reason=reason)
        self._note_failure(trip_now=reason in ("quota", "auth"))
        self._note_fallback(reason, item, _safe_error(fetch.error) if fetch.error is not None else "")
        if not self._stale(item):
            self._report_sentence(item, f"{self.fallback.voice_for(item.language) or 'say'} (fallback: {reason})")
            self._speak_with_say(item)

    def _normalization_rejected(self, error: BaseException | None) -> bool:
        """A 400 that names `apply_text_normalization` (a model or plan that does not take it): drop the
        flag for the rest of the process so the sentence is retried, and the next ones sent, without it."""
        if self.text_normalization == "auto" or not isinstance(error, ElevenLabsError) or error.status_code != 400:
            return False
        if "normaliz" not in str(error).lower():
            return False
        log.warning(
            "ElevenLabs rejected apply_text_normalization=%s for %s; sending without it from now on", self.text_normalization, self.model
        )
        self.text_normalization = "auto"
        return True

    def _precheck(self, item: _Item) -> str | None:
        """Why this sentence goes straight to `say`, or None when ElevenLabs may be tried."""
        if item.local_only:
            return "local_only"
        if not self.voice_id:
            return "no_voice_id"
        if not self._api_key:
            return "no_key"
        if self.circuit_open:
            return "circuit_open"
        if self.monthly_char_budget > 0 and self._db is not None:
            try:
                used = self._db.external_units_since(SERVICE, start_of_month_utc())
            except Exception as exc:
                log.warning("budget check failed: %s", _safe_error(exc))
                used = 0
            with self._cond:
                inflight = sum(len(f.item.text) for f in self._inflight)
            if used + inflight + len(item.text) > self.monthly_char_budget:
                return "budget"
        return None

    def _build_request(self, item: _Item) -> httpx.Request:
        assert self._client is not None
        body: dict[str, Any] = {"text": item.text, "model_id": self.model}
        if self.model not in MODELS_WITHOUT_LANGUAGE_CODE and item.language in ("tr", "en"):
            body["language_code"] = item.language
        if self.text_normalization != "auto":  # "auto" is the API default
            body["apply_text_normalization"] = self.text_normalization
        return self._client.build_request(
            "POST", f"/v1/text-to-speech/{self.voice_id}/stream", params={"output_format": OUTPUT_FORMAT}, json=body
        )

    def _start_fetch(self, item: _Item) -> _Fetch:
        assert self._client is not None
        fetch = _Fetch(self._client, self._build_request(item), item)
        with self._cond:
            self._inflight.add(fetch)
        return fetch

    def _take_fetch(self, item: _Item) -> _Fetch:
        with self._cond:
            prefetched, self._prefetch = self._prefetch, None
        if prefetched is not None and prefetched.item is item:
            fetch = prefetched
        else:
            if prefetched is not None:
                prefetched.cancel()
                with self._cond:
                    self._inflight.discard(prefetched)
            fetch = self._start_fetch(item)
        with self._cond:
            self._fetch = fetch
        return fetch

    def _prefetch_next(self) -> None:
        """Start the request for the next queued sentence (one ahead) while this one plays."""
        with self._cond:
            if self._prefetch is not None or not self._queue:
                return
            nxt = self._queue[0]
        if nxt.generation != self._generation or self._precheck(nxt) is not None:
            return
        if nxt.cacheable and self._cache_path(nxt).is_file():
            return
        fetch = self._start_fetch(nxt)
        with self._cond:
            if nxt.generation != self._generation:
                self._inflight.discard(fetch)
                fetch.cancel()
                return
            self._prefetch = fetch

    def _play_fetch(self, item: _Item, fetch: _Fetch) -> tuple[str, str, bytes | None]:
        """Wait for the first chunk and play the stream. Returns (status, reason, audio)."""
        remaining = fetch.started + self.first_chunk_timeout_s - time.monotonic()
        if not fetch.ready.wait(max(0.0, remaining)):
            fetch.cancel()
            return "timeout", "timeout", None
        if fetch.error is not None:
            return "error", self._classify(fetch.error), None
        if self._stale(item):
            fetch.cancel()
            return "cancelled", "", None
        audio = bytearray()
        try:
            player = self._player()
        except Exception:
            fetch.cancel()
            return "error", "player", None
        self._fire_first_audio()
        self._prefetch_next()
        carry = b""  # HTTP chunks split anywhere; the int16 player only accepts whole 2-byte samples
        try:
            for chunk in fetch.chunks():
                if self._stale(item):
                    fetch.cancel()
                    return "cancelled", "", None
                data = carry + chunk
                cut = len(data) - (len(data) % SAMPLE_BYTES)
                data, carry = data[:cut], data[cut:]
                if data:
                    player.write(data)
                    audio += data
            if carry:  # a truncated last sample: complete it with silence rather than drop it
                data = carry + b"\x00" * (SAMPLE_BYTES - len(carry))
                player.write(data)
                audio += data
            player.stop()
        except Exception as exc:
            if self._stale(item):
                return "cancelled", "", None
            try:
                player.abort()
            except Exception:
                pass
            fetch.error = exc
            return "error", "stream", None
        return "ok", "", bytes(audio)

    def _play_bytes(self, item: _Item, audio: bytes) -> bool:
        player = self._player()
        self._fire_first_audio()
        for offset in range(0, len(audio), PLAY_CHUNK_BYTES):
            if self._stale(item):
                player.abort()
                return False
            player.write(audio[offset : offset + PLAY_CHUNK_BYTES])
        player.stop()
        return True

    def _speak_with_say(self, item: _Item) -> None:
        self.fallback.speak(item.text, item.language)
        while not self.fallback.wait(timeout=0.05):
            if self._stale(item):
                return

    @staticmethod
    def _classify(error: BaseException) -> str:
        if isinstance(error, ElevenLabsError):
            return error.reason
        if isinstance(error, httpx.TimeoutException):
            return "timeout"
        if isinstance(error, httpx.ConnectError):
            return "offline"
        if isinstance(error, httpx.HTTPError):
            return "network"
        return "stream"

    # -- bookkeeping --

    def _note_failure(self, *, trip_now: bool) -> None:
        self._failures += 1
        if trip_now or self._failures >= self.FAILURE_THRESHOLD:
            self._open_until = self.clock() + self.COOLDOWN_S
            self._failures = 0
            log.warning("ElevenLabs circuit breaker open: using say for the next %.0f s", self.COOLDOWN_S)

    def _note_fallback(self, reason: str, item: _Item, detail: str = "") -> None:
        log.warning("ElevenLabs fallback to say: %s (%d chars)%s", reason, len(item.text), f" - {detail}" if detail else "")
        logging_setup.audit({"tool": "voice", "decision": "tts_fallback", "reason": reason, "chars": len(item.text)})
        if self._db is not None:
            try:
                self._db.kv_set(LAST_FALLBACK_KEY, json.dumps({"ts": now_iso(), "reason": reason}))
            except Exception as exc:
                log.warning("could not store the last fallback: %s", _safe_error(exc))

    def _record(
        self,
        item: _Item,
        *,
        status: str,
        cached: bool = False,
        duration_ms: float | None = None,
        first_chunk_ms: float | None = None,
        reason: str | None = None,
    ) -> None:
        detail = self.voice_id if not reason else f"{self.voice_id}: {reason}"
        if self._db is not None:
            try:
                self._db.add_external_call(
                    service=SERVICE,
                    operation=self.model,
                    units=len(item.text),
                    cached=cached,
                    status=status,
                    duration_ms=duration_ms,
                    detail=detail,
                )
            except Exception as exc:
                log.warning("could not record the ElevenLabs call: %s", _safe_error(exc))
        logging_setup.usage(
            {
                "kind": SERVICE,
                "model": self.model,
                "voice_id": self.voice_id,
                "chars": len(item.text),
                "cached": cached,
                "status": status,
                "first_chunk_ms": first_chunk_ms,
                "duration_ms": duration_ms,
            }
        )

    # -- phrase cache --

    def _cache_path(self, item: _Item) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / f"{cache_key(self.model, self.voice_id, item.text)}.pcm"

    def _cache_read(self, item: _Item) -> bytes | None:
        if self.cache_dir is None:
            return None
        try:
            path = self._cache_path(item)
            return path.read_bytes() if path.is_file() else None
        except OSError as exc:
            log.warning("tts cache read failed: %s", exc)
            return None

    def _cache_write(self, item: _Item, audio: bytes) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(item)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(audio)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("tts cache write failed: %s", exc)


# --- account helpers (doctor, CLI) ---------------------------------------------------------------------


def _client(key: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=API_BASE, headers={"xi-api-key": key}, timeout=HTTP_TIMEOUT, transport=transport)


def fetch_subscription(key: str, *, transport: httpx.BaseTransport | None = None) -> dict | None:
    """GET /v1/user/subscription: tier, character_count, character_limit, next_character_count_reset_unix.
    None (and a log line without the key) on any failure."""
    try:
        with _client(key, transport) as client:
            response = client.get("/v1/user/subscription")
            if response.status_code != 200:
                raise _error_from_response(response)
            data = response.json()
            return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("could not fetch the ElevenLabs subscription: %s", _safe_error(exc))
        return None


def fetch_voices(key: str, *, transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Every voice on the account from GET /v2/voices (paginated). Raises on failure."""
    voices: list[dict] = []
    params: dict[str, Any] = {"page_size": 100}
    with _client(key, transport) as client:
        while True:
            response = client.get("/v2/voices", params=params)
            if response.status_code != 200:
                raise _error_from_response(response)
            data = response.json()
            voices.extend(v for v in data.get("voices", []) if isinstance(v, dict))
            token = data.get("next_page_token")
            if not data.get("has_more") or not token:
                return voices
            params["page_token"] = token


def list_voices_cli(out: Callable[[str], None], *, api_key: str | None = None, transport: httpx.BaseTransport | None = None) -> int:
    """`jarvis voice list-voices`: name, id, category and labels of every voice on the account."""
    key = api_key or get_api_key()
    if not key:
        out(t("voice.elevenlabs.no_key"))
        return 1
    try:
        voices = fetch_voices(key, transport=transport)
    except Exception as exc:
        out(t("voice.elevenlabs.list_failed", error=_safe_error(exc)))
        return 1
    if not voices:
        out(t("voice.elevenlabs.no_voices"))
        return 1
    rows = []
    for voice in voices:
        labels = voice.get("labels") or {}
        label_text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if v) if isinstance(labels, dict) else ""
        rows.append([str(voice.get("name", "")), str(voice.get("voice_id", "")), str(voice.get("category", "")), label_text])
    header = ["name", "voice_id", "category", "labels"]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    out("  ".join(c.ljust(w) for c, w in zip(header, widths)).rstrip())
    out("  ".join("-" * w for w in widths))
    for row in rows:
        out("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    out("")
    out(t("voice.elevenlabs.voices", count=len(rows)))
    return 0


def set_key_cli(prompt: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print) -> int:
    """`jarvis voice set-elevenlabs-key`: store the key in the Keychain. Never echoed, never logged."""
    key = prompt(t("voice.elevenlabs.key_prompt")).strip()
    if not key:
        out(t("voice.elevenlabs.key_empty"))
        return 1
    if not key.startswith("sk_"):
        out(t("voice.elevenlabs.key_prefix"))
    secrets.set_secret(secrets.ELEVENLABS_API_KEY, key)
    out(t("voice.elevenlabs.key_stored", name=secrets.ELEVENLABS_API_KEY))
    return 0


def last_fallback(db: Any) -> dict | None:
    """The last fallback the engine stored ({"ts", "reason"}) or None."""
    try:
        raw = db.kv_get(LAST_FALLBACK_KEY)
        data = json.loads(raw) if raw else None
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def usage_report(db: Any, days: int = 7, *, budget: int | None = None) -> str:
    """The ElevenLabs section of `jarvis usage`; "" when nothing was recorded in the period."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.conn.execute(
        "SELECT substr(created_at, 1, 10) AS day,"
        " SUM(CASE WHEN cached = 0 AND status IN ('ok', 'error', 'timeout') THEN 1 ELSE 0 END) AS requests,"
        " SUM(CASE WHEN cached = 0 AND status = 'ok' THEN units ELSE 0 END) AS chars,"
        " SUM(cached) AS cached_hits,"
        " SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS fallbacks"
        " FROM external_calls WHERE service = ? AND created_at >= ? GROUP BY day ORDER BY day",
        (SERVICE, since),
    ).fetchall()
    if not rows:
        return ""
    header = ["day", "requests", "characters", "cached", "fallbacks"]
    table = [[r["day"], str(r["requests"] or 0), str(r["chars"] or 0), str(r["cached_hits"] or 0), str(r["fallbacks"] or 0)] for r in rows]
    widths = [max(len(r[i]) for r in [header, *table]) for i in range(len(header))]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    month = db.external_units_since(SERVICE, start_of_month_utc())
    month_line = f"This month: {month:,} characters billed"
    if budget:
        month_line += f" of the {budget:,} budget ({month / budget:.0%})"
    lines = [
        "ElevenLabs (characters are billed per request; cached phrases and fallbacks to say are free):",
        fmt(header),
        fmt(["-" * w for w in widths]),
        *(fmt(r) for r in table),
        month_line + ".",
    ]
    return "\n".join(lines)
