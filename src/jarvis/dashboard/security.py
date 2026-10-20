"""Who may talk to the dashboard, enforced in one ASGI middleware so no route can forget it:

- the Host header must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding protection);
- every request except `/auth`, `/auth/issue` and `/static/...` needs the session cookie that `/auth` set in
  exchange for a one-time token (HttpOnly, SameSite=Strict); otherwise 401 with the expired page;
- state-changing methods need the session's CSRF token in the `X-CSRF-Token` header (HTMX sends it);
- a session locks after `dashboard.idle_lock_min` without user activity: while locked every request but the
  unlock call answers 423 (a page request gets the lock panel), and the unlock call needs the same cookie,
  CSRF token and Host as everything else; polling never counts as activity, only requests the browser marks
  as user-driven (`X-Activity: 1`), the activity ping and plain page navigations do;
- every response carries a strict Content-Security-Policy (self only), `X-Frame-Options: DENY`, nosniff,
  no referrer and, for pages, `Cache-Control: no-store`. There is no CORS middleware at all.

Sessions live in memory and die with the process (the cookie stays valid until `jarvis dashboard` stops).
A second `jarvis dashboard` asks the running server for a fresh one-time link through `/auth/issue` with the
control token the server wrote next to its data (readable by this user only).
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE_NAME = "jarvis_dashboard"
CSRF_HEADER = "x-csrf-token"
ACTIVITY_HEADER = "x-activity"
CONTROL_HEADER = "x-dashboard-control"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_PATHS = ("/auth", "/auth/issue")
PUBLIC_PREFIXES = ("/static/",)
UNLOCK_PATH = "/unlock"
ACTIVITY_PATH = "/session/activity"
LOCKED_STATUS = 423

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS: dict[str, str] = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


@dataclass
class Session:
    id: str
    csrf: str
    created_at: float
    last_activity: float
    locked: bool = False


@dataclass
class SessionStore:
    """One-time sign-in tokens, the sessions they became, and the lock clock. `clock` is monotonic seconds
    (tests pass their own)."""

    lock_after_s: float
    clock: Callable[[], float] = time.monotonic
    control_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    _tokens: set[str] = field(default_factory=set)
    _sessions: dict[str, Session] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.token = self.issue_token()  # the link printed at start

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def exchange(self, token: str | None) -> Session | None:
        """Turn a one-time token into a session. Wrong, missing or already used tokens give None."""
        if not token:
            return None
        match = next((t for t in self._tokens if hmac.compare_digest(t, token)), None)
        if match is None:
            return None
        self._tokens.discard(match)
        now = self.clock()
        session = Session(id=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(32), created_at=now, last_activity=now)
        self._sessions[session.id] = session
        return session

    @property
    def token_used(self) -> bool:
        return self.token not in self._tokens

    def get(self, session_id: str | None) -> Session | None:
        """The session for a cookie value, with its lock state brought up to date; None when unknown."""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if not session.locked and self.clock() - session.last_activity > self.lock_after_s:
            session.locked = True
        return session

    def touch(self, session: Session) -> None:
        """User activity: the lock clock starts again (a locked session stays locked until `unlock`)."""
        if not session.locked:
            session.last_activity = self.clock()

    def unlock(self, session: Session) -> None:
        session.locked = False
        session.last_activity = self.clock()

    def seconds_left(self, session: Session) -> float:
        return max(0.0, self.lock_after_s - (self.clock() - session.last_activity))

    def revoke(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def is_control(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(token, self.control_token)

    @property
    def active(self) -> int:
        return len(self._sessions)


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def set_session_cookie(response: Response, session: Session) -> None:
    response.set_cookie(COOKIE_NAME, session.id, httponly=True, samesite="strict", secure=False, path="/")


def is_page_request(request: Request) -> bool:
    """A browser navigation (not an HTMX partial, not a JSON fetch)."""
    return (
        request.method.upper() == "GET"
        and "hx-request" not in request.headers
        and "application/json" not in request.headers.get("accept", "")
    )


class SecurityMiddleware:
    """See the module docstring. `expired_page` and `locked_page` render the 401 and 423 bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: SessionStore,
        port: int,
        expired_page: Callable[[], str] | None = None,
        locked_page: Callable[[Session], str] | None = None,
    ) -> None:
        self.app = app
        self.store = store
        self.allowed_hosts = allowed_hosts(port)
        self.expired_page = expired_page or (lambda: "Your session expired. Run `jarvis dashboard` again.")
        self.locked_page = locked_page or (lambda session: "Session locked")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websockets on this dashboard
            await self._reply(PlainTextResponse("Not found.", status_code=404), scope, receive, send)
            return
        request = Request(scope)
        if request.headers.get("host", "") not in self.allowed_hosts:
            await self._reply(PlainTextResponse("Bad request.", status_code=400), scope, receive, send)
            return
        path = scope.get("path", "")
        session: Session | None = None
        if path not in PUBLIC_PATHS and not path.startswith(PUBLIC_PREFIXES):
            session = self.store.get(request.cookies.get(COOKIE_NAME))
            if session is None:
                response = HTMLResponse(self.expired_page(), status_code=401)
                response.delete_cookie(COOKIE_NAME, path="/")
                await self._reply(response, scope, receive, send)
                return
            if scope.get("method", "GET").upper() not in SAFE_METHODS:
                sent = request.headers.get(CSRF_HEADER, "")
                if not sent or not hmac.compare_digest(sent, session.csrf):
                    await self._reply(PlainTextResponse("Forbidden.", status_code=403), scope, receive, send)
                    return
            if session.locked and path != UNLOCK_PATH:
                if is_page_request(request):
                    await self._reply(HTMLResponse(self.locked_page(session), status_code=LOCKED_STATUS), scope, receive, send)
                else:
                    await self._reply(JSONResponse({"locked": True}, status_code=LOCKED_STATUS), scope, receive, send)
                return
            if path == ACTIVITY_PATH or request.headers.get(ACTIVITY_HEADER) == "1" or is_page_request(request):
                self.store.touch(session)
        scope.setdefault("state", {})["session"] = session
        await self.app(scope, receive, self._with_headers(send, path))

    async def _reply(self, response: Response, scope: Scope, receive: Receive, send: Send) -> None:
        await response(scope, receive, self._with_headers(send, scope.get("path", "")))

    @staticmethod
    def _with_headers(send: Send, path: str) -> Callable[[Message], Awaitable[None]]:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                for name, value in SECURITY_HEADERS.items():
                    if name.encode() not in present:
                        raw.append((name.encode("latin-1"), value.encode("latin-1")))
                if not path.startswith(PUBLIC_PREFIXES) and b"cache-control" not in present:
                    raw.append((b"cache-control", b"no-store"))
                message["headers"] = raw
            await send(message)

        return send_with_headers
