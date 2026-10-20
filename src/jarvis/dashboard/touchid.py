"""Unlocking the dashboard with Touch ID (`dashboard.unlock = "touch_id"`): macOS LocalAuthentication through
pyobjc. `LAPolicyDeviceOwnerAuthentication` shows Touch ID when the Mac has it and the user's password
otherwise (and as the fallback inside the Touch ID sheet), so one policy covers both cases. The prompt belongs
to the process that runs `jarvis dashboard`, which is the user's own terminal session."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("jarvis.dashboard.touchid")

REASON = "unlock the Jarvis Dashboard"
TIMEOUT_S = 120.0


def _context():
    import LocalAuthentication as LA  # noqa: PLC0415 - macOS only, loaded when asked

    return LA, LA.LAContext.alloc().init()


def available() -> tuple[bool, str]:
    """(can a prompt be shown, why not): biometrics or the account password, whichever the Mac offers."""
    try:
        LA, ctx = _context()
    except Exception as exc:  # the framework is missing (not macOS, or the binding is not installed)
        return False, f"{type(exc).__name__}: {exc}"
    ok, error = ctx.canEvaluatePolicy_error_(LA.LAPolicyDeviceOwnerAuthentication, None)
    if ok:
        return True, ""
    return False, str(error.localizedDescription()) if error is not None else "not available"


def authenticate(reason: str = REASON, *, timeout_s: float = TIMEOUT_S) -> tuple[bool, str]:
    """Show the Touch ID / password prompt and wait for the answer. (success, error text)."""
    try:
        LA, ctx = _context()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    ok, error = ctx.canEvaluatePolicy_error_(LA.LAPolicyDeviceOwnerAuthentication, None)
    if not ok:
        return False, str(error.localizedDescription()) if error is not None else "not available"
    done = threading.Event()
    outcome: dict[str, object] = {"success": False, "error": ""}

    def reply(success, error):  # the reply block runs on a private queue
        outcome["success"] = bool(success)
        if error is not None:
            try:
                outcome["error"] = str(error.localizedDescription())
            except Exception:
                outcome["error"] = "authentication failed"
        done.set()

    try:
        ctx.evaluatePolicy_localizedReason_reply_(LA.LAPolicyDeviceOwnerAuthentication, reason, reply)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not done.wait(timeout_s):
        try:
            ctx.invalidate()
        except Exception:
            pass
        return False, "timed out"
    if outcome["success"]:
        log.info("dashboard unlocked with Touch ID / password")
        return True, ""
    log.info("dashboard unlock refused: %s", outcome["error"])
    return False, str(outcome["error"]) or "authentication failed"
