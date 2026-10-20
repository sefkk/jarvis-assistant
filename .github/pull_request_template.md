## What changes for the user
One or two sentences.

## Checklist
- [ ] Tests added or updated; `uv run pytest tests` passes
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] User-facing text in both `src/jarvis/locales/en/` and `src/jarvis/locales/tr/`; commands stay English
- [ ] Generated files refreshed if needed: `scripts/update_requirements.sh`, `scripts/gen_helpfile.py`, `scripts/gen_config_doc.py`
- [ ] A new or changed tool keeps its risk level, `taints` flag and confirmation; security-relevant changes explained here
- [ ] No personal data in code, tests or fixtures
- [ ] CHANGELOG.md updated under "Unreleased"
