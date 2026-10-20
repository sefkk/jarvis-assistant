# Tools

Everything Jarvis can do is a tool, one module per capability (`gmail.py`, `timers.py`, ...). The model
sees each tool's name, description and JSON schema; the code decides whether a call may run.

- `base.py`: `Tool`, `RiskLevel`, `ToolContext` (config, paths and the shared guards), `ToolResult`, `EmptyInput`.
- `registry.py`: `ToolRegistry.discover` imports every module here (except `base`, `registry` and names
  starting with `_`) and registers each `Tool` subclass that has a `name`. `subset_by_patterns` builds the
  Telegram profile; `subset` filters by risk.
- `delegate.py`: a second model with its own restricted tool loop, for heavy work.

## Adding a tool

1. Write a `Tool` subclass in the module of its capability (or a new module):

   ```python
   from pydantic import BaseModel, Field

   from jarvis.tools.base import RiskLevel, Tool, ToolResult


   class WordCountInput(BaseModel):
       text: str = Field(description="The text to count, exactly as the user gave it.")


   class WordCount(Tool):
       name = "word_count"
       description = "Count the words in a text the user gives."
       input_model = WordCountInput
       risk = RiskLevel.SAFE  # SAFE runs, CONFIRM asks first, BLOCKED never runs
       taints = False  # True when the result is untrusted content (email, web, files)
       delegate_ok = True  # True / False / None (ask the user the first time)

       def run(self, args: WordCountInput) -> ToolResult:
           return ToolResult(f"{len(args.text.split())} words")
   ```

   - Validate input in the Pydantic model (types, `field_validator`); invalid arguments never reach `run`.
   - Raise `ToolExecutionError` (`jarvis.exceptions`) for a failure the model should report.
   - Subprocesses are argv lists, never a shell string. Use the guards on `self.require_ctx()`
     (`path_guard`, `shell_guard`, `url_guard`) at every boundary.
   - A `SAFE` tool that changes state sets `confirm_when_tainted = True` (or overrides
     `tainted_confirm_reason`) so it asks in a tainted turn. `risk_for` and `taints_for` can depend on the arguments.
   - `available(ctx)` returns `False` to hide the tool when its integration is not set up (no key, no
     login). It must be cheap and must never prompt.
2. Audit logging is automatic: the Brain records every call in the `tool_calls` table and `logs/audit.jsonl`.
3. Telegram: a tool is usable from the phone only when `telegram.remote_allowed_tools` lists it
   (`"module.tool"` or `"module.*"`). Add it to `config.example.toml` only when it is safe from a phone.
4. Example phrases: add `word_count = ["...", "..."]` under `[tools]` in `src/jarvis/locales/en/examples.toml`
   and `src/jarvis/locales/tr/examples.toml`. A new module also needs a heading in `MODULE_TITLES` in
   `scripts/gen_helpfile.py`. Then run `uv run python scripts/gen_helpfile.py` to update `HELPFILE.md`.
5. When the tool changes what the model should do, update `src/jarvis/prompts/rules.md`.
6. An integration with setup steps exposes `health_checks(config, paths)` and is listed in
   `INTEGRATION_MODULES` in `src/jarvis/observability/health.py`, so `jarvis doctor` reports it.

## Tests

- Add the name to `test_registry_discovers_the_core_tools` in `tests/unit/test_tools.py`; write
  `tests/unit/test_<module>.py` (mock the client or subprocess) and, at a boundary (files, shell, URLs,
  AppleScript, Telegram), a test in `tests/security/`. `test_helpfile.py` checks the example phrases.
- Add an eval case to `eval/cases.yaml`; a tool that reads external data needs a fixture class in
  `FIXTURE_CLASSES` in `eval/run_eval.py`. See [eval/README.md](../../../eval/README.md).
