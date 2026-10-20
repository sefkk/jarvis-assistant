"""docs/configuration.md is generated from the config model by scripts/gen_config_doc.py, matches it, and
names every key of the model."""

from __future__ import annotations

import importlib.util
import re
import sys
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from jarvis.config import Config

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_config_doc.py"
DOC = ROOT / "docs" / "configuration.md"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_config_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_config_doc"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("gen_config_doc", None)


def test_configuration_doc_is_up_to_date(gen):
    assert DOC.read_text(encoding="utf-8") == gen.generate(), (
        "docs/configuration.md is stale: run `uv run python scripts/gen_config_doc.py`"
    )


def test_check_mode_passes_on_the_committed_file(gen, capsys):
    assert gen.main(["--check"]) == 0


def test_the_output_is_deterministic_and_has_no_local_paths(gen):
    text = gen.generate()
    assert text == gen.generate()
    assert str(ROOT) not in text
    assert "/Users/" not in text


def _sections(model: type[BaseModel], path: tuple[str, ...]) -> typing.Iterator[tuple[tuple[str, ...], type[BaseModel]]]:
    """Every table of the model, walked independently of the generator."""
    yield path, model
    for name, info in model.model_fields.items():
        annotation = info.annotation
        args = typing.get_args(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield from _sections(annotation, (*path, name))
        for arg in args:
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                yield from _sections(arg, (*path, name))


def _doc_sections(text: str) -> dict[str, str]:
    """{dotted table path: the text under its heading}."""
    parts = re.split(r"^#{2,3} \[\[?([^\]]+?)\]\]?\s*$", text, flags=re.MULTILINE)
    return {parts[i].replace('."NAME"', ""): parts[i + 1] for i in range(1, len(parts), 2)}


def test_every_key_of_the_model_is_documented_under_its_section():
    sections = _doc_sections(DOC.read_text(encoding="utf-8"))
    missing = []
    for top, info in Config.model_fields.items():
        for path, model in _sections(info.annotation, (top,)):
            body = sections.get(".".join(path))
            if body is None:
                missing.append(".".join(path) + " (section)")
                continue
            for name in model.model_fields:
                if f"- **`{name}`**" not in body:
                    missing.append(".".join((*path, name)))
    assert not missing, f"not in docs/configuration.md: {missing}"


def test_deprecated_keys_are_marked():
    sections = _doc_sections(DOC.read_text(encoding="utf-8"))
    for section, key in (("general", "language_default"), ("weather", "default_location")):
        line = next(line for line in sections[section].splitlines() if line.startswith(f"- **`{key}`**"))
        assert "**deprecated**" in line, line
