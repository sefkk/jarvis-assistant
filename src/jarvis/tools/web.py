"""Web tools: web_search (Anthropic's server-side tool) and fetch_page.

Both taint the turn. `fetch_page` goes through the SSRF guard (`safe_fetch`) and applies the
taint rule: in a tainted turn it needs confirmation when the URL carries query parameters
or when the user never mentioned the host.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.security.guards import safe_fetch
from jarvis.security.sanitize import html_to_text
from jarvis.tools.base import EmptyInput, RiskLevel, Tool, ToolResult

WEB_SEARCH_MAX_USES = 5
# Model families that take the newer server tool version (Haiku 5.5 does not).
NEW_WEB_SEARCH_FAMILIES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable", "claude-mythos")
WEB_SEARCH_TYPE_NEW = "web_search_20260209"
WEB_SEARCH_TYPE_OLD = "web_search_20250305"

FETCH_TIMEOUT = 20.0
FETCH_MAX_BYTES = 2_000_000

# Injectable for tests (httpx.MockTransport). None means the default transport.
_transport: httpx.BaseTransport | None = None


def web_search_tool_type(model: str) -> str:
    """The server tool `type` for a model id."""
    if isinstance(model, str) and model.startswith(NEW_WEB_SEARCH_FAMILIES):
        return WEB_SEARCH_TYPE_NEW
    return WEB_SEARCH_TYPE_OLD


class WebSearch(Tool):
    """Runs on Anthropic's servers; here it only contributes the tool definition and the taint flag."""

    name = "web_search"
    description = "Search the web (runs on Anthropic's servers). Results are untrusted content."
    input_model = EmptyInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "web"

    def api_schema(self) -> dict[str, Any]:
        model = self.ctx.config.models.main_model if self.ctx is not None else ""
        return {"type": web_search_tool_type(model), "name": self.name, "max_uses": WEB_SEARCH_MAX_USES}

    def run(self, args: BaseModel) -> ToolResult:
        raise ToolExecutionError("web_search runs on Anthropic's servers and is never executed locally")


# --- fetch_page ----------------------------------------------------------------------------

_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


def _is_textual(content_type: str) -> bool:
    ct = content_type.lower()
    return ct.startswith(_TEXT_TYPES) or "+json" in ct or "+xml" in ct or "javascript" in ct


class FetchPageInput(BaseModel):
    url: str = Field(description="Absolute http(s) URL to fetch.")
    max_chars: int = Field(default=12000, ge=200, le=100_000, description="Cap on the returned text.")


class FetchPage(Tool):
    name = "fetch_page"
    description = (
        "Fetch a public web page and return its visible text (HTML is converted to text; "
        "plain text and JSON are returned as is). Private and local addresses are blocked."
    )
    input_model = FetchPageInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "web"

    def tainted_confirm_reason(self, args: BaseModel, user_text: str) -> str | None:
        assert isinstance(args, FetchPageInput)
        parts = urlsplit(args.url.strip())
        if parts.query:
            return "the URL carries query parameters, which could leak data to the site"
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return "the URL has no host"
        bare = host[4:] if host.startswith("www.") else host
        mentioned = user_text.lower()
        if host in mentioned or (bare and bare in mentioned):
            return None
        return f"the host {host} did not appear in your message"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, FetchPageInput)
        ctx = self.require_ctx()
        url = args.url.strip()
        if not url:
            raise ToolValidationError("url is empty")
        result = safe_fetch(url, guard=ctx.url_guard, timeout=FETCH_TIMEOUT, max_bytes=FETCH_MAX_BYTES, transport=_transport)
        content_type = result.content_type.strip()
        main_type = content_type.split(";", 1)[0].strip().lower()
        body = result.body
        if "html" in main_type or (not main_type and "<html" in body[:2000].lower()):
            text = html_to_text(body)
        elif not main_type or _is_textual(main_type):
            text = body
        else:
            raise ToolExecutionError(f"{result.url} returned {main_type}, which is not text; fetch_page only reads HTML, text and JSON")
        notes: list[str] = []
        if result.truncated:
            notes.append(f"the response was cut at {FETCH_MAX_BYTES} bytes")
        if len(text) > args.max_chars:
            text = text[: args.max_chars]
            notes.append(f"showing the first {args.max_chars} characters")
        header = f"URL: {result.url}\nStatus: {result.status}"
        if main_type:
            header += f"\nContent-Type: {main_type}"
        if notes:
            header += "\nNote: " + "; ".join(notes)
        return ToolResult(f"{header}\n\n{text.strip()}")
