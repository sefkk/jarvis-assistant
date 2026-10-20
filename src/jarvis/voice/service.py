"""The voice service: an explicit state machine around the same `Assistant`.

    IDLE (wake word / hotkey) -> LISTENING (recording, VAD or hold-to-talk) -> TRANSCRIBING
        -> THINKING (Assistant, tools, confirmations) -> SPEAKING (sentence-by-sentence TTS) -> IDLE

Follow-up mode (`voice.follow_up_mode`): once the reply has been played to the end, LISTENING opens
again for `follow_up_window_s` without the wake word. Speech that starts in the window is recorded
to its end and becomes the next turn; silence or noise closes the window quietly and the service
is back to the wake word.

Cue sounds (`[voice.sounds]`, `jarvis.voice.cues`): `heard` plays the moment a request has been
recorded and keeps playing (non-blocking) through transcription and thinking until the reply's
first audio stops it; `ready` plays before the follow-up window and blocks it, so the microphone
opens only after the cue has finished. The `thinking` sound is armed right after `heard`: once that
cue has ended and `thinking_delay_ms` passed without a spoken reply it loops quietly through
transcription, API calls and tool calls, and stops the moment the first audio, a confirmation, an
error, a barge-in or a pause arrives (never over Jarvis's voice). No cue while paused; a barge-in
kills any cue.

Threads: the microphone callback fills a bounded frame queue; `_audio_loop` consumes it and routes
frames by state (wake word in IDLE, the recorder while listening, barge-in while speaking); the
hotkey listener calls back from pynput's thread; the main thread runs the turns. Every transition
is logged with a timestamp and, in `--foreground` mode, printed. `cancel()` works from any state.

Privacy: audio never leaves the Mac and is not written to disk unless `voice.save_audio` is on;
nothing is sent to the API before the wake word or the hotkey triggers.
"""

from __future__ import annotations

import collections
import logging
import os
import queue
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from jarvis.config import Config, Paths
from jarvis.context import ends_with_question
from jarvis.exceptions import JarvisError
from jarvis.i18n import LANGUAGES, t
from jarvis.io.voice import SpeechToText, VoiceInputSource, VoiceOutputSink, VoiceUnavailable, spawn_daemon_thread
from jarvis.observability import logging_setup
from jarvis.voice import announce as vannounce
from jarvis.voice import audio as vaudio
from jarvis.voice import confirm as vconfirm
from jarvis.voice import pause as vpause
from jarvis.voice.cues import CuePlayer
from jarvis.voice.hotkey import HotkeyListener
from jarvis.voice.tts import TextToSpeech
from jarvis.voice.vad import EndOfSpeechDetector, Recording, SileroVad, record_until_silence, record_while
from jarvis.voice.wakeword import WakeWordDetector

log = logging.getLogger("jarvis.voice.service")

TRIGGER_POLL_S = 0.25
MIC_READ_TIMEOUT_S = 0.5
MIC_STALL_S = 5.0  # no frame for this long while the stream should run: reopen it (the device vanished or changed)
MIC_RESTART_MIN_GAP_S = 15.0  # at most one reopen this often, so a dead microphone does not spin
SPEAK_WAIT_POLL_S = 0.1
CONFIRM_LISTEN_SILENCE_MS = 500
PAUSE_POLL_S = 1.0  # how often the audio thread looks at the pause file (`jarvis voice pause|resume`, /mute, the tool)
# Timing stages written to the `timings` table. `first_audio` is the key number:
# end of speech -> the first `say` process starts.
STAGE_WAKE = "wake_detected"  # trigger -> recording started (includes the listen sound)
STAGE_VAD_END = "vad_end"  # recording started -> end of speech
STAGE_STT = "stt"  # transcription
PREROLL_MODES = ("wake", "hotkey", "follow_up")  # recordings that may start with the pre-roll
PREROLL_FRESH_S = 30.0  # a mark older than this is stale (the trigger never became a recording)
WAKE_PHRASE_RE = re.compile(r"^\s*(?:(?:hey|hay|hei|hi)[\s,]+)?(?:jarvis|carvis|cervis|jarviz|çarvis)\b[\s,.!:;-]*", re.IGNORECASE)


def strip_wake_phrase(text: str) -> str:
    """The pre-roll may catch the tail of "Hey Jarvis": drop it from the start of the transcript."""
    return WAKE_PHRASE_RE.sub("", text or "", count=1).strip()


STAGE_FIRST_AUDIO = "first_audio"  # end of speech -> first audio
STAGE_TTS_TOTAL = "tts_total"  # end of speech -> last sentence finished
VOICE_STAGES = (STAGE_WAKE, STAGE_VAD_END, STAGE_STT, "ttft", STAGE_FIRST_AUDIO, STAGE_TTS_TOTAL)


ALARM_LISTEN_S = 4.0  # one listening round while an alarm rings: speech must start within this
ALARM_SILENCE_MS = 500


class VoiceState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    PAUSED = "PAUSED"  # microphone released (`jarvis voice pause`, "sesi kapat", /mute); the hotkey asks to resume
    STOPPED = "STOPPED"


@dataclass
class Trigger:
    kind: str  # "wake" | "hotkey" | "follow_up" | "resume_dialog" | "announce"
    at: float
    payload: Any = None  # "announce": the vannounce.Announcement to speak or to hand to the Assistant


@dataclass
class TurnTimings:
    """Per-utterance measurements, written to the database once the turn id is known."""

    trigger_at: float | None = None
    record_start: float | None = None
    speech_end: float | None = None
    stt_ms: float | None = None
    first_audio: float | None = None
    tts_done: float | None = None
    stages: dict[str, float] = field(default_factory=dict)

    def compute(self) -> dict[str, float]:
        out = dict(self.stages)
        if self.trigger_at is not None and self.record_start is not None:
            out[STAGE_WAKE] = (self.record_start - self.trigger_at) * 1000.0
        if self.record_start is not None and self.speech_end is not None:
            out[STAGE_VAD_END] = (self.speech_end - self.record_start) * 1000.0
        if self.stt_ms is not None:
            out[STAGE_STT] = self.stt_ms
        if self.speech_end is not None and self.first_audio is not None:
            out[STAGE_FIRST_AUDIO] = (self.first_audio - self.speech_end) * 1000.0
        if self.speech_end is not None and self.tts_done is not None:
            out[STAGE_TTS_TOTAL] = (self.tts_done - self.speech_end) * 1000.0
        return out


def _default_assistant_factory(config: Config, paths: Paths):
    from jarvis.assistant import Assistant  # noqa: PLC0415 - heavy, and tests inject a fake

    return Assistant(config, paths, channel="voice")


def _default_notifier(config: Config) -> Callable[[str, str], None]:
    from jarvis.scheduler.notify import MacNotifier  # noqa: PLC0415

    notifier = MacNotifier(config.timers.notification_sound)
    return notifier.notify


def build_tts(
    v: Any,
    paths: Any,
    *,
    language: str,
    on_first_audio: Callable[[], None] | None = None,
    on_sentence: Callable[[str, str], None] | None = None,
) -> Any:
    """The TTS engine from `[voice]`: `say`, or ElevenLabs with `say` inside as the fallback
    (`voice.tts_engine`). Shared by the service and `jarvis voice say`."""
    say = TextToSpeech(
        voice_tr=v.tts_voice_tr,
        voice_en=v.tts_voice_en,
        rate=v.tts_rate,
        default_language=language,
        on_first_audio=on_first_audio,
        on_sentence=on_sentence,
    )
    if v.tts_engine != "elevenlabs":
        return say
    from jarvis.voice.elevenlabs import ElevenLabsTTS  # noqa: PLC0415

    # The engine opens its own SQLite connection on its worker thread (WAL mode: it coexists
    # with the Assistant's connection); a connection must not cross threads.
    return ElevenLabsTTS(
        voice_id=v.elevenlabs_voice_id,
        model=v.elevenlabs_model,
        first_chunk_timeout_s=v.elevenlabs_first_chunk_timeout_s,
        monthly_char_budget=v.elevenlabs_monthly_char_budget,
        default_language=language,
        text_normalization=v.elevenlabs_text_normalization,
        fallback=say,
        db_path=paths.db_file,
        cache_dir=paths.tts_cache_dir,
        on_first_audio=on_first_audio,
        on_sentence=on_sentence,
    )


class VoiceService:
    def __init__(
        self,
        config: Config,
        paths: Paths,
        *,
        foreground: bool = False,
        assistant_factory: Callable[[Config, Paths], Any] | None = None,
        mic: Any | None = None,
        wake: WakeWordDetector | None = None,
        vad: SileroVad | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        hotkey: HotkeyListener | None = None,
        notify: Callable[[str, str], None] | None = None,
        reporter: Callable[[str], None] | None = None,
        play_sound: Callable[[str], Any] = vaudio.play_sound,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        db: Any | None = None,
        choice_dialog: Callable[..., str] = vconfirm.ask_choice_dialog,
        cues: CuePlayer | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.v = config.voice
        self.foreground = foreground
        self.assistant_factory = assistant_factory or _default_assistant_factory
        self.mic = mic
        self.wake = wake
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.hotkey = hotkey
        self._notify = notify
        self.reporter = reporter
        self.play_sound = play_sound
        self.clock = clock
        self.sleep = sleep
        self._db = db
        self.cues = cues or CuePlayer(paths.sounds_dir, config.voice.sounds, clock=clock, sleep=sleep)

        self.state = VoiceState.IDLE
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._trigger: Trigger | None = None
        self._trigger_event = threading.Event()
        self._listen_queue: queue.Queue = queue.Queue()
        # Pre-roll: the frames since the last mark (wake word detected, hotkey pressed, the ready cue's sound
        # ended), at most `preroll_ms`, handed to the recorder first so the first word is never cut.
        self._preroll: collections.deque = collections.deque(maxlen=max(0, -(-int(config.voice.preroll_ms) // 80)))
        self._preroll_lock = threading.Lock()
        self._preroll_marked_at: float | None = None
        self._last_preroll_ms = 0.0
        self._previous_transcript: str | None = None  # for learning "hayır, X dedim" corrections
        self._capturing = False  # frames go to the recorder (LISTENING, or a confirmation answer)
        self._cancel_listen = False
        self._audio_thread: threading.Thread | None = None
        self.mic_stall_s = MIC_STALL_S  # tests shorten it
        self._last_mic_restart: float | None = None
        self.mic_restarts = 0
        self._assistant: Any = None
        self._session_date: str | None = None
        self._sink: VoiceOutputSink | None = None
        self._timings = TurnTimings()
        self._language = config.general.language
        self.transitions: list[tuple[VoiceState, VoiceState, str]] = []  # for tests and the foreground log
        # Pause: the CLI and the spoken phrases share one file; the audio thread polls it.
        self._pause_file = paths.voice_pause_file
        self._paused = vpause.is_paused(self._pause_file)
        self._pause_until: str | None = None
        self._pause_checked = 0.0
        self._pause_lock = threading.Lock()
        self._pending_resume_dialog = False
        self._choice_dialog = choice_dialog
        # Announcements: another process (the presence engine in the scheduler) leaves a
        # one-shot request file; it is taken only while idle and not paused.
        self._announce_file = paths.voice_announce_file
        # Follow-up window: set by `handle_utterance` once the reply has been played to the end,
        # consumed by `next_utterance`; `_follow_up_turn` carries the window of the utterance in flight.
        self._follow_up_pending = False
        self._follow_up_turn: tuple[float, float | None] | None = None  # (window_s, speech_after_ms)
        self._last_reply_was_question = False  # the last spoken reply ended with "?": short answers are expected

    # --- reporting ----------------------------------------------------------------------------------

    def _report(self, text: str) -> None:
        if self.reporter is not None:
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.reporter(f"[{stamp}] {text}")

    def _set_state(self, new: VoiceState, reason: str = "") -> None:
        with self._state_lock:
            old, self.state = self.state, new
        self.transitions.append((old, new, reason))
        log.info(
            "state %s -> %s%s",
            old.value,
            new.value,
            f" ({reason})" if reason else "",
            extra={"data": {"from": old.value, "to": new.value, "reason": reason}},
        )
        self._report(f"{old.value} → {new.value}" + (f"  ({reason})" if reason else ""))

    @property
    def paused(self) -> bool:
        return self._paused

    def _rest_state(self) -> VoiceState:
        """Where a finished turn (or a cancel) lands: IDLE, or PAUSED while listening is paused."""
        return VoiceState.PAUSED if self._paused else VoiceState.IDLE

    # --- lifecycle ------------------------------------------------------------------------------------

    @staticmethod
    def missing_dependencies() -> list[str]:
        import importlib.util  # noqa: PLC0415

        needed = {
            "sounddevice": "sounddevice",
            "openwakeword": "openwakeword",
            "silero_vad": "silero-vad",
            "pynput": "pynput",
            "mlx_whisper": "mlx-whisper",
            "numpy": "numpy",
        }
        return sorted(pkg for module, pkg in needed.items() if importlib.util.find_spec(module) is None)

    def start(self) -> None:
        """Load every model once and start the audio threads."""
        v = self.v
        if self.wake is None:
            self.wake = WakeWordDetector(
                v.wake_word_model, threshold=v.wake_word_threshold, cooldown_s=v.wake_word_cooldown_s, clock=self.clock
            )
        if self.vad is None:
            self.vad = SileroVad()
        if self.stt is None:
            self.stt = SpeechToText.from_config(v, self.paths, config=self.config, db=self._database)
        if self.tts is None:
            self.tts = self._build_tts()
        else:
            self.tts.on_first_audio = self._on_first_audio  # type: ignore[attr-defined]
            if getattr(self.tts, "on_sentence", None) is None:
                self.tts.on_sentence = self._on_sentence  # type: ignore[attr-defined]
        if self.mic is None:
            self.mic = vaudio.Microphone()
        if self.hotkey is None:
            self.hotkey = HotkeyListener(v.hotkey, self._on_hotkey_press, self._on_hotkey_release)

        t0 = self.clock()
        self.wake.load()
        self._report(f"wake word model '{v.wake_word_model}' loaded in {(self.clock() - t0) * 1000:.0f} ms")
        t0 = self.clock()
        self.vad.load()
        self._report(f"Silero VAD loaded in {(self.clock() - t0) * 1000:.0f} ms")
        if not self.stt.warmed_up:
            ms = self.stt.warm_up()
            self._report(
                f"speech to text ({self.stt.describe()}) ready in {ms:.0f} ms"
                + (
                    f"; {self.stt.engine} not ready, Whisper is used: {self.stt.last_error}"
                    if getattr(self.stt, "last_error", None)
                    else ""
                )
            )
        self.tts.start()
        self._paused = vpause.is_paused(self._pause_file)  # `jarvis voice pause` may have run before the start
        if self._paused:
            self._pause_until = vpause.load_state(self._pause_file).until
            self._send_notification(vpause.paused_notification(vpause.load_state(self._pause_file), self._language))
        else:
            self.mic.start()  # paused: the microphone stays released until the pause ends
        try:
            self.hotkey.start()
            self._report(f"hotkey {v.hotkey}: hold to talk, press while speaking to interrupt")
        except VoiceUnavailable as exc:
            log.warning("hotkey disabled: %s", exc)
            self._report(f"hotkey disabled: {exc}")
        except Exception as exc:  # Input Monitoring missing: wake word still works
            log.warning("hotkey listener failed to start: %s", exc)
            self._report(f"hotkey disabled ({type(exc).__name__}: {exc}); see docs/integrations/voice.md (Input Monitoring)")
        for cue in ("heard", "ready", "thinking"):
            self._report("cue " + self.cues.describe(cue))
        self.cues.thinking.on_start = lambda: self._report("thinking sound started")
        self._stop.clear()
        self.state = self._rest_state()
        self._audio_thread = threading.Thread(target=self._audio_loop, name="jarvis-voice-audio", daemon=True)
        self._audio_thread.start()
        self._report(f"state {self.state.value} (started)" + ("; `jarvis voice resume` turns listening on" if self._paused else ""))
        log.info("voice service started", extra={"data": {"wake_word": v.wake_word_model, "hotkey": v.hotkey, "paused": self._paused}})

    def _build_tts(self) -> Any:
        """`say`, or ElevenLabs with `say` inside as the fallback (`voice.tts_engine`)."""
        return build_tts(self.v, self.paths, language=self._language, on_first_audio=self._on_first_audio, on_sentence=self._on_sentence)

    def request_stop(self) -> None:
        self._stop.set()
        self._cancel_listen = True
        self._trigger_event.set()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self.request_stop()
        if self._sink is not None:
            self._sink.cancel()
        for part, name in ((self.hotkey, "hotkey"), (self.mic, "microphone")):
            try:
                if part is not None:
                    part.stop()
            except Exception as exc:  # closing must not hide the original problem
                log.warning("%s stop failed: %s", name, exc)
        if self.tts is not None:
            try:
                self.tts.close()
            except Exception as exc:
                log.warning("tts close failed: %s", exc)
        self.cues.stop()
        if self._audio_thread is not None and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=2.0)
        if self._assistant is not None:
            try:
                self._assistant.close()
            except Exception as exc:
                log.warning("assistant close failed: %s", exc)
            self._assistant = None
        self._set_state(VoiceState.STOPPED, "closed")

    # --- audio routing (its own thread) ------------------------------------------------------------------

    def _audio_loop(self) -> None:
        assert self.mic is not None and self.wake is not None
        last_frame = self.clock()
        while not self._stop.is_set():
            try:
                frame = self.mic.read(timeout=MIC_READ_TIMEOUT_S)
            except Exception as exc:
                log.exception("microphone read failed")
                self._report(f"microphone error: {exc}")
                self.sleep(1.0)
                continue
            if frame is not None:
                last_frame = self.clock()
            elif self._reopen_stalled_mic(last_frame):
                last_frame = self.clock()
            with self._preroll_lock:
                capturing = self._capturing
                if capturing:
                    self._listen_queue.put(frame)
                elif frame is not None and self._preroll.maxlen:
                    self._preroll.append(frame)
            if capturing:
                continue
            try:
                self._poll_pause()  # also on a quiet microphone (frame None): the CLI must not wait for audio
            except Exception:
                log.exception("pause check failed")
            if frame is None:
                continue
            try:
                state = self.state
                if state == VoiceState.IDLE:
                    if self.wake.process(frame):
                        self._fire_trigger("wake")
                elif state == VoiceState.SPEAKING and self.v.wake_word_barge_in:
                    if self.wake.process(frame):
                        self._barge_in("wake")
                # PAUSED, LISTENING without capturing, TRANSCRIBING, THINKING: frames are dropped.
            except Exception:
                log.exception("audio routing failed")

    def _reopen_stalled_mic(self, last_frame: float) -> bool:
        """The stream should be running but has delivered nothing for `mic_stall_s`: close and reopen it.

        sounddevice keeps a stream open on a device that is gone (headphones unplugged, the input
        switched) and simply stops calling back; the service then looks alive but is deaf. Returns
        True when a reopen was attempted (the stall clock starts over).
        """
        now = self.clock()
        if self._paused or now - last_frame < self.mic_stall_s or not getattr(self.mic, "running", True):
            return False
        if self._last_mic_restart is not None and now - self._last_mic_restart < MIC_RESTART_MIN_GAP_S:
            return False
        self._last_mic_restart = now
        self.mic_restarts += 1
        log.warning("no microphone audio for %.1f s; reopening the input stream (reopen #%d)", now - last_frame, self.mic_restarts)
        self._report(f"microphone silent for {now - last_frame:.0f} s: reopening the input stream")
        logging_setup.audit({"tool": "voice", "decision": "mic_reopen", "silent_s": round(now - last_frame, 1), "count": self.mic_restarts})
        with self._pause_lock:
            if self._paused or self.mic is None:
                return False
            try:
                self.mic.stop()
                self.mic.start()
            except Exception as exc:
                log.exception("microphone reopen failed")
                self._report(f"microphone reopen failed: {exc}")
                return True
        if self.wake is not None:
            self.wake.reset()
        return True

    # --- pause: the file is the truth; the service owns the microphone, the notification and the state ------

    def _poll_pause(self, *, force: bool = False) -> None:
        """Pick up the pause file (CLI, Telegram, the tool, the timer) at most once per PAUSE_POLL_S."""
        now = self.clock()
        if not force and now - self._pause_checked < PAUSE_POLL_S:
            return
        self._pause_checked = now
        self._sync_pause()

    def _sync_pause(self, *, reason: str | None = None) -> None:
        state = vpause.load_state(self._pause_file)
        if state.expired:
            vpause.resume(self._pause_file)  # the pause time is over: auto-resume
            reason = reason or "resumed (pause time over)"
        if state.paused != self._paused:
            if state.paused:
                self._enter_pause(state, reason or f"paused ({vpause.SOURCES.get(state.by or '', state.by or 'file')})")
            else:
                self._leave_pause(reason or "resumed (jarvis voice resume)")
        elif state.paused and state.until != self._pause_until:  # re-paused with a new duration
            self._pause_until = state.until
            self._send_notification(vpause.paused_notification(state, self._language))

    def _enter_pause(self, state: vpause.PauseState, reason: str) -> None:
        with self._pause_lock:
            self._paused = True
            self._pause_until = state.until
            if self.mic is not None:
                try:
                    self.mic.stop()  # releases the audio stream: the orange mic indicator goes away
                except Exception as exc:
                    log.warning("microphone release failed: %s", exc)
        self.cues.stop()  # a pause silences Jarvis at once, the thinking sound included
        logging_setup.audit({"tool": "voice", "decision": "paused", "by": state.by, "until": state.until})
        self._send_notification(vpause.paused_notification(state, self._language))
        if self.state == VoiceState.IDLE:
            self._set_state(VoiceState.PAUSED, reason)
        # In any other state `_rest_state()` lands on PAUSED when the current turn ends.

    def _leave_pause(self, reason: str) -> None:
        with self._pause_lock:
            self._paused = False
            self._pause_until = None
            if self.mic is not None:
                try:
                    self.mic.start()
                except Exception as exc:
                    log.exception("microphone restart failed")
                    self._report(f"microphone restart failed: {exc}")
        if self.wake is not None:
            self.wake.reset()
        logging_setup.audit({"tool": "voice", "decision": "resumed", "reason": reason})
        self._send_notification(vpause.resumed_notification(self._language))
        if self.state == VoiceState.PAUSED:
            self._set_state(VoiceState.IDLE, reason)

    def _send_notification(self, body: str) -> None:
        if self._notify is None:
            try:
                self._notify = _default_notifier(self.config)
            except Exception as exc:
                log.warning("notifier unavailable: %s", exc)
                return
        try:
            self._notify(vpause.PAUSED_TITLE, body)
        except Exception as exc:  # a failed notification must never break the service
            log.warning("notification failed: %s", exc)

    def mark_preroll(self) -> None:
        """Start the pre-roll from now: earlier audio (the wake word, Jarvis's voice, a cue) is dropped."""
        with self._preroll_lock:
            self._preroll.clear()
            self._preroll_marked_at = self.clock()

    def _fire_trigger(self, kind: str) -> None:
        if self._trigger is not None or self.state != VoiceState.IDLE:
            return
        self.mark_preroll()
        self._trigger = Trigger(kind=kind, at=self.clock())
        logging_setup.audit({"tool": "voice", "decision": "trigger", "kind": kind})
        self._voice_event("trigger", kind)
        self._trigger_event.set()

    def _barge_in(self, kind: str) -> None:
        """Interrupt speech (and anything still being produced), then listen again."""
        log.info("barge-in by %s", kind)
        self.mark_preroll()
        self._report(f"barge-in ({kind})")
        if self._sink is not None:
            self._sink.cancel()
        if self.tts is not None:
            self.tts.stop()
        self.cues.stop()  # a cue must never outlive an interruption
        self._pending_barge_in = kind

    _pending_barge_in: str | None = None
    _listening_kind: str | None = None  # what opened the current LISTENING state

    # --- hotkey ---------------------------------------------------------------------------------------------

    def _on_hotkey_press(self) -> None:
        try:
            if self._stop_alarm_by("hotkey"):
                return  # the press stopped the ringing alarm; it does not start a turn
            state = self.state
            if state == VoiceState.IDLE:
                self._fire_trigger("hotkey")
            elif state == VoiceState.PAUSED:  # the microphone is closed: ask on screen whether to resume
                self._pending_resume_dialog = True
                self._trigger_event.set()
            elif state == VoiceState.SPEAKING:
                self._barge_in("hotkey")
            elif state == VoiceState.THINKING:
                self._report("hotkey while thinking: cancelling the reply")
                if self._sink is not None:
                    self._sink.cancel()
                self.cues.stop()
                self._pending_barge_in = "hotkey"
            elif state == VoiceState.LISTENING and self._listening_kind in ("wake", "follow_up"):
                self._cancel_listen = True  # a press while listening (wake word, follow-up window) cancels it; the hold takes over
        except Exception:
            log.exception("hotkey press handling failed")

    def _on_hotkey_release(self) -> None:
        return None  # `record_while` polls `hotkey.is_held`

    # --- recording -----------------------------------------------------------------------------------------

    def _frames(self):
        """Frames for the recorder, from the audio thread through the listen queue."""
        while True:
            try:
                yield self._listen_queue.get(timeout=MIC_READ_TIMEOUT_S)
            except queue.Empty:
                yield None

    def _record(
        self,
        *,
        mode: str,
        no_speech_timeout_s: float | None = None,
        silence_ms: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Recording:
        assert self.vad is not None
        self._cancel_listen = False
        with self._listen_queue.mutex:
            self._listen_queue.queue.clear()
        with self._preroll_lock:
            # Only a fresh mark counts (wake, hotkey, follow-up); a confirmation answer or the alarm word
            # starts clean, so Jarvis's own question never lands in the recording.
            fresh = self._preroll_marked_at is not None and self.clock() - self._preroll_marked_at <= PREROLL_FRESH_S
            frames = list(self._preroll) if mode in PREROLL_MODES and fresh else []
            self._preroll.clear()
            self._preroll_marked_at = None
            for frame in frames:
                self._listen_queue.put(frame)
            self._last_preroll_ms = sum(len(f) for f in frames) * 1000.0 / vaudio.SAMPLE_RATE
            self._capturing = True
        stop = self._should_stop_listening if should_stop is None else (lambda: self._should_stop_listening() or should_stop())
        try:
            if mode == "hotkey" and self.hotkey is not None:
                return record_while(self._frames(), still_held=lambda: self.hotkey.is_held and not stop(), max_record_s=self.v.max_record_s)
            self.vad.reset()
            detector = EndOfSpeechDetector(
                self.vad,
                threshold=self.v.vad_threshold,
                silence_ms=silence_ms or self.v.vad_silence_ms,
                max_record_s=self.v.max_record_s,
                no_speech_timeout_s=no_speech_timeout_s or self.v.no_speech_timeout_s,
            )
            return record_until_silence(
                self._frames(),
                detector,
                should_stop=stop,
                idle_timeout_s=float(no_speech_timeout_s or self.v.no_speech_timeout_s),
                clock=self.clock,
            )
        finally:
            self._capturing = False
            with self._listen_queue.mutex:
                self._listen_queue.queue.clear()

    def _should_stop_listening(self) -> bool:
        return self._cancel_listen or self._stop.is_set()

    def _save_audio(self, recording: Recording, tag: str) -> None:
        if not self.v.save_audio:
            return
        try:
            self.paths.voice_audio_dir.mkdir(parents=True, exist_ok=True)
            path = self.paths.voice_audio_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.wav"
            vaudio.write_wav_int16(path, recording.audio)
            log.info("recording saved to %s", path)
        except Exception as exc:
            log.warning("could not save the recording: %s", exc)

    def _learn_correction(self, text: str) -> None:
        """ "hayır, X dedim" after a misheard request: remember the mishearing (voice/corrections.py)."""
        store = getattr(self.stt, "corrections", None)
        previous, self._previous_transcript = self._previous_transcript, text
        if store is None:
            return
        try:
            learned = store.learn(previous, text)
        except Exception as exc:  # learning must never break a turn
            log.warning("correction learning failed: %s", exc)
            return
        if learned is not None:
            self._report(f"learned: {learned.heard!r} is {learned.meant!r} from now on")
            logging_setup.audit({"tool": "voice", "decision": "correction_learned", "chars": len(learned.meant)})
            cache = getattr(self.stt, "vocabulary_cache", None)
            if cache is not None:
                cache.invalidate()

    def _transcribe(self, recording: Recording) -> tuple[str | None, str | None]:
        assert self.stt is not None
        t0 = self.clock()
        text, language = self.stt.transcribe_array(vaudio.int16_to_float32(recording.audio))
        self._timings.stt_ms = (self.clock() - t0) * 1000.0
        # After a question, a one-word answer is never dropped as noise (the hallucination list still applies).
        cleaned = self.stt.clean(text, after_question=self._last_reply_was_question)
        logging_setup.audit(
            {
                "tool": "voice",
                "decision": "transcribed" if cleaned else "transcript_dropped",
                "seconds": round(recording.duration_ms / 1000.0, 2),
                "chars": len(text or ""),
                "language": language,
                "after_question": self._last_reply_was_question,
            }
        )
        if cleaned is None and text:
            log.info("transcript dropped as empty or hallucination: %r", text[:80])
        self._voice_event("transcribed" if cleaned else "transcript_dropped", language)
        self._last_raw_transcript = (text or "")[:2000]  # stored with the turn once its id is known (the Logs page)
        return cleaned, language

    # --- the input source --------------------------------------------------------------------------------------

    def _wait_for_trigger(self) -> Trigger | None:
        while not self._stop.is_set():
            if self._pending_barge_in:
                kind, self._pending_barge_in = self._pending_barge_in, None
                return Trigger(kind=kind, at=self.clock())
            if self._pending_resume_dialog or (self._paused and self.hotkey is not None and self.hotkey.is_held):
                self._pending_resume_dialog = False
                return Trigger(kind="resume_dialog", at=self.clock())
            if self.hotkey is not None and self.hotkey.is_held and self._trigger is None:
                return Trigger(kind="hotkey", at=self.clock())
            announcement = self._take_announcement()
            if announcement is not None:
                return Trigger(kind="announce", at=self.clock(), payload=announcement)
            if self._alarm_ringing():  # listen for "tamam" / "kapat" / "ertele" without the wake word
                return Trigger(kind="alarm", at=self.clock())
            if self._trigger_event.wait(timeout=TRIGGER_POLL_S):
                self._trigger_event.clear()
                trigger, self._trigger = self._trigger, None
                if trigger is not None:
                    return trigger
        return None

    def next_utterance(self) -> str | None:
        """Block until the user said something worth sending; None when stopping."""
        while not self._stop.is_set():
            self._follow_up_turn = None
            if self._follow_up_pending:
                self._follow_up_pending = False
                trigger = self._open_follow_up()
                if trigger is None:
                    continue
            else:
                trigger = self._wait_for_trigger()
            if trigger is None:
                return None
            if trigger.kind == "resume_dialog":
                self._ask_resume()
                continue
            if trigger.kind == "announce":
                text = self._handle_announcement(trigger)
                if text is None:
                    continue
                return text
            if trigger.kind == "alarm":
                self._listen_for_alarm_word()
                continue
            follow_up = trigger.kind == "follow_up"
            window_s = float(self.v.follow_up_window_s)
            self._timings = TurnTimings(trigger_at=trigger.at)
            self._listening_kind = trigger.kind
            self._set_state(VoiceState.LISTENING, "follow-up window" if follow_up else trigger.kind)
            if trigger.kind == "wake" or (not follow_up and self.hotkey is not None and self.hotkey.is_held):
                self.play_sound(self.v.listen_sound)
            self._timings.record_start = self.clock()
            try:
                # The follow-up window only limits when speech must start; once it starts the VAD ends it.
                recording = self._record(mode=trigger.kind, no_speech_timeout_s=window_s if follow_up else None)
            except Exception as exc:
                log.exception("recording failed")
                self._report(f"recording failed: {exc}")
                self._set_state(self._rest_state(), "error")
                continue
            finally:
                self._listening_kind = None
            now = self.clock()
            self._timings.speech_end = now
            if recording.speech_end_ms is not None and self._timings.record_start is not None:
                # The VAD knows when speech really ended; the trailing silence is not the user's
                # fault. Never later than now (frames can arrive faster than real time in tests).
                self._timings.speech_end = min(now, self._timings.record_start + recording.speech_end_ms / 1000.0)
            self._report(
                f"recording: {recording.duration_ms:.0f} ms, {recording.reason}"
                + (f", pre-roll {self._last_preroll_ms:.0f} ms" if self._last_preroll_ms else "")
            )
            if self._stop.is_set():
                return None
            if recording.reason == "stopped" or not recording.speech_started or recording.reason == "no_speech":
                cancelled = recording.reason == "stopped"
                if follow_up:  # nobody spoke (or the hotkey took over): close quietly, back to the wake word
                    self._record_follow_up("cancelled" if cancelled else "silence", window_s)
                    self._set_state(self._rest_state(), "follow-up: cancelled" if cancelled else "follow-up: silence")
                else:
                    self.play_sound(self.v.no_speech_sound)
                    self._set_state(self._rest_state(), "cancelled" if cancelled else "no speech")
                self._trigger = None
                continue
            heard = self._play_heard_cue()  # non-blocking: transcription and the model start while it plays
            self._arm_thinking(after=heard)  # loops once `heard` ended and the delay passed without a reply
            self._save_audio(recording, trigger.kind)
            self._set_state(VoiceState.TRANSCRIBING, f"{recording.duration_ms:.0f} ms of audio")
            try:
                text, language = self._transcribe(recording)
            except VoiceUnavailable as exc:
                self.cues.stop()
                raise JarvisError(str(exc)) from exc
            except Exception as exc:
                log.exception("transcription failed")
                self._report(f"transcription failed: {exc}")
                self.cues.stop()
                self._set_state(self._rest_state(), "error")
                continue
            if text is None:
                self.cues.stop()  # it was noise after all: neither the "heard" cue nor the thinking sound may go on
                if follow_up:  # noise, a cough, the TV: dropped by the transcript filters, no sound
                    self._record_follow_up("noise", window_s, speech_after_ms=recording.speech_start_ms)
                    self._set_state(self._rest_state(), "follow-up: noise")
                else:
                    self.play_sound(self.v.no_speech_sound)
                    self._set_state(self._rest_state(), "empty transcript")
                continue
            if trigger.kind == "wake":
                text = strip_wake_phrase(text) or text
            self._learn_correction(text)
            if language in LANGUAGES:
                self._language = language
            self._report(
                t("voice.transcript.heard", self._language, text=text)
                + f"  [{language}, stt {self._timings.stt_ms:.0f} ms]"
                + ("  (follow-up)" if follow_up else "")
            )
            matched, minutes = vpause.match_pause_phrase(text, list(self.v.pause_phrases))
            if matched:
                if follow_up:
                    self._record_follow_up("pause", window_s, speech_after_ms=recording.speech_start_ms)
                self._pause_by_voice(text, minutes)
                continue
            cancelled = self._cancel_sleep_on_request(text)
            if cancelled:  # "iptal" / "dur" while the Mac is about to sleep (tools/power.py): no model call
                self.cues.stop()
                self._speak_phrase(cancelled)
                self._set_state(self._rest_state(), "sleep cancelled by voice")
                continue
            if follow_up:
                self._follow_up_turn = (window_s, recording.speech_start_ms)
            if not self.foreground and self.v.show_transcript_notification and self._notify is not None:
                try:
                    self._notify("Jarvis", t("voice.transcript.heard", self._language, text=text))
                except Exception as exc:
                    log.warning("transcript notification failed: %s", exc)
            return text
        return None

    # --- announcements: a request file from another process (presence.on_arrive_greet) ---------------

    def _take_announcement(self) -> vannounce.Announcement | None:
        """The pending announcement, only while idle and not paused (a paused Jarvis stays silent)."""
        if self._paused or self.state != VoiceState.IDLE or not vannounce.pending(self._announce_file):
            return None
        try:
            return vannounce.take(self._announce_file)
        except Exception as exc:  # a broken file must never stall the trigger loop
            log.warning("announcement file unreadable: %s", exc)
            return None

    def _handle_announcement(self, trigger: Trigger) -> str | None:
        """`say`: speak the text now (no model). `turn`: the text becomes this utterance, so the
        reply comes from the Assistant like a spoken request; returns it for `handle_utterance`."""
        ann: vannounce.Announcement = trigger.payload
        logging_setup.audit({"tool": "voice", "decision": "announce", "kind": ann.kind, "source": ann.source, "chars": len(ann.text)})
        self._report(f"announcement from {ann.source} ({ann.kind})")
        if ann.kind == "say":
            self._set_state(VoiceState.SPEAKING, f"announcement ({ann.source})")
            try:
                self._speak_phrase(ann.text)
            except Exception as exc:
                log.warning("announcement could not be spoken: %s", exc)
            self._set_state(self._rest_state(), "announcement done")
            return None
        self._timings = TurnTimings(trigger_at=trigger.at)
        self._listening_kind = None
        return ann.text

    # --- follow-up window: listening after a reply without the wake word --------------------------------------

    @staticmethod
    def _is_question(text: str) -> bool:
        """The reply ends with a question mark (closing quotes, brackets and emoji after it are ignored)."""
        return ends_with_question(text)

    def _wants_follow_up(self, sink: VoiceOutputSink, result: Any) -> bool:
        """Open a window after this turn? Never after an interrupted or failed turn, nor while paused."""
        mode = self.v.follow_up_mode
        if mode == "off" or result is None or sink.cancelled or self._pending_barge_in or self._stop.is_set() or self._paused:
            return False
        if mode == "questions_only":
            return self._is_question(sink.reply_text)
        return True

    def _open_follow_up(self) -> Trigger | None:
        """The reply has been played to the end: play the `ready` cue to the end, then listen again
        without the wake word. The window (and its seconds) starts only after the cue, so the
        microphone never records it."""
        self._poll_pause(force=True)  # a pause that arrived during the reply wins
        if self._interrupts_follow_up():
            return None
        window_s = float(self.v.follow_up_window_s)
        if not self.cues.play_blocking("ready", should_stop=self._interrupts_follow_up, until_silent=True):
            self._report("follow-up window skipped: interrupted during the ready cue")
            return None
        self.mark_preroll()  # the chime is over: what comes now is the user's
        if self.wake is not None:  # the cue came out of the speakers too: count the cooldown from its end
            self.wake.suppress(self.v.post_speech_cooldown_s)
        self._report(f"follow-up window: {window_s:g} s to start talking, no wake word needed")
        logging_setup.audit({"tool": "voice", "decision": "follow_up_open", "window_s": window_s, "mode": self.v.follow_up_mode})
        return Trigger(kind="follow_up", at=self.clock())

    def _interrupts_follow_up(self) -> bool:
        """Anything that must win over the follow-up window (and kill the ready cue at once)."""
        return bool(self._paused or self._stop.is_set() or self._pending_barge_in or (self.hotkey is not None and self.hotkey.is_held))

    # --- cue sounds -------------------------------------------------------------------------------------------

    def _play_heard_cue(self) -> Any:
        """`voice_heard`: the request is recorded; never while paused, never for dropped recordings
        (the caller only reaches this with real speech). Returns the cue handle (None when nothing plays)."""
        if self._paused or self._stop.is_set():
            return None
        try:
            handle = self.cues.play("heard")
            if handle is not None:
                self._report("cue heard")
            return handle
        except Exception as exc:  # a cue must never break a turn
            log.warning("heard cue failed: %s", exc)
            return None

    def _arm_thinking(self, *, after: Any = None) -> None:
        """Arm the thinking sound for the turn in flight (see `cues.ThinkingLoop`); never while paused."""
        if self._paused or self._stop.is_set() or self._pending_barge_in:
            return
        try:
            if self.cues.thinking.start(after=after):
                self._report("thinking sound armed")
        except Exception as exc:  # a sound must never break a turn
            log.warning("thinking sound failed: %s", exc)

    def _stop_thinking(self, reason: str) -> None:
        try:
            if self.cues.stop("thinking"):
                self._report(f"thinking sound stopped ({reason})")
        except Exception as exc:
            log.warning("thinking sound stop failed: %s", exc)

    def _on_attention(self, kind: str) -> None:
        """The sink is about to speak to the user before the reply (confirmation, choice, error)."""
        self._stop_thinking(kind)

    def _on_attention_done(self, kind: str) -> None:
        """The confirmation or choice is answered and the turn goes on (tool, next API call): the
        silence may be long again, so the thinking sound is armed once more (with its delay)."""
        if self.state == VoiceState.THINKING and self._sink is not None and not self._sink.cancelled:
            self._arm_thinking()

    def _record_follow_up(self, outcome: str, window_s: float, *, turn_id: str | None = None, speech_after_ms: float | None = None) -> None:
        logging_setup.audit(
            {
                "tool": "voice",
                "decision": "follow_up",
                "outcome": outcome,
                "turn_id": turn_id,
                "window_s": window_s,
                "speech_after_ms": speech_after_ms,
            }
        )
        self._report(f"follow-up: {outcome}" + (f" (turn {turn_id})" if turn_id else ""))
        db = self._db
        if db is None and self._assistant is not None:
            db = getattr(self._assistant, "db", None)
        add = getattr(db, "add_follow_up", None)
        if add is None:
            return
        try:
            add(outcome, window_s=window_s, turn_id=turn_id, speech_after_ms=speech_after_ms)
        except Exception as exc:
            log.warning("could not store the follow-up outcome: %s", exc)

    # --- a ringing alarm: the hotkey stops it; "tamam" / "kapat" / "durdur" / "ertele" without the wake word ----

    def _alarm_ringing(self) -> bool:
        if self._paused or self.state != VoiceState.IDLE:
            return False
        try:
            from jarvis.scheduler import alarm  # noqa: PLC0415

            return alarm.ringing(self.paths) is not None
        except Exception:
            log.debug("alarm state unreadable", exc_info=True)
            return False

    def _stop_alarm_by(self, by: str) -> bool:
        """Stop the ringing alarm (any state, even paused). False when nothing rings."""
        try:
            from jarvis.scheduler import alarm  # noqa: PLC0415

            if alarm.ringing(self.paths) is None:
                return False
            ok = alarm.request_stop(self.paths, by=by)
        except Exception:
            log.exception("alarm stop by %s failed", by)
            return False
        if ok:
            logging_setup.audit({"tool": "voice", "decision": "alarm_stopped", "by": by})
            self._report(f"alarm stopped ({by})")
        return ok

    def _listen_for_alarm_word(self) -> None:
        """One short recording while the alarm rings (no wake word, no cues: the alarm is loud enough),
        then the stop / snooze words are matched in code. Anything else is ignored and the loop listens again."""
        from jarvis.scheduler import alarm  # noqa: PLC0415

        self._listening_kind = "alarm"
        self._set_state(VoiceState.LISTENING, "alarm ringing: listening for a stop word")
        try:
            recording = self._record(
                mode="alarm",
                no_speech_timeout_s=ALARM_LISTEN_S,
                silence_ms=ALARM_SILENCE_MS,
                should_stop=lambda: not self._alarm_still_ringing(),
            )
        except Exception as exc:
            log.exception("alarm listening failed")
            self._report(f"alarm listening failed: {exc}")
            self.sleep(ALARM_LISTEN_S)
            return
        finally:
            self._listening_kind = None
            self._set_state(self._rest_state(), "alarm listen done")
        if self._stop.is_set() or not recording.speech_started or recording.reason in ("no_speech", "stopped"):
            return
        try:
            text, _language = self._transcribe(recording)
        except Exception as exc:
            log.warning("alarm transcription failed: %s", exc)
            return
        if not text:
            return
        action = alarm.classify_alarm_word(text)
        self._report(t("voice.transcript.alarm", self._language, text=text, action=action or "ignored"))
        if action == "stop":
            self._stop_alarm_by("voice")
        elif action == "snooze":
            if alarm.request_snooze(self.paths, by="voice"):
                logging_setup.audit({"tool": "voice", "decision": "alarm_snoozed", "by": "voice"})
                self._report("alarm snoozed (voice)")

    def _alarm_still_ringing(self) -> bool:
        try:
            from jarvis.scheduler import alarm  # noqa: PLC0415

            return alarm.ringing(self.paths) is not None
        except Exception:
            return False

    # --- pause by voice (exact phrase: no API call) and the hotkey's resume dialog ------------------------------

    def _cancel_sleep_on_request(self, text: str) -> str | None:
        from jarvis.tools import power  # noqa: PLC0415

        if not power.is_cancel_word(text):
            return None
        try:
            message = power.cancel_on_request(text, paths=self.paths)
        except Exception:
            log.exception("sleep cancel check failed")
            return None
        if message:
            logging_setup.audit({"tool": "voice", "decision": "sleep_cancelled", "heard_chars": len(text)})
            self._report(f"sleep cancelled by voice ({text!r})")
        return message

    def _pause_by_voice(self, heard: str, minutes: int | None) -> None:
        """ "sesi kapat" / "1 saat sesi kapat" matched exactly: the same pause as the `pause_listening`
        tool, with a short spoken acknowledgement first (the model is not called). Anything less
        exact goes to the model, which has the tool."""
        logging_setup.audit({"tool": "voice", "decision": "pause_phrase", "heard_chars": len(heard), "minutes": minutes})
        self._report(f"pause phrase heard ({heard!r}, {minutes or 'no'} min)")
        vpause.pause(self._pause_file, by="voice", minutes=minutes)
        self.cues.stop()  # the heard cue and the thinking sound: the acknowledgement is spoken alone
        self._speak_phrase(vpause.spoken_pause_ack(self._language, minutes))
        self._sync_pause(reason="paused (voice)")  # releases the microphone and notifies
        if self.state != VoiceState.PAUSED:  # we are in TRANSCRIBING: land on PAUSED explicitly
            self._set_state(self._rest_state(), "paused (voice)")

    def _ask_resume(self) -> None:
        """Hotkey while paused: "Should I start listening again?" on screen; yes resumes, anything else keeps the pause."""
        question = vpause.resume_question(self._language)
        self._report("hotkey while paused: asking on screen whether to resume")
        answer = self._choice_dialog(question, vpause.resume_options(self._language), "no", timeout_s=self.v.confirm_timeout_s)
        logging_setup.audit({"tool": "voice", "decision": "resume_dialog", "answer": answer})
        self._report(f"resume dialog: {answer}")
        if answer == "yes":
            vpause.resume(self._pause_file)
            self._sync_pause(reason="resumed (hotkey dialog)")
        while self.hotkey is not None and self.hotkey.is_held and not self._stop.is_set():
            self.sleep(0.05)  # the rest of this hold must not start a recording

    def _speak_phrase(self, text: str) -> None:
        """One cacheable system sentence, then the usual post-speech wake-word suppression."""
        assert self.tts is not None
        self.tts.begin_turn()
        self.tts.speak(text, cacheable=True)
        self.tts.wait(timeout=10.0)
        if self.wake is not None:  # reset first: it clears any suppression, then the post-speech cooldown applies
            self.wake.reset()
            self.wake.suppress(self.v.post_speech_cooldown_s)

    def listen_for_answer(self, timeout_s: float, should_stop: Callable[[], bool] | None = None) -> str | None:
        """Used by the sink for spoken confirmations: one short utterance, or None for silence.
        `should_stop` (the dialog decided) ends the recording at once. The raw Whisper transcript
        is logged and audited, so a denied confirmation can be explained afterwards."""
        if should_stop is not None and should_stop():
            return None
        try:
            recording = self._record(
                mode="vad", no_speech_timeout_s=max(1.0, timeout_s), silence_ms=CONFIRM_LISTEN_SILENCE_MS, should_stop=should_stop
            )
        except Exception:
            log.exception("confirmation listening failed")
            return None
        if not recording.speech_started or recording.reason in ("no_speech", "stopped"):
            if recording.reason == "stopped":
                self._report(t("voice.transcript.confirm_stopped", self._language))
            return None
        try:
            text, language = self.stt.transcribe_array(vaudio.int16_to_float32(recording.audio))  # type: ignore[union-attr]
        except Exception:
            log.exception("confirmation transcription failed")
            return None
        cleaned = self.stt.clean(text)  # type: ignore[union-attr]
        log.info(
            "confirmation answer heard: raw=%r cleaned=%r (%s, %.0f ms, speech at %s ms, %s)",
            text,
            cleaned,
            language,
            recording.duration_ms,
            recording.speech_start_ms,
            recording.reason,
        )
        logging_setup.audit(
            {
                "tool": "voice",
                "decision": "confirm_answer",
                "raw": text,
                "cleaned": cleaned,
                "language": language,
                "duration_ms": round(recording.duration_ms, 1),
                "speech_start_ms": recording.speech_start_ms,
                "recording_reason": recording.reason,
            }
        )
        self._report(t("voice.transcript.confirm_answer", self._language, cleaned=repr(cleaned), raw=repr(text)))
        return cleaned

    # --- turns ------------------------------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d")

    def _ensure_assistant(self) -> Any:
        if self._assistant is None:
            self._assistant = self.assistant_factory(self.config, self.paths)
            self._session_date = self._today()
        elif self._session_date != self._today():
            self._assistant.new_session(resume=True)  # one session per day; a chat over midnight goes on
            self._session_date = self._today()
        return self._assistant

    def _on_first_audio(self) -> None:
        if self.cues.stop("heard"):  # the reply starts: the cue must not play over it
            self._report("cue heard stopped (first audio)")
        self._stop_thinking("first audio")
        if self._timings.first_audio is None:
            self._timings.first_audio = self.clock()
            if self._timings.speech_end is not None:
                self._report(f"first audio {(self._timings.first_audio - self._timings.speech_end) * 1000:.0f} ms after end of speech")

    def _on_sentence(self, text: str, voice: str | None) -> None:
        self._stop_thinking("speech")  # every sentence start: the loop never plays under Jarvis's voice
        self._report(f"🔊 [{voice or 'default'}] {text}")

    def make_sink(self) -> VoiceOutputSink:
        assert self.tts is not None
        if self._notify is None:
            self._notify = _default_notifier(self.config)
        self._sink = VoiceOutputSink(
            self.tts,
            confirm_phrases=list(self.v.confirm_phrases),
            confirm_timeout_s=self.v.confirm_timeout_s,
            listen=self.listen_for_answer,
            notify=self._notify,
            on_text=lambda text: self._report(f"💬 {text}"),
            on_event=lambda text: self._report(f"ℹ️ {text}"),
            on_attention=self._on_attention,
            on_attention_done=self._on_attention_done,
            language=self._language,
            clock=self.clock,
            sleep=self.sleep,
            use_cloud_for_tainted=self.v.elevenlabs_use_for_tainted,
            spawn_thread=spawn_daemon_thread,  # a dialog click settles the confirmation while the microphone is busy
        )
        return self._sink

    def _wait_for_speech(self) -> None:
        assert self.tts is not None
        while not self._stop.is_set() and not self._pending_barge_in:
            if self.tts.wait(timeout=SPEAK_WAIT_POLL_S):
                return

    def handle_utterance(self, text: str, sink: VoiceOutputSink) -> Any:
        """THINKING -> SPEAKING -> IDLE for one utterance; returns the TurnResult (or None on error)."""
        assistant = self._ensure_assistant()
        sink.begin_turn(self._language)
        self._set_state(VoiceState.THINKING, f"{len(text)} chars")
        result = None
        try:
            result = assistant.handle(text, sink)
        except Exception as exc:  # the Assistant catches its own errors; this is the last net
            log.exception("turn crashed in the voice service")
            self._report(f"turn crashed: {exc}")
        self._stop_thinking("turn done")  # a reply without any audio (or a crash) must not leave it looping
        self._set_state(VoiceState.SPEAKING, "reply streamed" if not sink.cancelled else "cancelled")
        self._wait_for_speech()
        self._timings.tts_done = self.clock()
        self._sync_pause()  # a `pause_listening` call in this turn takes effect now: microphone released, notified
        if self.wake is not None:  # reset first: it clears any suppression, then the post-speech cooldown applies
            self.wake.reset()
            self.wake.suppress(self.v.post_speech_cooldown_s)
        stages = self._timings.compute()
        turn_id = getattr(result, "turn_id", None) if result is not None else None
        if turn_id:
            self._store_timings(turn_id, stages)
        if self._follow_up_turn is not None:
            window_s, speech_after_ms = self._follow_up_turn
            self._follow_up_turn = None
            self._record_follow_up("turn", window_s, turn_id=turn_id, speech_after_ms=speech_after_ms)
        if stages:
            self._report("timings: " + ", ".join(f"{k} {v:.0f} ms" for k, v in stages.items()))
        # A reply that asked something: the next utterance is probably a short answer (see `_transcribe`).
        self._last_reply_was_question = bool(result is not None and not sink.cancelled and self._is_question(sink.reply_text))
        # The window opens only now, after the last sentence has been played (Jarvis must not hear itself).
        self._follow_up_pending = self._wants_follow_up(sink, result)
        self._set_state(self._rest_state(), "turn done" if not sink.cancelled else "interrupted")
        return result

    def _database(self) -> Any | None:
        db = self._db
        if db is None and self._assistant is not None:
            db = getattr(self._assistant, "db", None)
        return db

    def _voice_event(self, kind: str, detail: str | None = None) -> None:
        """Counted by the dashboard's Voice analytics (wake-word triggers, dropped transcripts); never fatal."""
        db = self._database()
        add = getattr(db, "add_voice_event", None)
        if add is None:
            return
        try:
            add(kind, detail)
        except Exception as exc:
            log.warning("could not store the voice event %s: %s", kind, exc)

    def _store_timings(self, turn_id: str, stages: dict[str, float]) -> None:
        db = self._database()
        if db is None:
            return
        for stage, ms in stages.items():
            if stage == "ttft":
                continue  # the Brain already recorded it
            try:
                db.add_timing(turn_id, stage, ms)
            except Exception as exc:
                log.warning("could not store timing %s: %s", stage, exc)
        raw = getattr(self, "_last_raw_transcript", None)
        set_raw = getattr(db, "set_turn_raw_transcript", None)
        if raw is not None and set_raw is not None:
            try:
                set_raw(turn_id, raw)
            except Exception as exc:
                log.warning("could not store the raw transcript: %s", exc)
            self._last_raw_transcript = None
        refresh = getattr(db, "add_turn_summary", None)
        if refresh is not None:
            try:
                refresh(turn_id)  # the summary row now includes the voice stages
            except Exception as exc:
                log.warning("could not refresh the turn summary: %s", exc)

    def cancel(self, reason: str = "cancel") -> None:
        """Back to IDLE from any state: stop listening, mute the reply, kill speech."""
        state = self.state
        log.info("cancel requested in state %s (%s)", state.value, reason)
        self._cancel_listen = True
        if self._sink is not None:
            self._sink.cancel()
        if self.tts is not None:
            self.tts.stop()
        self.cues.stop()
        self._trigger = None
        self._trigger_event.clear()
        if state in (VoiceState.THINKING, VoiceState.SPEAKING):
            return  # `handle_utterance` finishes the transition once the turn unwinds
        if state != VoiceState.STOPPED:
            self._set_state(self._rest_state(), reason)

    def run(self) -> None:
        """Start everything and serve utterances until `request_stop()`."""
        self.start()
        source = VoiceInputSource(self.next_utterance)
        sink = self.make_sink()
        self._report(
            "ready: listening is paused; hold the hotkey to be asked to resume, or run `jarvis voice resume`"
            if self._paused
            else 'ready: say "Hey Jarvis" or hold the hotkey'
        )
        try:
            while not self._stop.is_set():
                try:
                    text = source.read()
                except JarvisError:
                    raise
                except Exception as exc:
                    log.exception("voice input failed")
                    self._report(f"input error: {exc}")
                    self._set_state(VoiceState.IDLE, "error")
                    self.sleep(1.0)
                    continue
                if text is None:
                    break
                self.handle_utterance(text, sink)
        finally:
            self.close()

    # --- calibration ------------------------------------------------------------------------------------------------

    def calibrate(self, seconds: float = 30.0, out: Callable[[str], None] = print) -> float:
        """Print live wake-word scores so the threshold can be tuned for the room. Returns the peak."""
        if self.wake is None:
            self.wake = WakeWordDetector(
                self.v.wake_word_model, threshold=self.v.wake_word_threshold, cooldown_s=self.v.wake_word_cooldown_s, clock=self.clock
            )
        if self.mic is None:
            self.mic = vaudio.Microphone()
        self.wake.load()
        self.mic.start()
        out(t("voice.service.calibrate_intro", seconds=f"{seconds:.0f}", threshold=self.v.wake_word_threshold))
        peak = 0.0
        deadline = self.clock() + seconds
        last_line = 0.0
        try:
            while self.clock() < deadline and not self._stop.is_set():
                frame = self.mic.read(timeout=MIC_READ_TIMEOUT_S)
                if frame is None:
                    continue
                score = self.wake.score(frame)
                peak = max(peak, score)
                now = self.clock()
                if score >= 0.05 or now - last_line >= 1.0:
                    bar = "█" * int(score * 40)
                    mark = t("voice.service.calibrate_detection") if score >= self.v.wake_word_threshold else ""
                    out(f"{score:5.2f} {bar}{mark}")
                    last_line = now
        finally:
            self.mic.stop()
        out(t("voice.service.calibrate_peak", peak=f"{peak:.2f}"))
        return peak


# --- CLI entry points -------------------------------------------------------------------------------------------------


def preflight(config: Config) -> list[str]:
    """Reasons `jarvis voice` cannot start (empty = go)."""
    problems: list[str] = []
    if not config.general.use_voice:
        problems.append(t("voice.service.use_voice_off"))
    missing = VoiceService.missing_dependencies()
    if missing:
        problems.append(t("voice.service.missing_packages", packages=", ".join(missing)))
    import shutil  # noqa: PLC0415

    for tool in ("say", "afplay", "osascript"):
        if shutil.which(tool) is None:
            problems.append(t("voice.service.needs_macos", tool=tool))
    return problems


def _record_start(paths: Paths) -> None:
    """Tell the dashboard when this agent started and which config version it loaded (never fatal)."""
    try:
        from jarvis import services  # noqa: PLC0415
        from jarvis.observability.db import Database  # noqa: PLC0415

        db = Database(paths.db_file)
        try:
            services.record_start(db, "voice", paths)
        finally:
            db.close()
    except Exception:
        log.debug("could not record the voice agent start", exc_info=True)


def run_service(config: Config, paths: Paths, *, foreground: bool = False, out: Callable[[str], None] = print) -> int:
    """`jarvis voice run [--foreground]`."""
    problems = preflight(config)
    if problems:
        for p in problems:
            out(p)
        return 1
    mic_perm = vaudio.microphone_permission()
    if mic_perm == "denied":
        out(t("voice.service.mic_denied"))
    from jarvis.voice.hotkey import input_monitoring_granted  # noqa: PLC0415

    if input_monitoring_granted() is False:
        out(t("voice.service.input_monitoring"))
    lock = acquire_service_lock(paths)
    if lock is None:
        out(t("voice.service.already_running", lock=paths.voice_lock_file))
        return 1
    service = VoiceService(config, paths, foreground=foreground, reporter=out if foreground else None)
    _record_start(paths)

    def _terminate(signum, frame) -> None:  # noqa: ARG001 - launchctl bootout sends SIGTERM
        service.request_stop()
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:  # not the main thread
        pass
    out(t("voice.service.starting_foreground" if foreground else "voice.service.starting"))
    try:
        service.run()
    except KeyboardInterrupt:
        pass
    except VoiceUnavailable as exc:
        out(str(exc))
        return 1
    finally:
        service.close()
        release_service_lock(lock)
    return 0


def acquire_service_lock(paths: Paths) -> Any | None:
    """flock on data/voice.lock: None when another voice service holds it. Two services at once each
    hear the user and run their own Assistant, so a question asked by one is answered to the other."""
    import fcntl  # noqa: PLC0415

    paths.voice_lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(paths.voice_lock_file, "a+", encoding="utf-8")  # noqa: SIM115 - kept open for the service's lifetime
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def release_service_lock(lock: Any | None) -> None:
    if lock is None:
        return
    import fcntl  # noqa: PLC0415

    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        lock.close()


def run_calibration(config: Config, paths: Paths, *, seconds: float = 30.0, out: Callable[[str], None] = print) -> int:
    """`jarvis voice calibrate`: live wake-word scores (no API, no assistant)."""
    missing = VoiceService.missing_dependencies()
    if missing:
        out(t("voice.service.missing_packages", packages=", ".join(missing)))
        return 1
    service = VoiceService(config, paths)
    try:
        service.calibrate(seconds, out)
    except KeyboardInterrupt:
        pass
    return 0
