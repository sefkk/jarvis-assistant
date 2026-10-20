# Security

The rules that keep Jarvis safe are enforced here, in code. The prompt (`prompts/rules.md`) explains
them to the model, but nothing depends on the model following it.

- `confirm.py`: `gate(tool, args, taint=..., sink=..., user_text=...)` runs before every tool call (the
  Brain calls it). `BLOCKED` raises `ToolBlockedError`; `CONFIRM` asks the user through the sink; a `SAFE`
  tool asks too when the turn is tainted and `Tool.tainted_confirm_reason` returns a reason. A denial
  raises `ToolDeniedError`, and the model is told the real outcome.
- `taint.py`: `TaintState` for one turn. The turn becomes tainted as soon as a tool with `taints = True`
  returns content (email, web pages, files, the clipboard, calendars). The sources are named in the
  confirmation prompt and in the audit log. A delegate run inherits the taint of its turn.
- `sanitize.py`: the three boundaries. `user_input` cleans what the user typed; `for_model`,
  `html_to_text` and `wrap_untrusted` prepare outside content for the model (normalized, control and
  invisible characters removed, wrapped in `<untrusted source="...">`); `for_terminal` strips escape
  sequences before anything is printed.
- `guards.py`: targeted checks at the risk boundaries. Only clearly dangerous input is blocked;
  everything else runs or goes to confirmation.
  - `PathGuard`: a path must resolve (symlinks followed) inside `security.allowed_file_roots` and outside
    `security.denied_paths`; Jarvis's own `data/` and `logs/` are always denied.
  - `ShellGuard`: `security.shell_denylist` matched against the command text and the parsed argv.
  - `UrlGuard` and `safe_fetch`: http(s) only, no embedded credentials, no local host names, and every
    resolved address (redirects included) must be public (SSRF protection).
  - `check_applescript_args`, `validate_email`, `validate_header_value`: shape checks for values passed
    to AppleScript (always as argv) and to Gmail drafts.

Related code elsewhere:

- `tools/base.py`: `RiskLevel`, the `taints` flag and the hooks `risk_for`, `taints_for`,
  `tainted_confirm_reason` and `confirm_preview`.
- `utils/secrets.py`: secrets in the macOS Keychain (service `jarvis`); `.env` only as a development
  fallback for `ANTHROPIC_API_KEY`. `observability/logging_setup.py` masks secrets in every log line.
- `interfaces/telegram_bot.py`: allowlisted user and chat, restricted tool profile, expiring
  confirmations, forwarded messages as untrusted content, kill switch (`/stop`), rate limit.
- `dashboard/security.py`: 127.0.0.1 only, one-time token, session cookie, Host and CSRF checks, idle lock.

## Extending

- Add a check where the risky input enters (a guard or the tool's Pydantic model), not in the prompt.
- Block the dangerous input, not the whole tool: a normal request should still run or ask.
- Never build a shell string; pass argv lists to `subprocess`.

## Tests

`tests/security/` (fixtures in `tests/security/conftest.py`: a fake home with allowed and denied
folders): paths, shell, SSRF, sanitizing, taint, AppleScript, email, Telegram, the dashboard lock and
more. Run them with `uv run pytest tests/security`.
