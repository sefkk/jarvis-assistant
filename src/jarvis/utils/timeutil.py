"""Small time helpers shared across modules."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone, tzinfo

from jarvis.exceptions import ToolValidationError
from jarvis.i18n import t, t_list
from jarvis.utils.text import fold


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """UTC timestamp with millisecond precision, e.g. 2026-10-14T07:23:01.123Z."""
    return now_utc().strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc().microsecond // 1000:03d}Z"


def now_local() -> datetime:
    return datetime.now().astimezone()


def local_tz() -> tzinfo:
    return now_local().tzinfo or timezone.utc


def today_local() -> str:
    """Local date as YYYY-MM-DD (used for memory entries and daily budgets)."""
    return now_local().strftime("%Y-%m-%d")


def monotonic_ms() -> float:
    return time.perf_counter() * 1000.0


# --- parsing user-facing dates ------------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")


def parse_datetime(text: str, *, default_time: tuple[int, int] = (0, 0), now: datetime | None = None) -> datetime:
    """'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' or ISO 8601 with offset -> aware datetime in the local zone."""
    text = text.strip()
    tz = (now or now_local()).tzinfo or local_tz()
    try:
        if _DATE_RE.match(text):
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, *default_time, tzinfo=tz)
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        raise ToolValidationError(f"cannot parse date/time {text!r}; use YYYY-MM-DD or YYYY-MM-DD HH:MM") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def parse_date_range(text: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """A range from words or dates. End is exclusive.

    Accepted: today, tomorrow, yesterday, week (today plus 7 days), next week, weekend,
    YYYY-MM-DD (that day), YYYY-MM-DD..YYYY-MM-DD (inclusive days), and YYYY-MM-DD HH:MM..YYYY-MM-DD HH:MM.
    """
    now = now or now_local()
    tz = now.tzinfo or local_tz()
    start_of_today = datetime(now.year, now.month, now.day, tzinfo=tz)
    word = fold(text.strip())  # "YARIN", "Yarın" and "yarin" alike
    day = timedelta(days=1)
    if word in ("", "today", "bugün", "bugun"):
        return start_of_today, start_of_today + day
    if word in ("tomorrow", "yarın", "yarin"):
        return start_of_today + day, start_of_today + 2 * day
    if word in ("yesterday", "dün", "dun"):
        return start_of_today - day, start_of_today
    if word in ("week", "this week", "bu hafta", "7 days", "next 7 days"):
        return start_of_today, start_of_today + 7 * day
    if word in ("next week", "gelecek hafta", "haftaya"):
        monday = start_of_today + timedelta(days=7 - start_of_today.weekday())
        return monday, monday + 7 * day
    if word in ("weekend", "hafta sonu"):
        saturday = start_of_today + timedelta(days=(5 - start_of_today.weekday()) % 7)
        return saturday, saturday + 2 * day
    if ".." in word:
        a, b = (part.strip() for part in text.split("..", 1))
        start = parse_datetime(a, now=now)
        end = parse_datetime(b, now=now)
        if _DATE_RE.match(b.strip()):
            end = end + day  # a bare end date means the whole day
        if end <= start:
            raise ToolValidationError("the end of the range must be after the start")
        return start, end
    start = parse_datetime(text, now=now)
    if _DATE_RE.match(text.strip()):
        return start, start + day
    return start, start + timedelta(hours=1)


def format_local(dt: datetime, *, with_date: bool = True) -> str:
    local = dt.astimezone(local_tz())
    return local.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")


# --- alarm times: "7:30", "yarın sabah 7'de", "hafta içi her gün 7:30", "cumartesi 10:00" ------------

_ASCII = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def fold_turkish(text: str) -> str:
    """`fold` with the Turkish letters also folded to ASCII, so "yarın"/"yarin", "çarşamba"/"carsamba" and
    "FRIDAY"/"friday" match. The word tables below hold both languages, whatever the main language is."""
    return fold(text).translate(_ASCII)


WEEKDAYS_TR = ("pazartesi", "salı", "çarşamba", "perşembe", "cuma", "cumartesi", "pazar")
WEEKDAYS_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAY_SHORT_TR = ("pzt", "sal", "çar", "per", "cum", "cmt", "paz")
WEEKDAY_SHORT_EN = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_WORDS: dict[str, int] = {}
for _names in (WEEKDAYS_TR, WEEKDAYS_EN, WEEKDAY_SHORT_TR, WEEKDAY_SHORT_EN):
    for _i, _name in enumerate(_names):
        WEEKDAY_WORDS[_name] = _i
        WEEKDAY_WORDS[fold_turkish(_name)] = _i  # "carsamba", "persembe", "sali"
WEEKDAYS = [0, 1, 2, 3, 4]
WEEKENDS = [5, 6]
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]

_TIME_RE = re.compile(r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?(?:\s*'?\s*(?:d[ae]|t[ae]|de|da|te|ta))?(?![\d])")
_REPEAT_WORDS = {
    "daily": EVERY_DAY,
    "everyday": EVERY_DAY,
    "every day": EVERY_DAY,
    "her gün": EVERY_DAY,
    "hergün": EVERY_DAY,
    "her sabah": EVERY_DAY,
    "her akşam": EVERY_DAY,
    "her gece": EVERY_DAY,
    "weekdays": WEEKDAYS,
    "weekday": WEEKDAYS,
    "hafta içi": WEEKDAYS,
    "haftaiçi": WEEKDAYS,
    "iş günleri": WEEKDAYS,
    "weekends": WEEKENDS,
    "weekend": WEEKENDS,
    "hafta sonu": WEEKENDS,
    "haftasonu": WEEKENDS,
    "hafta sonları": WEEKENDS,
}
_DAY_OFFSET_WORDS = {
    "bugün": 0,
    "today": 0,
    "tonight": 0,
    "bu gece": 0,
    "bu akşam": 0,
    "yarın": 1,
    "tomorrow": 1,
    "öbür gün": 2,
    "öbürgün": 2,
    "ertesi gün": 1,
}
_EVENING_WORDS = ("akşam", "gece", "öğleden sonra", "pm", "evening", "night", "afternoon")
_MORNING_WORDS = ("sabah", "am", "morning")
_NOON_WORDS = ("öğlen", "öğle", "noon")


def _norm(text: str) -> str:
    return " ".join(fold_turkish(text).replace("’", "'").replace("`", "'").split())


_REPEAT_FOLDED = {fold_turkish(k): v for k, v in _REPEAT_WORDS.items()}
_DAY_OFFSET_FOLDED = {fold_turkish(k): v for k, v in _DAY_OFFSET_WORDS.items()}
_EVENING_FOLDED = tuple(fold_turkish(w) for w in _EVENING_WORDS)
_MORNING_FOLDED = tuple(fold_turkish(w) for w in _MORNING_WORDS)
_NOON_FOLDED = tuple(fold_turkish(w) for w in _NOON_WORDS)


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def parse_repeat(text: str | None) -> list[int]:
    """Repeat days from words: 'daily' / 'her gün', 'weekdays' / 'hafta içi', 'weekends' / 'hafta sonu', or
    weekday names in Turkish or English separated by commas, spaces or 've' ('mon,wed', 'pazartesi ve çarşamba').
    Returns sorted weekday numbers (0 = Monday); [] for empty, 'none', 'off', 'once'."""
    raw = _norm(text or "")
    if raw in ("", "none", "off", "once", "no", "tek", "bir kez", "tek sefer"):
        return []
    found: list[int] = []
    for word, days in _REPEAT_FOLDED.items():
        if _has_word(raw, word) and (not found or len(days) < len(found)):
            found = list(days)
    if found:
        return found
    days: set[int] = set()
    for token in re.split(r"[,\s/]+|\bve\b|\band\b", raw):
        token = token.strip("'.").removesuffix("leri").removesuffix("lari")  # "cumartesileri"
        if not token or token in ("her", "every", "gunleri", "gunu"):
            continue
        if token in WEEKDAY_WORDS:
            days.add(WEEKDAY_WORDS[token])
        else:
            raise ToolValidationError(f"cannot understand the repeat {text!r}; use daily, weekdays, weekends or weekday names")
    return sorted(days)


def _weekdays_in(text: str) -> tuple[list[int], bool]:
    """Weekday names mentioned in free text and whether 'her' / 'every' precedes one of them."""
    days: list[int] = []
    repeating = False
    for match in re.finditer(r"(?<![a-z])(her|every)?\s*([a-z]+)", text):
        word = match.group(2).removesuffix("leri").removesuffix("lari")
        if word in WEEKDAY_WORDS and WEEKDAY_WORDS[word] not in days:
            days.append(WEEKDAY_WORDS[word])
            if match.group(1):
                repeating = True
    return days, repeating


def parse_alarm_time(text: str, *, now: datetime | None = None) -> tuple[int, int, date | None, list[int]]:
    """Free text -> (hour, minute, explicit day or None, repeat days). Understands 'HH:MM', 'H.MM',
    "7'de", 'saat 7', 'sabah 7' / 'akşam 9' / 'gece 1' (the period fixes am/pm), 'bugün' / 'yarın' /
    'öbür gün', weekday names (one name: that day, one shot; 'her cumartesi' or several names: a repeat),
    'her gün', 'hafta içi', 'hafta sonu', and 'YYYY-MM-DD HH:MM'. Raises ToolValidationError when no time
    is found."""
    now = now or now_local()
    raw = text.strip()
    if _DATETIME_RE.match(raw):
        when = parse_datetime(raw, now=now)
        return when.hour, when.minute, when.date(), []
    norm = _norm(raw)
    repeat: list[int] = []
    for word, days in _REPEAT_FOLDED.items():  # "hafta içi her gün": the narrower set wins over "her gün"
        if _has_word(norm, word):
            if not repeat or len(days) < len(repeat):
                repeat = list(days)
            norm = re.sub(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", " ", norm)
    day_offset: int | None = None
    for word, offset in _DAY_OFFSET_FOLDED.items():
        if _has_word(norm, word):
            day_offset = offset
            norm = re.sub(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", " ", norm)
            break
    weekdays, every = _weekdays_in(norm)
    if weekdays and not repeat:
        if every or len(weekdays) > 1:
            repeat = sorted(weekdays)
        norm = re.sub(
            r"(?<![a-z])[a-z]+",
            lambda m: " " if m.group(0).removesuffix("leri").removesuffix("lari") in WEEKDAY_WORDS else m.group(0),
            norm,
        )
    match = _TIME_RE.search(norm)
    if not match:
        raise ToolValidationError(f"no clock time in {text!r}; say for example '7:30', 'tomorrow morning 7' or 'weekdays 7:30'")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 24 or minute > 59 or (hour == 24 and minute > 0):
        raise ToolValidationError(f"invalid clock time in {text!r}")
    hour %= 24
    if any(_has_word(norm, w) for w in _EVENING_FOLDED) and hour < 12 and not (hour < 6 and _has_word(norm, "gece")):
        hour += 12  # "akşam 9" = 21:00, "gece 11" = 23:00 ("gece 1" stays 01:00)
    elif any(_has_word(norm, w) for w in _NOON_FOLDED) and hour < 6:
        hour += 12  # "öğlen 1" = 13:00
    elif any(_has_word(norm, w) for w in _MORNING_FOLDED) and hour == 12:
        hour = 0
    day: date | None = None
    if day_offset is not None:
        day = (now + timedelta(days=day_offset)).date()
    elif len(weekdays) == 1 and not repeat:
        ahead = (weekdays[0] - now.weekday()) % 7
        day = (now + timedelta(days=ahead)).date()
        if ahead == 0 and now.replace(hour=hour, minute=minute, second=0, microsecond=0) <= now:
            day += timedelta(days=7)
    return hour, minute, day, repeat


def next_alarm_occurrence(hour: int, minute: int, repeat: list[int], *, after: datetime, day: date | None = None) -> datetime:
    """The first moment strictly after `after` at hour:minute on a repeat day (any day when `repeat` is
    empty), or on `day` when given. Raises ToolValidationError when `day` at that time is already past."""
    tz = after.tzinfo or local_tz()
    if day is not None:
        when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if when <= after:
            raise ToolValidationError(f"{format_local(when)} is in the past; give a future time")
        return when
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(8):
        if candidate > after and (not repeat or candidate.weekday() in repeat):
            return candidate
        candidate += timedelta(days=1)
    raise ToolValidationError("could not find a day for the alarm")


def describe_repeat(repeat: list[int], language: str | None = None) -> str:
    """'daily', 'weekdays', 'weekends', 'every Mon, Wed' in `language` (default: the main language)."""
    if not repeat:
        return ""
    days = sorted(repeat)
    if days == EVERY_DAY:
        return t("alarm.repeat.daily", language)
    if days == WEEKDAYS:
        return t("alarm.repeat.weekdays", language)
    if days == WEEKENDS:
        return t("alarm.repeat.weekends", language)
    names = t_list("alarm.repeat.weekday_short", language)
    return t("alarm.repeat.days", language, days=", ".join(names[d] for d in days))
