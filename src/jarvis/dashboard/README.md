# Dashboard

`jarvis dashboard` serves a local monitoring and settings page: FastAPI, Jinja2 templates and HTMX, on
127.0.0.1 only, while the command runs in a terminal (Ctrl-C stops it; there is no launchd agent).
`--no-open` prints the one-time link instead of opening the browser. A second `jarvis dashboard` signs in
to the server that is already running (through `data/dashboard.json`).

## Main files

- `server.py`: binds `dashboard.port` (or the next free port), mints the one-time token, runs uvicorn.
- `app.py`: `create_app`, every route and the template environment.
- `security.py`: one ASGI middleware for every request: Host check (DNS rebinding), session cookie in
  exchange for the one-time token, CSRF token on state-changing requests, and the idle lock
  (`dashboard.idle_lock_min`; 423 while locked, `/unlock` to continue). `touchid.py` handles
  `dashboard.unlock = "touch_id"`.
- Pages and their data: `state.py` (Overview: health cache, services, voice, quick actions), `queries.py`
  (read-only SQLite), `analytics.py` (Analytics), `logs.py` (sessions, full-text search, turn detail),
  `audit.py` (`logs/audit.jsonl`).
- Editing: `settings.py` (the Settings form, generated from the Pydantic config model: widgets, labels,
  which services a change restarts, read-only fields), `cart.py` (changes staged in the `dashboard_drafts`
  table and applied together: validation, conflict check, backup under `data/backups/`, write, graceful
  restart, rollback), `tomlwrite.py` (tomlkit edits that keep comments; a key set in `config.local.toml`
  is written there), `markdown.py` (the sanitized preview for the Persona and Profile editors).
- Look: `static/tokens.css` holds every design token (colors as `light-dark()` pairs, type, spacing, radii,
  motion, chart palette) and `static/app.css` uses only those; `static/theme.js` applies the System / Light /
  Dark toggle; `icons.py` inlines the vendored Lucide icons (`icon('name')` in templates). Every third-party
  asset is vendored under `static/vendor/` with its license; the page loads nothing from the internet.
- `ui_text.py`: every UI string. The dashboard is English only.

## Extending

- New text goes into `ui_text.py`, never into a template.
- New colors or sizes go into `static/tokens.css` first.
- A new setting appears in the Settings form from the config model; give it a label and help text in
  `LABELS` in `settings.py`. A new section needs an entry in `SECTIONS` (its title and the services a
  change restarts).
- Templates autoescape; anything of user or tool origin (transcripts, tool results) stays escaped.

## Tests

`tests/unit/test_dashboard.py`, `test_dashboard_location.py`, `test_dashboard_polish.py` (the UI stays
English), `test_cart.py`, `test_analytics.py`, `test_logs.py`; `tests/security/test_dashboard_security.py`
and `test_dashboard_lock.py`. `tests/unit/test_dashboard_browser.py` drives the real server in Google
Chrome through Playwright and skips when Chrome is not installed.
