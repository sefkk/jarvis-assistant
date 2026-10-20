"""`jarvis voice say "<text>" [--dry-run] [--language tr|en]`: the spoken-form normalization by hand.

The text goes through the same steps as a reply in the voice service: `make_speakable` (markdown,
URLs and code out), the sentence splitter, `detect_language` per sentence and `normalize_for_speech`
(voice/normalize.py). The normalized sentences are printed; without `--dry-run` they are then spoken
by the configured engine (`voice.tts_engine`: ElevenLabs with `say` as the fallback, or `say`), which
runs the same normalization once more inside `speak()` (it is idempotent), so what is printed is
what is sent.
"""

from __future__ import annotations

from typing import Any, Callable

from jarvis.i18n import t
from jarvis.voice.normalize import normalize_for_speech
from jarvis.voice.tts import detect_language, make_speakable, split_sentences

SPEAK_TIMEOUT_S = 120.0


def normalized_sentences(text: str, *, language: str | None, default_language: str) -> list[tuple[str, str]]:
    """(language, normalized sentence) for every sentence of `text`, as the voice service would speak them."""
    rows: list[tuple[str, str]] = []
    for sentence in split_sentences(make_speakable(text, language or default_language).text):
        lang = language or detect_language(sentence, default_language)
        normalized = normalize_for_speech(sentence, lang)
        if normalized:
            rows.append((lang, normalized))
    return rows


def say_cli(
    config: Any,
    paths: Any,
    text: str,
    *,
    language: str | None = None,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
    build: Callable[..., Any] | None = None,
) -> int:
    text = text.strip()
    if not text:
        out(t("voice.say.usage"))
        return 2
    default = config.general.language
    rows = normalized_sentences(text, language=language, default_language=default)
    if not rows:
        out(t("voice.say.nothing"))
        return 0
    for lang, sentence in rows:
        out(f"[{lang}] {sentence}")
    if dry_run:
        return 0
    if build is None:
        from jarvis.voice.service import build_tts  # noqa: PLC0415

        build = build_tts
    engine = build(config.voice, paths, language=default, on_sentence=lambda spoken, voice: out(t("voice.say.spoken_by", voice=voice)))
    engine.start()
    try:
        engine.begin_turn()
        for lang, sentence in rows:
            engine.speak(sentence, lang)
        if not engine.wait(timeout=SPEAK_TIMEOUT_S):
            out(t("voice.say.timeout"))
            return 1
    finally:
        engine.close()
    return 0
