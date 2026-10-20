# The `jarvis` package

Python 3.12, built with hatchling; the `jarvis` command is `jarvis.main:main`. The big picture is in
[docs/architecture.md](../../docs/architecture.md); this page is a map of the folder.

## Core

- `main.py`: the CLI (`build_parser`, one `cmd_*` function per subcommand) and `bootstrap` (config, paths,
  logging, the main language).
- `assistant.py`: `Assistant.handle(text, sink)`, one turn from input to reply; sessions; the memory pass.
- `brain.py`: `Brain`, the streaming Anthropic API loop with tool calls, retries, the iteration cap and
  thinking settings.
- `context.py`: history, compaction, the system prompt and its cache layout.
- `config.py`: the Pydantic model of `config.toml` (merged with `config.local.toml`) and `Paths`, which
  derives every file location from the Jarvis home (`$JARVIS_HOME`, else the repository of the editable
  install). Every key is listed in [docs/configuration.md](../../docs/configuration.md).
- `memory.py`, `memory_pass.py`: `data/memory.md` and the end-of-session pass. `profile.py`: `data/profile.md`.
- `i18n.py`: user-facing text from `locales/`. `exceptions.py`: the error types.
- `setup_wizard.py` (`jarvis setup`), `uninstall.py` (`jarvis uninstall`), `services.py` (the launchd agents
  as a group), `version.py`.

## Folders

| Folder | What it holds |
| --- | --- |
| `tools/` | one module per capability; see [tools/README.md](tools/README.md) |
| `security/` | confirmation gate, taint, guards, sanitizing; see [security/README.md](security/README.md) |
| `observability/` | SQLite, logs, usage and budget, `jarvis doctor`; see [observability/README.md](observability/README.md) |
| `io/` | the input and output interfaces and the terminal; see [io/README.md](io/README.md) |
| `interfaces/` | the Telegram bot, and where every other interface lives; see [interfaces/README.md](interfaces/README.md) |
| `voice/` | the hands-free voice service: wake word, VAD, speech to text, text to speech |
| `scheduler/` | the background agent: timers, alarms, the morning brief, periodic jobs |
| `dashboard/` | `jarvis dashboard`; see [dashboard/README.md](dashboard/README.md) |
| `prompts/` | persona and rules; see [prompts/README.md](prompts/README.md) |
| `locales/` | English and Turkish text; see [locales/README.md](locales/README.md) |
| `templates/` | the starting files `jarvis setup` copies into `data/` |
| `football/`, `maps/`, `location/`, `network/`, `presence/`, `training/` | backends behind the tools |
| `utils/` | Keychain secrets, AppleScript, EventKit, Google sign-in, calendar filters, Sheets tables, images, the platform check, time and file helpers |

## Rules for code in this package

- Every tool is a `Tool` subclass with a Pydantic input model, a `RiskLevel` and a `taints` flag.
- Subprocesses are argv lists, never a shell string.
- Secrets come from the Keychain (`utils/secrets.py`) and never reach a log, a file or a message.
- Every path comes from `Paths`; never hardcode the repository or the working directory.
- User-facing text goes through `i18n.t`; code, comments, logs and tool results are English.

## Tests

`tests/` mirrors the package: `tests/unit/` and `tests/security/`. See [tests/README.md](../../tests/README.md).
