"""Alarms: `set_alarm`, `list_alarms`, `cancel_alarm`, `snooze_alarm`, `stop_alarm`, plus `health_checks`
for the doctor and the helpers behind `jarvis alarms`.

An alarm is different from a timer ("tell me in 25 minutes"): it is at a clock time, may repeat,
rings loudly on the Mac until stopped or snoozed (scheduler/alarm.py) and, for wake-up alarms, also
reaches the phone (an Apple Reminder and/or a Telegram message, `alarm.phone_backup`). The tools only
write rows and the phone backup; the launchd scheduler (`jarvis scheduler run`) rings them, so the
usual scheduler warning applies when it is not installed.

Setting an alarm says honestly where it will ring, and warns when the lid is closed or the Mac is on
battery below `alarm.low_battery_percent`: `caffeinate` keeps the Mac from idle sleep, not from a
closed lid or an empty battery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.i18n import t
from jarvis.observability import logging_setup
from jarvis.observability.db import Database
from jarvis.scheduler import alarm as ringer_mod
from jarvis.scheduler.alarm import (
    AlarmState,
    PhoneBackup,
    describe_row,
    lid_closed,
    pending_alarms,
    resolve_sound,
    ringing,
)
from jarvis.scheduler.store import KIND_ALARM, TimerRow, TimerStore
from jarvis.tools.base import EmptyInput, RiskLevel, ToolContext, ToolResult
from jarvis.tools.timers import _TimerTool
from jarvis.utils.text import fold
from jarvis.utils.timeutil import describe_repeat, format_local, next_alarm_occurrence, now_local, parse_alarm_time, parse_repeat

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check

log = logging.getLogger("jarvis.tools.alarms")

MAX_AHEAD_DAYS = 366
# For the model, which passes them on in the user's language.
LID_WARNING = "Warning: the lid is closed. With the lid closed the Mac sleeps and the alarm may not ring; leave the lid open or rely on the phone backup."
BATTERY_WARNING = (
    "Warning: the Mac is on battery at {percent}%. If the battery runs out the alarm will not ring; plug in the power adapter."
)
NIGHT_HINT = (
    "(A night or early-morning alarm: say in the reply that the phone backup is set and that the Mac's lid must stay "
    "open and the power adapter plugged in.)"
)
ALL_WORDS = ("all", "hepsi", "tümü", "tumu", "*")  # cancel_alarm's label for every alarm
NIGHT_HOURS = range(0, 9)  # a wake-up alarm: the reply mentions the phone backup explicitly


def _now() -> datetime:  # module-level so tests can freeze the clock
    return now_local()


# --- doctor --------------------------------------------------------------------------------------------


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Alarm sound, caffeinate, the Reminders permission for the phone backup, pending alarms. Never raises."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    try:
        sound = resolve_sound(paths, config)
        if sound.custom:
            checks.append(Check("Alarm sound", True, str(sound.path)))
        else:
            checks.append(Check("Alarm sound", True, f"missing: {sound.describe(paths.sounds_dir)} (see sounds/README.md)", warn=True))
    except Exception as exc:
        checks.append(Check("Alarm sound", False, f"cannot check: {type(exc).__name__}: {exc}"))
    keep = ringer_mod.KeepAwake()
    if config.alarm.keep_awake_hours <= 0:
        checks.append(Check("Alarm keep-awake (caffeinate)", True, "disabled (alarm.keep_awake_hours = 0)"))
    elif keep.available():
        checks.append(
            Check(
                "Alarm keep-awake (caffeinate)",
                True,
                f"caffeinate available; idle sleep is prevented while an alarm is within {config.alarm.keep_awake_hours} h",
            )
        )
    else:
        checks.append(Check("Alarm keep-awake (caffeinate)", False, "caffeinate not found: the Mac may idle-sleep before an alarm"))
    checks.append(_backup_check(config))
    try:
        db = Database(paths.db_file)
        try:
            rows = pending_alarms(TimerStore(db))
        finally:
            db.close()
        info = ringing(paths)
        if info is not None:
            detail = f"RINGING now: #{info.alarm_id} {info.label}; "
        else:
            detail = ""
        detail += f"{len(rows)} pending, next {format_local(rows[0].fire_at)} ({rows[0].label})" if rows else "none pending"
        checks.append(Check("Alarms", True, detail))
    except Exception as exc:
        checks.append(Check("Alarms", False, f"cannot read alarms: {type(exc).__name__}: {exc}"))
    return checks


def _backup_check(config: "Config") -> "Check":
    from jarvis.observability.health import Check  # noqa: PLC0415

    mode = config.alarm.phone_backup
    parts = []
    ok = True
    warn = False
    if mode in ("reminder", "both"):
        try:
            from jarvis.utils.eventkit import EventKitStore  # noqa: PLC0415

            status = EventKitStore.authorization_status("reminders")
        except Exception as exc:
            status = f"unknown ({type(exc).__name__})"
        if status == "authorized":
            parts.append(f"Reminders access granted; backups go to the list '{config.alarm.reminder_list}'")
        else:
            parts.append(f"Reminders access is {status}: no reminder backup until it is granted (docs/integrations/apple.md)")
            warn = True
    if mode in ("telegram", "both"):
        if config.telegram.enabled and config.telegram.allowed_chat_id:
            parts.append("Telegram message at the alarm time")
        else:
            parts.append("Telegram not configured: no Telegram backup")
            warn = True
    if mode == "off":
        parts.append("off: alarms ring on the Mac only")
    return Check("Alarm phone backup", ok, f"{mode}: " + "; ".join(parts), warn=warn)


# --- shared ------------------------------------------------------------------------------------------------


class _AlarmTool(_TimerTool):
    delegate_ok = False  # the delegate never sets, cancels, stops or snoozes alarms

    def __init__(self, ctx: ToolContext | None = None, db: Database | None = None, backup: PhoneBackup | None = None) -> None:
        super().__init__(ctx, db)
        self._backup = backup

    @property
    def backup(self) -> PhoneBackup:
        if self._backup is None:
            self._backup = PhoneBackup(self.require_ctx().config)
        return self._backup

    @property
    def alarms(self) -> list[TimerRow]:
        return pending_alarms(self.store)

    def telegram_ok(self) -> bool:
        cfg = self.require_ctx().config.telegram
        return bool(cfg.enabled and cfg.allowed_chat_id)


def mac_warnings(config: "Config") -> list[str]:
    """The lid and the battery: why the alarm might not ring on the Mac. Never raises."""
    from jarvis.tools import power  # noqa: PLC0415

    warnings: list[str] = []
    try:
        if lid_closed():
            warnings.append(LID_WARNING)
    except Exception as exc:
        log.debug("lid state unavailable: %s", exc)
    try:
        batt = power.parse_batt(power._output(["pmset", "-g", "batt"]))
        percent = batt.get("battery_percent")
        if batt.get("power_source") == "Battery" and percent is not None and percent < config.alarm.low_battery_percent:
            warnings.append(BATTERY_WARNING.format(percent=percent))
    except Exception as exc:
        log.debug("battery state unavailable: %s", exc)
    return warnings


# --- set_alarm ----------------------------------------------------------------------------------------------


class SetAlarmInput(BaseModel):
    time: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "When the alarm rings, as the user said it (English or Turkish) or as 'HH:MM': '7:30', 'tomorrow morning 7', "
            "'evening 9', 'saturday 10:00' (next Saturday, once), 'weekdays 7:30', 'every saturday 10:00', 'YYYY-MM-DD HH:MM'. "
            "A bare clock time means today, or tomorrow if it has passed."
        ),
    )
    label: str | None = Field(
        default=None, max_length=200, description="What the alarm is for ('Wake up', 'Medicine'), in the user's language; default 'Alarm'."
    )
    repeat: str | None = Field(
        default=None,
        max_length=80,
        description=(
            "Repeat days: 'daily', 'weekdays', 'weekends', or weekday names ('mon,wed', 'monday and wednesday'); "
            "the Turkish words work too. Overrides a repeat found in `time`. Omit for a one-shot alarm."
        ),
    )


class SetAlarm(_AlarmTool):
    name = "set_alarm"
    description = (
        "Set an alarm at a clock time ('set an alarm', 'wake me at 7', 'get me up at 6:30'): a loud looping sound "
        "on the Mac with Stop / Snooze buttons, optionally repeating, with a phone backup (Apple Reminder and/or "
        "Telegram) so a wake-up alarm also reaches the iPhone. The result says exactly where it will ring and warns about "
        "the lid or the battery; pass that on. Not for 'tell me in N minutes': that is set_timer."
    )
    input_model = SetAlarmInput
    risk = RiskLevel.SAFE

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SetAlarmInput)
        ctx = self.require_ctx()
        now = _now()
        hour, minute, day, repeat = parse_alarm_time(args.time, now=now)
        if args.repeat is not None and args.repeat.strip():
            repeat = parse_repeat(args.repeat)
            day = None
        fire_at = next_alarm_occurrence(hour, minute, repeat, after=now, day=day)
        if fire_at - now > timedelta(days=MAX_AHEAD_DAYS):
            raise ToolValidationError("alarms can be at most a year ahead")
        label = (args.label or "").strip() or t("alarm.default_label")
        state = AlarmState(hour=hour, minute=minute, repeat=repeat, phone_backup=ctx.config.alarm.phone_backup)
        backup_error: str | None = None
        if state.wants_reminder:
            try:
                state.reminder_id = self.backup.create(label, fire_at)
            except Exception as exc:  # the alarm is still set; the reply says the phone part failed
                backup_error = f"{type(exc).__name__}: {exc}"
                log.warning("phone backup for alarm %r failed: %s", label, backup_error)
        alarm_id = self.store.add(label, fire_at, KIND_ALARM, state)
        logging_setup.audit(
            {
                "tool": "set_alarm",
                "decision": "set",
                "alarm_id": alarm_id,
                "at": fire_at.isoformat(timespec="seconds"),
                "repeat": repeat,
                "reminder": state.reminder_id,
                "backup": state.phone_backup,
            }
        )
        log.info(
            "alarm %s set for %s (%s): %s", alarm_id, fire_at.isoformat(timespec="seconds"), describe_repeat(repeat, "en") or "once", label
        )
        lines = [self._set_text(alarm_id, label, fire_at, repeat, now, ctx.config.alarm.snooze_min)]
        lines.append(self.backup.describe(fire_at, reminder_id=state.reminder_id, telegram_ok=self.telegram_ok()))
        if backup_error:
            lines.append(ringer_mod.BACKUP_FAILED_TEXT.format(error=backup_error))
        lines.extend(mac_warnings(ctx.config))
        if fire_at.hour in NIGHT_HOURS:
            lines.append(NIGHT_HINT)
        return ToolResult("\n".join(lines) + self.scheduler_warning())

    @staticmethod
    def _set_text(alarm_id: int, label: str, fire_at: datetime, repeat: list[int], now: datetime, snooze_min: int) -> str:
        delta = fire_at - now
        hours, rest = divmod(int(delta.total_seconds()), 3600)
        minutes = rest // 60
        wait = f"{hours} h {minutes} min" if hours else f"{minutes} min"
        if fire_at.date() == now.date():
            day = "today"
        elif fire_at.date() == (now + timedelta(days=1)).date():
            day = "tomorrow"
        else:
            day = fire_at.strftime("%Y-%m-%d")
        text = f"Alarm #{alarm_id} set: {day} {format_local(fire_at, with_date=False)} (in {wait}), '{label}'"
        if repeat:
            text += f", repeats {describe_repeat(repeat, 'en')}"
        text += (
            f". It rings loudly on the Mac (a dialog with the buttons '{t('alarm.dialog.stop')}' and "
            f"'{t('alarm.dialog.snooze', minutes=snooze_min)}'; spoken 'ok' / 'stop' / 'snooze' or 'tamam' / 'kapat' / "
            "'ertele'; the hotkey; Telegram /stop_alarm)."
        )
        return text


# --- list_alarms ------------------------------------------------------------------------------------------------


class ListAlarms(_AlarmTool):
    name = "list_alarms"
    description = "List the pending alarms with their times, repeats and phone backup, and whether one is ringing now."
    input_model = EmptyInput
    delegate_ok = True

    def run(self, args: BaseModel) -> ToolResult:
        now = _now()
        rows = self.alarms
        lines = []
        info = ringing(self.require_ctx().paths)
        if info is not None:
            lines.append(f"RINGING: #{info.alarm_id} {info.label} (stop_alarm / snooze_alarm)")
        if not rows:
            lines.append("No alarms set.")
        else:
            lines.append(f"{len(rows)} alarm(s):")
            lines.extend(describe_row(r, now, language="en") for r in rows)
        return ToolResult("\n".join(lines))


# --- cancel_alarm -------------------------------------------------------------------------------------------------


class CancelAlarmInput(BaseModel):
    id: int | None = Field(default=None, ge=1, description="Alarm id from list_alarms or set_alarm.")
    label: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Cancel every pending alarm with this label (case-insensitive; a partial match if nothing matches exactly). 'all' cancels every alarm.",
    )
    time: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
        description="Cancel the alarm(s) at this clock time, 'HH:MM' (e.g. the user says 'cancel the 7:30 one').",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "CancelAlarmInput":
        if sum(x is not None for x in (self.id, self.label, self.time)) != 1:
            raise ValueError("give exactly one of id, label or time")
        return self


class CancelAlarm(_AlarmTool):
    name = "cancel_alarm"
    description = (
        "Cancel a pending alarm by id, label or clock time ('cancel the alarm', 'remove the 7:30 alarm'). A repeating "
        "alarm is cancelled as a whole. Its phone backup reminder is removed too. For the alarm ringing right now use stop_alarm."
    )
    input_model = CancelAlarmInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CancelAlarmInput)
        rows = self.alarms
        if args.id is not None:
            row = self.store.get(args.id)
            if row is None or row.kind != KIND_ALARM:
                raise ToolValidationError(f"no alarm with id {args.id}")
            if row.status != "pending":
                return ToolResult(t("alarm.cancel.already", id=args.id, label=row.label, status=row.status))
            chosen = [row]
        elif args.time is not None:
            hour, minute, _day, _repeat = parse_alarm_time(args.time, now=_now())
            chosen = [r for r in rows if r.fire_at.hour == hour and r.fire_at.minute == minute]
            if not chosen:
                return ToolResult(t("alarm.cancel.none_at", at=f"{hour:02d}:{minute:02d}"))
        else:
            needle = fold((args.label or "").strip())
            everything = needle in ALL_WORDS
            if everything:
                chosen = list(rows)
            else:
                chosen = [r for r in rows if fold(r.label.strip()) == needle]
                if not chosen:
                    chosen = [r for r in rows if needle in fold(r.label)]
            if not chosen:
                return ToolResult(t("alarm.cancel.none") if everything else t("alarm.cancel.none_labelled", label=args.label))
        cancelled = []
        for row in chosen:
            if self.store.cancel(row.id):
                state = AlarmState.from_json(row.pomodoro_state_json)
                if state is not None and state.reminder_id:
                    self.backup.remove(state.reminder_id)
                cancelled.append(row)
        logging_setup.audit({"tool": "cancel_alarm", "decision": "cancelled", "ids": [r.id for r in cancelled]})
        log.info("%d alarm(s) cancelled", len(cancelled))
        if not cancelled:
            return ToolResult(t("alarm.cancel.nothing"))
        described = "; ".join(f"#{r.id} {format_local(r.fire_at)} '{r.label}'" for r in cancelled)
        return ToolResult(t("alarm.cancel.done", count=len(cancelled), alarms=described))


# --- snooze_alarm / stop_alarm ------------------------------------------------------------------------------------


class SnoozeAlarmInput(BaseModel):
    minutes: int | None = Field(default=None, ge=1, le=60, description="Snooze length in minutes (default alarm.snooze_min, 9).")


class SnoozeAlarm(_AlarmTool):
    name = "snooze_alarm"
    description = (
        "Snooze the alarm that is ringing right now ('snooze', '10 more minutes'): it rings again after the given minutes (default 9)."
    )
    input_model = SnoozeAlarmInput

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SnoozeAlarmInput)
        ctx = self.require_ctx()
        minutes = args.minutes or ctx.config.alarm.snooze_min
        channel = getattr(ctx.runtime, "channel", None) or "tool"
        if not ringer_mod.request_snooze(ctx.paths, by=f"tool/{channel}", minutes=minutes, wait_s=ringer_mod.ACK_WAIT_S):
            if ringing(ctx.paths) is None:
                return ToolResult(t("alarm.notify.not_ringing"))
            raise ToolExecutionError("the ringing alarm did not answer the snooze request (is the scheduler running?)")
        at = format_local(_now() + timedelta(minutes=minutes), with_date=False)
        return ToolResult(t("alarm.notify.snoozed", minutes=minutes, at=at))


class StopAlarm(_AlarmTool):
    name = "stop_alarm"
    description = "Stop the alarm that is ringing right now ('turn off the alarm', 'stop', 'ok I'm up'). Says so when nothing rings."
    input_model = EmptyInput

    def run(self, args: BaseModel) -> ToolResult:
        ctx = self.require_ctx()
        channel = getattr(ctx.runtime, "channel", None) or "tool"
        if not ringer_mod.request_stop(ctx.paths, by=f"tool/{channel}", wait_s=ringer_mod.ACK_WAIT_S):
            if ringing(ctx.paths) is None:
                return ToolResult(t("alarm.notify.not_ringing"))
            raise ToolExecutionError("the ringing alarm did not answer the stop request (is the scheduler running?)")
        return ToolResult(t("alarm.notify.stopped"))


# --- `jarvis alarms` ------------------------------------------------------------------------------------------------


def cli_list(paths: "Paths", *, now: datetime | None = None) -> str:
    db = Database(paths.db_file)
    try:
        rows = pending_alarms(TimerStore(db))
    finally:
        db.close()
    lines = []
    info = ringing(paths)
    if info is not None:
        lines.append(t("alarm.cli.ringing", id=info.alarm_id, label=info.label))
    if not rows:
        lines.append(t("alarm.cli.none"))
    else:
        lines.append(t("alarm.cli.count", count=len(rows)))
        lines.extend(describe_row(r, now) for r in rows)
    return "\n".join(lines)


def cli_cancel(config: "Config", paths: "Paths", target: str) -> str:
    """`jarvis alarms cancel <id|label|all>`."""
    db = Database(paths.db_file)
    try:
        ctx = ToolContext.build(config, paths)
        tool = CancelAlarm(ctx, db=db)
        if target.strip().isdigit():
            args: dict[str, Any] = {"id": int(target)}
        else:
            args = {"label": target}
        return tool.run(tool.parse_args(args)).content
    finally:
        db.close()


__all__ = [
    "SetAlarm",
    "ListAlarms",
    "CancelAlarm",
    "SnoozeAlarm",
    "StopAlarm",
    "health_checks",
    "mac_warnings",
    "cli_list",
    "cli_cancel",
    "LID_WARNING",
    "BATTERY_WARNING",
]
