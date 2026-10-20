# Jarvis: working notes for Claude Code (and for contributors)

Jarvis is a personal AI assistant for one user on an Apple Silicon Mac (macOS 26+). The model runs on the
Anthropic API; everything else (tools, memory, logging, voice, scheduler, Telegram, dashboard) runs locally.
This file is the context for every coding session. The code is the truth; [docs/architecture.md](docs/architecture.md)
is the longer map.

## Standing rules

- **Cycle:** one change at a time: test, commit, report. A commit message is one sentence saying what changed
  for the user, not how. Open pull requests against `main`.
- **Language:** English in code, comments, docs, prompts and config comments. Every user-facing text
  (CLI lines, notifications, Telegram messages, spoken sentences, dialogs, the setup wizard) lives in
  `src/jarvis/locales/en/*.toml` and `src/jarvis/locales/tr/*.toml` and is read with `jarvis.i18n.t(...)`;
  `tests/unit/test_i18n.py` keeps both catalogs in step. Text only the model reads (tool results, tool
  descriptions) is English. Logs, the audit trail, `jarvis doctor` and the dashboard are English.
  Jarvis replies in the language of the user's latest message; `general.language` is the fallback.
- **Commands are English in every language:** `jarvis doctor`, `/location`. Never translate or alias a
  command; `tests/unit/test_commands_are_english.py` pins the full list.
- **Security in code, not prompts:** every tool is a `Tool` subclass with a Pydantic input model, a
  `RiskLevel` (`SAFE` runs, `CONFIRM` asks, `BLOCKED` never runs), a `taints` flag and audit logging.
  Untrusted content is wrapped in `<untrusted source="...">` and taints the turn; `security/confirm.gate`
  decides when a tainted turn needs a confirmation. Guards (`PathGuard`, `ShellGuard`, `UrlGuard` with SSRF
  blocking, AppleScript argument checks) live in `security/guards.py`. Subprocesses are argv lists, never a
  shell string. Keep validation targeted: block the dangerous input, not the tool.
- **Email:** Gmail is read plus drafts only. There is no send scope and Jarvis never claims it sent one.
- **Secrets:** macOS Keychain via `utils/secrets.py` (service `jarvis`), `.env` only as a development fallback
  for the Anthropic key. Never print, log, commit or store a key anywhere else; log channels mask them.
- **Personal data:** never in code, tests, fixtures, evals, commits or logs. Use synthetic data. Logs carry
  towns, never coordinates. `config.toml`, `config.local.toml`, `data/`, `logs/`, `sounds/*` and
  `eval/recordings/` are gitignored.
- **Manual work:** every step a user must do by hand (accounts, keys, macOS permissions) belongs in
  `MANUAL_STEPS.md` or the matching guide in `docs/integrations/`. Code that waits for such a step is marked
  `TODO(setup)`.
- **Paths:** everything on disk derives from `Paths(home)`; the home is `$JARVIS_HOME`, else the repository of
  the editable install (`config.resolve_home`). Never hardcode a user path or rely on `cwd`.
- **External APIs and macOS commands:** verify against current docs or by running the command, not from
  memory (Anthropic, ElevenLabs, Google, EventKit, launchd, pmset).
- **Generated files:** `requirements*.txt` (`scripts/update_requirements.sh`), `HELPFILE.md`
  (`scripts/gen_helpfile.py`), `docs/configuration.md` (`scripts/gen_config_doc.py`). Regenerate, never edit;
  tests fail when they are stale.
- When in doubt, ask. Do not silently "improve" a decision that is written down in config or code.

## Architecture map

Package: `src/jarvis/` (Python 3.12, `uv`, hatchling; entry point `jarvis = jarvis.main:main`).

- **Core:** `assistant.py` (`Assistant.handle(text, sink)`, the one public surface), `brain.py` (streaming API
  loop, tool calls, retries, iteration cap), `context.py` (history, compaction, prompt-cache layout: tools,
  persona, rules, profile, memory; `load_prompts`), `config.py` (Pydantic model of `config.toml` + `Paths`),
  `memory.py` / `memory_pass.py` (`data/memory.md`), `profile.py` (`data/profile.md` with the auto-managed
  `Current Location` block), `i18n.py` + `locales/` (user-facing text), `setup_wizard.py` (`jarvis setup`),
  `uninstall.py`, `templates/` (profile and data file templates), `version.py`.
- **Prompts:** `prompts/persona.<lang>.md` (or the user's own `data/persona.md`), `prompts/rules.md` plus
  `prompts/rules.<lang>.md`; markers `[[HUMOR]]` (`persona.humor_level`), `[[TEAM]]` (`football.team`),
  `[[MEMORY_AUTO]]` (`memory.auto`). Update the rules whenever a tool's contract changes.
- **Tools:** `tools/base.py` (`Tool`, `RiskLevel`, `ToolContext`, `ToolResult`), `tools/registry.py`
  (`ToolRegistry.discover` imports every module in `jarvis.tools`; a tool hides itself with `available()`;
  subsets by risk or glob for Telegram and the delegate). One module per capability; backends in `football/`,
  `maps/`, `location/`, `network/`, `presence/`, `training/`. Example phrases for HELPFILE in
  `locales/*/examples.toml`.
- **Security:** `security/confirm.py`, `taint.py`, `sanitize.py`, `guards.py`; tests in `tests/security/`.
- **Observability:** `observability/db.py` (SQLite `data/jarvis.db`, versioned schema), `logging_setup.py`
  (`logs/jarvis.log`, `logs/audit.jsonl`, secret masking), `usage.py` (tokens, cost, daily budget), `reports.py`,
  `health.py` (`jarvis doctor`; each module contributes `health_checks`).
- **Interfaces** (all drive the same `Assistant` through an `io.base.OutputSink`): CLI chat (`main.py`,
  `io/text.py`); voice (`voice/service.py` and friends: wake word, VAD, STT engines, TTS, confirmations, pause,
  cue sounds); Telegram (`interfaces/telegram_bot.py`, allowlisted user, restricted tool profile, kill switch);
  scheduler (`scheduler/`: timers, pomodoro, alarms, the morning brief, config hot reload). Each background
  part is a launchd user agent (`*/launchd.py`, labels `com.jarvis-assistant.*`), managed by `services.py`.
- **Dashboard** (`dashboard/`): `jarvis dashboard` serves FastAPI + Jinja2 + HTMX on 127.0.0.1 with a one-time
  token, session cookie, host and CSRF checks, a self-only CSP and an idle lock. Every asset is vendored; every
  UI string is English and lives in `ui_text.py`; design tokens in `static/tokens.css`.

## Key commands

```
jarvis setup [--language] [--answers FILE]   jarvis doctor            jarvis uninstall
jarvis                                       chat                     jarvis services status|reinstall
jarvis scheduler|telegram|voice run|install|status                    jarvis dashboard
jarvis brief [schedule|--send ...]           jarvis alarms ...        jarvis location [refresh]
jarvis usage | stats | sessions | export     jarvis memory [forget <text>]
jarvis key set|delete NAME                   jarvis google login|logout
```

`uv run jarvis` from the repository is the development environment; `uv tool install --editable '.[voice]'
--python 3.12` installs the global command (keep the extra and the Python version).

## Tests and quality

- `uv run pytest tests`: no network (a guard in `tests/conftest.py` refuses non-local connections), no keys,
  no macOS permissions, a temporary home per test. The suite takes a few minutes; run it in the background.
  The test config is `tests/fixtures/config.test.toml` (a complete, Turkish-speaking setup); the `config`
  fixture sets the main language from it.
- `uv run ruff check . && uv run ruff format --check .`
- Evals: `uv run python eval/run_eval.py` (replay by default; `--live` costs money, `--record` saves locally,
  recordings are gitignored). New tool behavior gets a case in `eval/cases.yaml`.
- Known noise: two maps `arrive_by` tests and the football cache TTL test can fail near local midnight.

## Behavior worth knowing

- **Football:** `web_search` first; the `football.*` API tools exist only with a key in the Keychain. The
  default team is Fenerbahçe (`[football]`), and `[[calendar.filters]]` hides the other teams' fixtures in the
  football calendar.
- **Reply text:** the reply of a turn is the text after the last tool block (`io.base.compose_reply`); the Brain
  calls `sink.segment_break()` at every tool block.
- **Location:** `Location` in `profile.md` is home; `Current Location` is detected by code (Shortcut, then
  CoreLocationCLI, then IP when enabled). On Telegram the Mac's location is never the user's.
- **Alarms** ring on the Mac through the scheduler agent; the phone backup is an Apple Reminder and/or a
  Telegram message.
- **Config changes** reach a running scheduler by themselves (`scheduler/config_watch.py`); the voice and
  Telegram agents need `jarvis services reinstall` after a code or config change.
