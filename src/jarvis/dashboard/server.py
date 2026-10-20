"""`jarvis dashboard`: bind 127.0.0.1 on the configured port (or the next free one),
mint the one-time token, open the browser on the token link, serve with uvicorn in the foreground until
Ctrl-C. No launchd service: the dashboard runs only while the terminal does."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from jarvis.dashboard.app import create_app
from jarvis.dashboard.security import SessionStore
from jarvis.dashboard.ui_text import UI
from jarvis.exceptions import JarvisError

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

log = logging.getLogger("jarvis.dashboard")

HOST = "127.0.0.1"  # never 0.0.0.0
BROWSER_DELAY_S = 0.5
CONTROL_FILE = "dashboard.json"  # data/dashboard.json: port, pid and the control token of the running server


def control_file(paths: "Paths") -> Path:
    return paths.data_dir / CONTROL_FILE


def write_control_file(paths: "Paths", *, port: int, control_token: str) -> None:
    """Readable by this user only: a second `jarvis dashboard` uses it to ask the running server for a link."""
    path = control_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "pid": os.getpid(), "control_token": control_token}, fh)


def remove_control_file(paths: "Paths") -> None:
    try:
        control_file(paths).unlink()
    except OSError:
        pass


def running_instance(paths: "Paths") -> dict[str, Any] | None:
    """The running dashboard's port and control token, or None when there is none (or the file is stale)."""
    path = control_file(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        port, pid, token = int(data["port"]), int(data["pid"]), str(data["control_token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        remove_control_file(paths)  # the process is gone
        return None
    return {"port": port, "pid": pid, "control_token": token}


def request_sign_in(instance: dict[str, Any], *, post: Callable[..., Any] | None = None) -> str | None:
    """Ask the running server for a fresh one-time link (`POST /auth/issue` with the control token)."""
    url = f"http://{HOST}:{instance['port']}/auth/issue"
    try:
        if post is None:
            import httpx  # noqa: PLC0415

            response = httpx.post(url, headers={"X-Dashboard-Control": instance["control_token"]}, timeout=5.0)
        else:
            response = post(url, headers={"X-Dashboard-Control": instance["control_token"]}, timeout=5.0)
        if response.status_code != 200:
            return None
        link = response.json().get("url")
        return str(link) if link else None
    except Exception as exc:
        log.info("the running dashboard did not answer: %s", type(exc).__name__)
        return None


def bind_socket(preferred_port: int, host: str = HOST) -> socket.socket:
    """A listening socket on `host`: the preferred port, else any free one (port 0). Bound before the
    server starts, so the browser's first request waits in the backlog instead of being refused."""
    for port in (int(preferred_port), 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            sock.listen(128)
            return sock
        except OSError as exc:
            sock.close()
            if port == 0:
                raise JarvisError(f"cannot listen on {host}: {exc}") from exc
            log.info("dashboard port %s is busy (%s); picking a free one", port, exc)
    raise JarvisError(f"cannot listen on {host}")  # pragma: no cover - the loop returns or raises


def _serve(app, sock: socket.socket, port: int) -> None:
    import uvicorn  # noqa: PLC0415 - only the dashboard needs it

    config = uvicorn.Config(app, host=HOST, port=port, log_config=None, access_log=False, server_header=False, lifespan="on")
    uvicorn.Server(config).run(sockets=[sock])


def _open_browser(url: str, out: Callable[[str], None]) -> None:
    try:
        opened = webbrowser.open(url)
    except Exception:  # pragma: no cover - depends on the Mac's browser setup
        opened = False
    if not opened:
        out(f"Could not open the browser. Open this one-time link yourself:\n{url}")


def run_dashboard(
    config: "Config",
    paths: "Paths",
    *,
    open_browser: bool = True,
    port: int | None = None,
    out: Callable[[str], None] = print,
    serve: Callable | None = None,
    post: Callable[..., Any] | None = None,
) -> int:
    instance = running_instance(paths)
    if instance is not None:
        link = request_sign_in(instance, post=post)
        if link is not None:
            out(f"{UI.APP_TITLE} is already running on http://{HOST}:{instance['port']}; opening a new sign-in there.")
            if open_browser:
                _open_browser(link, out)
            else:
                out(f"Open this one-time link in your browser:\n{link}")
            return 0
        out("A dashboard control file exists but the server did not answer; starting a new one.")
    sock = bind_socket(port or config.dashboard.port)
    bound_port = int(sock.getsockname()[1])
    store = SessionStore(lock_after_s=config.dashboard.idle_lock_min * 60)
    app = create_app(config, paths, store=store, port=bound_port)
    url = f"http://{HOST}:{bound_port}/auth?token={store.token}"
    out(f"{UI.APP_TITLE}: http://{HOST}:{bound_port} (Ctrl-C stops it)")
    if open_browser:
        threading.Timer(BROWSER_DELAY_S, _open_browser, args=(url, out)).start()
    else:
        out(f"Open this one-time link in your browser:\n{url}")
    write_control_file(paths, port=bound_port, control_token=store.control_token)
    log.info("dashboard started on %s:%s", HOST, bound_port)
    try:
        (serve or _serve)(app, sock, bound_port)
    finally:
        sock.close()
        remove_control_file(paths)
        log.info("dashboard stopped")
    return 0
