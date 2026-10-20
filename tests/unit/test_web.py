"""web_search (server tool definition) and fetch_page (SSRF guard, conversion, cap, 9.2 rule)."""

from __future__ import annotations

import httpx
import pytest

from jarvis.exceptions import ToolBlockedError, ToolExecutionError
from jarvis.security.guards import UrlGuard
from jarvis.tools import web
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.tools.web import FetchPage, WebSearch, web_search_tool_type

DNS = {"example.com": ["93.184.216.34"], "www.example.com": ["93.184.216.34"], "news.site": ["93.184.216.35"]}


def fake_resolver(host: str) -> list[str]:
    if host not in DNS:
        raise ToolExecutionError(f"DNS lookup failed for {host}")
    return DNS[host]


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    web._transport = None


@pytest.fixture
def ctx(config, paths) -> ToolContext:
    built = ToolContext.build(config, paths)
    built.url_guard = UrlGuard(resolver=fake_resolver)
    return built


def _routes(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in routes:
            return httpx.Response(404, text="nope")
        status, headers, body = routes[key]
        if isinstance(body, bytes):
            return httpx.Response(status, headers=headers, content=body)
        return httpx.Response(status, headers=headers, text=body)

    return httpx.MockTransport(handler)


# --- web_search -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-sonnet-5-5", "web_search_20260209"),
        ("claude-sonnet-5-5-20260301", "web_search_20260209"),
        ("claude-opus-5-5", "web_search_20260209"),
        ("claude-fable-5-1", "web_search_20260209"),
        ("claude-mythos-1", "web_search_20260209"),
        ("claude-haiku-5-5", "web_search_20250305"),
        ("claude-sonnet-4-5", "web_search_20250305"),
        ("claude-opus-4-1", "web_search_20250305"),
        ("", "web_search_20250305"),
    ],
)
def test_web_search_tool_type_per_model(model, expected):
    assert web_search_tool_type(model) == expected


def test_web_search_api_schema_is_the_server_definition(ctx):
    ctx.config.models.main_model = "claude-sonnet-5-5"
    schema = WebSearch(ctx).api_schema()
    assert schema == {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    assert "input_schema" not in schema
    ctx.config.models.main_model = "claude-haiku-5-5"
    assert WebSearch(ctx).api_schema()["type"] == "web_search_20250305"
    # without a context (registry discovery in tests) it still produces a valid definition
    assert WebSearch().api_schema()["type"] == "web_search_20250305"


def test_web_search_is_never_run_locally(ctx):
    tool = WebSearch(ctx)
    assert tool.risk == RiskLevel.SAFE and tool.taints is True and tool.taint_source == "web"
    with pytest.raises(ToolExecutionError, match="Anthropic's servers"):
        tool.run(tool.parse_args({}))


# --- fetch_page ---------------------------------------------------------------------------------


def test_fetch_page_converts_html_and_drops_hidden_content(ctx):
    html = (
        "<html><head><title>T</title><script>alert(1)</script><style>p{}</style></head>"
        "<body><h1>Hello</h1><p>World <a href='/x'>link</a></p><div style='display:none'>SECRET</div></body></html>"
    )
    web._transport = _routes({"https://example.com/page": (200, {"content-type": "text/html; charset=utf-8"}, html)})
    tool = FetchPage(ctx)
    assert tool.risk == RiskLevel.SAFE and tool.taints is True and tool.taint_source == "web"
    result = tool.run(tool.parse_args({"url": "https://example.com/page"}))
    assert result.content.startswith("URL: https://example.com/page\nStatus: 200\nContent-Type: text/html")
    assert "Hello" in result.content and "World" in result.content
    assert "alert(1)" not in result.content and "SECRET" not in result.content and "<p>" not in result.content
    assert "<untrusted" not in result.content


def test_fetch_page_keeps_text_and_json_as_is(ctx):
    web._transport = _routes(
        {
            "https://example.com/a.txt": (200, {"content-type": "text/plain"}, "<b>not html</b>\nline 2"),
            "https://example.com/a.json": (200, {"content-type": "application/json"}, '{"a": [1, 2]}'),
            "https://example.com/a.ld": (200, {"content-type": "application/ld+json"}, '{"b": 1}'),
        }
    )
    tool = FetchPage(ctx)
    assert "<b>not html</b>\nline 2" in tool.run(tool.parse_args({"url": "https://example.com/a.txt"})).content
    assert '{"a": [1, 2]}' in tool.run(tool.parse_args({"url": "https://example.com/a.json"})).content
    assert '{"b": 1}' in tool.run(tool.parse_args({"url": "https://example.com/a.ld"})).content


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/octet-stream", "audio/mpeg"])
def test_fetch_page_refuses_binary(ctx, content_type):
    web._transport = _routes({"https://example.com/file": (200, {"content-type": content_type}, b"\x89PNG\x00\x01")})
    tool = FetchPage(ctx)
    with pytest.raises(ToolExecutionError, match="not text"):
        tool.run(tool.parse_args({"url": "https://example.com/file"}))


def test_fetch_page_caps_output_and_reports_final_url(ctx):
    web._transport = _routes(
        {
            "https://example.com/go": (302, {"location": "https://www.example.com/final"}, ""),
            "https://www.example.com/final": (200, {"content-type": "text/plain"}, "x" * 5000),
        }
    )
    tool = FetchPage(ctx)
    result = tool.run(tool.parse_args({"url": "https://example.com/go", "max_chars": 1000}))
    header, _, body = result.content.partition("\n\n")
    assert "URL: https://www.example.com/final" in header and "Status: 200" in header
    assert "showing the first 1000 characters" in header
    assert body == "x" * 1000


def test_fetch_page_blocks_private_targets_and_bad_schemes(ctx):
    web._transport = _routes({})
    tool = FetchPage(ctx)
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"url": "http://127.0.0.1/admin"}))
    with pytest.raises(ToolBlockedError):
        tool.run(tool.parse_args({"url": "file:///etc/passwd"}))
    with pytest.raises(ToolExecutionError, match="DNS"):
        tool.run(tool.parse_args({"url": "https://unknown.example/"}))


def test_fetch_page_empty_content_type_is_sniffed(ctx):
    web._transport = _routes({"https://example.com/raw": (200, {}, b"<html><body><p>Sniffed</p></body></html>")})
    tool = FetchPage(ctx)
    text = tool.run(tool.parse_args({"url": "https://example.com/raw"})).content
    assert "Sniffed" in text and "<p>" not in text


# --- the taint rule for fetch_page ----------------------------------------------------------------------


def _reason(url: str, user_text: str) -> str | None:
    tool = FetchPage()
    return tool.tainted_confirm_reason(tool.parse_args({"url": url}), user_text)


def test_tainted_rule_query_string_needs_confirmation():
    reason = _reason("https://example.com/search?q=secret", "please open example.com for me")
    assert reason and "query" in reason


def test_tainted_rule_unmentioned_host_needs_confirmation():
    reason = _reason("https://news.site/article", "summarize the page from example.com")
    assert reason and "news.site" in reason


def test_tainted_rule_mentioned_host_without_query_runs():
    assert _reason("https://example.com/article/1", "read https://example.com/article/1 and summarize") is None
    assert _reason("https://EXAMPLE.com/a", "what does Example.COM say?") is None
    assert _reason("https://example.com/a#frag", "look at example.com") is None


def test_tainted_rule_tolerates_www():
    assert _reason("https://www.example.com/a", "open example.com") is None
    assert _reason("https://example.com/a", "open www.example.com") is None
    assert _reason("https://www.example.com/a", "open www.example.com") is None
    assert _reason("https://www.news.site/a", "open example.com") is not None


def test_tainted_rule_is_not_consulted_in_a_clean_turn(ctx):
    """Without taint, the gate never calls the hook; with taint, the query rule triggers."""
    from jarvis.security.confirm import gate
    from jarvis.security.taint import TaintState
    from tests.conftest import RecordingSink

    tool = FetchPage(ctx)
    args = tool.parse_args({"url": "https://example.com/x?y=1"})
    sink = RecordingSink(confirm_answer=True)
    assert gate(tool, args, taint=TaintState(), sink=sink, user_text="") == "ran"
    tainted = TaintState()
    tainted.mark("email")
    assert gate(tool, args, taint=tainted, sink=sink, user_text="example.com") == "confirmed"
    assert sink.confirms[-1].reason and "query" in sink.confirms[-1].reason
