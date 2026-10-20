"""Voice cue sounds: `voice_heard`, `voice_ready` and the looping `thinking_sound`.

- `heard` plays right after a recording ends (VAD end of speech or hotkey release) so the user
  knows Jarvis got the request during the silence of transcription and thinking. It is
  non-blocking: transcription and the API call start while it plays, and the service stops it as
  soon as the reply's first audio starts so the two never overlap.
- `ready` plays when the reply has been played to the end and the follow-up window is about to
  open. It blocks the microphone: the window (and its seconds) starts only after the cue has
  finished, so the microphone never records the cue (no self-trigger, no fake transcript).
- `thinking` (`ThinkingLoop`) loops quietly while Jarvis works on the answer, so silence never means
  "stuck": it is armed right after `heard`, waits for that cue to finish and then `thinking_delay_ms`
  more, and only starts when no spoken reply arrived meanwhile (fast replies stay clean). It loops
  (with `thinking_gap_ms` of silence between runs) through transcription, API calls and tool calls,
  and is killed the moment the reply's first audio, a confirmation, an error, a barge-in or a pause
  arrives. It never plays over Jarvis's voice.

Files live under `<JARVIS_HOME>/sounds/` as `<name>.m4a`, `.mp3`, `.wav` or `.aiff` (first found
wins, in that order). Without a file a macOS system sound plays instead; nothing here raises.
Everything goes through `afplay` as argv (never a shell); `volume` becomes `afplay -v`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from jarvis.config import VoiceSoundsConfig

log = logging.getLogger("jarvis.voice.cues")

CUE_EXTENSIONS = (".m4a", ".mp3", ".wav", ".aiff")
CUES = ("heard", "ready", "thinking")
# System sounds used when sounds/<name>.* is missing. `ready` keeps the Purr the follow-up window
# used before this change; `heard` is a short, neutral sound distinct from Tink (listen) and Pop (no
# speech); `thinking` is a low, soft tone that can repeat without grating.
FALLBACK_SOUNDS = {
    "heard": "/System/Library/Sounds/Bottle.aiff",
    "ready": "/System/Library/Sounds/Purr.aiff",
    "thinking": "/System/Library/Sounds/Submarine.aiff",
}
MAX_CUE_S = 3.0  # a blocking cue never holds the microphone longer than this, whatever the file length
POLL_S = 0.02
AUDIBLE_RMS = 0.01  # a 50 ms window quieter than this (RMS of full scale) is silence
AUDIBLE_MARGIN_S = 0.1  # the room's echo of the cue's last sound


def audible_seconds(path: Path, *, ffmpeg: str | None = None, run: Callable[..., Any] = subprocess.run) -> float | None:
    """Where the sound in a cue file ends (a 2 s file may hold a 0.45 s chime and silence). None when
    it cannot be measured (no ffmpeg, an unreadable file): callers then wait for the whole file."""
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    try:
        proc = run(
            [ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    import numpy as np  # noqa: PLC0415

    samples = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype=np.int16).astype(np.float64) / 32768.0
    window = 800  # 50 ms at 16 kHz
    last = -1
    for i, start in enumerate(range(0, len(samples) - window + 1, window)):
        if float(np.sqrt(np.mean(np.square(samples[start : start + window])))) > AUDIBLE_RMS:
            last = i
    return (last + 1) * window / 16000.0 if last >= 0 else 0.0


@dataclass(frozen=True)
class CueFile:
    cue: str
    name: str  # the configured base name (e.g. "voice_heard")
    path: Path | None  # the file that will play (custom or fallback); None when nothing is playable
    custom: bool  # True: found under sounds/; False: the macOS fallback

    @property
    def missing_custom(self) -> bool:
        return not self.custom


def find_cue_file(sounds_dir: Path, name: str) -> Path | None:
    """`sounds/<name>.m4a`, then `.mp3`, `.wav`, `.aiff`: the first regular file wins."""
    for ext in CUE_EXTENSIONS:
        candidate = sounds_dir / f"{name}{ext}"
        try:
            if candidate.is_file():
                return candidate
        except OSError as exc:  # unreadable directory: treated as missing
            log.debug("cannot stat %s: %s", candidate, exc)
    return None


class CueHandle:
    """One running `afplay`; `stop()` kills it, `done()` reports whether it finished."""

    def __init__(self, cue: str, proc: Any) -> None:
        self.cue = cue
        self._proc = proc

    def done(self) -> bool:
        try:
            return self._proc.poll() is not None
        except Exception:  # a fake or a dead handle
            return True

    def stop(self) -> None:
        if self.done():
            return
        try:
            self._proc.kill()
        except Exception as exc:  # already gone
            log.debug("cue kill failed: %s", exc)


class CuePlayer:
    """Resolves and plays the cues. Thread-safe: `stop()` may come from the TTS worker (first
    audio) or the hotkey thread (barge-in) while the main thread waits on a blocking cue."""

    def __init__(
        self,
        sounds_dir: Path,
        settings: "VoiceSoundsConfig",
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        fallbacks: dict[str, str] | None = None,
    ) -> None:
        self.sounds_dir = Path(sounds_dir)
        self.settings = settings
        self._popen = popen
        self._which = which
        self._clock = clock
        self._sleep = sleep
        self._fallbacks = dict(FALLBACK_SOUNDS if fallbacks is None else fallbacks)
        self._lock = threading.Lock()
        self._current: CueHandle | None = None
        self.thinking = ThinkingLoop(self)

    # --- resolution -------------------------------------------------------------------------------------

    def name_for(self, cue: str) -> str:
        return {"heard": self.settings.heard, "ready": self.settings.ready, "thinking": self.settings.thinking}[cue]

    def enabled(self, cue: str) -> bool:
        return {
            "heard": self.settings.heard_enabled,
            "ready": self.settings.ready_enabled,
            "thinking": self.settings.thinking_enabled,
        }[cue]

    def volume_for(self, cue: str) -> float:
        return float(self.settings.thinking_volume if cue == "thinking" else self.settings.volume)

    def resolve(self, cue: str) -> CueFile:
        """The file that would play for `cue` right now (looked up on every call: the user may add
        a file while the service runs)."""
        name = self.name_for(cue)
        custom = find_cue_file(self.sounds_dir, name)
        if custom is not None:
            return CueFile(cue, name, custom, True)
        fallback = Path(self._fallbacks.get(cue, ""))
        if fallback.name and fallback.is_file():
            return CueFile(cue, name, fallback, False)
        return CueFile(cue, name, None, False)

    def describe(self, cue: str) -> str:
        """One line for the foreground log and `jarvis doctor`."""
        if not self.enabled(cue):
            return f"{cue}: disabled"
        file = self.resolve(cue)
        if file.custom:
            return f"{cue}: {file.path}"
        expected = ", ".join(f"{file.name}{ext}" for ext in CUE_EXTENSIONS)
        where = f"{self.sounds_dir}/ has no {expected}"
        return f"{cue}: {where}; playing the macOS fallback {file.path}" if file.path else f"{cue}: {where} and no fallback: silent"

    # --- playback ---------------------------------------------------------------------------------------

    def argv_for(self, cue: str) -> list[str] | None:
        """The `afplay` command line for the cue, or None when it is disabled or nothing can play."""
        if not self.enabled(cue):
            return None
        file = self.resolve(cue)
        if file.path is None:
            log.warning("no sound for cue %s (%s)", cue, file.name)
            return None
        afplay = self._which("afplay")
        if afplay is None:
            log.warning("afplay not found; cannot play cue %s", cue)
            return None
        return [afplay, "-v", f"{self.volume_for(cue):g}", str(file.path)]

    def spawn(self, cue: str, argv: list[str]) -> Any | None:
        """Start one `afplay` (argv, never a shell); None when the process could not start."""
        try:
            return self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("afplay failed for cue %s: %s", cue, exc)
            return None

    def play(self, cue: str) -> CueHandle | None:
        """Start the cue (non-blocking). None when disabled or nothing can play. Any cue still
        playing is stopped first: cues never overlap each other. The thinking loop has its own
        `thinking.start()`; this method is for the one-shot cues."""
        argv = self.argv_for(cue)
        if argv is None:
            return None
        with self._lock:
            if self._current is not None:
                self._current.stop()
            proc = self.spawn(cue, argv)
            if proc is None:
                self._current = None
                return None
            handle = CueHandle(cue, proc)
            self._current = handle
        log.debug("cue %s: %s", cue, argv[-1])
        return handle

    def audible_s(self, cue: str) -> float | None:
        """Seconds of actual sound in the cue's file (measured once per file), None when unknown."""
        path = self.resolve(cue).path
        if path is None:
            return None
        cache = self.__dict__.setdefault("_audible_cache", {})
        if path not in cache:
            measure = getattr(self, "measure_audible", None) or audible_seconds
            try:
                cache[path] = measure(path)
            except Exception as exc:
                log.debug("could not measure %s: %s", path, exc)
                cache[path] = None
            if cache[path] is not None:
                log.info("cue %s: sound ends at %.2f s", cue, cache[path])
        return cache[path]

    def play_blocking(
        self, cue: str, *, should_stop: Callable[[], bool] | None = None, max_s: float = MAX_CUE_S, until_silent: bool = False
    ) -> bool:
        """Play the cue and return once it has finished (or `max_s` passed). With `until_silent` it returns
        as soon as the audible part is over (+ a short echo margin) and lets the file's silent tail play
        out unheard. Returns False when `should_stop()` turned true meanwhile (the cue is killed at once);
        True otherwise, including when there was nothing to play."""
        audible = self.audible_s(cue) if until_silent else None
        handle = self.play(cue)
        if handle is None:
            return not (should_stop is not None and should_stop())
        started = self._clock()
        deadline = started + max_s
        if audible is not None:
            deadline = min(deadline, started + audible + AUDIBLE_MARGIN_S)
        while not handle.done():
            if should_stop is not None and should_stop():
                handle.stop()
                return False
            if self._clock() >= deadline:
                if audible is not None and self._clock() - started < max_s:
                    break  # the sound is over; the silent rest of the file may play on
                log.warning("cue %s ran longer than %.1f s; stopped", cue, max_s)
                handle.stop()
                break
            self._sleep(POLL_S)
        return True

    def stop(self, cue: str | None = None) -> bool:
        """Kill the cue that is playing (only `cue` when given; "thinking" means the loop). Without
        a name everything stops, the thinking loop included (barge-in, cancel, pause, close). True
        when something was stopped."""
        stopped = False
        if cue in (None, "thinking"):
            stopped = self.thinking.stop()
            if cue == "thinking":
                return stopped
        with self._lock:
            handle = self._current
            if handle is None or (cue is not None and handle.cue != cue):
                return stopped
            was_playing = not handle.done()
            handle.stop()
            self._current = None
        return stopped or was_playing

    @property
    def playing(self) -> str | None:
        with self._lock:
            if self._current is not None and not self._current.done():
                return self._current.cue
            return None


class ThinkingLoop:
    """The looping `thinking` sound (see the module docstring). Thread-safe: `start()` runs on the
    service's main thread, `stop()` comes from the TTS worker (first audio), the hotkey thread
    (barge-in), the audio thread (pause) or the sink (confirmation, error).

    `start(after=heard_handle)` arms the loop: a worker thread waits for the heard cue to finish,
    then `thinking_delay_ms` more, and only then starts looping `afplay`; a `stop()` at any point
    before that means the sound is never heard. One loop at a time: a second `start()` while armed or
    looping is a no-op."""

    def __init__(self, player: CuePlayer) -> None:
        self._player = player
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: Any | None = None
        self._active = False  # armed or looping; cleared by stop() and when the worker ends
        self.loops = 0  # afplay runs started since the service began (tests, the foreground log)
        self.on_start: Callable[[], None] | None = None  # called once, when the first loop starts

    @property
    def settings(self) -> "VoiceSoundsConfig":
        return self._player.settings

    @property
    def armed(self) -> bool:
        """Waiting for the heard cue or the delay, or looping."""
        return self._active

    @property
    def playing(self) -> bool:
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False

    def start(self, *, after: CueHandle | None = None) -> bool:
        """Arm the loop. False when disabled, nothing can play, or it is already armed."""
        if not self._player.enabled("thinking"):
            return False
        if self._player.resolve("thinking").path is None or self._player._which("afplay") is None:
            return False
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive() and self._active:
                return False  # one loop at a time
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)  # stopped a moment ago: let the worker finish its cleanup (it needs the lock)
        with self._lock:
            if self._active or (self._thread is not None and self._thread.is_alive()):
                return False
            self._stop.clear()
            self._active = True
            self._thread = threading.Thread(target=self._run, args=(after,), name="jarvis-voice-thinking", daemon=True)
            self._thread.start()
        return True

    def _wait(self, seconds: float) -> bool:
        """Sleep in small steps on the player's clock; True when stop() arrived meanwhile."""
        deadline = self._player._clock() + seconds
        while self._player._clock() < deadline:
            if self._stop.is_set():
                return True
            self._player._sleep(min(POLL_S, max(0.0, deadline - self._player._clock())) or POLL_S)
        return self._stop.is_set()

    def _run(self, after: CueHandle | None) -> None:
        try:
            # 1. Never over the heard cue: wait for it to end (it is short; a stop() wins at once).
            while after is not None and not after.done():
                if self._stop.is_set():
                    return
                self._player._sleep(POLL_S)
            # 2. The delay: a reply whose audio starts in time stops the loop before anything plays.
            if self._wait(self.settings.thinking_delay_ms / 1000.0):
                return
            first = True
            while not self._stop.is_set():
                argv = self._player.argv_for("thinking")  # re-resolved each loop: the file may appear or go
                if argv is None:
                    return
                with self._lock:
                    if self._stop.is_set():
                        return
                    proc = self._player.spawn("thinking", argv)
                    if proc is None:
                        return
                    self._proc = proc
                    self.loops += 1
                if first:
                    first = False
                    log.debug("thinking sound started: %s", argv[-1])
                    if self.on_start is not None:
                        try:
                            self.on_start()
                        except Exception:
                            log.exception("thinking on_start hook failed")
                while proc.poll() is None:
                    if self._stop.is_set():
                        _kill(proc)
                        break
                    self._player._sleep(POLL_S)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                if self._stop.is_set():
                    return
                if self.settings.thinking_gap_ms and self._wait(self.settings.thinking_gap_ms / 1000.0):
                    return
        except Exception:  # the loop must never take the service down
            log.exception("thinking loop failed")
        finally:
            with self._lock:
                proc, self._proc = self._proc, None
                if self._thread is threading.current_thread():
                    self._active = False
            if proc is not None:
                _kill(proc)

    def stop(self) -> bool:
        """Kill the sound now and disarm. True when it was armed or playing. Never blocks: the
        event is set and the process killed under the lock, so the worker cannot start another
        `afplay` afterwards (it checks the event under the same lock before every spawn) and is
        left to finish on its own."""
        with self._lock:
            was_active, self._active = self._active, False
            self._stop.set()
            proc = self._proc
        if proc is not None:
            _kill(proc)  # at once, on the caller's thread: the worker only cleans up
        return was_active


def _kill(proc: Any) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception as exc:  # already gone, or a fake
        log.debug("thinking kill failed: %s", exc)
