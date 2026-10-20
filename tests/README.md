# Tests

The suite runs without network, API keys or macOS permissions, in a temporary home for every test.

```bash
uv run pytest tests                              # everything (slow: several minutes)
uv run pytest tests/unit/test_config_doc.py      # one file
uv run pytest tests/security                     # the security tests
uv run pytest tests -k telegram                  # by name
```

The full suite takes a few minutes; while you work, run the files you touched and leave the full run in
the background. Lint with `uv run ruff check . && uv run ruff format --check .`.

## Layout

- `unit/`: one file per module or feature (`test_brain.py`, `test_alarms.py`, `test_dashboard.py`, ...).
- `security/`: paths, shell, SSRF, sanitizing, taint, AppleScript, email, Telegram, the dashboard lock,
  location privacy and more. `security/conftest.py` builds a fake home with allowed and denied folders.
- `fixtures/`: `config.test.toml` plus recorded API shapes (`football/`) and Health exports (`health/`).
  Everything here is synthetic.

## `tests/conftest.py`

Applied to every test automatically:

- `_no_network`: any connection to an address other than this machine raises, so a test that forgot a
  mock fails instead of reaching the internet or costing money. Local servers (the dashboard tests) work.
- `_main_language_reset`: every test starts with the default main language.
- `_platform_check_off`, `_football_key_absent`, `_elevenlabs_offline`, `location_lookups_off`: no
  platform check, no football key, no ElevenLabs key, no location lookups.

On request:

- `paths`: a temporary Jarvis home with `config.toml` copied from `fixtures/config.test.toml`.
- `config`: that config loaded (it also sets the main language). `db`: a fresh SQLite database.
  `logs`: logging into the temporary `logs/`.
- Helpers for the API: `FakeClient`, `text_message`, `tool_message`, `thinking_block`, `api_error`,
  `connection_error`; `RecordingSink` records what a turn wrote.

## The test config

`fixtures/config.test.toml` is a complete setup with Turkish as the main language and every feature
switched on, so the tests exercise everything; `eval/run_eval.py` loads it too. It is not the file new users
start from (that is `config.example.toml`). When you add a config key, add it here as well.

## Writing a test

- Use synthetic data only: names like Ada or Alex, example domains, ids like 123456789.
- Mock subprocesses, AppleScript, EventKit and HTTP clients; never call a real service.
- `tests/unit/test_dashboard_browser.py` drives the dashboard in Google Chrome through Playwright and
  skips when Chrome or Playwright is missing.
- Generated files are checked against their generators: `test_helpfile.py` (`HELPFILE.md`),
  `test_config_doc.py` (`docs/configuration.md`), `test_requirements.py` (the requirements files).
