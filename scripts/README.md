# Scripts

Maintenance scripts. Each one generates files from a source of truth; never edit the generated files by
hand. Every script has a `--check` mode that changes nothing and exits 1 when its output is out of date,
and a test fails in the same case.

| Script | Generates | From | Test |
| --- | --- | --- | --- |
| `update_requirements.sh` | `requirements.txt`, `requirements-voice.txt`, `requirements-dev.txt` | `uv.lock` (`uv export`) | `tests/unit/test_requirements.py` |
| `gen_helpfile.py` | `HELPFILE.md` | the CLI parser, the tool modules, the Telegram commands, `config.example.toml`, `src/jarvis/locales/*/examples.toml` | `tests/unit/test_helpfile.py` |
| `gen_config_doc.py` | `docs/configuration.md` | the config model in `src/jarvis/config.py` and the comments in `config.example.toml` | `tests/unit/test_config_doc.py` |

## When to run them

```bash
scripts/update_requirements.sh                    # after changing dependencies in pyproject.toml (uv lock first)
uv run python scripts/gen_helpfile.py             # after adding or changing a command, a tool or example phrases
uv run python scripts/gen_config_doc.py           # after changing src/jarvis/config.py or config.example.toml
```

Add `--check` to any of them to verify without writing.

## Notes

- `gen_helpfile.py` keeps a few hand-written parts at its top: CLI examples, Telegram command
  descriptions, module headings (`MODULE_TITLES`; a new tool module needs one) and setup notes.
- `gen_config_doc.py` writes only what the code and `config.example.toml` say: a key's description is the
  comment next to it in `config.example.toml`, else its comment in `config.py`; a section's introduction is
  the class docstring plus the comment block that opens the section in `config.example.toml`. A key
  without a comment gets no description, so document new keys in `config.example.toml`.
- The output of both Python generators is deterministic and contains no local paths.
