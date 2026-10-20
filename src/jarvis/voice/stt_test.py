"""`jarvis voice test-stt`: record 15 sentences once, then score every STT option on the same recordings.

The sentences are in the configured speech language (`voice.stt_language`; Turkish or English) with song
and artist names, places, people, numbers and a few names from the other language; `{...}` marks the names
the name-accuracy metric looks for. A session lives under data/stt_test/<stamp>/ (WAVs, manifest.json with
the language, results.json) so the comparison can be run again later with `--compare DIR`.

Scoring: both sides are normalized (lower-casing, punctuation and apostrophe suffixes split off, number
words of the session's language turned into digits), word accuracy is 1 - WER (Levenshtein over words),
name accuracy is the share of marked names that appear in the hypothesis as a contiguous word sequence.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from jarvis.config import Config, Paths
from jarvis.i18n import current_language, t
from jarvis.voice import audio as vaudio
from jarvis.voice.audio import SAMPLE_RATE, Microphone, read_wav_int16, write_wav_int16
from jarvis.voice.stt_engines import AppleSpeech, ElevenLabsStt, EngineSpec, SttResult, WhisperMlx, locale_for
from jarvis.voice.vad import EndOfSpeechDetector, SileroVad, record_until_silence

log = logging.getLogger("jarvis.voice.stt_test")

MIC_READ_TIMEOUT_S = 0.5
TEST_SILENCE_MS = 1000  # a little more patience than the service: the user reads from the screen
LARGE_V3_MODEL = "mlx-community/whisper-large-v3-mlx"
TURKISH_MODEL = "denizyldz24/whisper-large-v3-turkish-general-mlx"  # turkmedstt/whisper-large-v3-turkish-general, MLX weights
HYBRID_THRESHOLD = 0.6

# Names inside {braces} count for the name metric; numbers are written as digits (the normalizer turns
# number words into digits on both sides).
SENTENCES_TR: list[str] = [
    "Spotify'da {Sezen Aksu}'dan {Gülümse} çal.",
    "{Kadıköy}'den {Taksim}'e metro ile ne kadar sürer?",
    "Yarın saat 15:30'da {Ahmet} ile {Beşiktaş}'ta toplantım var.",
    "{Fenerbahçe} bu hafta {Galatasaray} ile kaçta oynuyor?",
    "Bana 25 dakikalık bir zamanlayıcı kur.",
    "{Tarkan}'dan {Kuzu Kuzu} şarkısını aç.",
    "Hava durumu {İzmir}'de yarın nasıl olacak?",
    "{Coldplay}'den {Yellow} çal.",
    "Sabah 7:45'e alarm kur, cumartesi 9'a.",
    "{Mehmet Yılmaz}'a e-posta taslağı hazırla, konu bütçe raporu.",
    "{Daft Punk}'tan {Get Lucky} şarkısını Spotify'da aç.",
    "{Bağdat Caddesi}'nde açık bir eczane var mı?",
    "Antrenman tablosuna 80 kilo 5 tekrar squat ekle.",
    "{Adele}'den {Hello} çal ve sesi 3 kademe kıs.",
    "{Elif}'in doğum günü 12 Mart, takvime ekle.",
]
SENTENCES_EN: list[str] = [
    "Play {Kuzu Kuzu} by {Tarkan} on Spotify.",
    "How long does it take from {Camden Town} to {King's Cross} by tube?",
    "I have a meeting with {Sarah} at {Canary Wharf} tomorrow at 3:30.",
    "When does {Arsenal} play {Tottenham} this week?",
    "Set a timer for 25 minutes.",
    "Play {Bohemian Rhapsody} by {Queen}.",
    "What will the weather be like in {Edinburgh} tomorrow?",
    "Play {Get Lucky} by {Daft Punk}.",
    "Set an alarm for 7:45 in the morning and 9 on Saturday.",
    "Draft an email to {Michael Thompson} about the budget report.",
    "Open {Shape of You} by {Ed Sheeran} on Spotify.",
    "Is there a pharmacy open on {Oxford Street}?",
    "Add squats, 80 kilos, 5 reps to the training log.",
    "Play {Hello} by {Adele} and turn the volume down 3 steps.",
    "{Emma}'s birthday is on March 12, add it to the calendar.",
]
SENTENCES_BY_LANGUAGE = {"tr": SENTENCES_TR, "en": SENTENCES_EN}
SENTENCES = SENTENCES_TR  # the Turkish set (older sessions and tests)


def session_language(config: Config) -> str:
    """The language the test runs in: `voice.stt_language`, or the main language when that is "auto"."""
    language = str(config.voice.stt_language).lower()
    return language if language in SENTENCES_BY_LANGUAGE else (current_language() if current_language() in SENTENCES_BY_LANGUAGE else "tr")


def sentences_for(language: str) -> list[str]:
    return list(SENTENCES_BY_LANGUAGE.get(language, SENTENCES_TR))


NAME_RE = re.compile(r"\{([^}]+)\}")

_UNITS = {"sıfır": 0, "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9}
_TENS = {"on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50, "altmış": 60, "yetmiş": 70, "seksen": 80, "doksan": 90}
_HUNDREDS = {"yüz": 100}
_UNITS_EN = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS_EN = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_HUNDREDS_EN = {"hundred": 100}


# --- text normalization and scoring ----------------------------------------------------------------------------------


def plain(sentence: str) -> str:
    """The sentence without the {name} markers."""
    return NAME_RE.sub(r"\1", sentence)


def names_in(sentence: str) -> list[str]:
    return NAME_RE.findall(sentence)


def turkish_lower(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("I", "ı").replace("İ", "i").lower()


def tokens(text: str, language: str = "tr") -> list[str]:
    """Words for scoring: lower-cased, punctuation dropped, apostrophe suffixes split, number words as digits."""
    text = turkish_lower(text) if language == "tr" else unicodedata.normalize("NFC", text).lower()
    text = text.replace("’", "'").replace("'", " ").replace(":", " ").replace(".", " ")
    words = [w for w in re.split(r"[^\w]+", text) if w]
    if language == "en":
        return _numbers_to_digits(words, _UNITS_EN, _TENS_EN, _HUNDREDS_EN, strip_suffixes=False)
    return _numbers_to_digits(words)


_NUMBER_SUFFIXES = (
    "dan",
    "den",
    "tan",
    "ten",
    "lık",
    "lik",
    "luk",
    "lük",
    "da",
    "de",
    "ta",
    "te",
    "ya",
    "ye",
    "a",
    "e",
    "ı",
    "i",
    "u",
    "ü",
)
_NUMBER_WORDS = {**_UNITS, **_TENS, **_HUNDREDS}


def _strip_number_suffix(word: str) -> str:
    """'otuzda' -> 'otuz', 'dokuza' -> 'dokuz', 'beşe' -> 'beş': a case suffix on a number word."""
    if word in _NUMBER_WORDS:
        return word
    for suffix in _NUMBER_SUFFIXES:
        stem = word[: -len(suffix)] if word.endswith(suffix) else None
        if stem and stem in _NUMBER_WORDS:
            return stem
    return word


def _numbers_to_digits(
    words: list[str],
    units: dict[str, int] = _UNITS,
    tens: dict[str, int] = _TENS,
    hundreds: dict[str, int] = _HUNDREDS,
    *,
    strip_suffixes: bool = True,
) -> list[str]:
    if strip_suffixes:
        words = [_strip_number_suffix(w) for w in words]
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        value: int | None = None
        consumed = 1
        if w in hundreds:
            value = 100
            if i + 1 < len(words) and words[i + 1] in tens:
                value += tens[words[i + 1]]
                consumed = 2
                if i + 2 < len(words) and words[i + 2] in units:
                    value += units[words[i + 2]]
                    consumed = 3
            elif i + 1 < len(words) and words[i + 1] in units:
                value += units[words[i + 1]]
                consumed = 2
        elif w in tens:
            value = tens[w]
            if i + 1 < len(words) and words[i + 1] in units:
                value += units[words[i + 1]]
                consumed = 2
        elif w in units:
            value = units[w]
        if value is not None:
            out.append(str(value))
        else:
            out.append(w)
        i += consumed
    return out


def levenshtein(a: list[str], b: list[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return True
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


@dataclass
class SentenceScore:
    index: int
    reference: str
    hypothesis: str
    words: int
    errors: int
    names: int
    names_found: int
    ms: float
    confidence: float | None
    cost_usd: float = 0.0
    routed_to: str | None = None

    @property
    def word_accuracy(self) -> float:
        return max(0.0, 1.0 - self.errors / self.words) if self.words else 1.0


def score_sentence(index: int, sentence: str, result: SttResult, language: str = "tr") -> SentenceScore:
    ref = tokens(plain(sentence), language)
    hyp = tokens(result.text, language)
    names = names_in(sentence)
    found = sum(1 for name in names if contains_sequence(hyp, tokens(name, language)))
    return SentenceScore(
        index,
        plain(sentence),
        result.text,
        len(ref),
        levenshtein(ref, hyp),
        len(names),
        found,
        result.ms,
        result.confidence,
        result.cost_usd,
        result.routed_to,
    )


@dataclass
class EngineReport:
    key: str
    label: str
    local: bool
    paid: bool
    usd_per_minute: float
    sentences: list[SentenceScore] = field(default_factory=list)
    error: str | None = None
    load_ms: float | None = None

    @property
    def word_accuracy(self) -> float:
        words = sum(s.words for s in self.sentences)
        errors = sum(s.errors for s in self.sentences)
        return max(0.0, 1.0 - errors / words) if words else 0.0

    @property
    def name_accuracy(self) -> float:
        names = sum(s.names for s in self.sentences)
        return sum(s.names_found for s in self.sentences) / names if names else 0.0

    @property
    def avg_ms(self) -> float:
        return sum(s.ms for s in self.sentences) / len(self.sentences) if self.sentences else 0.0

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.sentences)


# --- the recording session --------------------------------------------------------------------------------------------


@dataclass
class Session:
    directory: Path
    sentences: list[str]
    files: list[Path]
    language: str = "tr"

    @property
    def manifest(self) -> Path:
        return self.directory / "manifest.json"

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "sample_rate": SAMPLE_RATE,
                    "language": self.language,
                    "items": [{"index": i + 1, "sentence": s, "file": f.name} for i, (s, f) in enumerate(zip(self.sentences, self.files))],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, directory: Path) -> "Session":
        data = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
        items = data["items"]
        return cls(
            Path(directory),
            [it["sentence"] for it in items],
            [Path(directory) / it["file"] for it in items],
            str(data.get("language") or "tr"),
        )

    def audio(self) -> Iterable[Any]:
        for path in self.files:
            yield read_wav_int16(path)

    def total_seconds(self) -> float:
        return sum(len(a) for a in self.audio()) / SAMPLE_RATE


def record_session(
    config: Config,
    paths: Paths,
    *,
    out: Callable[[str], None] = print,
    wait: Callable[[str], str] = input,
    mic: Any = None,
    vad: SileroVad | None = None,
    play_sound: Callable[[str], Any] = vaudio.play_sound,
    clock: Callable[[], float] = time.monotonic,
    sentences: list[str] | None = None,
    directory: Path | None = None,
) -> Session:
    """Show each sentence, wait for Enter, record it with the service's VAD, save it."""
    v = config.voice
    language = session_language(config)
    sentences = list(sentences or sentences_for(language))
    directory = directory or paths.stt_test_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    vad = vad or SileroVad()
    vad.load()
    mic = mic or Microphone()
    mic.start()
    files: list[Path] = []
    try:
        out(t("voice.stt_test.intro", count=len(sentences), silence=TEST_SILENCE_MS))
        for i, sentence in enumerate(sentences, 1):
            while True:
                out("")
                out(f"[{i}/{len(sentences)}]  {plain(sentence)}")
                wait(t("voice.stt_test.enter"))
                mic.flush()
                vad.reset()
                detector = EndOfSpeechDetector(
                    vad,
                    threshold=v.vad_threshold,
                    silence_ms=TEST_SILENCE_MS,
                    max_record_s=v.max_record_s,
                    no_speech_timeout_s=v.no_speech_timeout_s,
                )
                play_sound(v.listen_sound)

                def frames():
                    while True:
                        yield mic.read(timeout=MIC_READ_TIMEOUT_S)

                recording = record_until_silence(frames(), detector, idle_timeout_s=float(v.no_speech_timeout_s), clock=clock)
                if not recording.speech_started or recording.reason == "no_speech":
                    out(t("voice.stt_test.nothing_heard"))
                    continue
                path = directory / f"{i:02d}.wav"
                write_wav_int16(path, recording.audio)
                files.append(path)
                out(t("voice.stt_test.recorded", seconds=f"{recording.duration_ms / 1000:.1f}"))
                break
    finally:
        mic.stop()
    session = Session(directory, sentences, files, language)
    session.save()
    out("")
    out(t("voice.stt_test.session_saved", path=directory))
    return session


# --- the engines under test -------------------------------------------------------------------------------------------


def hint_terms(sentences: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for sentence in sentences:
        for name in names_in(sentence):
            seen.setdefault(name, None)
    return list(seen)


def engine_specs(
    config: Config, paths: Paths, *, hints: list[str], cloud: bool, api_key: str | None, transport: Any = None, language: str = "tr"
) -> list[EngineSpec]:
    """Every option of the comparison in `language`, in table order. The Turkish fine-tuned model only for
    Turkish; cloud ones only with `cloud` and a key."""
    v = config.voice
    lang = language
    current = dict(v.stt_model_by_language).get(lang, v.stt_model)
    locale = locale_for(lang)
    prompt = ", ".join(hints)
    specs = [
        EngineSpec(
            "original",
            f"original ({current.split('/')[-1]}, auto language)",
            lambda: WhisperMlx(current, language=None, name="original"),
            True,
            False,
            requires_model=current,
        ),
        EngineSpec(
            "configured",
            f"configured ({v.stt_engine}, {v.stt_language}, vocabulary + corrections)",
            lambda: ConfiguredStt(config, paths),
            v.stt_engine in ("apple", "whisper"),
            v.stt_engine in ("elevenlabs", "hybrid"),
            requires_model=current,
        ),
        EngineSpec(
            "current",
            f"current ({current.split('/')[-1]}, {lang})",
            lambda: WhisperMlx(current, language=lang, name="current"),
            True,
            False,
            requires_model=current,
        ),
        EngineSpec(
            "current+hints",
            "current + vocabulary hints",
            lambda: WhisperMlx(current, language=lang, initial_prompt=prompt, name="current+hints"),
            True,
            False,
            requires_model=current,
        ),
        EngineSpec(
            "large-v3",
            "whisper-large-v3 (local)",
            lambda: WhisperMlx(LARGE_V3_MODEL, language=lang, name="large-v3"),
            True,
            False,
            requires_model=LARGE_V3_MODEL,
        ),
        EngineSpec(
            "large-v3+hints",
            "whisper-large-v3 + hints",
            lambda: WhisperMlx(LARGE_V3_MODEL, language=lang, initial_prompt=prompt, name="large-v3+hints"),
            True,
            False,
            requires_model=LARGE_V3_MODEL,
        ),
    ]
    if lang == "tr":
        specs += [
            EngineSpec(
                "turkish",
                "Turkish fine-tuned large-v3",
                lambda: WhisperMlx(TURKISH_MODEL, language=lang, name="turkish"),
                True,
                False,
                requires_model=TURKISH_MODEL,
            ),
            EngineSpec(
                "turkish+hints",
                "Turkish fine-tuned + hints",
                lambda: WhisperMlx(TURKISH_MODEL, language=lang, initial_prompt=prompt, name="turkish+hints"),
                True,
                False,
                requires_model=TURKISH_MODEL,
            ),
        ]
    specs += [
        EngineSpec("apple", "Apple Speech (on-device)", lambda: AppleSpeech(paths.bin_dir, locale=locale, name="apple"), True, False),
        EngineSpec(
            "apple+hints",
            "Apple Speech + hints",
            lambda: AppleSpeech(paths.bin_dir, locale=locale, hints=hints, name="apple+hints"),
            True,
            False,
        ),
    ]
    if cloud and api_key:
        plain_cloud = ElevenLabsStt(api_key, language=lang, name="elevenlabs", transport=transport)
        hinted_cloud = ElevenLabsStt(api_key, language=lang, keyterms=hints, name="elevenlabs+hints", transport=transport)
        specs.append(EngineSpec("elevenlabs", "ElevenLabs Scribe v2", lambda: plain_cloud, False, True, plain_cloud.usd_per_minute))
        specs.append(
            EngineSpec(
                "elevenlabs+hints", "ElevenLabs Scribe v2 + keyterms", lambda: hinted_cloud, False, True, hinted_cloud.usd_per_minute
            )
        )
    return specs


class ConfiguredStt:
    """The pipeline the voice service runs (`SpeechToText.from_config`: the configured engine, the
    vocabulary from data/, the learned corrections, Whisper as the fallback), as a comparison option."""

    def __init__(self, config: Config, paths: Paths) -> None:
        from jarvis.io.voice import SpeechToText  # noqa: PLC0415

        self.stt = SpeechToText.from_config(config.voice, paths, config=config)
        self.name = "configured"

    def warm_up(self) -> float:
        return self.stt.warm_up()

    def transcribe_array(self, audio: Any) -> SttResult:
        t0 = time.perf_counter()
        text, language = self.stt.transcribe_array(audio)
        ms = (time.perf_counter() - t0) * 1000.0
        return SttResult(text, language, self.stt.last_confidence, ms, self.name, routed_to=self.stt.last_engine)


def model_available(model: str) -> bool:
    from jarvis.io.voice import SpeechToText  # noqa: PLC0415

    return Path(model).expanduser().is_dir() or SpeechToText.model_cached(model)


def download_model(model: str, out: Callable[[str], None]) -> None:
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    out(t("voice.stt_test.downloading", model=model))
    t0 = time.perf_counter()
    snapshot_download(model)
    out(t("voice.stt_test.downloaded", seconds=f"{time.perf_counter() - t0:.0f}"))


# --- the comparison ---------------------------------------------------------------------------------------------------


def run_engine(spec: EngineSpec, session: Session, out: Callable[[str], None]) -> EngineReport:
    report = EngineReport(spec.key, spec.label, spec.local, spec.paid, spec.usd_per_minute)
    try:
        engine = spec.build()
        if hasattr(engine, "warm_up"):
            report.load_ms = float(engine.warm_up())
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        log.warning("STT option %s failed to load: %s", spec.key, exc)
        return report
    failures = 0
    for i, (sentence, audio) in enumerate(zip(session.sentences, session.audio()), 1):
        try:
            result = engine.transcribe_array(audio)
        except Exception as exc:  # one bad sentence (no speech, a timeout) must not drop the whole option
            failures += 1
            log.warning("STT option %s failed on sentence %d: %s", spec.key, i, exc)
            out(t("voice.stt_test.sentence_failed", index=i, error=f"{type(exc).__name__}: {exc}"))
            result = SttResult("", None, None, 0.0, spec.key)
        report.sentences.append(score_sentence(i, sentence, result, session.language))
    if failures == len(report.sentences):
        report.error = "failed on every sentence (see the log)"
    return report


def simulate_hybrid(local: EngineReport, cloud: EngineReport, *, threshold: float = HYBRID_THRESHOLD) -> EngineReport:
    """The hybrid option from two finished runs: per sentence, the local result unless its confidence is
    below the threshold, then the cloud result (and its cost)."""
    report = EngineReport("hybrid", f"hybrid: {local.key}, cloud below {threshold:.2f}", False, True, cloud.usd_per_minute)
    for a, b in zip(local.sentences, cloud.sentences):
        if a.confidence is not None and a.confidence >= threshold and a.hypothesis:
            report.sentences.append(
                SentenceScore(
                    a.index, a.reference, a.hypothesis, a.words, a.errors, a.names, a.names_found, a.ms, a.confidence, 0.0, local.key
                )
            )
        else:
            report.sentences.append(
                SentenceScore(
                    b.index,
                    b.reference,
                    b.hypothesis,
                    b.words,
                    b.errors,
                    b.names,
                    b.names_found,
                    a.ms + b.ms,
                    b.confidence,
                    b.cost_usd,
                    cloud.key,
                )
            )
    return report


def compare(
    config: Config,
    paths: Paths,
    session: Session,
    *,
    out: Callable[[str], None] = print,
    engines: list[str] | None = None,
    cloud: bool = False,
    download: bool = False,
    verbose: bool = False,
    api_key: str | None = None,
    specs: list[EngineSpec] | None = None,
    hybrid_threshold: float = HYBRID_THRESHOLD,
) -> list[EngineReport]:
    hints = hint_terms(session.sentences)
    if specs is None:
        if cloud and api_key is None:
            from jarvis.voice.elevenlabs import get_api_key  # noqa: PLC0415

            api_key = get_api_key()
            if api_key is None:
                out(t("voice.stt_test.no_key"))
        specs = engine_specs(config, paths, hints=hints, cloud=cloud, api_key=api_key, language=session.language)
    wanted = set(engines) if engines else None
    reports: list[EngineReport] = []
    for spec in specs:
        if wanted is not None and spec.key not in wanted:
            continue
        if spec.requires_model and not model_available(spec.requires_model):
            if download:
                try:
                    download_model(spec.requires_model, out)
                except Exception as exc:
                    reports.append(
                        EngineReport(spec.key, spec.label, spec.local, spec.paid, spec.usd_per_minute, error=f"download failed: {exc}")
                    )
                    continue
            else:
                reports.append(
                    EngineReport(
                        spec.key,
                        spec.label,
                        spec.local,
                        spec.paid,
                        spec.usd_per_minute,
                        error=f"model not downloaded ({spec.requires_model}); run again with --download",
                    )
                )
                out(t("voice.stt_test.skipped", label=spec.label, reason=reports[-1].error))
                continue
        out(t("voice.stt_test.running", label=spec.label))
        report = run_engine(spec, session, out)
        if report.error:
            out(t("voice.stt_test.failed", error=report.error))
        else:
            out(
                t(
                    "voice.stt_test.scores",
                    words=f"{report.word_accuracy * 100:.0f}",
                    names=f"{report.name_accuracy * 100:.0f}",
                    ms=f"{report.avg_ms:.0f}",
                )
            )
        reports.append(report)
    by_key = {r.key: r for r in reports if not r.error and r.sentences}
    accuracy = lambda r: (r.name_accuracy, r.word_accuracy)  # noqa: E731
    cloud_candidates = [r for r in by_key.values() if not r.local]
    local_candidates = [r for r in by_key.values() if r.local]
    if cloud_candidates and local_candidates and (wanted is None or "hybrid" in wanted):
        reports.append(
            simulate_hybrid(max(local_candidates, key=accuracy), max(cloud_candidates, key=accuracy), threshold=hybrid_threshold)
        )
    out("")
    for line in format_table(reports, session.total_seconds()):
        out(line)
    if verbose:
        out("")
        for line in format_details(reports):
            out(line)
    save_results(session, reports)
    out("")
    out(t("voice.stt_test.results_saved", path=session.directory / "results.json"))
    return reports


def format_table(reports: list[EngineReport], total_seconds: float) -> list[str]:
    header = f"{'option':<34} {'words':>6} {'names':>6} {'latency':>9} {'$/min':>8}  {'cost':<5} {'where':<5}"
    lines = [header, "-" * len(header)]
    for r in reports:
        if r.error:
            lines.append(
                f"{r.label:<34} {'-':>6} {'-':>6} {'-':>9} {'-':>8}  {'paid' if r.paid else 'free':<5} {'cloud' if not r.local else 'local':<5}  {r.error}"
            )
            continue
        price = f"{r.usd_per_minute:.4f}" if r.usd_per_minute else "0"
        lines.append(
            f"{r.label:<34} {r.word_accuracy * 100:>5.0f}% {r.name_accuracy * 100:>5.0f}% {r.avg_ms:>7.0f}ms {price:>8}  "
            f"{'paid' if r.paid else 'free':<5} {'cloud' if not r.local else 'local':<5}"
        )
    lines.append(f"({total_seconds:.0f} s of audio; latency is the engine time per sentence, cloud includes the upload)")
    return lines


def format_details(reports: list[EngineReport]) -> list[str]:
    lines: list[str] = []
    for r in reports:
        if r.error:
            continue
        lines.append(f"== {r.label}")
        for s in r.sentences:
            mark = "ok " if s.errors == 0 else f"{s.errors}e "
            names = "" if s.names == 0 else f" names {s.names_found}/{s.names}"
            route = f" [{s.routed_to}]" if s.routed_to else ""
            conf = f" conf {s.confidence:.2f}" if s.confidence is not None else ""
            lines.append(f"  {s.index:>2} {mark}{s.hypothesis!r}{names}{conf}{route}")
    return lines


def save_results(session: Session, reports: list[EngineReport]) -> Path:
    """results.json of the session; options not in this run keep their earlier rows (a partial re-run
    with --engines never throws away the paid cloud rows)."""
    path = session.directory / "results.json"
    earlier: list[dict[str, Any]] = []
    if path.is_file():
        try:
            earlier = [e for e in json.loads(path.read_text(encoding="utf-8")).get("engines", []) if isinstance(e, dict)]
        except (OSError, ValueError):
            earlier = []
    fresh = {
        r.key: {**asdict(r), "word_accuracy": r.word_accuracy, "name_accuracy": r.name_accuracy, "avg_ms": r.avg_ms, "cost_usd": r.cost_usd}
        for r in reports
    }
    merged = [fresh.pop(e["key"]) if e.get("key") in fresh else e for e in earlier]
    merged.extend(fresh.values())
    payload = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "session": str(session.directory),
        "engines": merged,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# --- CLI ---------------------------------------------------------------------------------------------------------------


def run_test_stt(
    config: Config,
    paths: Paths,
    *,
    compare_dir: str | None = None,
    engines: str | None = None,
    cloud: bool = False,
    download: bool = False,
    verbose: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    """`jarvis voice test-stt [--compare DIR] [--engines a,b] [--cloud] [--download] [--verbose]`."""
    from jarvis.voice.service import VoiceService  # noqa: PLC0415

    missing = VoiceService.missing_dependencies()
    if missing:
        out(t("voice.service.missing_packages", packages=", ".join(missing)))
        return 1
    try:
        if compare_dir:
            session = Session.load(Path(compare_dir).expanduser())
        else:
            session = record_session(config, paths, out=out)
        wanted = [e.strip() for e in engines.split(",") if e.strip()] if engines else None
        if cloud:
            out(t("voice.stt_test.cloud_warning"))
        compare(config, paths, session, out=out, engines=wanted, cloud=cloud, download=download, verbose=verbose)
    except KeyboardInterrupt:
        out(t("voice.service.cancelled"))
        return 130
    return 0
