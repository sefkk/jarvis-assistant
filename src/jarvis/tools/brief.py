"""Daily brief: the merged agenda, the weather,
reminders due that day and whether the configured team plays, as one structured text block the
model turns into a short friendly summary.

Match-day detection order (the user's decision): (1) the calendar events of the day
and (2) the reminders due that day are scanned for the team name; if neither mentions the team
the brief tells the model to check with `web_search`. The football API is never used here.
The agenda comes through `merge_calendars`, so the `[[calendar.filters]]` already apply: other
teams' matches in the football calendar are neither listed nor considered a match day.

Yesterday's match comes from yesterday's football calendar only (the one named in `[[calendar.filters]]`;
"Fenerbahçe" is also a neighbourhood, so a plain mention elsewhere is not a match), so the model knows to
look up the result. The alarms and timers set for the day come from the scheduler's table (no backend to fail).

The mail of the day (`[brief] include_mail`, today only) is what reached the inbox since midnight: sender,
subject and Gmail's labels per message (promotions and social mail only counted), so the model can name the
important ones. It is read only when the turn may use Gmail (the gmail tools are in its registry).

On a remote turn (Telegram, the scheduled brief) the Mac's location is not the user's: without a location
shared from the phone the weather is the home Location's, marked "(home)", instead of a request to share one.
The first line carries the local time, so the greeting fits it.

Every part is guarded: one failing backend becomes a note at the end, never a failed brief.
`build_brief` is also what the CLI (`jarvis brief [--date today] [--send telegram|voice]`) and the
automatic morning brief (scheduler/brief.py, `[brief.schedule]`) end up calling through the model.
`set_brief_schedule` (SAFE) changes that schedule from the chat ("move the morning brief to 7:30").
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from jarvis.config import load_config, parse_hhmm, update_local_config
from jarvis.exceptions import ConfigError, JarvisError, ToolValidationError
from jarvis.football.client import fold
from jarvis.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from jarvis.tools.calendar_merged import merge_calendars
from jarvis.utils.calendar_types import CalendarEvent, format_events
from jarvis.utils.timeutil import format_local, local_tz, now_local, parse_date_range

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

log = logging.getLogger("jarvis.tools.brief")

WEATHER_TOOL = "get_weather"
GMAIL_TOOL = "gmail_search_threads"  # present in the registry = this turn may read Gmail
REMINDERS_TOOL = "list_reminders"
TRAVEL_TOOL = "travel_time"
MIN_KEYWORD_LEN = 4  # "Fener" matches "Fenerbahçe"; "FB" is too short to be safe

WEB_SEARCH_HINT = (
    "Not found in the calendar or the reminders. Check with web_search (Mackolik, the club or the league site)"
    " whether {team} plays {day_word} and how the last match ended. Do not use the football.* tools for this."
)
YESTERDAY_HINT = "For yesterday's result use web_search if the user wants it."
YESTERDAY_MATCH = "Yesterday's match (from the {source} calendar): {title}. Look up the result with web_search."


def mentions_team(text: str, team: str) -> bool:
    """Does a title or a reminder line refer to the team? Accent- and case-insensitive; a word of
    at least four letters that starts the team name also counts ("Fener" for "Fenerbahçe")."""
    team_key = fold(team)
    if not team_key or not text:
        return False
    text_key = fold(text)
    if team_key in text_key:
        return True
    words = re.findall(r"[^\W\d_]+", text_key)
    return any(len(w) >= MIN_KEYWORD_LEN and team_key.startswith(w) for w in words)


@dataclass
class BriefData:
    day: date
    agenda: list[CalendarEvent] = field(default_factory=list)
    weather: str | None = None
    reminders: str | None = None
    match_event: CalendarEvent | None = None  # the calendar event that mentions the team
    match_reminder: str | None = None  # the reminder line that mentions the team
    yesterday_match: CalendarEvent | None = None  # yesterday's calendar event that mentions the team
    timers: list[str] = field(default_factory=list)  # alarms and timers set for the day (scheduler table)
    travel: str | None = None  # travel time to the first event with a location (brief.include_travel)
    mail: str | None = None  # today's inbox (brief.include_mail); None = not part of this brief
    as_of: datetime | None = None  # when the brief was built (local), for a greeting that fits the time
    team: str = ""
    failures: dict[str, str] = field(default_factory=dict)  # part -> why it is missing or partial

    @property
    def match_found(self) -> bool:
        return self.match_event is not None or self.match_reminder is not None

    def football_lines(self) -> list[str]:
        lines: list[str] = []
        if self.match_event is not None:
            ev = self.match_event
            when = "all day" if ev.all_day else "at " + format_local(ev.start, with_date=False)
            where = f" @ {ev.location}" if ev.location else ""
            lines.append(f"Match day (from the {ev.source} calendar): {when}: {ev.title}{where}")
            lines.append(YESTERDAY_HINT)
        elif self.match_reminder is not None:
            lines.append(f"A reminder due today mentions {self.team}: {self.match_reminder}")
            lines.append(YESTERDAY_HINT)
        else:
            day_word = "today" if self.day == now_local().date() else f"on {self.day.isoformat()}"
            lines.append(WEB_SEARCH_HINT.format(team=self.team or "the team", day_word=day_word))
        if self.yesterday_match is not None:
            lines.append(YESTERDAY_MATCH.format(source=self.yesterday_match.source, title=self.yesterday_match.title))
        return lines

    def as_text(self) -> str:
        head = f"Daily brief for {self.day.strftime('%a %Y-%m-%d')}"
        if self.as_of is not None:
            head += f" (as of {format_local(self.as_of, with_date=False)} local time)"
        sections: list[str] = [head, "", "== Agenda ==", format_events(self.agenda, empty="No events.")]
        if self.travel:
            sections += ["", "== Travel ==", self.travel]
        sections += ["", "== Weather ==", self.weather or "(not available)"]
        sections += ["", "== Reminders due ==", self.reminders or "(not available)"]
        sections += ["", "== Alarms and timers ==", "\n".join(self.timers) or "None."]
        if self.mail is not None:
            sections += ["", "== Mail today ==", self.mail]
        sections += ["", f"== {self.team or 'Football'} ==", *self.football_lines()]
        if self.failures:
            sections += ["", "== Notes =="] + [f"- {part}: {reason}" for part, reason in self.failures.items()]
        return "\n".join(sections)


def _tool(runtime: Any, name: str) -> Any:
    registry = getattr(runtime, "registry", None) if runtime is not None else None
    if registry is None:
        return None
    try:
        return registry.get(name)
    except Exception:
        return None


def _run_tool(tool: Any, args: dict[str, Any]) -> str:
    result = tool.run(tool.parse_args(args))
    return str(getattr(result, "content", result))


def _parse_day(text: str) -> tuple[datetime, datetime]:
    try:
        start, end = parse_date_range(text or "today")
    except ValueError as exc:
        raise ToolValidationError(f"invalid date {text!r}: {exc}") from None
    if end - start > timedelta(days=1):
        raise ToolValidationError("the brief covers one day; give 'today', 'tomorrow' or YYYY-MM-DD")
    return start, end


def football_calendars(config: "Config") -> set[str]:
    """The calendars that hold the matches by construction: those named in `[[calendar.filters]]`."""
    return {f.calendar.casefold() for f in config.calendar.filters}


def match_of_the_day(
    team: str, events: list[CalendarEvent], reminders_text: str | None, *, prefer_calendars: set[str] | None = None
) -> tuple[CalendarEvent | None, str | None]:
    """Match-day detection order: (1) a calendar event of the day that mentions the team, else
    (2) a reminder line that does. Returns (event, reminder line); both None means "ask the web".
    An event in one of `prefer_calendars` (the football calendar) wins over a mention elsewhere."""
    ordered = sorted(events, key=CalendarEvent.sort_key)
    if prefer_calendars:
        ordered = [e for e in ordered if e.calendar.casefold() in prefer_calendars] + [
            e for e in ordered if e.calendar.casefold() not in prefer_calendars
        ]
    for ev in ordered:
        if mentions_team(ev.title, team) or mentions_team(ev.notes, team):
            return ev, None
    for line in (reminders_text or "").splitlines():
        if mentions_team(line, team):
            return None, line.strip()
    return None, None


def yesterdays_match(team: str, events: list[CalendarEvent], calendars: set[str]) -> CalendarEvent | None:
    """Yesterday's match, stricter than the match of the day because nothing is going to be asked about it
    but the score: an event of the football calendar(s), or, when none is configured, a fixture-shaped
    title ("A - B", "A vs B") that mentions the team. A plain mention ("Fenerbahçe'de kahve") does not count."""
    from jarvis.utils.calendar_filters import looks_like_fixture  # noqa: PLC0415

    for ev in sorted(events, key=CalendarEvent.sort_key):
        if calendars and ev.calendar.casefold() not in calendars:
            continue
        if calendars or (looks_like_fixture(ev.title) and mentions_team(ev.title, team)):
            return ev
    return None


def timers_of_the_day(db: Any, day: date) -> list[str]:
    """Pending alarms, timers and the pomodoro phase that fire on `day`, one line each (`alarm 07:00 Kalk`)."""
    from jarvis.scheduler.store import KIND_SLEEP, KIND_SLEEP_NOTICE, TimerStore  # noqa: PLC0415

    lines = []
    for row in TimerStore(db).pending():
        local_at = row.fire_at.astimezone(local_tz())
        if local_at.date() != day or row.kind in (KIND_SLEEP, KIND_SLEEP_NOTICE):
            continue
        lines.append(f"{row.kind} {format_local(local_at, with_date=False)}: {row.label}")
    return lines


def first_event_with_location(events: list[CalendarEvent]) -> CalendarEvent | None:
    for ev in sorted(events, key=CalendarEvent.sort_key):
        if ev.location.strip() and not ev.all_day:
            return ev
    return None


def _weather_from_home(runtime: Any, weather_ctx: Any, paths: "Paths") -> bool:
    """A remote turn without a location shared from the phone: the brief's weather is for home rather than
    a request to share a location (a pushed brief has nobody to ask)."""
    from jarvis.location import service as location_service  # noqa: PLC0415

    if str(getattr(runtime, "channel", None) or "cli") in location_service.LOCAL_CHANNELS or weather_ctx is None:
        return False
    if not location_service.home_location(paths):
        return False
    try:
        return location_service.current_location(weather_ctx).location is None
    except Exception:
        return True


def build_brief(config: "Config", paths: "Paths", runtime: Any, date_text: str = "today") -> BriefData:
    """Assemble the brief. `runtime` is the Assistant's Runtime or any object with `.registry`
    (a ToolRegistry) and `.db` (a Database); None skips weather and reminders with a note."""
    start, end = _parse_day(date_text)
    day = start.astimezone(local_tz()).date()
    now = now_local()
    data = BriefData(day=day, team=config.football.team, as_of=now)

    # 1. agenda (both calendars, each labeled; partial failures are notes)
    try:
        events, failures = merge_calendars(config, start, end)
        data.agenda = events
        for name, reason in failures.items():
            data.failures[f"{name} calendar"] = reason
    except Exception as exc:
        data.failures["calendar"] = str(exc) if isinstance(exc, JarvisError) else f"{type(exc).__name__}: {exc}"
        log.warning("brief: calendar failed: %s", exc)

    # 2. weather for the default location, through the registered tool
    weather = _tool(runtime, WEATHER_TOOL)
    if weather is None:
        data.failures["weather"] = "skipped (no tool runtime)"
    else:
        try:
            days_ahead = (day - now.date()).days
            args: dict[str, Any] = {"days": max(1, min(7, days_ahead + 1))}
            if _weather_from_home(runtime, getattr(weather, "ctx", None), paths):
                args["location"] = "home"
            data.weather = _run_tool(weather, args)
        except Exception as exc:
            data.failures["weather"] = str(exc) if isinstance(exc, JarvisError) else f"{type(exc).__name__}: {exc}"
            log.warning("brief: weather failed: %s", exc)

    # 3. reminders due that day
    reminders = _tool(runtime, REMINDERS_TOOL)
    if reminders is None:
        data.failures["reminders"] = "skipped (no tool runtime)"
    else:
        try:
            data.reminders = _run_tool(reminders, {"due_range": day.isoformat()})
        except Exception as exc:
            data.failures["reminders"] = str(exc) if isinstance(exc, JarvisError) else f"{type(exc).__name__}: {exc}"
            log.warning("brief: reminders failed: %s", exc)

    # 4. football: the calendar first, then the reminders; otherwise the model web-searches.
    #    Yesterday's calendar says whether there was a match whose result is worth looking up.
    calendars = football_calendars(config)
    data.match_event, data.match_reminder = match_of_the_day(data.team, data.agenda, data.reminders, prefer_calendars=calendars)
    try:
        yesterday, _ = merge_calendars(config, start - timedelta(days=1), start)
        data.yesterday_match = yesterdays_match(data.team, yesterday, calendars)
    except Exception as exc:  # a note is not worth it: today's agenda already reported the backend
        log.warning("brief: yesterday's calendar failed: %s", exc)

    # 4b. alarms and timers set for the day (the scheduler's table; nothing to fail but a missing db)
    db = getattr(runtime, "db", None) if runtime is not None else None
    if db is not None:
        try:
            data.timers = timers_of_the_day(db, day)
        except Exception as exc:
            data.failures["alarms and timers"] = f"{type(exc).__name__}: {exc}"
            log.warning("brief: timers failed: %s", exc)

    # 4c. today's mail, when this turn may read Gmail
    if config.brief.include_mail and day == now.date() and _tool(runtime, GMAIL_TOOL) is not None:
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=local_tz())
        try:
            from jarvis.tools import gmail  # noqa: PLC0415 - Google client libraries load only when needed

            messages, more = gmail.inbox_since(midnight)
            data.mail = gmail.format_inbox(messages, more, midnight, now)
        except Exception as exc:
            data.failures["mail"] = str(exc) if isinstance(exc, JarvisError) else f"{type(exc).__name__}: {exc}"
            log.warning("brief: mail failed: %s", exc)

    # 5. optional: travel time to the first timed event with a location
    if config.brief.include_travel:
        target = first_event_with_location(data.agenda)
        travel = _tool(runtime, TRAVEL_TOOL)
        if target is None:
            pass  # nothing to travel to
        elif travel is None:
            data.failures["travel"] = "skipped (no maps tool; is the Google Maps key set?)"
        else:
            try:
                args = {
                    "destination": target.location,
                    "mode": config.maps.default_mode,
                    "arrive_by": target.start.astimezone(local_tz()).strftime("%Y-%m-%d %H:%M"),
                }
                data.travel = f"To {target.title} ({target.location}):\n" + _run_tool(travel, args)
            except Exception as exc:
                data.failures["travel"] = str(exc) if isinstance(exc, JarvisError) else f"{type(exc).__name__}: {exc}"
                log.warning("brief: travel time failed: %s", exc)
    return data


class DailyBriefInput(BaseModel):
    date: str = Field(default="today", description="today, tomorrow, yesterday or YYYY-MM-DD.")


class DailyBrief(Tool):
    name = "daily_brief"
    description = (
        "The daily brief for one day: agenda from both calendars, weather, reminders due, and whether "
        "the user's football team plays (from the calendar or the reminders; otherwise it tells you to web-search), the "
        "alarms and timers of the day and today's mail. Summarize it briefly."
    )
    input_model = DailyBriefInput
    risk = RiskLevel.SAFE
    taints = True  # it carries calendar, reminder and mail text, which are untrusted
    taint_source = "brief"
    delegate_ok = True

    def __init__(self, ctx: ToolContext | None = None) -> None:
        super().__init__(ctx)

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, DailyBriefInput)
        ctx = self.require_ctx()
        data = build_brief(ctx.config, ctx.paths, ctx.runtime, args.date)
        return ToolResult(data.as_text())


class BriefScheduleInput(BaseModel):
    enabled: bool | None = Field(default=None, description="Turn the automatic morning brief on or off.")
    weekday_time: str | None = Field(default=None, description="Monday-Friday delivery time as HH:MM, or 'off' for no weekday brief.")
    weekend_time: str | None = Field(default=None, description="Saturday-Sunday delivery time as HH:MM, or 'off' for no weekend brief.")
    channel: Literal["auto", "telegram", "voice", "both"] | None = Field(
        default=None, description="auto = spoken when the user is at the Mac, else Telegram; or force telegram, voice, both."
    )
    late_until: str | None = Field(
        default=None, description="HH:MM: a brief missed while the Mac slept is still delivered on wake until this time."
    )


class SetBriefSchedule(Tool):
    name = "set_brief_schedule"
    description = (
        "Show or change the automatic morning brief's schedule: delivery time on weekdays and at the weekend "
        "('off' disables those days), on/off, channel (auto / telegram / voice / both) and the late-delivery limit. "
        "Call it without arguments to see the current schedule and the next run. Use for 'move the morning brief to 7:30', "
        "'no brief at the weekend', 'turn the brief off / on', 'send the brief on Telegram'. Writes config.local.toml; "
        "the running scheduler applies the change on its next check (seconds), nothing to restart or reinstall."
    )
    input_model = BriefScheduleInput
    risk = RiskLevel.SAFE
    delegate_ok = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, BriefScheduleInput)
        from jarvis.scheduler import brief as schedule  # noqa: PLC0415

        ctx = self.require_ctx()
        values: dict[str, Any] = {}
        for name in ("weekday_time", "weekend_time"):
            value = getattr(args, name)
            if value is None:
                continue
            if value.strip().lower() in ("", "off", "kapalı", "yok"):
                values[name] = "off"
            elif parse_hhmm(value) is None:
                raise ToolValidationError(f"{name} must be HH:MM or 'off' (got {value!r})")
            else:
                values[name] = "%02d:%02d" % parse_hhmm(value)
        if args.late_until is not None:
            if parse_hhmm(args.late_until) is None:
                raise ToolValidationError(f"late_until must be HH:MM (got {args.late_until!r})")
            values["late_until"] = "%02d:%02d" % parse_hhmm(args.late_until)
        if args.channel is not None:
            values["channel"] = args.channel
        if args.enabled is not None:
            values["enabled"] = args.enabled
        config = ctx.config
        changed = ""
        if values:
            try:
                update_local_config(ctx.paths.config_file, "brief.schedule", values)
                config = load_config(ctx.paths.config_file)
            except ConfigError as exc:
                raise ToolValidationError(str(exc)) from None
            changed = (
                "Morning brief schedule updated (config.local.toml; the scheduler applies it on its next check, no restart needed). Now: "
            )
        state = None
        db = getattr(ctx.runtime, "db", None) if ctx.runtime is not None else None
        if db is not None:
            try:
                state = schedule.load_state(db)
            except Exception:
                state = None
        return ToolResult((changed or "Morning brief schedule: ") + schedule.describe(config.brief.schedule, now_local(), state))
