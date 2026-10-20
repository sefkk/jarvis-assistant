"""Three log channels with JSON lines and secret redaction.

- jarvis.log   : application log, daily rotation
- audit.jsonl  : one line per tool call, append-only (retention cleanup only)
- usage.jsonl  : one line per API call

Every line carries `session_id` and `turn_id`, taken from context variables so that
callers never have to pass them around.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jarvis.utils.timeutil import now_iso

APP_LOGGER = "jarvis"
AUDIT_LOGGER = "jarvis.audit"
USAGE_LOGGER = "jarvis.usage"

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("turn_id", default=None)


def set_session_id(value: str | None) -> None:
    _session_id.set(value)


def set_turn_id(value: str | None) -> None:
    _turn_id.set(value)


def get_session_id() -> str | None:
    return _session_id.get()


def get_turn_id() -> str | None:
    return _turn_id.get()


# --- redaction -----------------------------------------------------------------

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-***"),
    (re.compile(r"\bsk_[A-Za-z0-9]{16,}"), "sk_***"),  # ElevenLabs API keys
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "AIza***"),  # Google API keys (Maps)
    (re.compile(r"\bya29\.[A-Za-z0-9_\-\.]+"), "ya29.***"),  # Google access tokens
    (re.compile(r"\b1//[A-Za-z0-9_\-]{10,}"), "1//***"),  # Google refresh tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{8,}"), "Bearer ***"),
    (re.compile(r"\d{6,}:[A-Za-z0-9_\-]{30,}"), "***:***"),  # Telegram bot tokens (also inside .../bot<token>/ URLs)
    (re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}"), "***@redacted"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# --- formatters and filters -------------------------------------------------------


class ContextFilter(logging.Filter):
    """Attach session_id and turn_id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session_id"):
            record.session_id = _session_id.get()
        if not hasattr(record, "turn_id"):
            record.turn_id = _turn_id.get()
        return True


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return repr(value)


def _dumps(payload: dict[str, Any]) -> str:
    return redact(json.dumps(payload, ensure_ascii=False, default=_json_default))


class AppJsonFormatter(logging.Formatter):
    """Application log line: message plus optional structured `data`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "session_id": getattr(record, "session_id", None),
            "turn_id": getattr(record, "turn_id", None),
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _dumps(payload)


class EventJsonFormatter(logging.Formatter):
    """Audit/usage line: the record's `data` dict is the event; ids and ts are added."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": now_iso(),
            "session_id": getattr(record, "session_id", None),
            "turn_id": getattr(record, "turn_id", None),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        else:
            payload["msg"] = record.getMessage()
        return _dumps(payload)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(f"[{record.levelname.lower()}] {record.getMessage()}")


# --- setup --------------------------------------------------------------------------


def _reset(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for flt in list(logger.filters):
        logger.removeFilter(flt)


def setup_logging(
    logs_dir: Path,
    *,
    level: str = "DEBUG",
    app_retention_days: int = 14,
    audit_retention_days: int = 90,
    console: bool = True,
) -> None:
    """Configure the three channels. Safe to call more than once (tests)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    ctx = ContextFilter()

    # The context filter sits on the handlers, not the loggers: logger-level filters do
    # not apply to records propagated from child loggers such as "jarvis.brain".
    app = logging.getLogger(APP_LOGGER)
    _reset(app)
    app.setLevel(level)
    app.propagate = False
    app_handler = logging.handlers.TimedRotatingFileHandler(
        logs_dir / "jarvis.log", when="midnight", backupCount=app_retention_days, encoding="utf-8"
    )
    app_handler.setFormatter(AppJsonFormatter())
    app_handler.addFilter(ctx)
    app.addHandler(app_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(ConsoleFormatter())
        console_handler.addFilter(ctx)
        app.addHandler(console_handler)

    for name, filename in ((AUDIT_LOGGER, "audit.jsonl"), (USAGE_LOGGER, "usage.jsonl")):
        logger = logging.getLogger(name)
        _reset(logger)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.FileHandler(logs_dir / filename, mode="a", encoding="utf-8")
        handler.setFormatter(EventJsonFormatter())
        handler.addFilter(ctx)
        logger.addHandler(handler)

    prune_jsonl(logs_dir / "audit.jsonl", audit_retention_days)


def prune_jsonl(path: Path, retention_days: int) -> int:
    """Retention cleanup: drop lines older than `retention_days`. Returns lines removed."""
    if not path.is_file():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
    kept: list[str] = []
    removed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = json.loads(line).get("ts", "")
        except (ValueError, AttributeError):
            kept.append(line)
            continue
        if isinstance(ts, str) and ts[:19] < cutoff:
            removed += 1
        else:
            kept.append(line)
    if removed:
        # Atomic replace so a crash mid-write cannot lose the append-only log.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
    return removed


def flush() -> None:
    """Push every buffered record of the three channels to disk (before the Mac goes to sleep)."""
    for name in (APP_LOGGER, AUDIT_LOGGER, USAGE_LOGGER):
        for handler in list(logging.getLogger(name).handlers):
            try:
                handler.flush()
            except Exception:  # a flush problem must never raise into the caller
                pass


def audit(event: dict[str, Any]) -> None:
    logging.getLogger(AUDIT_LOGGER).info("audit", extra={"data": event})


def usage(event: dict[str, Any]) -> None:
    logging.getLogger(USAGE_LOGGER).info("usage", extra={"data": event})
