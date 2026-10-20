import httpx
import pytest

from jarvis.exceptions import ToolBlockedError, ToolExecutionError, ToolValidationError
from jarvis.security.guards import UrlGuard, is_blocked_address, safe_fetch

DNS = {
    "example.com": ["93.184.216.34"],
    "evil.example": ["10.0.0.5"],
    "dual.example": ["93.184.216.34", "127.0.0.1"],
    "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    "v6mapped.example": ["::ffff:192.168.1.1"],
    "redirector.example": ["93.184.216.34"],
}


def fake_resolver(host: str) -> list[str]:
    if host not in DNS:
        raise ToolExecutionError(f"DNS lookup failed for {host}")
    return DNS[host]


@pytest.fixture
def guard() -> UrlGuard:
    return UrlGuard(resolver=fake_resolver)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://127.1.2.3/",
        "http://0.0.0.0/",
        "http://10.1.2.3/",
        "http://192.168.1.10/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://printer.local/",
        "http://evil.example/",
        "http://dual.example/",
        "http://v6mapped.example/",
        "http://user:pass@example.com/",
    ],
)
def test_private_and_local_targets_are_blocked(guard, url):
    with pytest.raises(ToolBlockedError):
        guard.check(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "gopher://example.com/", "javascript:alert(1)"])
def test_non_http_schemes_are_blocked(guard, url):
    with pytest.raises(ToolBlockedError, match="http"):
        guard.check(url)


def test_public_targets_pass(guard):
    assert guard.check("https://example.com/page?q=1") == "https://example.com/page?q=1"
    assert guard.check("http://v6.example/") == "http://v6.example/"
    assert guard.check("http://93.184.216.34/") == "http://93.184.216.34/"


def test_bad_urls(guard):
    with pytest.raises(ToolValidationError):
        guard.check("")
    with pytest.raises(ToolValidationError, match="host"):
        guard.check("http:///nohost")
    with pytest.raises(ToolExecutionError, match="DNS"):
        guard.check("http://does-not-resolve.example/")


def test_is_blocked_address_edge_cases():
    assert is_blocked_address("::ffff:10.0.0.1") and is_blocked_address("224.0.0.1") and is_blocked_address("240.0.0.1")
    assert not is_blocked_address("8.8.8.8") and not is_blocked_address("2001:4860:4860::8888")


def _transport(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in routes:
            return httpx.Response(404, text="nope")
        status, headers, body = routes[key]
        return httpx.Response(status, headers=headers, text=body)

    return httpx.MockTransport(handler)


def test_redirect_to_private_address_is_blocked(guard):
    transport = _transport(
        {
            "http://redirector.example/": (302, {"location": "http://169.254.169.254/latest/meta-data/"}, ""),
            "http://redirector.example/rel": (301, {"location": "//evil.example/x"}, ""),
            "http://redirector.example/loop": (302, {"location": "http://redirector.example/loop"}, ""),
        }
    )
    with pytest.raises(ToolBlockedError):
        safe_fetch("http://redirector.example/", guard=guard, transport=transport)
    with pytest.raises(ToolBlockedError):
        safe_fetch("http://redirector.example/rel", guard=guard, transport=transport)
    with pytest.raises(ToolExecutionError, match="redirects"):
        safe_fetch("http://redirector.example/loop", guard=guard, transport=transport, max_redirects=3)


def test_redirect_to_public_address_is_followed_and_body_capped(guard):
    transport = _transport(
        {
            "http://redirector.example/go": (302, {"location": "https://example.com/final"}, ""),
            "https://example.com/final": (200, {"content-type": "text/html; charset=utf-8"}, "<p>" + "x" * 5000 + "</p>"),
        }
    )
    result = safe_fetch("http://redirector.example/go", guard=guard, transport=transport, max_bytes=1000)
    assert result.url == "https://example.com/final" and result.status == 200
    assert result.truncated and len(result.body) == 1000
    assert "text/html" in result.content_type
