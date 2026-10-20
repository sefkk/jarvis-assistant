"""Alarms: what happens when a row of kind `alarm` fires, and the shared state the other processes
use to stop it (tools/alarms.py has the tools, the CLI and the doctor).

An alarm is a timer row whose `pomodoro_state_json` holds an `AlarmState` (clock time, repeat days,
snooze origin, the phone-backup reminder id). The scheduler fires it like any timer, then the
`AlarmHandler` here takes over instead of a plain notification:

- on time: a Telegram message when the phone backup says so, then the `Ringer` in a thread of the
  scheduler process: the system volume goes up to `alarm.volume`, `sounds/alarm.*` loops through
  `afplay` (a macOS system sound when the file is missing; our own sound, so Do Not Disturb cannot
  mute it), and a dialog with "Stop" / "Snooze (9 min)" (in the main language) is on screen. The ring
  ends with a button, a stop or snooze request from another process (the command file: the tools, the
  push-to-talk hotkey, a spoken "ok" / "stop" / "snooze" or "tamam" / "kapat" / "ertele", Telegram's
  /stop_alarm, `jarvis alarms stop`), or `alarm.max_ring_min`. The volume is restored afterwards, whatever happened.
- late (the Mac was asleep; `late_after_s` of the engine): no ringing at a random moment after the
  wake, a "missed" notification on the Mac and on Telegram instead.
- a repeating alarm schedules its next occurrence before anything else, so a broken sound cannot
  stop the series.

Cross-process state lives in two small JSON files under data/: `alarm_ringing.json` while an alarm
rings (id, label, started_at, pid) and `alarm_command.json` for a stop / snooze request. `caffeinate -i`
keeps the Mac from idle sleep while an alarm is due within `alarm.keep_awake_hours` (`KeepAwake`,
driven by a periodic scheduler job); it does not stop a closed lid or a manual sleep, which is why
setting an alarm warns about the lid and the battery.

The phone backup (`PhoneBackup`) creates an Apple Reminder in the list `alarm.reminder_list` at the
alarm time (synced through iCloud) and removes it when the alarm is stopped on the Mac. It is a normal
timed reminder: EventKit on current macOS has no API for the alarm-style "Urgent" reminders that
iOS 26.2 added, so it notifies like a reminder, it does not ring like an alarm.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field, ValidationError

from jarvis.i18n import t, t_all
from jarvis.observability import logging_setup
from jarvis.scheduler.notify import Notifier
from jarvis.scheduler.store import KIND_ALARM, TimerRow, TimerStore
from jarvis.utils.applescript import run_applescript
from jarvis.utils.text import fold, fold_words
from jarvis.utils.timeutil import describe_repeat, format_local, now_local
from jarvis.voice.cues import CUE_EXTENSIONS, find_cue_file

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.db import Database
    from jarvis.scheduler.engine import FiredTimer

log = logging.getLogger("jarvis.scheduler.alarm")

NOTIFICATION_TITLE = "Jarvis Alarm"
DIALOG_TITLE = "Jarvis Alarm"
FALLBACK_SOUND = "/System/Library/Sounds/Sosumi.aiff"  # loud and short: loops cleanly
POLL_S = 0.2
COMMAND_MAX_AGE_S = 120.0  # an older stop/snooze request is stale (nothing was ringing when it was written)
ACK_WAIT_S = 3.0  # how long a stop/snooze request waits for the ringer to acknowledge

# What the user reads (notifications, Telegram, the dialog) comes from locales/<language>/alarm.toml. Other
# modules read these names as attributes (`alarm.STOPPED_TEXT`); each access returns the text in the current
# main language, so a template here is unformatted.
_TEXTS = {
    "RING_TEXT": "alarm.notify.ring",
    "DIALOG_TEXT": "alarm.dialog.text",
    "MISSED_TEXT": "alarm.notify.missed",
    "TIMEOUT_TEXT": "alarm.notify.timeout",
    "SNOOZED_TEXT": "alarm.notify.snoozed",
    "STOPPED_TEXT": "alarm.notify.stopped",
    "NOT_RINGING_TEXT": "alarm.notify.not_ringing",
    "STOP_LABEL": "alarm.dialog.stop",
    "SNOOZE_LABEL": "alarm.dialog.snooze",
}
BACKUP_FAILED_TEXT = "The phone backup (Reminder) could not be created: {error}"  # for the model


def __getattr__(name: str) -> str:
    key = _TEXTS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return t(key)


# Whole-utterance words that stop or snooze the ringing alarm without the wake word (voice) or the
# model (Telegram, CLI), in every language at once. A snooze word anywhere means snooze; the stop words
# must make up the whole message.
STOP_WORDS = frozenset(fold_words(t_all("alarm.words.stop")))
SNOOZE_MARKERS = tuple(sorted(fold_words(t_all("alarm.words.snooze"))))


# --- the state stored in the row ----------------------------------------------------------------------


class AlarmState(BaseModel):
    """Everything about an alarm beyond label and fire time, stored as JSON in the timers row."""

    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    repeat: list[int] = Field(default_factory=list)  # weekdays, 0 = Monday; empty = one shot
    phone_backup: str = "both"  # the effective `alarm.phone_backup` when it was set
    reminder_id: str | None = None  # the Apple Reminder that backs this occurrence on the phone
    snoozed_from: int | None = None  # the row this snooze came from
    snooze_count: int = 0

    @classmethod
    def from_json(cls, text: str | None) -> "AlarmState | None":
        if not text:
            return None
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            log.error("corrupt alarm state ignored: %s", exc)
            return None

    @property
    def wants_reminder(self) -> bool:
        return self.phone_backup in ("reminder", "both")

    @property
    def wants_telegram(self) -> bool:
        return self.phone_backup in ("telegram", "both")

    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def describe_repeat(self, language: str | None = None) -> str:
        return describe_repeat(self.repeat, language)

    def for_next_occurrence(self) -> "AlarmState":
        return self.model_copy(update={"reminder_id": None, "snoozed_from": None, "snooze_count": 0})

    def for_snooze(self, from_id: int) -> "AlarmState":
        return self.model_copy(update={"reminder_id": None, "snoozed_from": from_id, "snooze_count": self.snooze_count + 1, "repeat": []})


def pending_alarms(store: TimerStore) -> list[TimerRow]:
    return store.pending(KIND_ALARM)


def next_alarm(store: TimerStore) -> TimerRow | None:
    rows = pending_alarms(store)
    return rows[0] if rows else None


# --- stop / snooze words ---------------------------------------------------------------------------------


def _clean(text: str) -> str:
    return " ".join(fold(text).strip().strip(" .!?,;:\"'").split())


def classify_alarm_word(text: str) -> str | None:
    """'stop', 'snooze' or None for what the user said or typed while an alarm rings."""
    cleaned = _clean(text)
    if not cleaned:
        return None
    if any(marker in cleaned for marker in SNOOZE_MARKERS):
        return "snooze"
    if cleaned in STOP_WORDS:
        return "stop"
    words = cleaned.split()
    if len(words) <= 3 and all(w in STOP_WORDS for w in words):
        return "stop"  # "tamam kapat", "dur dur"
    return None


# --- the ring state file and the command file ----------------------------------------------------------


@dataclass
class RingInfo:
    alarm_id: int
    label: str
    fire_at: str
    started_at: str
    pid: int
    max_ring_s: float

    def age_s(self, now: datetime | None = None) -> float:
        try:
            started = datetime.fromisoformat(self.started_at)
        except ValueError:
            return float("inf")
        return ((now or datetime.now(timezone.utc)) - started).total_seconds()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("%s is unreadable (%s); dropped", path, exc)
        _unlink(path)
        return None
    return raw if isinstance(raw, dict) else None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def ringing(paths: "Paths", *, now: datetime | None = None) -> RingInfo | None:
    """The alarm ringing right now, or None. A file left behind by a dead ringer (the scheduler was
    killed mid-ring) or older than its maximum ring time is removed and counts as not ringing."""
    raw = _read_json(paths.alarm_ring_file)
    if raw is None:
        return None
    try:
        info = RingInfo(
            int(raw["alarm_id"]),
            str(raw.get("label") or ""),
            str(raw.get("fire_at") or ""),
            str(raw.get("started_at") or ""),
            int(raw.get("pid") or 0),
            float(raw.get("max_ring_s") or 0),
        )
    except (KeyError, TypeError, ValueError):
        _unlink(paths.alarm_ring_file)
        return None
    if not _pid_alive(info.pid) or info.age_s(now) > info.max_ring_s + 120:
        log.info("stale ring file for alarm %s removed", info.alarm_id)
        _unlink(paths.alarm_ring_file)
        return None
    return info


def request_command(
    paths: "Paths", action: str, *, by: str, minutes: int | None = None, wait_s: float = 0.0, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Ask the ringer to stop or snooze. False when nothing rings (nothing is written). With `wait_s`
    the call waits for the ringer to take the file down and returns whether it did."""
    if action not in ("stop", "snooze"):
        raise ValueError(f"unknown alarm command {action!r}")
    info = ringing(paths)
    if info is None:
        return False
    payload = {
        "action": action,
        "by": by[:40],
        "minutes": minutes,
        "alarm_id": info.alarm_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(paths.alarm_command_file, payload)
    logging_setup.audit({"tool": "alarm", "decision": f"{action}_requested", "by": by, "alarm_id": info.alarm_id, "minutes": minutes})
    deadline = time.monotonic() + wait_s
    while wait_s > 0 and paths.alarm_ring_file.is_file():
        if time.monotonic() >= deadline:
            return False
        sleep(0.1)
    return True


def request_stop(paths: "Paths", *, by: str, wait_s: float = 0.0, sleep: Callable[[float], None] = time.sleep) -> bool:
    return request_command(paths, "stop", by=by, wait_s=wait_s, sleep=sleep)


def request_snooze(
    paths: "Paths", *, by: str, minutes: int | None = None, wait_s: float = 0.0, sleep: Callable[[float], None] = time.sleep
) -> bool:
    return request_command(paths, "snooze", by=by, minutes=minutes, wait_s=wait_s, sleep=sleep)


def take_command(paths: "Paths", *, now: datetime | None = None) -> dict[str, Any] | None:
    """Read and remove the pending command; None when there is none or it is stale."""
    raw = _read_json(paths.alarm_command_file)
    if raw is None:
        return None
    _unlink(paths.alarm_command_file)
    if raw.get("action") not in ("stop", "snooze"):
        return None
    try:
        created = datetime.fromisoformat(str(raw.get("created_at") or ""))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if ((now or datetime.now(timezone.utc)) - created).total_seconds() > COMMAND_MAX_AGE_S:
        return None
    return raw


# --- sound, volume, dialog -------------------------------------------------------------------------------


@dataclass(frozen=True)
class AlarmSound:
    name: str
    path: Path | None
    custom: bool

    def describe(self, sounds_dir: Path) -> str:
        if self.custom:
            return str(self.path)
        expected = ", ".join(f"{self.name}{ext}" for ext in CUE_EXTENSIONS)
        where = f"{sounds_dir}/ has no {expected}"
        return f"{where}; the macOS fallback {self.path} loops instead" if self.path else f"{where} and no fallback: silent"


def resolve_sound(paths: "Paths", config: "Config", *, fallback: str = FALLBACK_SOUND) -> AlarmSound:
    """`sounds/alarm.m4a|.mp3|.wav|.aiff` (first found), else the macOS fallback. Same lookup as the voice cues."""
    name = config.alarm.sound
    custom = find_cue_file(paths.sounds_dir, name)
    if custom is not None:
        return AlarmSound(name, custom, True)
    fb = Path(fallback)
    return AlarmSound(name, fb if fb.is_file() else None, False)


VOLUME_GET_SCRIPT = """on run argv
    set s to get volume settings
    return (output volume of s as text) & "," & (output muted of s as text)
end run"""

VOLUME_SET_SCRIPT = """on run argv
    set v to (item 1 of argv) as integer
    set m to item 2 of argv
    if m is "true" then
        set volume output volume v with output muted
    else
        set volume output volume v without output muted
    end if
    return "ok"
end run"""

# Durdur is the default button (Enter stops); Ertele is the cancel button (Escape snoozes). Values travel as argv.
DIALOG_SCRIPT = """on run argv
    set theText to item 1 of argv
    set stopLabel to item 2 of argv
    set snoozeLabel to item 3 of argv
    set secs to (item 4 of argv) as integer
    try
        set r to display dialog theText with title "Jarvis Alarm" buttons {snoozeLabel, stopLabel} default button stopLabel cancel button snoozeLabel giving up after secs with icon caution
        if gave up of r then return "timeout"
        return button returned of r
    on error number -128
        return "cancel"
    end try
end run"""


def read_volume(run: Callable[..., str] = run_applescript) -> tuple[int, bool] | None:
    """(output volume 0-100, muted) or None when it cannot be read."""
    try:
        out = run(VOLUME_GET_SCRIPT, [], timeout=10)
        volume, _, muted = out.strip().partition(",")
        return max(0, min(100, int(float(volume)))), muted.strip().lower() == "true"
    except Exception as exc:
        log.warning("could not read the system volume: %s", exc)
        return None


def set_volume(volume: int, muted: bool = False, run: Callable[..., str] = run_applescript) -> bool:
    try:
        run(VOLUME_SET_SCRIPT, [str(max(0, min(100, int(volume)))), "true" if muted else "false"], timeout=10)
        return True
    except Exception as exc:
        log.warning("could not set the system volume: %s", exc)
        return False


def lid_closed(run_argv: Callable[..., Any] | None = None) -> bool | None:
    """`ioreg -r -k AppleClamshellState -d 4`: True when the lid is closed; None when unknown."""
    from jarvis.tools.power import run_argv as power_run  # noqa: PLC0415

    try:
        proc = (run_argv or power_run)(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"])
    except Exception as exc:
        log.debug("clamshell state unavailable: %s", exc)
        return None
    match = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', proc.stdout or "")
    return match.group(1) == "Yes" if match else None


# --- the ringer ----------------------------------------------------------------------------------------------


@dataclass
class RingOutcome:
    alarm_id: int
    outcome: str  # stopped | snoozed | timeout
    by: str  # dialog | voice | hotkey | telegram | tool | cli | timeout
    minutes: int | None = None  # snooze length
    snooze_id: int | None = None  # the row the snooze created
    loops: int = 0
    volume_restored: bool | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class Ringer:
    """Plays one alarm to the end (blocking; the handler runs it in a thread). Every external effect
    is injectable: `popen` (afplay, the dialog), `run_applescript` (the volume), the clock and sleep."""

    def __init__(
        self,
        paths: "Paths",
        config: "Config",
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        run_applescript: Callable[..., str] = run_applescript,
        fallback_sound: str = FALLBACK_SOUND,
        db_factory: Callable[[], "Database"] | None = None,
        phone_backup: "PhoneBackup | None" = None,
        notifier: Notifier | None = None,
        persist: bool = True,
    ) -> None:
        self.paths = paths
        self.persist = persist  # False: `jarvis alarms test`, no snooze row, no reminder
        self.config = config
        self._popen = popen
        self._which = which
        self._clock = clock
        self._sleep = sleep
        self._run_applescript = run_applescript
        self._fallback = fallback_sound
        self._db_factory = db_factory
        self.phone_backup = phone_backup
        self.notifier = notifier  # Mac + Telegram, for the "rang to the end" message
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.current: RingInfo | None = None
        self.last: RingOutcome | None = None

    # --- state ---------------------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _db(self) -> "Database":
        if self._db_factory is not None:
            return self._db_factory()
        from jarvis.observability.db import Database  # noqa: PLC0415

        return Database(self.paths.db_file)

    # --- entry points --------------------------------------------------------------------------------------

    def start(self, row: TimerRow, state: AlarmState, *, blocking: bool = False) -> bool:
        """Ring `row` in a daemon thread (or inline). False when another alarm is already ringing."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if blocking:
                self._thread = None
            else:
                self._thread = threading.Thread(target=self._guarded_ring, args=(row, state), name="jarvis-alarm-ring", daemon=True)
                self._thread.start()
                return True
        self._guarded_ring(row, state)
        return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _guarded_ring(self, row: TimerRow, state: AlarmState) -> None:
        try:
            self.ring(row, state)
        except Exception:  # never let a ring take the scheduler down
            log.exception("alarm %s ring failed", row.id)
            _unlink(self.paths.alarm_ring_file)

    # --- the ring itself -----------------------------------------------------------------------------------

    def argv(self) -> list[str] | None:
        sound = resolve_sound(self.paths, self.config, fallback=self._fallback)
        afplay = self._which("afplay")
        if sound.path is None or afplay is None:
            log.warning("no alarm sound to play (%s, afplay %s)", sound.describe(self.paths.sounds_dir), afplay)
            return None
        return [afplay, "-v", f"{float(self.config.alarm.sound_volume):g}", str(sound.path)]

    def _spawn(self, argv: list[str], **kw: Any) -> Any | None:
        try:
            return self._popen(argv, stdin=subprocess.DEVNULL, stdout=kw.pop("stdout", subprocess.DEVNULL), stderr=subprocess.DEVNULL, **kw)
        except OSError as exc:
            log.warning("could not start %s: %s", argv[0], exc)
            return None

    def _start_dialog(self, row: TimerRow, state: AlarmState, max_ring_s: float, buttons: tuple[str, str]) -> Any | None:
        text = t("alarm.dialog.text", label=row.label[:200], at=format_local(row.fire_at, with_date=False))
        osascript = self._which("osascript")
        if osascript is None:
            return None
        argv = [osascript, "-e", DIALOG_SCRIPT, "--", text, *buttons, str(int(max_ring_s))]
        return self._spawn(argv, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    @staticmethod
    def _dialog_result(proc: Any) -> str | None:
        try:
            out, _ = proc.communicate(timeout=2)
        except Exception:
            return None
        return (out or "").strip() or None

    def ring(self, row: TimerRow, state: AlarmState, *, max_ring_s: float | None = None) -> RingOutcome:
        cfg = self.config.alarm
        max_ring_s = cfg.max_ring_min * 60.0 if max_ring_s is None else float(max_ring_s)
        started = now_local()
        info = RingInfo(
            row.id, row.label, row.fire_at.isoformat(timespec="seconds"), datetime.now(timezone.utc).isoformat(), os.getpid(), max_ring_s
        )
        self.current = info
        _unlink(self.paths.alarm_command_file)  # a request from before this ring is not about it
        _write_json(self.paths.alarm_ring_file, info.__dict__)
        logging_setup.audit({"tool": "alarm", "decision": "ringing", "alarm_id": row.id, "label": row.label[:80]})
        log.info("alarm %s ringing: %s (%s)", row.id, row.label, info.fire_at)

        saved = read_volume(self._run_applescript)
        volume_set = set_volume(cfg.volume, False, self._run_applescript)
        argv = self.argv()
        # The answer is the label of the button pressed, so it is compared in the language the buttons were shown in.
        stop_label, snooze_label = t("alarm.dialog.stop"), t("alarm.dialog.snooze", minutes=cfg.snooze_min)
        dialog = self._start_dialog(row, state, max_ring_s, (stop_label, snooze_label))
        outcome, by, minutes = "timeout", "timeout", None
        loops = 0
        proc: Any | None = None
        deadline = self._clock() + max_ring_s
        try:
            while True:
                if argv is not None and (proc is None or proc.poll() is not None):
                    proc = self._spawn(argv)
                    if proc is not None:
                        loops += 1
                command = take_command(self.paths)
                if command is not None:
                    outcome = "stopped" if command["action"] == "stop" else "snoozed"
                    by = str(command.get("by") or "command")
                    minutes = command.get("minutes")
                    break
                if dialog is not None and dialog.poll() is not None:
                    answer = self._dialog_result(dialog)
                    dialog = None
                    if answer == stop_label:
                        outcome, by = "stopped", "dialog"
                        break
                    if answer in ("cancel", snooze_label):
                        outcome, by = "snoozed", "dialog"
                        break
                    if answer == "timeout":
                        outcome, by = "timeout", "timeout"
                        break
                    log.warning("alarm dialog ended with %r; the sound goes on until the time limit", answer)
                if self._clock() >= deadline:
                    outcome, by = "timeout", "timeout"
                    break
                self._sleep(POLL_S)
        finally:
            _kill(proc)
            if dialog is not None:
                _kill(dialog)
            restored: bool | None = None
            if volume_set and saved is not None:
                restored = set_volume(saved[0], saved[1], self._run_applescript)
            _unlink(self.paths.alarm_ring_file)
            _unlink(self.paths.alarm_command_file)
            self.current = None
        result = RingOutcome(
            row.id, outcome, by, minutes=None, loops=loops, volume_restored=restored, started_at=started, ended_at=now_local()
        )
        if outcome == "snoozed":
            result.minutes = int(minutes) if minutes else int(cfg.snooze_min)
        self._finish(row, state, result)
        self.last = result
        logging_setup.audit(
            {
                "tool": "alarm",
                "decision": outcome,
                "by": by,
                "alarm_id": row.id,
                "loops": loops,
                "minutes": result.minutes,
                "volume_restored": restored,
            }
        )
        log.info("alarm %s %s by %s after %d loop(s)", row.id, outcome, by, loops)
        return result

    def _finish(self, row: TimerRow, state: AlarmState, result: RingOutcome) -> None:
        """After the sound: the snooze row, the phone reminder, the "rang to the end" message."""
        if not self.persist:
            return
        if result.outcome == "snoozed":
            minutes = int(result.minutes or self.config.alarm.snooze_min)
            fire_at = now_local() + timedelta(minutes=minutes)
            snooze_state = state.for_snooze(row.id)
            try:
                db = self._db()
                try:
                    store = TimerStore(db)
                    if self.phone_backup is not None:
                        self.phone_backup.remove(state.reminder_id)
                        snooze_state.reminder_id = self.phone_backup.create(row.label, fire_at, require_authorized=True)
                    result.snooze_id = store.add(row.label, fire_at, KIND_ALARM, snooze_state)
                finally:
                    db.close()
                log.info("alarm %s snoozed for %d min -> row %s", row.id, minutes, result.snooze_id)
            except Exception:
                log.exception("could not schedule the snooze of alarm %s", row.id)
            return
        if result.outcome == "stopped" and self.phone_backup is not None:
            self.phone_backup.remove(state.reminder_id)
            return
        if result.outcome == "timeout" and self.notifier is not None:
            try:
                self.notifier.notify(
                    NOTIFICATION_TITLE,
                    t(
                        "alarm.notify.timeout",
                        label=row.label,
                        at=format_local(row.fire_at, with_date=False),
                        minutes=self.config.alarm.max_ring_min,
                    ),
                )
            except Exception as exc:
                log.error("alarm timeout notification failed: %s", exc)


def _kill(proc: Any) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception as exc:
        log.debug("kill failed: %s", exc)


# --- keep the Mac awake ---------------------------------------------------------------------------------------


class KeepAwake:
    """One `caffeinate -i -t N` child while an alarm is due within `alarm.keep_awake_hours`. `-i` prevents
    idle sleep only: a closed lid, a manual sleep or `pmset sleepnow` still sleep the Mac."""

    def __init__(
        self,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._popen = popen
        self._which = which
        self._clock = clock
        self._proc: Any | None = None
        self.until: datetime | None = None  # the moment the running caffeinate ends
        self.alarm_at: datetime | None = None
        self.starts = 0

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def available(self) -> bool:
        return self._which("caffeinate") is not None

    def update(self, next_at: datetime | None, *, hours: int, extra_min: int, now: datetime | None = None) -> bool:
        """Start, keep, restart or stop caffeinate for the next alarm. Returns whether it is active afterwards."""
        now = now or now_local()
        if next_at is None or hours <= 0 or next_at - now > timedelta(hours=hours):
            self.stop()
            return False
        until = next_at + timedelta(minutes=extra_min)
        if self.active and self.until is not None and abs((self.until - until).total_seconds()) < 60:
            return True
        self.stop()
        caffeinate = self._which("caffeinate")
        if caffeinate is None:
            log.warning("caffeinate not found; the Mac may idle-sleep before the alarm at %s", next_at)
            return False
        seconds = max(60, int((until - now).total_seconds()))
        try:
            self._proc = self._popen(
                [caffeinate, "-i", "-t", str(seconds)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            log.warning("caffeinate failed to start: %s", exc)
            self._proc = None
            return False
        self.until, self.alarm_at, self.starts = until, next_at, self.starts + 1
        log.info("caffeinate -i for %d s: alarm at %s", seconds, next_at.isoformat(timespec="seconds"))
        logging_setup.audit({"tool": "alarm", "decision": "keep_awake", "until": until.isoformat(timespec="seconds"), "seconds": seconds})
        return True

    def stop(self) -> None:
        if self._proc is not None:
            _kill(self._proc)
            if self.until is not None:
                log.info("caffeinate stopped")
        self._proc, self.until, self.alarm_at = None, None, None

    def describe(self) -> str:
        if self.active and self.alarm_at is not None:
            return f"caffeinate -i running until {format_local(self.until)} for the alarm at {format_local(self.alarm_at)}"
        return "caffeinate not running (no alarm within the keep-awake window)"


KEEP_AWAKE = KeepAwake()  # the scheduler process's one instance (the periodic job drives it)


def keep_awake_job(config: "Config", paths: "Paths", db: "Database", notifier: Any) -> None:  # noqa: ARG001 - the jobs share one signature
    """Periodic scheduler job: caffeinate on while the next alarm is within `alarm.keep_awake_hours`."""
    KEEP_AWAKE.update(_next_alarm_time(TimerStore(db)), hours=config.alarm.keep_awake_hours, extra_min=config.alarm.max_ring_min + 2)


def _next_alarm_time(store: TimerStore) -> datetime | None:
    row = next_alarm(store)
    return row.fire_at if row else None


# --- the phone backup ------------------------------------------------------------------------------------------


class PhoneBackup:
    """The Apple Reminder that backs an alarm on the iPhone (EventKit, list `alarm.reminder_list`)."""

    def __init__(self, config: "Config", *, store_factory: Callable[[], Any] | None = None) -> None:
        self.config = config
        self._store_factory = store_factory
        self._list_id: str | None = None

    @property
    def mode(self) -> str:
        return self.config.alarm.phone_backup

    @property
    def wants_reminder(self) -> bool:
        return self.mode in ("reminder", "both")

    @property
    def wants_telegram(self) -> bool:
        return self.mode in ("telegram", "both")

    def _store(self) -> Any:
        if self._store_factory is not None:
            return self._store_factory()
        from jarvis.utils import eventkit  # noqa: PLC0415

        return eventkit.get_store()

    def authorized(self) -> bool:
        try:
            return self._store().authorization_status("reminders") == "authorized"
        except Exception as exc:
            log.debug("reminders authorization unknown: %s", exc)
            return False

    def create(self, label: str, fire_at: datetime, *, require_authorized: bool = False) -> str | None:
        """Create the reminder; its identifier, or None when the backup is off or it could not be made.
        With `require_authorized` (the scheduler, a background process) nothing is created unless
        Reminders access was already granted: a permission prompt must never hang the scheduler."""
        if not self.wants_reminder:
            return None
        if require_authorized and not self.authorized():
            log.warning("phone backup for %r not created: Reminders access is not granted to this process", label)
            logging_setup.audit({"tool": "alarm", "decision": "backup_skipped", "reason": "reminders_not_authorized"})
            return None
        store = self._store()
        store.ensure_access("reminders")
        if self._list_id is None:
            self._list_id = store.ensure_reminder_list(self.config.alarm.reminder_list).identifier
        record = store.create_reminder(
            t("alarm.reminder.title", label=label),
            due=fire_at,
            calendar_id=self._list_id,
            notes=t("alarm.reminder.notes", at=format_local(fire_at)),
        )
        logging_setup.audit({"tool": "alarm", "decision": "backup_created", "reminder": record.identifier})
        log.info("phone backup reminder %s created for %s", record.identifier, format_local(fire_at))
        return record.identifier

    def remove(self, reminder_id: str | None) -> bool:
        """Delete the reminder (complete it when deleting fails). Never raises."""
        if not reminder_id:
            return False
        try:
            store = self._store()
            if store.authorization_status("reminders") != "authorized":
                log.warning("phone backup reminder %s not removed: Reminders access is not granted", reminder_id)
                return False
            if store.delete_reminder(reminder_id):
                logging_setup.audit({"tool": "alarm", "decision": "backup_removed", "reminder": reminder_id})
                return True
            store.complete_reminder(reminder_id)
            return True
        except Exception as exc:
            log.warning("could not remove the phone backup reminder %s: %s", reminder_id, exc)
            return False

    def describe(self, fire_at: datetime, *, reminder_id: str | None, telegram_ok: bool) -> str:
        """Where the alarm reaches the phone, honestly (for the model, which passes it on)."""
        parts = []
        if self.wants_reminder:
            if reminder_id:
                parts.append(
                    f"an Apple Reminder in the list '{self.config.alarm.reminder_list}' at {format_local(fire_at, with_date=False)} "
                    "(reaches the iPhone through iCloud; a normal reminder notification, it does not ring like an alarm)"
                )
            else:
                parts.append("the Apple Reminder could not be created")
        if self.wants_telegram:
            parts.append("a Telegram message" if telegram_ok else "a Telegram message (the bot is not set up, so it will not be sent)")
        if not parts:
            return "Phone backup off (alarm.phone_backup = off): it rings on the Mac only."
        return "Phone backup: " + " + ".join(parts) + "."


# --- the handler the engine calls ----------------------------------------------------------------------------------


class AlarmHandler:
    """What `fire_due` hands an alarm row to. `mac` and `telegram` are the notification channels
    (Telegram may be None when the bot is not configured); `ringer` plays the sound."""

    def __init__(
        self,
        config: "Config",
        paths: "Paths",
        *,
        mac: Notifier,
        telegram: Notifier | None,
        ringer: Ringer,
        phone_backup: PhoneBackup | None = None,
        blocking: bool = False,
    ) -> None:
        self.config = config
        self.paths = paths
        self.mac = mac
        self.telegram = telegram
        self.ringer = ringer
        self.phone_backup = phone_backup
        self.blocking = blocking

    def _notify_both(self, text: str) -> None:
        for channel in (self.mac, self.telegram):
            if channel is None:
                continue
            try:
                channel.notify(NOTIFICATION_TITLE, text)
            except Exception as exc:
                log.error("alarm notification through %s failed: %s", channel.name, exc)

    def handle(self, timer: TimerRow, state: AlarmState, result: "FiredTimer", store: TimerStore) -> None:
        at = format_local(timer.fire_at, with_date=False)
        if result.next_id is not None and self.phone_backup is not None:
            nxt = store.get(result.next_id)
            if nxt is not None:
                self._backup_next(nxt, state, store)
        if result.late:
            result.missed = True
            result.message = t("alarm.notify.missed", label=timer.label, at=at)
            self._notify_both(result.message)
            result.notified = True
            logging_setup.audit(
                {
                    "tool": "alarm",
                    "decision": "missed",
                    "alarm_id": timer.id,
                    "late_s": round((now_local() - timer.fire_at).total_seconds()),
                }
            )
            log.warning("alarm %s missed: due %s", timer.id, timer.fire_at.isoformat(timespec="seconds"))
            return
        result.message = t("alarm.notify.ring", label=timer.label, at=at)
        if state.wants_telegram and self.telegram is not None:
            try:
                self.telegram.notify(NOTIFICATION_TITLE, result.message)
                result.notified = True
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                log.error("alarm %s Telegram message failed: %s", timer.id, result.error)
        if self.ringer.start(timer, state, blocking=self.blocking):
            result.rang = True
        else:  # another alarm is ringing: this one is a notification
            log.warning("alarm %s fired while another rings; notified only", timer.id)
            try:
                self.mac.notify(NOTIFICATION_TITLE, result.message)
                result.notified = True
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"

    def _backup_next(self, nxt: TimerRow, state: AlarmState, store: TimerStore) -> None:
        """The reminder for the next occurrence of a repeating alarm (only when access is granted)."""
        assert self.phone_backup is not None
        try:
            reminder_id = self.phone_backup.create(nxt.label, nxt.fire_at, require_authorized=True)
        except Exception as exc:
            log.warning("phone backup for the next occurrence (row %s) failed: %s", nxt.id, exc)
            return
        if reminder_id:
            nxt_state = AlarmState.from_json(nxt.pomodoro_state_json) or state.for_next_occurrence()
            nxt_state.reminder_id = reminder_id
            store_state(store, nxt.id, nxt_state)


def store_state(store: TimerStore, row_id: int, state: AlarmState) -> None:
    """Rewrite the state JSON of a pending alarm row (the reminder id, mostly)."""
    store.conn.execute("UPDATE timers SET pomodoro_state_json = ? WHERE id = ? AND kind = ?", (state.model_dump_json(), row_id, KIND_ALARM))


def handler_for(config: "Config", paths: "Paths", *, mac: Notifier, telegram: Notifier | None, blocking: bool = False) -> AlarmHandler:
    """The scheduler's handler: a Ringer with the phone backup and a Mac + Telegram notifier."""
    from jarvis.scheduler.notify import MultiNotifier  # noqa: PLC0415

    backup = PhoneBackup(config)
    both: Notifier = mac if telegram is None else MultiNotifier([mac, telegram])
    ringer = Ringer(paths, config, phone_backup=backup, notifier=both)
    return AlarmHandler(config, paths, mac=mac, telegram=telegram, ringer=ringer, phone_backup=backup, blocking=blocking)


def describe_row(row: TimerRow, now: datetime | None = None, *, language: str | None = None) -> str:
    """One line for `list_alarms` (English) and `jarvis alarms` (the main language):
    `#id  2026-10-19 07:00 (yarın, 9 sa sonra)  Kalk  [hafta içi]  tel: hatırlatıcı+telegram`."""
    now = now or now_local()
    state = AlarmState.from_json(row.pomodoro_state_json)
    delta = row.fire_at - now
    seconds = int(delta.total_seconds())
    hours, rest = divmod(max(0, seconds), 3600)
    minutes = rest // 60
    when = (
        t("alarm.row.in_hours", language, hours=hours, minutes=minutes) if hours else t("alarm.row.in_minutes", language, minutes=minutes)
    )
    if row.fire_at.date() == now.date():
        day = t("alarm.row.today", language)
    elif row.fire_at.date() == (now + timedelta(days=1)).date():
        day = t("alarm.row.tomorrow", language)
    else:
        day = row.fire_at.strftime("%d.%m")
    parts = [f"#{row.id}", f"{format_local(row.fire_at)} ({day}, {when})", row.label]
    if state is not None:
        if state.repeat:
            parts.append(f"[{state.describe_repeat(language)}]")
        if state.snoozed_from is not None:
            parts.append(t("alarm.row.snoozed", language, id=state.snoozed_from))
        phone = []
        if state.wants_reminder:
            phone.append(t("alarm.row.reminder" if state.reminder_id else "alarm.row.no_reminder", language))
        if state.wants_telegram:
            phone.append(t("alarm.row.telegram", language))
        parts.append(t("alarm.row.phone", language, channels="+".join(phone) if phone else t("alarm.row.phone_off", language)))
    return "  ".join(parts)


__all__ = [
    "AlarmState",
    "AlarmHandler",
    "KeepAwake",
    "KEEP_AWAKE",
    "PhoneBackup",
    "RingInfo",
    "RingOutcome",
    "Ringer",
    "classify_alarm_word",
    "describe_row",
    "handler_for",
    "keep_awake_job",
    "lid_closed",
    "next_alarm",
    "pending_alarms",
    "read_volume",
    "request_snooze",
    "request_stop",
    "resolve_sound",
    "ringing",
    "set_volume",
    "store_state",
    "take_command",
    "MISSED_TEXT",
    "RING_TEXT",
    "SNOOZED_TEXT",
    "STOPPED_TEXT",
    "NOT_RINGING_TEXT",
    "TIMEOUT_TEXT",
    "STOP_WORDS",
]
