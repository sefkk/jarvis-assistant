"""The FastAPI application: routes, templates and the security middleware wired
together by `create_app`. Pages are server-rendered Jinja2; HTMX polls the Overview cards as partials.
Every string comes from `ui_text.UI`; every template autoescapes (transcripts may hold hostile HTML)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import jinja2
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from jarvis.dashboard import analytics, icons, logs, markdown, queries, touchid
from jarvis.dashboard import settings as settings_mod
from jarvis.dashboard.audit import DECISION_GROUPS, AuditLog, filter_entries, session_channels
from jarvis.dashboard.cart import (
    AREA_LABELS,
    AREA_MEMORY,
    AREA_PERSONA,
    AREA_PROFILE,
    FILE_PERSONA,
    FILE_PROFILE,
    KIND_MEMORY_ADD,
    KIND_MEMORY_DELETE,
    KIND_MEMORY_EDIT,
    TEXT_RESTARTS,
    Cart,
)
from jarvis.dashboard.security import CONTROL_HEADER, SecurityMiddleware, SessionStore, set_session_cookie
from jarvis.dashboard.state import Dashboard, format_duration, format_money, format_ms, parse_pause_minutes
from jarvis.dashboard.ui_text import UI, channel_label
from jarvis.exceptions import JarvisError
from jarvis.memory import _ENTRY_RE
from jarvis.observability.composition import estimate_tokens

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

import logging

log = logging.getLogger("jarvis.dashboard")

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

# (key, path, label): the sidebar, in this order.
NAV: tuple[tuple[str, str, str], ...] = (
    ("overview", "/", UI.NAV_OVERVIEW),
    ("analytics", "/analytics", UI.NAV_ANALYTICS),
    ("logs", "/logs", UI.NAV_LOGS),
    ("audit", "/audit", UI.NAV_AUDIT),
    ("settings", "/settings", UI.NAV_SETTINGS),
    ("memory", "/memory", UI.NAV_MEMORY),
    ("persona", "/persona", UI.NAV_PERSONA),
    ("history", "/history", UI.NAV_HISTORY),
)
PLACEHOLDER_PAGES: dict[str, str] = {}
AUDIT_FILE = "audit.jsonl"


def _local_time(value: Any, fmt: str = "%H:%M") -> str:
    """ISO text (UTC `...Z` or offset) or a datetime -> local wall clock."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    return dt.astimezone().strftime(fmt)


def _preview(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@lru_cache(maxsize=256)
def _asset_version(path: str, mtime_ns: int) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]


def asset_url(path: str) -> str:
    """`/static/<path>?v=<content hash>`: a changed file gets a new URL, so a browser never runs an old cached copy."""
    file = (STATIC_DIR / path).resolve()
    if not file.is_relative_to(STATIC_DIR) or not file.is_file():
        raise ValueError(f"no static file {path!r}")
    return f"/static/{path}?v={_asset_version(path, file.stat().st_mtime_ns)}"


def build_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,  # every template, whatever its name: tool results and transcripts are hostile input
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["local_time"] = _local_time
    env.filters["local_time_s"] = lambda value: _local_time(value, "%H:%M:%S")
    env.filters["money"] = format_money
    env.filters["ms"] = format_ms
    env.filters["duration"] = format_duration
    env.filters["preview"] = _preview
    env.filters["channel"] = channel_label
    env.globals["T"] = UI
    env.globals["nav"] = NAV
    env.globals["active"] = ""  # the page's context names the highlighted sidebar item
    env.globals["settings_display"] = settings_mod.display_value
    env.globals["cart_count"] = 0
    env.globals["icon"] = icons.icon
    env.globals["asset"] = asset_url
    env.globals["nav_icons"] = icons.NAV_ICONS
    return env


def create_app(
    config: "Config", paths: "Paths", *, store: SessionStore, port: int, dashboard: Dashboard | None = None, cart: Cart | None = None
) -> FastAPI:
    board = dashboard or Dashboard(config, paths)
    basket = cart or Cart(config, paths)
    env = build_env()
    refresh_s = int(config.dashboard.doctor_refresh_s)

    lock_minutes = int(config.dashboard.idle_lock_min)
    unlock_mode = str(config.dashboard.unlock)

    def context(request: Request) -> dict[str, Any]:
        session = request.scope.get("state", {}).get("session")
        return {
            "csrf": session.csrf if session else "",
            "refresh_s": refresh_s,
            "cart_count": basket.count() if session else 0,
            "lock_seconds": lock_minutes * 60,
            "lock_minutes": lock_minutes,
            "unlock_mode": unlock_mode,
        }

    templates = Jinja2Templates(env=env, context_processors=[context])
    expired_html = lambda: env.get_template("expired.html").render()  # noqa: E731
    locked_html = lambda session: env.get_template("locked.html").render(  # noqa: E731
        csrf=session.csrf, lock_seconds=lock_minutes * 60, lock_minutes=lock_minutes, unlock_mode=unlock_mode
    )

    app = FastAPI(title=UI.APP_TITLE, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dashboard = board
    app.state.store = store
    app.state.cart = basket
    app.add_middleware(SecurityMiddleware, store=store, port=port, expired_page=expired_html, locked_page=locked_html)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(request: Request, name: str, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx)

    def render_staged(request: Request, name: str, **ctx: Any) -> Response:
        """A partial after a staging action, plus the Save button swapped out of band so its count follows the cart."""
        session = request.scope.get("state", {}).get("session")
        base = {"csrf": session.csrf if session else "", "refresh_s": refresh_s, "cart_count": basket.count()}
        body = env.get_template(name).render({**base, **ctx})
        button = env.get_template("partials/save_button.html").render(base)
        return HTMLResponse(body + "\n" + button)

    def overview_context() -> dict[str, Any]:
        return {
            "active": "overview",
            "strip": board.strip(),
            "health": board.health.snapshot(),
            "services": board.services_view(),
            "voice": board.voice_view(),
            "flash": "",
            "flash_error": "",
            "today": board.today(),
            "loc": board.location_view(),
            "actions": board.actions,
            "actions_running": board.any_action_running(),
        }

    # --- session ---

    @app.get("/auth")
    def auth(token: str = "") -> Response:
        session = store.exchange(token)
        if session is None:
            return HTMLResponse(expired_html(), status_code=401)
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, session)
        return response

    @app.post("/auth/issue")
    def auth_issue(request: Request) -> Response:
        """A second `jarvis dashboard` asks for a fresh sign-in link with the control token (never a browser)."""
        if not store.is_control(request.headers.get(CONTROL_HEADER)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        token = store.issue_token()
        return JSONResponse({"url": f"http://127.0.0.1:{port}/auth?token={token}"})

    @app.post("/session/activity")
    def session_activity(request: Request) -> Response:
        session = request.scope["state"]["session"]
        return JSONResponse({"locked": session.locked, "seconds_left": round(store.seconds_left(session))})

    @app.post("/unlock")
    def unlock(request: Request) -> Response:
        session = request.scope["state"]["session"]
        if unlock_mode == "touch_id":
            ok, error = touchid.authenticate()
            if not ok:
                log.info("dashboard unlock refused (%s)", error or "no reason")
                return JSONResponse({"ok": False, "error": UI.UNLOCK_FAILED})
        store.unlock(session)
        return JSONResponse({"ok": True})

    # --- overview ---

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> Response:
        return render(request, "overview.html", **overview_context())

    @app.get("/partials/strip", response_class=HTMLResponse)
    def strip(request: Request) -> Response:
        return render(request, "partials/strip.html", strip=board.strip())

    @app.get("/partials/health", response_class=HTMLResponse)
    def health_partial(request: Request) -> Response:
        return render(request, "partials/health.html", health=board.health.snapshot())

    @app.post("/partials/health/run", response_class=HTMLResponse)
    def health_run(request: Request) -> Response:
        board.health.run_full()
        return render(request, "partials/health.html", health=board.health.snapshot())

    @app.get("/partials/services", response_class=HTMLResponse)
    def services_partial(request: Request) -> Response:
        return render(request, "partials/services.html", services=board.services_view())

    @app.post("/services/{name}/restart", response_class=HTMLResponse)
    def restart(request: Request, name: str) -> Response:
        board.restart_service(name)  # JarvisError for an unknown name -> the 400 handler below
        return render(request, "partials/services.html", services=board.services_view())

    @app.get("/partials/voice", response_class=HTMLResponse)
    def voice_partial(request: Request) -> Response:
        return render(request, "partials/voice_state.html", voice=board.voice_view(), flash="", flash_error="")

    @app.post("/voice/pause", response_class=HTMLResponse)
    def voice_pause(
        request: Request, preset: Annotated[str | None, Form()] = None, minutes: Annotated[str | None, Form()] = None
    ) -> Response:
        flash = flash_error = ""
        try:
            flash = board.pause_voice(parse_pause_minutes(preset, minutes))
        except (JarvisError, ValueError) as exc:
            flash_error = str(exc)
        return render(request, "partials/voice_state.html", voice=board.voice_view(), flash=flash, flash_error=flash_error)

    @app.post("/voice/resume", response_class=HTMLResponse)
    def voice_resume(request: Request) -> Response:
        flash = board.resume_voice()
        return render(request, "partials/voice_state.html", voice=board.voice_view(), flash=flash, flash_error="")

    @app.get("/partials/location", response_class=HTMLResponse)
    def location_partial(request: Request) -> Response:
        return render(request, "partials/location.html", loc=board.location_view(), flash_error="")

    @app.post("/location/refresh", response_class=HTMLResponse)
    def location_refresh(request: Request) -> Response:
        error = board.refresh_location()
        return render(request, "partials/location.html", loc=board.location_view(), flash_error=error or "")

    @app.get("/partials/today", response_class=HTMLResponse)
    def today_partial(request: Request) -> Response:
        return render(request, "partials/today.html", today=board.today(), refresh_recent=True)

    @app.get("/partials/actions", response_class=HTMLResponse)
    def actions_partial(request: Request) -> Response:
        return render(request, "partials/actions.html", actions=board.actions, actions_running=board.any_action_running())

    @app.post("/actions/brief", response_class=HTMLResponse)
    def action_brief(request: Request) -> Response:
        board.send_brief()
        return render(request, "partials/actions.html", actions=board.actions, actions_running=board.any_action_running())

    @app.post("/actions/telegram-test", response_class=HTMLResponse)
    def action_telegram_test(request: Request) -> Response:
        board.send_test_message()
        return render(request, "partials/actions.html", actions=board.actions, actions_running=board.any_action_running())

    # --- analytics ---

    def filters_from(request: Request) -> analytics.Filters:
        q = request.query_params
        return analytics.Filters.from_query(
            range_key=q.get("range"), channel=q.get("channel"), model=q.get("model"), start=q.get("start"), end=q.get("end")
        )

    def requests_table(request: Request, f: analytics.Filters, conn) -> dict[str, Any]:
        q = request.query_params
        try:
            page = int(q.get("page", "1"))
        except ValueError:
            page = 1
        if conn is None:
            return {"rows": [], "total": 0, "page": 1, "pages": 1, "sort": "time", "direction": "desc"}
        return analytics.requests(conn, f, sort=q.get("sort", "time"), direction=q.get("dir", "desc"), page=page)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request) -> Response:
        f = filters_from(request)
        conn = queries.connect_readonly(paths.db_file)
        try:
            if conn is None:
                ctx: dict[str, Any] = {
                    "monthly": analytics.empty_chart_data(f, config)["monthly"],
                    "latency": None,
                    "cache": None,
                    "errors": {"by_type": {}, "retries": 0, "final": 0, "top": [], "failing_tools": []},
                    "tools": [],
                    "confirmations": [],
                    "voice": None,
                    "models": [],
                }
            else:
                ctx = {
                    "monthly": analytics.monthly(conn, config),
                    "latency": analytics.latency(conn, f),
                    "cache": analytics.cache_stats(conn, f),
                    "errors": analytics.errors(conn, f),
                    "tools": analytics.tool_usage(conn, f),
                    "confirmations": analytics.confirmations(conn, f),
                    "voice": analytics.voice(conn, f, config.dashboard.correction_words),
                    "models": analytics.models_available(conn),
                }
            ctx["requests"] = requests_table(request, f, conn)
        finally:
            if conn is not None:
                conn.close()
        return render(request, "analytics.html", active="analytics", f=f, qs=f.query_string(), channels=analytics.CHANNELS, **ctx)

    @app.get("/analytics/data")
    def analytics_data(request: Request) -> Response:
        f = filters_from(request)
        conn = queries.connect_readonly(paths.db_file)
        try:
            data = analytics.empty_chart_data(f, config) if conn is None else analytics.chart_data(conn, f, config)
        finally:
            if conn is not None:
                conn.close()
        return JSONResponse(data)

    @app.get("/analytics/requests", response_class=HTMLResponse)
    def analytics_requests(request: Request) -> Response:
        f = filters_from(request)
        conn = queries.connect_readonly(paths.db_file)
        try:
            table = requests_table(request, f, conn)
        finally:
            if conn is not None:
                conn.close()
        return render(request, "partials/requests.html", f=f, qs=f.query_string(), requests=table)

    # --- logs ---

    audit_log = AuditLog(paths.logs_dir / AUDIT_FILE)

    def page_number(request: Request) -> int:
        try:
            return max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            return 1

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request) -> Response:
        f = filters_from(request)
        query = (request.query_params.get("q") or "").strip()[:200]
        conn = queries.connect_readonly(paths.db_file)
        empty = {"rows": [], "total": 0, "page": 1, "pages": 1}
        try:
            if conn is None:
                table, results = dict(empty), {**empty, "query": query}
            elif query:
                table, results = dict(empty), logs.search(conn, f, query, page=page_number(request))
            else:
                table, results = logs.sessions(conn, f, page=page_number(request)), {**empty, "query": query}
        finally:
            if conn is not None:
                conn.close()
        return render(
            request,
            "transcripts/index.html",
            active="logs",
            f=f,
            qs=f.query_string(),
            channels=analytics.CHANNELS,
            query=query,
            sessions=table,
            results=results,
        )

    @app.get("/logs/sessions/{session_id}", response_class=HTMLResponse)
    def session_page(request: Request, session_id: str) -> Response:
        conn = queries.connect_readonly(paths.db_file)
        try:
            detail = logs.session_detail(conn, session_id, page=page_number(request)) if conn is not None else None
        finally:
            if conn is not None:
                conn.close()
        if detail is None:
            return render(request, "placeholder.html", active="logs", title=UI.LOGS_SESSION)
        return render(request, "transcripts/session.html", active="logs", s=detail)

    @app.get("/logs/sessions/{session_id}/export.md")
    def session_export(session_id: str) -> Response:
        conn = queries.connect_readonly(paths.db_file)
        try:
            text = logs.export_markdown(conn, session_id) if conn is not None else None
        finally:
            if conn is not None:
                conn.close()
        if text is None:
            return PlainTextResponse(UI.EMPTY, status_code=404)
        safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:40] or "session"
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"content-disposition": f'attachment; filename="jarvis-session-{safe_id}.md"'},
        )

    @app.get("/logs/turns/{turn_id}", response_class=HTMLResponse)
    def turn_detail(request: Request, turn_id: str) -> Response:
        conn = queries.connect_readonly(paths.db_file)
        try:
            detail = logs.turn_detail(conn, turn_id, audit_log.for_turn(turn_id)) if conn is not None else None
        finally:
            if conn is not None:
                conn.close()
        if detail is None:
            return render(request, "placeholder.html", active="logs", title=UI.TURN_DETAIL)
        return render(request, "transcripts/turn.html", active="logs", d=detail)

    # --- audit ---

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request) -> Response:
        f = filters_from(request)
        q = request.query_params
        conn = queries.connect_readonly(paths.db_file)
        try:
            channels_by_session = session_channels(conn)
        finally:
            if conn is not None:
                conn.close()
        entries = audit_log.entries()
        tool = (q.get("tool") or "").strip() or None
        decision = q.get("decision") if q.get("decision") in (*DECISION_GROUPS, UI.DECISION_OTHER) else None
        tainted = q.get("tainted") if q.get("tainted") in ("yes", "no") else None
        table = filter_entries(
            entries,
            start_iso=f.start_iso,
            end_iso=f.end_iso,
            tool=tool,
            decision=decision,
            tainted=tainted,
            channel=f.channel,
            channels=channels_by_session,
            page=page_number(request),
        )
        return render(
            request,
            "audit.html",
            active="audit",
            f=f,
            qs=f.query_string(tool=tool, decision=decision, tainted=tainted),
            channels=analytics.CHANNELS,
            tools=audit_log.tools(),
            decisions=[*DECISION_GROUPS, UI.DECISION_OTHER],
            tool=tool or "",
            decision=decision or "",
            tainted=tainted or "",
            table=table,
            exists=audit_log.path.is_file(),
        )

    # --- settings ---

    def field_partial(request: Request, path: str, error: str | None = None) -> Response:
        config_now = basket.current_config()
        spec = settings_mod.field_spec(config_now, path)
        if spec is None:
            raise JarvisError(f"unknown setting {path!r}")
        staged = basket.by_key(f"setting:{path}")
        if staged is not None:
            spec.staged, spec.has_staged = staged.new, True
        spec.error = error
        return render_staged(request, "partials/setting_field.html", f=spec)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        config_now = basket.current_config()
        items, _general = basket.check()
        errors = {i.key.split(":", 1)[1]: i.error for i in items if i.kind == "setting" and i.error}
        sections = settings_mod.build_sections(config_now, basket.staged_settings(), errors)
        return render(
            request,
            "settings.html",
            active="settings",
            sections=sections,
            secrets=settings_mod.secret_status(),
            model_choices=sorted(config_now.budget.prices),
        )

    @app.post("/cart/settings", response_class=HTMLResponse)
    def stage_setting(request: Request, path: Annotated[str, Form()], value: Annotated[str | None, Form()] = None) -> Response:
        _item, error = basket.stage_setting(path, value)
        return field_partial(request, path, error)

    # --- memory ---

    def memory_rows(query: str = "") -> list[dict[str, Any]]:
        items = {i.key: i for i in basket.items() if i.area == AREA_MEMORY}
        rows = []
        for line in basket.memory_lines():
            key = f"memory:{__import__('hashlib').sha256(line.encode('utf-8')).hexdigest()[:16]}"
            item = items.get(key)
            state = "deleted" if item and item.kind == KIND_MEMORY_DELETE else "edited" if item and item.kind == KIND_MEMORY_EDIT else ""
            shown = str(item.new) if item and item.kind == KIND_MEMORY_EDIT else line
            m = _ENTRY_RE.match(shown)
            rows.append(
                {
                    "line": line,
                    "key": key,
                    "date": m.group(1) if m else "",
                    "text": m.group(2) if m else shown,
                    "source": "jarvis" if m else "manual",
                    "state": state,
                }
            )
        for item in items.values():
            if item.kind == KIND_MEMORY_ADD:
                m = _ENTRY_RE.match(str(item.new))
                rows.append(
                    {
                        "line": str(item.new),
                        "key": item.key,
                        "date": m.group(1) if m else "",
                        "text": m.group(2) if m else str(item.new),
                        "source": "manual",
                        "state": "added",
                    }
                )
        needle = query.casefold().strip()
        if needle:
            rows = [r for r in rows if needle in r["text"].casefold()]
        return rows

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(request: Request) -> Response:
        query = (request.query_params.get("q") or "")[:200]
        return render(request, "memory.html", active="memory", query=query, rows=memory_rows(query), error="")

    def memory_list(request: Request, error: str | None) -> Response:
        return render_staged(request, "partials/memory_list.html", rows=memory_rows(), error=error or "")

    @app.post("/cart/memory/add", response_class=HTMLResponse)
    def memory_add(request: Request, text: Annotated[str, Form()] = "") -> Response:
        _item, error = basket.stage_memory_add(text)
        return memory_list(request, error)

    @app.post("/cart/memory/edit", response_class=HTMLResponse)
    def memory_edit(request: Request, line: Annotated[str, Form()], text: Annotated[str, Form()] = "") -> Response:
        _item, error = basket.stage_memory_edit(line, text)
        return memory_list(request, error)

    @app.post("/cart/memory/delete", response_class=HTMLResponse)
    def memory_delete(request: Request, line: Annotated[str, Form()]) -> Response:
        _item, error = basket.stage_memory_delete(line)
        return memory_list(request, error)

    # --- persona and profile ---

    def editor_context(area: str, error: str | None = None) -> dict[str, Any]:
        file_name = FILE_PERSONA if area == AREA_PERSONA else FILE_PROFILE
        item = basket.by_key(f"text:{area}")
        text = str(item.new) if item is not None else basket.read_text(file_name)
        return {
            "area": area,
            "file_name": file_name,
            "text": text,
            "staged": item is not None,
            "chars": len(text),
            "tokens": estimate_tokens(text),
            "preview": markdown.render(text),
            "error": error or "",
            "restarts": TEXT_RESTARTS.get(file_name, ()),
        }

    @app.get("/persona", response_class=HTMLResponse)
    def persona_page(request: Request) -> Response:
        area = AREA_PROFILE if request.query_params.get("tab") == "profile" else AREA_PERSONA
        return render(request, "persona.html", active="persona", **editor_context(area))

    @app.post("/cart/text", response_class=HTMLResponse)
    def stage_text(request: Request, area: Annotated[str, Form()], text: Annotated[str, Form()] = "") -> Response:
        if area not in (AREA_PERSONA, AREA_PROFILE):
            raise JarvisError("unknown editor")
        _item, error = basket.stage_text(area, text)
        ctx = editor_context(area, error)
        if error:  # keep what was typed in the status, the preview shows the typed text too
            ctx.update(text=text, chars=len(text), tokens=estimate_tokens(text), preview=markdown.render(text))
        return render_staged(request, "partials/persona_status.html", **ctx)

    # --- history ---

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request) -> Response:
        restored = request.query_params.get("restored")
        flash = ""
        error = False
        if restored is not None:
            if restored == "same":
                flash = UI.HISTORY_RESTORE_SAME
            elif restored == "missing":
                flash, error = UI.HISTORY_MISSING, True
            elif restored.isdigit():
                flash = UI.HISTORY_RESTORE_STAGED.format(n=int(restored))
        return render(request, "history.html", active="history", backups=basket.backups(limit=20), flash=flash, flash_error=error)

    @app.post("/history/restore")
    def history_restore(id: Annotated[str, Form()]) -> Response:
        staged, error = basket.stage_restore(id)
        outcome = "missing" if error else (str(len(staged)) if staged else "same")
        response = Response(status_code=204)
        response.headers["HX-Redirect"] = f"/history?restored={outcome}"
        return response

    # --- the cart ---

    def drawer(request: Request) -> Response:
        items, general = basket.check()
        groups: list[tuple[str, list]] = []
        for area, label in AREA_LABELS.items():
            group = [i for i in items if i.area == area]
            if group:
                groups.append((label, group))
        restarts = [UI.SERVICE_LABEL.format(service=s) for s in basket.services_to_restart(items)]
        return render_staged(request, "partials/cart_drawer.html", items=items, groups=groups, general=general, restarts=restarts)

    def progress(request: Request) -> Response:
        job = basket.job
        if job is None:
            return drawer(request)
        return render(request, "partials/cart_progress.html", job=job)

    @app.get("/cart", response_class=HTMLResponse)
    def cart_view(request: Request) -> Response:
        if basket.job is not None and basket.job.running:
            return progress(request)
        return drawer(request)

    @app.post("/cart/remove", response_class=HTMLResponse)
    def cart_remove(request: Request, id: Annotated[str, Form()]) -> Response:
        if id.isdigit():
            basket.remove(int(id))
        return drawer(request)

    @app.post("/cart/remove-key", response_class=HTMLResponse)
    def cart_remove_key(request: Request, key: Annotated[str, Form()]) -> Response:
        basket.remove_key(key)
        if key.startswith("setting:"):
            return field_partial(request, key.split(":", 1)[1])
        if key.startswith("memory"):
            return memory_list(request, None)
        if key.startswith("text:"):
            area = key.split(":", 1)[1]
            if area in (AREA_PERSONA, AREA_PROFILE):
                return render_staged(request, "partials/persona_editor.html", **editor_context(area))
        return drawer(request)

    @app.post("/cart/discard", response_class=HTMLResponse)
    def cart_discard(request: Request) -> Response:
        basket.clear()
        return drawer(request)

    @app.post("/cart/apply", response_class=HTMLResponse)
    def cart_apply(request: Request) -> Response:
        basket.start_apply()
        return progress(request)

    @app.get("/cart/apply/status", response_class=HTMLResponse)
    def cart_apply_status(request: Request) -> Response:
        return progress(request)

    @app.post("/cart/apply/force", response_class=HTMLResponse)
    def cart_apply_force(request: Request) -> Response:
        if basket.job is not None:
            basket.job.force_restart()
        return progress(request)

    @app.post("/cart/apply/wait", response_class=HTMLResponse)
    def cart_apply_wait(request: Request) -> Response:
        if basket.job is not None:
            basket.job.keep_waiting()
        return progress(request)

    # --- the other pages (placeholders until they have content) ---

    def placeholder(key: str):
        def page(request: Request) -> Response:
            return render(request, "placeholder.html", active=key, title=PLACEHOLDER_PAGES[key])

        page.__name__ = f"page_{key}"
        return page

    for key, path, _label in NAV:
        if key in PLACEHOLDER_PAGES:
            app.get(path, response_class=HTMLResponse)(placeholder(key))

    # --- errors ---

    @app.exception_handler(JarvisError)
    def jarvis_error(request: Request, exc: JarvisError) -> Response:
        return HTMLResponse(env.get_template("error.html").render(message=str(exc)), status_code=400)

    @app.exception_handler(Exception)
    def unexpected_error(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return HTMLResponse(env.get_template("error.html").render(message=""), status_code=500)

    return app
