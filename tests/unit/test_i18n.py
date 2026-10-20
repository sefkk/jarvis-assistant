"""The English and Turkish catalogs (src/jarvis/locales/) stay in step, and the loader behaves."""

from __future__ import annotations

import pytest

from jarvis import i18n


@pytest.fixture(autouse=True)
def _restore_language():
    before = i18n.current_language()
    yield
    i18n.set_language(before)


def test_every_key_exists_in_every_language_with_the_same_type_and_placeholders():
    reference = i18n.catalog("en")
    assert reference, "the English catalog is empty"
    for language in i18n.LANGUAGES:
        entries = i18n.catalog(language)
        assert set(entries) == set(reference), (
            f"{language}: missing {sorted(set(reference) - set(entries))}, extra {sorted(set(entries) - set(reference))}"
        )
        for key, value in reference.items():
            other = entries[key]
            assert type(other) is type(value), f"{language}:{key} is a {type(other).__name__}, English a {type(value).__name__}"
            if isinstance(value, str):
                assert i18n.placeholders(other) == i18n.placeholders(value), f"{language}:{key} placeholders differ"
                assert other.strip(), f"{language}:{key} is empty"
            else:
                assert other, f"{language}:{key} is an empty list"


def test_the_locale_folders_hold_the_same_files():
    names = {lang: sorted(p.name for p in (i18n.LOCALES_DIR / lang).glob("*.toml")) for lang in i18n.LANGUAGES}
    assert all(files == names["en"] for files in names.values()), names


def test_t_fills_placeholders_and_follows_the_main_language():
    i18n.set_language("tr")
    assert i18n.t("cli.goodbye.text", name=", Ada") == "Görüşürüz, Ada."
    assert i18n.t("cli.goodbye.text", "en", name="") == "Bye."
    i18n.set_language("en")
    assert i18n.t("cli.goodbye.text", name="") == "Bye."
    assert i18n.t("cli.goodbye.text", "xx", name="") == "Bye."  # an unknown language: the main one


def test_unknown_keys_and_languages_fail_loudly():
    with pytest.raises(KeyError):
        i18n.t("cli.no_such_key")
    with pytest.raises(ValueError):
        i18n.set_language("de")
