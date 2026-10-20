"""Thin Telegram Bot API client over `httpx`.

Why plain `httpx` instead of `python-telegram-bot`: the bot serves one user, the `Assistant`
is synchronous, `httpx` is already a dependency, there is no webhook (long polling only, no
inbound port) and no need for an async framework. The whole surface Jarvis uses is seven
methods, each one a POST to `https://api.telegram.org/bot<token>/<method>`.

Security notes:
- The token appears only in the request URL. Every error message that leaves this module goes
  through `mask_token`, and `httpx` / `httpcore` loggers are silenced so their "HTTP Request"
  lines (which contain the URL) never reach a log file.
- Outgoing text is cleaned (`for_terminal`: no ANSI escapes or control characters) and capped
  at Telegram's 4096-character limit; `split_message` splits longer replies.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from jarvis.exceptions import JarvisError
from jarvis.security.sanitize import for_terminal

log = logging.getLogger("jarvis.telegram.api")

# The httpx loggers print "HTTP Request: POST https://api.telegram.org/bot<token>/..." at INFO.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

BASE_URL = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
MAX_FILE_BYTES = 20 * 1024 * 1024  # Bot API getFile limit
TYPING = "typing"

_TOKEN_IN_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
_TOKEN_SHAPE_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_\-]{20,}$")


class TelegramApiError(JarvisError):
    """A Bot API call failed. The message never contains the token."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def mask_token(text: str, token: str | None = None) -> str:
    """Replace the bot token (and anything shaped like one in a URL) with ***."""
    if token:
        text = text.replace(token, "***")
    return _TOKEN_IN_URL_RE.sub("bot***", text)


def looks_like_token(token: str) -> bool:
    return bool(_TOKEN_SHAPE_RE.match(token.strip()))


def clean_outgoing(text: str) -> str:
    """Anything sent to the phone: no escape or control characters."""
    return for_terminal(text)


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split a reply into chunks of at most `limit` characters, preferring newline then space
    boundaries. Empty input gives an empty list."""
    text = clean_outgoing(text)
    if not text.strip():
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return [c for c in chunks if c]


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """InlineKeyboardMarkup from rows of (label, callback_data). callback_data is at most 64 bytes."""
    keyboard = []
    for row in rows:
        buttons = []
        for label, data in row:
            if len(data.encode("utf-8")) > 64:
                raise ValueError("callback_data must be at most 64 bytes")
            buttons.append({"text": label[:64], "callback_data": data})
        keyboard.append(buttons)
    return {"inline_keyboard": keyboard}


class TelegramApi:
    """Synchronous Bot API client. `transport` is injectable (tests use `httpx.MockTransport`)."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
        timeout_s: float = 30.0,
    ) -> None:
        if not token or not token.strip():
            raise TelegramApiError("no bot token")
        self._token = token.strip()
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = httpx.Client(transport=transport, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    # --- plumbing ---------------------------------------------------------------------------

    def _method_url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    def _file_url(self, file_path: str) -> str:
        return f"{self._base}/file/bot{self._token}/{file_path.lstrip('/')}"

    def _mask(self, text: str) -> str:
        return mask_token(text, self._token)

    def call(self, method: str, *, timeout_s: float | None = None, **params: Any) -> Any:
        """POST `params` as JSON; return `result`. Raises TelegramApiError (token masked)."""
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            response = self._client.post(self._method_url(method), json=payload, timeout=timeout_s or self._timeout_s)
        except httpx.HTTPError as exc:
            # `from None`: the original exception text can carry the request URL (and the token).
            raise TelegramApiError(f"{method}: {type(exc).__name__}: {self._mask(str(exc))}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramApiError(f"{method}: HTTP {response.status_code} with a non-JSON body", status=response.status_code) from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = str(body.get("description", "unknown error")) if isinstance(body, dict) else "unknown error"
            retry_after = None
            if isinstance(body, dict):
                retry_after = (body.get("parameters") or {}).get("retry_after")
            raise TelegramApiError(
                f"{method}: HTTP {response.status_code}: {self._mask(description)}",
                status=response.status_code,
                retry_after=float(retry_after) if retry_after is not None else None,
            )
        return body.get("result")

    # --- methods Jarvis uses -----------------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe") or {}

    def get_updates(self, offset: int | None, timeout: int, allowed_updates: list[str] | None = None) -> list[dict[str, Any]]:
        """Long poll. The HTTP timeout is the poll timeout plus a margin so the server answers first."""
        result = self.call(
            "getUpdates",
            timeout_s=float(timeout) + 10.0,
            offset=offset,
            timeout=timeout,
            allowed_updates=allowed_updates or ["message", "callback_query"],
        )
        return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        """One message of at most 4096 characters (use `send_long` for arbitrary text)."""
        text = clean_outgoing(text)
        if len(text) > MAX_MESSAGE_CHARS:
            raise TelegramApiError(f"sendMessage: text is {len(text)} characters, the limit is {MAX_MESSAGE_CHARS}")
        if not text.strip():
            raise TelegramApiError("sendMessage: empty text")
        return self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup) or {}

    def send_long(self, chat_id: int, text: str) -> list[dict[str, Any]]:
        """Split a long reply over several messages. Returns the sent messages in order."""
        return [self.send_message(chat_id, chunk) for chunk in split_message(text)]

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        text = clean_outgoing(text)
        if len(text) > MAX_MESSAGE_CHARS:
            raise TelegramApiError(f"editMessageText: text is {len(text)} characters, the limit is {MAX_MESSAGE_CHARS}")
        result = self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
        return result if isinstance(result, dict) else {}

    def send_chat_action(self, chat_id: int, action: str = TYPING) -> None:
        self.call("sendChatAction", chat_id=chat_id, action=action)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=clean_outgoing(text)[:200] if text else None)

    def get_file(self, file_id: str) -> dict[str, Any]:
        """-> {"file_id", "file_unique_id", "file_size", "file_path"}"""
        return self.call("getFile", file_id=file_id) or {}

    def download_file(self, file_path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        """Stream the file; stop with an error as soon as it exceeds `max_bytes`."""
        try:
            with self._client.stream("GET", self._file_url(file_path), timeout=self._timeout_s) as response:
                if response.status_code != 200:
                    raise TelegramApiError(f"file download: HTTP {response.status_code}", status=response.status_code)
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise TelegramApiError(f"file download: {declared} bytes exceeds the {max_bytes} byte limit")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramApiError(f"file download: more than {max_bytes} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"file download: {type(exc).__name__}: {self._mask(str(exc))}") from None
        return b"".join(chunks)
