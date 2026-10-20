# Observability

Everything Jarvis does is recorded on the Mac: what was said, which tools ran and why, what each API
call cost and how long each stage took. Nothing here leaves the machine.

- `db.py`: `Database`, the SQLite store at `data/jarvis.db` (WAL mode, parameterized queries only).
  The schema is versioned: `MIGRATIONS` is a list of `(version, sql)` steps applied in order and recorded
  in `schema_version`. Main tables: `sessions`, `turns`, `messages`, `tool_calls`, `api_calls`, `timings`,
  `turn_summaries`, `voice_events`, `turns_fts` (full-text search), plus the tables of the features
  (`timers`, `workouts`, `network_tests`, `presence_*`, `kv`, `api_cache`, `dashboard_drafts`, ...).
- `logging_setup.py`: three log channels with JSON lines, each tagged with the session and turn id:
  `logs/jarvis.log` (application log, rotated daily, kept `logging.app_log_retention_days`),
  `logs/audit.jsonl` (one line per tool call: arguments, risk, taint, decision; pruned after
  `logging.audit_log_retention_days`) and `logs/usage.jsonl` (one line per API call). `redact` masks
  secrets in every line.
- `usage.py`: `TokenUsage`, `UsageTracker` (tokens and cost per API call, from `budget.prices` and the
  cache multipliers) and `BudgetTracker` (`budget.daily_budget_usd`; informational: one notice when it is reached, nothing is switched off).
- `timing.py`: `TurnTimer`, per-stage timings of a turn (prompt build, API call, time to first token,
  each tool) in the `timings` table.
- `composition.py`: how many input tokens went to the persona, the rules, the profile, the memory, the
  tool definitions, the history, the tool results and the user's message (the dashboard's Analytics).
- `reports.py`: read-only reports for `jarvis stats`, `jarvis usage`, `jarvis sessions` and `jarvis export`.
- `health.py`: `run_checks` for `jarvis doctor`: the core checks plus `health_checks(config, paths)` from
  each module in `INTEGRATION_MODULES`.

## Extending

- A schema change is a new entry at the end of `MIGRATIONS`, never an edit of an old one.
- Log through `logging.getLogger("jarvis.<area>")` and put structured values under
  `extra={"data": {...}}`. Never log a secret, and log towns, never coordinates.
- A new integration contributes its own `health_checks(config, paths) -> list[Check]` (never raising,
  never prompting) and is added to `INTEGRATION_MODULES`.

## Tests

`tests/unit/test_db.py`, `test_logging.py`, `test_reports.py`, `test_instrumentation.py`,
`test_analytics.py`, `test_api_timeouts.py`.
