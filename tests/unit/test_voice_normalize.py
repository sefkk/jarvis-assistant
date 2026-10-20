"""Spoken-form normalization (voice/normalize.py): the table of inputs the TTS engines must never see as
digits, the Turkish suffix rules, and `jarvis voice say --dry-run`."""

from __future__ import annotations

import pytest

from jarvis.main import build_parser, parse_args
from jarvis.voice.normalize import attach_suffix_tr, int_to_words_tr, normalize_for_speech, number_words, ordinal_words
from jarvis.voice.say import say_cli

TURKISH = [
    # times
    ("Maç saat 19.00'da başlıyor.", "Maç saat on dokuzda başlıyor."),
    ("Maç 19:00'da başlıyor.", "Maç saat on dokuzda başlıyor."),
    ("19.00", "saat on dokuz"),
    ("08:30", "sekiz otuz"),
    ("Uçuş 08:30'da.", "Uçuş sekiz otuzda."),
    ("Saat 19:30.", "Saat on dokuz otuz."),
    ("09:05", "dokuz sıfır beş"),
    ("19.00'de görüşürüz.", "saat on dokuzda görüşürüz."),  # a wrong written suffix is fixed by harmony
    ("19:00-20:00 arası", "saat on dokuz ila saat yirmi arası"),
    # percentages
    ("%75", "yüzde yetmiş beş"),
    ("75%", "yüzde yetmiş beş"),
    ("%75'e çıktı.", "yüzde yetmiş beşe çıktı."),
    ("%12,5 arttı.", "yüzde on iki virgül beş arttı."),
    # decimals, thousands, plain numbers
    ("1,14", "bir virgül on dört"),
    ("1.14", "bir nokta on dört"),  # the safe reading: a version or an English-style decimal, not a time
    ("0,05", "sıfır virgül sıfır beş"),
    ("1.500", "bin beş yüz"),
    ("1.500.000", "bir milyon beş yüz bin"),
    ("2026", "iki bin yirmi altı"),
    ("05321234567", "sıfır beş üç iki bir iki üç dört beş altı yedi"),
    ("-3", "eksi üç"),
    # dates
    ("17.10.2026", "on yedi Ekim iki bin yirmi altı"),
    ("17/10/2026'da görüşürüz.", "on yedi Ekim iki bin yirmi altıda görüşürüz."),
    ("2026-01-05", "beş Ocak iki bin yirmi altı"),
    # currency
    ("€50", "elli euro"),
    ("50 €", "elli euro"),
    ("$20", "yirmi dolar"),
    ("1.500 TL", "bin beş yüz lira"),
    ("20₺", "yirmi lira"),
    ("Fiyat 1,14 TL.", "Fiyat bir virgül on dört lira."),
    # units
    ("21°C", "yirmi bir derece"),
    ("-5°C", "eksi beş derece"),
    ("5 km", "beş kilometre"),
    ("3,5 kg", "üç virgül beş kilogram"),
    ("70 kg verdin", "yetmiş kilogram verdin"),
    ("10 dk", "on dakika"),
    ("30 sn", "otuz saniye"),
    ("2 sa", "iki saat"),
    ("500 m", "beş yüz metre"),
    ("1,5 lt", "bir virgül beş litre"),
    ("5 km'ye", "beş kilometreye"),
    # ordinals
    ("1. madde", "birinci madde"),
    ("3'üncü kat", "üçüncü kat"),
    ("5'inci oldu.", "beşinci oldu."),
    ("3'üncüsü", "üçüncüsü"),
    # suffixes after numbers
    ("3'te", "üçte"),
    ("4'ü", "dördü"),
    ("6'ya", "altıya"),
    ("2026'da", "iki bin yirmi altıda"),
    ("100'e", "yüze"),
    ("40'tan", "kırktan"),
    ("1990'lar", "bin dokuz yüz doksanlar"),
    ("5'lik", "beşlik"),
    # symbols
    ("#toplantı * ve/veya | a_b", "toplantı ve veya a b"),
    ("Sıcaklık → 21°", "Sıcaklık, yirmi bir derece"),
    ("A & B", "A ve B"),
    # mixed sentences
    (
        "Yarın 15:00'te diş randevun var, hava 21°C ve %40 yağış ihtimali.",
        "Yarın saat on beşte diş randevun var, hava yirmi bir derece ve yüzde kırk yağış ihtimali.",
    ),
    (
        "Toplam 1.500 €, yani 1.500.000 lira değil; 3. maddeye bak.",
        "Toplam bin beş yüz euro, yani bir milyon beş yüz bin lira değil; üçüncü maddeye bak.",
    ),
    ("Dr. Ahmet 17.10.2026 günü 08:30'da 5 km koştu.", "Dr. Ahmet on yedi Ekim iki bin yirmi altı günü sekiz otuzda beş kilometre koştu."),
    # untouched
    ("Merhaba, nasılsın?", "Merhaba, nasılsın?"),
    ("Ahmet'in evi yakın.", "Ahmet'in evi yakın."),
]

ENGLISH = [
    # times
    ("19:00", "seven PM"),
    ("08:30", "eight thirty AM"),
    ("The meeting is at 14:30.", "The meeting is at two thirty PM."),
    ("12:00", "twelve PM"),
    ("00:15", "twelve fifteen AM"),
    ("09:05", "nine oh five AM"),
    # percentages, decimals, numbers
    ("75%", "seventy-five percent"),
    ("Battery is at 75% now.", "Battery is at seventy-five percent now."),
    ("1.14", "one point one four"),
    ("3.5 kg", "three point five kilograms"),
    ("1,500", "one thousand, five hundred"),
    ("-3", "minus three"),
    ("5-10 km", "five to ten kilometers"),
    # currency
    ("$19.99", "nineteen point nine nine dollars"),
    ("€20", "twenty euros"),
    ("$1", "one dollar"),
    ("£5", "five pounds"),
    # dates
    ("2026-10-17", "October seventeenth twenty twenty-six"),
    ("10/17/2026", "October seventeenth twenty twenty-six"),
    ("17/10/2026", "October seventeenth twenty twenty-six"),
    # units
    ("5 km", "five kilometers"),
    ("1 km", "one kilometer"),
    ("25 min", "twenty-five minutes"),
    ("21°C", "twenty-one degrees"),
    ("10 km/h", "ten kilometers per hour"),
    ("2 h", "two hours"),
    # ordinals
    ("1st", "first"),
    ("22nd floor", "twenty-second floor"),
    ("3rd", "third"),
    ("10th", "tenth"),
    # suffixes: nothing to guess at in English, the apostrophe part is dropped
    ("The 90's", "The ninety"),
    # symbols and mixed sentences
    ("Version 1.14 costs $5 & more", "Version one point one four costs five dollars and more"),
    (
        "Your meeting is at 14:30 on 10/17/2026, battery at 75%, temperature 21°C.",
        "Your meeting is at two thirty PM on October seventeenth twenty twenty-six, battery at seventy-five percent,"
        " temperature twenty-one degrees.",
    ),
    ("Hello there, how are you?", "Hello there, how are you?"),
]


@pytest.mark.parametrize("text,expected", TURKISH, ids=[t for t, _ in TURKISH])
def test_turkish(text, expected):
    assert normalize_for_speech(text, "tr") == expected


@pytest.mark.parametrize("text,expected", ENGLISH, ids=[t for t, _ in ENGLISH])
def test_english(text, expected):
    assert normalize_for_speech(text, "en") == expected


@pytest.mark.parametrize("text,language", [(t, "tr") for t, _ in TURKISH] + [(t, "en") for t, _ in ENGLISH])
def test_idempotent_and_digit_free(text, language):
    """Normalizing twice changes nothing (the ElevenLabs engine and its `say` fallback both run it), and no digit
    survives, so the engines only ever see words."""
    once = normalize_for_speech(text, language)
    assert normalize_for_speech(once, language) == once
    assert not any(ch.isdigit() for ch in once)


def test_unknown_language_is_english_and_empty_passes():
    assert normalize_for_speech("75%", "de") == "seventy-five percent"
    assert normalize_for_speech("", "tr") == ""


# --- building blocks ----------------------------------------------------------------------------------


def test_turkish_number_words_have_spaces_unlike_num2words():
    assert int_to_words_tr(0) == "sıfır"
    assert int_to_words_tr(75) == "yetmiş beş"
    assert int_to_words_tr(100) == "yüz"
    assert int_to_words_tr(1000) == "bin"
    assert int_to_words_tr(2026) == "iki bin yirmi altı"
    assert int_to_words_tr(1_500_000) == "bir milyon beş yüz bin"
    assert int_to_words_tr(2_000_000_000) == "iki milyar"
    assert int_to_words_tr(-14) == "eksi on dört"


def test_ordinals():
    assert ordinal_words(1, "tr") == "birinci" and ordinal_words(4, "tr") == "dördüncü"
    assert ordinal_words(20, "tr") == "yirminci" and ordinal_words(100, "tr") == "yüzüncü"
    assert ordinal_words(21, "tr") == "yirmi birinci"
    assert ordinal_words(1, "en") == "first" and ordinal_words(22, "en") == "twenty-second"


def test_number_words_follow_the_language_conventions():
    assert number_words("1.500", "tr") == "bin beş yüz" and number_words("1.500", "en") == "one point five zero zero"
    assert number_words("1,500", "en") == "one thousand, five hundred" and number_words("1,5", "en") == "one point five"
    assert number_words("1,14", "tr") == "bir virgül on dört" and number_words("1.14", "tr") == "bir nokta on dört"
    assert number_words("0,05", "tr") == "sıfır virgül sıfır beş"
    assert number_words("007", "tr") == "sıfır sıfır yedi"


@pytest.mark.parametrize(
    "words,suffix,expected",
    [
        ("on dokuz", "da", "on dokuzda"),
        ("saat on dokuz", "de", "saat on dokuzda"),  # harmony is recomputed, the written vowel is not trusted
        ("üç", "de", "üçte"),  # d -> t after a hard consonant
        ("dört", "ü", "dördü"),  # softening
        ("altı", "a", "altıya"),  # the buffer letter is added when missing
        ("dokuz", "ya", "dokuza"),  # and removed when it does not belong
        ("yüz", "e", "yüze"),
        ("iki", "yi", "ikiyi"),
        ("beş", "inci", "beşinci"),
        ("üç", "teki", "üçteki"),  # -ki never harmonises
        ("kırk", "tan", "kırktan"),
        ("yetmiş beş", "e", "yetmiş beşe"),
        ("on dokuz", "x9", "on dokuz"),  # not a suffix: dropped rather than spoken as garbage
        ("on dokuz", "abcdefghijk", "on dokuz"),  # too long to be one
        ("", "da", ""),
    ],
)
def test_attach_suffix_tr(words, suffix, expected):
    assert attach_suffix_tr(words, suffix) == expected


# --- jarvis voice say ----------------------------------------------------------------------------------


class _Engine:
    def __init__(self):
        self.spoken: list[tuple[str, str]] = []
        self.started = self.closed = self.turns = 0

    def start(self):
        self.started += 1

    def begin_turn(self):
        self.turns += 1

    def speak(self, text, language=None, **kw):
        self.spoken.append((language, text))

    def wait(self, timeout=None):
        return True

    def close(self):
        self.closed += 1


def test_voice_say_dry_run_prints_the_normalized_sentences(config, paths):
    lines: list[str] = []
    rc = say_cli(
        config,
        paths,
        "Maç 19.00'da başlıyor, batarya %75. Meeting is at 14:30.",
        dry_run=True,
        out=lines.append,
        build=lambda *a, **kw: pytest.fail("dry run must not build an engine"),
    )
    assert rc == 0
    assert lines == ["[tr] Maç saat on dokuzda başlıyor, batarya yüzde yetmiş beş.", "[en] Meeting is at two thirty PM."]


def test_voice_say_speaks_through_the_built_engine(config, paths):
    engine = _Engine()
    lines: list[str] = []
    built = []

    def build(voice_config, p, *, language, on_first_audio=None, on_sentence=None):
        built.append((voice_config.tts_engine, p, language))
        return engine

    rc = say_cli(config, paths, "Saat 19:00'da gel.", language="tr", out=lines.append, build=build)
    assert rc == 0
    assert built == [(config.voice.tts_engine, paths, config.general.language)]
    assert engine.spoken == [("tr", "Saat on dokuzda gel.")]
    assert engine.started == 1 and engine.turns == 1 and engine.closed == 1
    assert lines == ["[tr] Saat on dokuzda gel."]


def test_voice_say_without_text_prints_usage(config, paths):
    lines: list[str] = []
    assert say_cli(config, paths, "   ", dry_run=True, out=lines.append) == 2
    assert lines and lines[0].startswith("kullanım: jarvis voice say")  # the test config is Turkish


def test_voice_say_parser_takes_the_text_before_or_after_the_options():
    args = parse_args(build_parser(), ["voice", "say", "Saat 19.00", "--dry-run", "--language", "tr"])
    assert args.voice_command == "say" and args.text == ["Saat 19.00"] and args.dry_run and args.language == "tr"
    args = parse_args(build_parser(), ["voice", "say", "--dry-run", "Saat 19.00", "-5°C"])
    assert args.text == ["Saat 19.00", "-5°C"] and args.dry_run
    assert parse_args(build_parser(), ["voice"]).voice_command == "status"
    with pytest.raises(SystemExit):  # leftovers are only text for `voice say`
        parse_args(build_parser(), ["voice", "status", "--dry-run", "extra"])
