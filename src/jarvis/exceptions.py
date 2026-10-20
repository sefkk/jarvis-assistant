"""Custom exception hierarchy."""

from __future__ import annotations

from jarvis.i18n import t


class JarvisError(Exception):
    """Base class for every Jarvis error."""


class ConfigError(JarvisError):
    """config.toml is missing, unreadable, or invalid. Stops startup."""


# Failure categories of `JarvisAPIError` (what the user is told):
#   timeout  - our own limit hit: no connection within api_connect_timeout_s (phase "connect") or a
#              stalled stream, no data for api_read_timeout_s (phase "stall" / "first_token")
#   network  - the connection failed or dropped without a timeout (DNS, reset, no route)
#   overload - 429 / 529 / 503: the API is busy, retrying later is the fix
#   auth     - 401 / 403: the key is wrong or revoked
#   api      - everything else (400 bad request, 5xx, ...)
API_ERROR_CATEGORIES = ("timeout", "network", "overload", "auth", "api")

# The texts live in `locales/<language>/errors.toml`: `errors.api.<key>` on screen, `errors.spoken.<category>`
# for the voice service.
_USER_MESSAGE_KEYS = {
    "timeout:connect": "timeout_connect",
    "timeout:first_token": "timeout_first_token",
    "timeout:stall": "timeout_stall",
}


class JarvisAPIError(JarvisError):
    """The Anthropic API call failed after all retry attempts."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
        attempts: int = 1,
        phase: str | None = None,
        connect_timeout_s: float | None = None,
        read_timeout_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.request_id = request_id
        self.attempts = attempts
        self.phase = phase  # for timeouts: connect | first_token | stall | write | pool
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s

    @property
    def category(self) -> str:
        """timeout | network | overload | auth | api (see API_ERROR_CATEGORIES)."""
        if self.error_type == "APITimeoutError":
            return "timeout"
        if self.status_code is None:
            return "network" if self.error_type in ("APIConnectionError", None) else "api"
        if self.status_code in (429, 529, 503):
            return "overload"
        if self.status_code in (401, 403):
            return "auth"
        return "api"

    def one_line(self) -> str:
        """Short description for the log and the eval runner. The full details go to the log."""
        parts = [f"API call failed after {self.attempts} attempt(s)"]
        if self.status_code is not None:
            parts.append(f"status {self.status_code}")
        if self.error_type:
            parts.append(self.error_type + (f" ({self.phase})" if self.phase else ""))
        if self.request_id:
            parts.append(f"request {self.request_id}")
        return ": ".join([parts[0], ", ".join(parts[1:])]) if len(parts) > 1 else parts[0]

    def user_message(self, language: str | None = None) -> str:
        """One or two short sentences for the user (default: the main language): the reason category, the
        numbers that matter and the request id when the API answered at all."""
        category = self.category
        key = _USER_MESSAGE_KEYS.get(f"timeout:{self.phase}", category) if category == "timeout" else category
        detail = ", ".join(str(x) for x in (f"status {self.status_code}" if self.status_code is not None else None, self.error_type) if x)
        text = t(
            f"errors.api.{key}",
            language,
            attempts=self.attempts,
            connect=f"{self.connect_timeout_s if self.connect_timeout_s is not None else 0:g}",
            read=f"{self.read_timeout_s if self.read_timeout_s is not None else 0:g}",
            status=self.status_code,
            detail=detail or t("errors.api.unknown", language),
        )
        if self.request_id:
            text += " " + t("errors.api.request_id", language, request_id=self.request_id)
        return text

    def spoken_message(self, language: str | None = None) -> str:
        """One short sentence for the voice service (default: the main language)."""
        return t(f"errors.spoken.{self.category}", language)


class BudgetExceededError(JarvisError):
    """The daily budget has been used up."""


class ToolError(JarvisError):
    """Base class for tool failures. Never crashes the loop; returned to the model."""


class ToolValidationError(ToolError):
    """The model passed bad arguments."""


class ToolExecutionError(ToolError):
    """The tool itself failed."""


class ToolBlockedError(ToolError):
    """Blocked by a security rule."""


class ToolDeniedError(ToolError):
    """The user refused the confirmation."""


class PermissionMissingError(JarvisError):
    """A macOS permission or an OAuth token is missing."""
