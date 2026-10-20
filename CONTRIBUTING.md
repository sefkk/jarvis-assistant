# Contributing

Thanks for helping. Jarvis is small enough to read in an afternoon; [docs/architecture.md](docs/architecture.md)
is the map and [CLAUDE.md](CLAUDE.md) lists the house rules (it is written for Claude Code, but it is just as
useful for people).

## Set up

```
git clone https://github.com/sefkk/jarvis-assistant.git && cd jarvis-assistant
uv sync --extra voice
uv run pytest tests            # no network, no keys, no macOS permissions; a few minutes
uv run ruff check . && uv run ruff format --check .
```

Use a separate home for experiments so your own data stays untouched:
`mkdir -p /tmp/jarvis-dev && JARVIS_HOME=/tmp/jarvis-dev uv run jarvis setup`. The Keychain is shared by
every home of your macOS account, so keys you store there are seen by both.

## Before you open a pull request

- One change per pull request, with a test. A new tool also gets example phrases in
  `src/jarvis/locales/{en,tr}/examples.toml` and, if it changes how the model should behave, a case in
  `eval/cases.yaml` (see [eval/README.md](eval/README.md)).
- Security lives in code: a new tool has a Pydantic input model, a risk level, the `taints` flag where it reads
  outside content, and audit logging. See [src/jarvis/tools/README.md](src/jarvis/tools/README.md).
- Every user-facing text goes into both `src/jarvis/locales/en/` and `src/jarvis/locales/tr/`; commands
  (`jarvis …`, Telegram `/…`) stay English.
- Regenerate what is generated: `scripts/update_requirements.sh` after a dependency change,
  `uv run python scripts/gen_helpfile.py` after a command or tool change,
  `uv run python scripts/gen_config_doc.py` after a config change. CI checks all three.
- No personal data in code, tests or fixtures: synthetic names, places and ids only.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

## Reporting bugs and ideas

Use the issue templates. For a bug, include `jarvis --version`, the relevant `jarvis doctor` lines and the steps.
Security problems go through private reporting, see [SECURITY.md](SECURITY.md).
