"""Spoken-form normalization: what the TTS engines get instead of digits and symbols.

ElevenLabs' Flash model (and `say`, in its own way) reads Turkish digits unreliably: "19.00" came
out as "19sırdfdıs", "%75" as "75yüsdffgh". Every engine therefore runs `normalize_for_speech`
on each sentence in `speak()` before anything is queued, so the audio pipeline only ever sees
words. The rules are language-aware (tr / en) and deliberately conservative: a form the rules do
not recognise is read as a plain number rather than guessed at, and a Turkish suffix that cannot
be attached safely is dropped rather than spoken as garbage.

What is rewritten, in this order (each pass removes the digits it handles, so later passes never
see them twice):
  dates        17.10.2026, 17/10/2026, 2026-10-17 -> "on yedi Ekim iki bin yirmi altı"
  times        19:00, 19.00, 08:30, 19:30:15      -> "saat on dokuz", "sekiz otuz" / "seven PM"
  percentages  %75, 75 %, 75%                      -> "yüzde yetmiş beş" / "seventy-five percent"
  currency     €50, 50 €, $20, 1.500 TL, 20₺       -> "elli euro", "yirmi dolar", "bin beş yüz lira"
  units        5 km, 21°C, 3.5 kg, 10 dk, 30 sn    -> "beş kilometre", "yirmi bir derece", ...
  ordinals     "1. madde", 3'üncü, 1st / 22nd      -> "birinci madde", "üçüncü", "first"
  ranges       19:00-20:00, 5-10 km                -> "... ila ...", "... to ..."
  numbers      1,14 (tr) / 1.14 (en) / 1.500 (tr)  -> "bir virgül on dört", "one point one four"
  suffixes     19.00'da, 3'te, 2026'da, %75'e, 5 km'ye -> "on dokuzda", "üçte", "altıda", "beşe"
  symbols      # * / | _ ~ ^ < > = { } [ ] \\ →     -> removed (or "ve" / "and" for &)

Turkish number words are built here (num2words joins them without spaces: "yetmişbeş"); English
ones come from num2words. `normalize_for_speech` is idempotent: text without digits or symbols
passes through unchanged, so the ElevenLabs engine may normalize and its `say` fallback may do it
again.
"""

from __future__ import annotations

import re
from typing import Callable

from num2words import num2words

Language = str  # "tr" | "en"

# --- Turkish number words ------------------------------------------------------------------------------

_ONES_TR = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
_TENS_TR = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
_SCALES_TR = [(10**9, "milyar"), (10**6, "milyon"), (10**3, "bin")]
_ORDINAL_TR = {
    "sıfır": "sıfırıncı",
    "bir": "birinci",
    "iki": "ikinci",
    "üç": "üçüncü",
    "dört": "dördüncü",
    "beş": "beşinci",
    "altı": "altıncı",
    "yedi": "yedinci",
    "sekiz": "sekizinci",
    "dokuz": "dokuzuncu",
    "on": "onuncu",
    "yirmi": "yirminci",
    "otuz": "otuzuncu",
    "kırk": "kırkıncı",
    "elli": "ellinci",
    "altmış": "altmışıncı",
    "yetmiş": "yetmişinci",
    "seksen": "sekseninci",
    "doksan": "doksanıncı",
    "yüz": "yüzüncü",
    "bin": "bininci",
    "milyon": "milyonuncu",
    "milyar": "milyarıncı",
}
_MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
DIGIT_BY_DIGIT_FROM = 10  # 10+ digits (phone numbers, ids) are read one digit at a time


def _below_thousand_tr(n: int) -> str:
    parts: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append("yüz" if hundreds == 1 else f"{_ONES_TR[hundreds]} yüz")
    tens, ones = divmod(rest, 10)
    if tens:
        parts.append(_TENS_TR[tens])
    if ones:
        parts.append(_ONES_TR[ones])
    return " ".join(parts)


def int_to_words_tr(n: int) -> str:
    """0 -> "sıfır", 1500 -> "bin beş yüz", 2026 -> "iki bin yirmi altı" (words separated by spaces)."""
    if n < 0:
        return "eksi " + int_to_words_tr(-n)
    if n == 0:
        return "sıfır"
    if n >= 10**12:
        return digits_to_words(str(n), "tr")
    parts: list[str] = []
    for value, name in _SCALES_TR:
        if n >= value:
            count, n = divmod(n, value)
            parts.append(name if value == 1000 and count == 1 else f"{_below_thousand_tr(count)} {name}")
    if n:
        parts.append(_below_thousand_tr(n))
    return " ".join(parts)


def int_to_words_en(n: int) -> str:
    if n >= 10**12:
        return digits_to_words(str(n), "en")
    return num2words(n, lang="en")


def int_to_words(n: int, language: Language) -> str:
    return int_to_words_tr(n) if language == "tr" else int_to_words_en(n)


def digits_to_words(digits: str, language: Language) -> str:
    """ "0532" -> "sıfır beş üç iki": one word per digit."""
    return " ".join(int_to_words(int(d), language) for d in digits)


def ordinal_words(n: int, language: Language) -> str:
    if language == "tr":
        words = int_to_words_tr(n).split(" ")
        words[-1] = _ORDINAL_TR.get(words[-1], words[-1] + "ıncı")
        return " ".join(words)
    return num2words(n, lang="en", to="ordinal")


def year_words(year: int, language: Language) -> str:
    if language == "tr":
        return int_to_words_tr(year)
    return num2words(year, lang="en", to="year")


# --- Turkish suffixes (vowel harmony) --------------------------------------------------------------------

_VOWELS = "aeıioöuü"
_BACK = "aıou"
_ROUNDED = "ouöü"
_HARD_CONSONANTS = "çfhkpsşt"
_SUFFIX_RE = re.compile(r"[a-zçğıöşü]{1,8}")


def _last_vowel(word: str) -> str | None:
    for ch in reversed(word):
        if ch in _VOWELS:
            return ch
    return None


def attach_suffix_tr(words: str, suffix: str) -> str:
    """Attach a written suffix ("da", "te", "e", "üncü", "ye", "lik") to the last word of a spoken number
    with vowel harmony and consonant assimilation: "on dokuz" + "da" -> "on dokuzda", "üç" + "te" ->
    "üçte", "dört" + "ü" -> "dördü", "altı" + "a" -> "altıya". An unrecognised suffix is dropped."""
    suffix = suffix.lower()
    if not words or not _SUFFIX_RE.fullmatch(suffix):
        return words
    head, _, stem = words.rpartition(" ")
    vowel = _last_vowel(stem)
    if vowel is None:
        return words
    back, rounded = vowel in _BACK, vowel in _ROUNDED
    invariant_tail = ""
    if suffix.endswith(("ki", "kı")):  # "-ki" never harmonises: "üçteki", "beşinciki"
        suffix, invariant_tail = suffix[:-2], "ki"
    out: list[str] = []
    for ch in suffix:
        if ch in "ae":
            out.append("a" if back else "e")
        elif ch in "ıiuü":
            out.append(("u" if back else "ü") if rounded else ("ı" if back else "i"))
        else:
            out.append(ch)
    suffix = "".join(out)
    if suffix:
        if suffix[0] in "dt":
            suffix = ("t" if stem[-1] in _HARD_CONSONANTS else "d") + suffix[1:]
        if suffix[0] in _VOWELS and stem[-1] in _VOWELS:
            suffix = "y" + suffix  # "altı" + "a" -> "altıya" (the buffer letter was left out)
        elif suffix[0] == "y" and len(suffix) > 1 and suffix[1] in _VOWELS and stem[-1] not in _VOWELS:
            suffix = suffix[1:]  # "dokuz" + "ya" -> "dokuza" (a buffer letter that does not belong)
        if suffix[0] in _VOWELS and stem.endswith("dört"):
            stem = stem[:-1] + "d"
    stem += suffix + invariant_tail
    return f"{head} {stem}" if head else stem


def _with_suffix(words: str, suffix: str | None, language: Language) -> str:
    """The spoken form plus whatever followed the apostrophe. English has no such suffixes worth
    guessing at ("90's"), so they are dropped there."""
    if not suffix:
        return words
    return attach_suffix_tr(words, suffix) if language == "tr" else words


# --- number reading ------------------------------------------------------------------------------------

_SUFFIX_PART = r"(?:['’](?P<suffix>[^\W\d_]{1,8}))?"
_NOT_AFTER_DIGIT = r"(?<![\d.,:])"
_NOT_BEFORE_DIGIT = r"(?![\d])"


def _fraction_words(fraction: str, language: Language) -> str:
    """ "14" -> "on dört" (tr) / "one four" (en); "05" -> "sıfır beş"; three or more digits one by one."""
    if language == "tr" and len(fraction) <= 2 and not fraction.startswith("0"):
        return int_to_words_tr(int(fraction))
    return digits_to_words(fraction, language)


def number_words(token: str, language: Language) -> str:
    """A numeric token in the written conventions of `language` ("1.500" and "1,14" in Turkish,
    "1,500" and "1.14" in English) as words. Unknown shapes are read digit by digit."""
    token = token.strip()
    sign = ""
    if token.startswith(("-", "−")):
        sign = "eksi " if language == "tr" else "minus "
        token = token[1:]
    thousands_sep, decimal_sep = (".", ",") if language == "tr" else (",", ".")
    decimal_word = "virgül" if language == "tr" else "point"
    integer, fraction = token, ""
    if re.fullmatch(r"\d{1,3}(?:" + re.escape(thousands_sep) + r"\d{3})+", token):
        integer = token.replace(thousands_sep, "")
    elif decimal_sep in token:
        integer, _, fraction = token.partition(decimal_sep)
    elif thousands_sep in token:
        # "1.14" in Turkish, "1,5" in English: the other language's decimal, read as "nokta" / "point"
        integer, _, fraction = token.partition(thousands_sep)
        decimal_word = "nokta" if language == "tr" else "point"
    if not integer.isdigit() or not (fraction == "" or fraction.isdigit()):
        return sign + digits_to_words(re.sub(r"\D", "", token), language)
    if len(integer) >= DIGIT_BY_DIGIT_FROM or (len(integer) > 1 and integer.startswith("0")):
        words = digits_to_words(integer, language)
    else:
        words = int_to_words(int(integer), language)
    if fraction:
        words += f" {decimal_word} {_fraction_words(fraction, language)}"
    return sign + words


def _is_one(token: str) -> bool:
    return re.fullmatch(r"1(?:[.,]0+)?", token) is not None


# --- rule tables -------------------------------------------------------------------------------------------

# unit as written -> (Turkish word, English singular, English plural)
UNITS: dict[str, tuple[str, str, str]] = {
    "km": ("kilometre", "kilometer", "kilometers"),
    "m": ("metre", "meter", "meters"),
    "cm": ("santimetre", "centimeter", "centimeters"),
    "mm": ("milimetre", "millimeter", "millimeters"),
    "m²": ("metrekare", "square meter", "square meters"),
    "m2": ("metrekare", "square meter", "square meters"),
    "kg": ("kilogram", "kilogram", "kilograms"),
    "g": ("gram", "gram", "grams"),
    "gr": ("gram", "gram", "grams"),
    "mg": ("miligram", "milligram", "milligrams"),
    "l": ("litre", "liter", "liters"),
    "lt": ("litre", "liter", "liters"),
    "ml": ("mililitre", "milliliter", "milliliters"),
    "km/s": ("kilometre saat", "kilometers per hour", "kilometers per hour"),
    "km/sa": ("kilometre saat", "kilometers per hour", "kilometers per hour"),
    "km/h": ("kilometre saat", "kilometers per hour", "kilometers per hour"),
    "°c": ("derece", "degree", "degrees"),
    "°f": ("fahrenheit", "degree Fahrenheit", "degrees Fahrenheit"),
    "°": ("derece", "degree", "degrees"),
    "dk": ("dakika", "minute", "minutes"),
    "min": ("dakika", "minute", "minutes"),
    "sn": ("saniye", "second", "seconds"),
    "sec": ("saniye", "second", "seconds"),
    "ms": ("milisaniye", "millisecond", "milliseconds"),
    "sa": ("saat", "hour", "hours"),
    "h": ("saat", "hour", "hours"),
    "kcal": ("kilokalori", "kilocalorie", "kilocalories"),
    "cal": ("kalori", "calorie", "calories"),
    "gb": ("gigabayt", "gigabyte", "gigabytes"),
    "mb": ("megabayt", "megabyte", "megabytes"),
    "tb": ("terabayt", "terabyte", "terabytes"),
    "kb": ("kilobayt", "kilobyte", "kilobytes"),
}
# symbol or code as written -> (Turkish, English singular, English plural)
CURRENCIES: dict[str, tuple[str, str, str]] = {
    "€": ("euro", "euro", "euros"),
    "eur": ("euro", "euro", "euros"),
    "$": ("dolar", "dollar", "dollars"),
    "usd": ("dolar", "dollar", "dollars"),
    "£": ("sterlin", "pound", "pounds"),
    "gbp": ("sterlin", "pound", "pounds"),
    "₺": ("lira", "lira", "lira"),
    "tl": ("lira", "lira", "lira"),
    "try": ("lira", "lira", "lira"),
}

_NUMBER = r"(?P<num>-?\d+(?:[.,]\d+)*)"
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
_CURRENCY_SYMBOLS = "€$£₺"
_CURRENCY_CODES = "|".join(c for c in CURRENCIES if c.isalpha())

DATE_RE = re.compile(
    _NOT_AFTER_DIGIT + r"(?:(?P<d>\d{1,2})(?P<sep>[./])(?P<m>\d{1,2})(?P=sep)(?P<y>\d{4})"
    r"|(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2}))" + _SUFFIX_PART + _NOT_BEFORE_DIGIT
)
TIME_RE = re.compile(
    _NOT_AFTER_DIGIT
    + r"(?P<h>[01]?\d|2[0-3])(?P<sep>[:.])(?P<mm>[0-5]\d)(?::(?P<ss>[0-5]\d))?"
    + _SUFFIX_PART
    + r"(?![\d.,]\d)"
    + _NOT_BEFORE_DIGIT
)
PERCENT_RE = re.compile(
    _NOT_AFTER_DIGIT + r"(?:%\s?" + _NUMBER + r"|" + _NUMBER.replace("num", "num2") + r"\s?%)" + _SUFFIX_PART + _NOT_BEFORE_DIGIT
)
CURRENCY_RE = re.compile(
    _NOT_AFTER_DIGIT
    + r"(?:(?P<pre>["
    + _CURRENCY_SYMBOLS
    + r"])\s?"
    + _NUMBER
    + r"|"
    + _NUMBER.replace("num", "num2")
    + r"\s?(?P<post>["
    + _CURRENCY_SYMBOLS
    + r"]|(?i:"
    + _CURRENCY_CODES
    + r")(?![^\W\d_])))"
    + _SUFFIX_PART
    + _NOT_BEFORE_DIGIT
)
UNIT_RE = re.compile(
    _NOT_AFTER_DIGIT + _NUMBER + r"\s?(?P<unit>" + _UNIT_ALT + r")(?![^\W\d_])" + _SUFFIX_PART + _NOT_BEFORE_DIGIT,
    re.IGNORECASE,
)
ORDINAL_DOT_RE = re.compile(_NOT_AFTER_DIGIT + r"(?P<n>\d{1,4})\.(?=[ \u00a0]+[a-zçğıöşü])")
ORDINAL_SUFFIX_TR_RE = re.compile(
    _NOT_AFTER_DIGIT + r"(?P<n>\d+)['’](?P<suf>[ıiuü]?nc[ıiuü])(?P<rest>[a-zçğıöşü]{0,6})" + _NOT_BEFORE_DIGIT
)
ORDINAL_EN_RE = re.compile(_NOT_AFTER_DIGIT + r"(?P<n>\d+)(?:st|nd|rd|th)(?![^\W\d_])")
RANGE_RE = re.compile(r"(?<=\d)\s?[-–—]\s?(?=\d)")
PLUS_RE = re.compile(r"(?<![\w])\+\s?(?=\d)")
NUMBER_RE = re.compile(_NOT_AFTER_DIGIT + r"(?P<neg>(?<![\w])[-−])?" + _NUMBER.replace("-?", "") + _SUFFIX_PART + _NOT_BEFORE_DIGIT)
SYMBOLS_RE = re.compile(r"[#*|_~^<>=\\{}\[\]]+")
ARROW_RE = re.compile(r"\s*(?:->|=>|→|⇒|←|↔)\s*")
AMP_RE = re.compile(r"\s*&\s*")
SLASH_RE = re.compile(r"/")
STRAY_PERCENT_RE = re.compile(r"%")
SPACE_PUNCT_RE = re.compile(r"\s+([.,!?;:])")
_PRECEDED_BY_SAAT_RE = re.compile(r"(?:^|\W)(?:saat|at)\s*$", re.IGNORECASE)


# --- the passes ----------------------------------------------------------------------------------------


def _date(match: re.Match, language: Language) -> str:
    if match.group("iy"):
        day, month, year = int(match.group("id")), int(match.group("im")), int(match.group("iy"))
    else:
        day, month, year = int(match.group("d")), int(match.group("m")), int(match.group("y"))
        if language == "en" and match.group("sep") == "/":
            day, month = month, day  # 10/17/2026
            if month > 12 >= day:
                day, month = month, day
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return match.group(0)
    if language == "tr":
        words = f"{int_to_words_tr(day)} {_MONTHS_TR[month - 1]} {int_to_words_tr(year)}"
    else:
        words = f"{_MONTHS_EN[month - 1]} {ordinal_words(day, 'en')} {year_words(year, 'en')}"
    return _with_suffix(words, match.group("suffix"), language)


def _looks_like_time(match: re.Match, text: str) -> bool:
    """ "19:00" always; "19.00" only when the context says so (two-digit hour, a "saat"/"at" before it,
    a suffix after it, or quarter minutes), so "1.14" stays a decimal."""
    if match.group("sep") == ":":
        return True
    hour, minutes = match.group("h"), match.group("mm")
    if match.group("ss") or match.group("suffix"):
        return True
    if _PRECEDED_BY_SAAT_RE.search(text[: match.start()]):
        return True
    return len(hour) == 2 or minutes in ("00", "15", "30", "45")


def _time(match: re.Match, text: str, language: Language) -> str:
    if not _looks_like_time(match, text):
        return match.group(0)
    hour, minutes = int(match.group("h")), int(match.group("mm"))
    seconds = int(match.group("ss")) if match.group("ss") else None
    if language == "tr":
        words = int_to_words_tr(hour)
        if minutes:
            words += " " + (f"sıfır {int_to_words_tr(minutes)}" if minutes < 10 else int_to_words_tr(minutes))
        elif not _PRECEDED_BY_SAAT_RE.search(text[: match.start()]):
            words = "saat " + words
        if seconds is not None:
            words += f" {int_to_words_tr(seconds)} saniye" if not minutes else f" {int_to_words_tr(seconds)}"
    else:
        suffix = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        words = int_to_words_en(hour12)
        if minutes:
            words += " " + (f"oh {int_to_words_en(minutes)}" if minutes < 10 else int_to_words_en(minutes))
        if seconds is not None:
            words += f" and {int_to_words_en(seconds)} seconds"
        words += f" {suffix}"
    return _with_suffix(words, match.group("suffix"), language)


def _percent(match: re.Match, language: Language) -> str:
    number = match.group("num") or match.group("num2")
    words = number_words(number, language)
    words = f"yüzde {words}" if language == "tr" else f"{words} percent"
    return _with_suffix(words, match.group("suffix"), language)


def _currency(match: re.Match, language: Language) -> str:
    symbol = (match.group("pre") or match.group("post")).lower()
    number = match.group("num") or match.group("num2")
    tr, one, many = CURRENCIES[symbol]
    name = tr if language == "tr" else (one if _is_one(number) else many)
    words = f"{number_words(number, language)} {name}"
    return _with_suffix(words, match.group("suffix"), language)


def _unit(match: re.Match, language: Language) -> str:
    unit = match.group("unit").lower()
    number = match.group("num")
    tr, one, many = UNITS[unit]
    name = tr if language == "tr" else (one if _is_one(number) else many)
    words = f"{number_words(number, language)} {name}"
    return _with_suffix(words, match.group("suffix"), language)


def _ordinal_dot(match: re.Match, language: Language) -> str:
    return ordinal_words(int(match.group("n")), language)


def _ordinal_suffix_tr(match: re.Match) -> str:
    words = ordinal_words(int(match.group("n")), "tr")
    return attach_suffix_tr(words, match.group("rest")) if match.group("rest") else words


def _ordinal_en(match: re.Match) -> str:
    return ordinal_words(int(match.group("n")), "en")


def _number(match: re.Match, language: Language) -> str:
    token = ("-" if match.group("neg") else "") + match.group("num")
    return _with_suffix(number_words(token, language), match.group("suffix"), language)


def _sub(pattern: re.Pattern, text: str, repl: Callable[[re.Match], str]) -> str:
    return pattern.sub(repl, text)


def normalize_for_speech(text: str, language: Language = "tr") -> str:
    """The spoken form of `text` for `language` ("tr" or "en"; anything else is treated as "en")."""
    if not text:
        return text
    language = "tr" if language == "tr" else "en"
    text = _sub(DATE_RE, text, lambda m: _date(m, language))
    text = RANGE_RE.sub(" ila " if language == "tr" else " to ", text)  # before times: "19:00-20:00"
    text = TIME_RE.sub(lambda m: _time(m, text, language), text)
    text = _sub(PERCENT_RE, text, lambda m: _percent(m, language))
    text = _sub(CURRENCY_RE, text, lambda m: _currency(m, language))
    text = _sub(UNIT_RE, text, lambda m: _unit(m, language))
    if language == "tr":
        text = ORDINAL_SUFFIX_TR_RE.sub(_ordinal_suffix_tr, text)
        text = _sub(ORDINAL_DOT_RE, text, lambda m: _ordinal_dot(m, language))
    else:
        text = ORDINAL_EN_RE.sub(_ordinal_en, text)
    text = PLUS_RE.sub("artı " if language == "tr" else "plus ", text)
    text = _sub(NUMBER_RE, text, lambda m: _number(m, language))
    text = STRAY_PERCENT_RE.sub(" yüzde " if language == "tr" else " percent ", text)
    text = ARROW_RE.sub(", ", text)
    text = AMP_RE.sub(" ve " if language == "tr" else " and ", text)
    text = SLASH_RE.sub(" ", text)
    text = SYMBOLS_RE.sub(" ", text)
    text = text.replace("+", " ")
    text = SPACE_PUNCT_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
