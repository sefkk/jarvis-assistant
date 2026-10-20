"""HELPFILE.md is generated from the code by scripts/gen_helpfile.py and must match it, and every tool has
example phrases in every language (src/jarvis/locales/<lang>/examples.toml)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from jarvis import i18n

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_helpfile.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_helpfile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_helpfile"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("gen_helpfile", None)


def test_helpfile_is_up_to_date(gen):
    committed = (ROOT / "HELPFILE.md").read_text(encoding="utf-8")
    assert gen.render() == committed, "HELPFILE.md is stale: run `uv run python scripts/gen_helpfile.py`"


def test_the_output_does_not_depend_on_the_main_language(gen):
    before = i18n.current_language()
    try:
        i18n.set_language("tr")
        turkish = gen.render()
        assert i18n.current_language() == "tr"  # render() restores the language it found
        i18n.set_language("en")
        assert gen.render() == turkish
    finally:
        i18n.set_language(before)


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_every_tool_has_example_phrases(gen, language):
    names = {cls.name for _, cls in gen.discover_tool_classes()}
    phrases = gen.examples(language)
    assert names, "no tools discovered"
    assert set(phrases) == names, f"missing {sorted(names - set(phrases))}, unknown {sorted(set(phrases) - names)}"
    for name, values in phrases.items():
        assert isinstance(values, list) and 1 <= len(values) <= 3, f"{language}:{name} needs 1-3 phrases"
        assert all(isinstance(v, str) and v.strip() for v in values), f"{language}:{name} has an empty phrase"


def test_every_command_has_a_parsing_example(gen):
    from jarvis.main import build_parser

    root = gen.walk(build_parser())
    gen.check_cli(root)  # raises SystemExit with the problem
    assert "voice test-stt" in gen.command_paths(root)


def test_the_helpfile_has_no_machine_specific_values():
    text = (ROOT / "HELPFILE.md").read_text(encoding="utf-8")
    assert str(Path.home()) not in text
    assert str(ROOT) not in text
