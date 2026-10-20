"""Learning from spoken corrections (voice/corrections.py)."""

from __future__ import annotations

import pytest

from jarvis.voice import corrections as cr


@pytest.mark.parametrize(
    "text, meant",
    [
        ("Hayır, Sezen Aksu dedim", "Sezen Aksu"),
        ("hayır Sezen Aksu dedim.", "Sezen Aksu"),
        ("yok Taksim diyorum", "Taksim"),
        ("Hayır, Bağdat Caddesi demek istedim", "Bağdat Caddesi"),
        ("Sezen Aksu dedim", "Sezen Aksu"),
        ("No, I said Daft Punk", "Daft Punk"),
        ("I meant Coldplay", "Coldplay"),
    ],
)
def test_detect(text, meant):
    assert cr.detect(text) == meant


@pytest.mark.parametrize(
    "text", ["Hayır", "Yarın ne var?", "Spotify'da Tarkan çal", "hayır dedim", "", "hayır bunu bir iki üç dört beş altı yedi dedim"]
)
def test_detect_ignores_ordinary_sentences(text):
    assert cr.detect(text) is None


def test_align_finds_the_misheard_span():
    c = cr.align("Spotify'da Sezen Aksa'dan Gülümse çal", "Sezen Aksu")
    assert c is not None and c.heard == "Sezen Aksa" and c.meant == "Sezen Aksu" and c.similarity > 0.8
    assert cr.align("Kadıköy'den taksi mi metroyla", "Taksim").heard in ("taksi", "taksi mi")
    assert cr.align("Yarın hava nasıl", "Sezen Aksu") is None  # nothing similar: nothing learned
    assert cr.align("Sezen Aksu'dan çal", "Sezen Aksu") is None  # heard right: nothing to learn
    assert cr.align("", "x") is None


def test_store_learns_applies_and_reloads(tmp_path):
    path = tmp_path / "corrections.md"
    store = cr.CorrectionStore(path)
    assert store.apply("Sezen Aksa'dan çal") == "Sezen Aksa'dan çal" and store.pairs() == []
    learned = store.learn("Spotify'da Sezen Aksa'dan Gülümse çal", "Hayır, Sezen Aksu dedim")
    assert learned is not None and (learned.heard, learned.meant) == ("Sezen Aksa", "Sezen Aksu")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Speech corrections") and "- Sezen Aksa → Sezen Aksu" in text
    assert store.apply("sezen aksa'dan bir şarkı") == "Sezen Aksu'dan bir şarkı"
    assert store.apply("Sezen Aksaray") == "Sezen Aksaray"  # whole words only
    assert store.meant_terms() == ["Sezen Aksu"]
    store.learn("Spotify'da Sezen Aksa'dan Gülümse çal", "Hayır, Sezen Aksu dedim")  # the same pair is not stored twice
    assert path.read_text(encoding="utf-8").count("→ Sezen Aksu") == 1
    assert store.learn(None, "Hayır, Sezen Aksu dedim") is None
    assert store.learn("Yarın ne var", "Bir şey daha") is None
    # the user edits the file: the change is picked up
    path.write_text("- taksi mi → Taksim'e\n- broken line\n- same → same\n", encoding="utf-8")
    assert store.pairs() == [("taksi mi", "Taksim'e")]
    assert store.apply("Kadıköy'den taksi mi metroyla") == "Kadıköy'den Taksim'e metroyla"


def test_fold_is_case_and_accent_insensitive():
    assert cr.fold("İSTANBUL") == cr.fold("istanbul") == cr.fold("Istanbul")
    assert cr.fold("Kadıköy") == "kadikoy"


def test_english_corrections_and_header_follow_the_main_language(tmp_path):
    from jarvis import i18n

    assert cr.detect("No, I said Ed Sheeran") == "Ed Sheeran" and cr.detect("I meant Oxford Street") == "Oxford Street"
    assert cr.fold("İSTANBUL") == cr.fold("istanbul") == "istanbul" and cr.fold("I") == "i"
    i18n.set_language("en")
    store = cr.CorrectionStore(tmp_path / "en.md")
    assert store.learn("play shape of you by ed shiran", "no, I said Ed Sheeran") is not None
    assert (tmp_path / "en.md").read_text(encoding="utf-8").startswith("# Speech corrections")
    i18n.set_language("tr")
    store = cr.CorrectionStore(tmp_path / "tr.md")
    assert store.learn("Sezen Aksa'dan çal", "hayır, Sezen Aksu dedim") is not None
    text = (tmp_path / "tr.md").read_text(encoding="utf-8")
    assert text.startswith("# Konuşma düzeltmeleri") and "- duyulan → kastedilen" in text
