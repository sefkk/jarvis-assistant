import pytest

from jarvis.exceptions import ToolValidationError
from jarvis.security.guards import validate_email, validate_header_value


@pytest.mark.parametrize("addr", ["kerem@example.com", " First.Last+tag@sub.example.co.uk ", "a_b-c@x-y.io"])
def test_valid_addresses(addr):
    assert validate_email(addr) == addr.strip()


@pytest.mark.parametrize(
    "addr",
    [
        "",
        "plainaddress",
        "a@b",
        "a@@b.com",
        "a..b@example.com",
        "victim@example.com\r\nBcc: attacker@evil.example",
        "victim@example.com\nCc: x@y.z",
        "Name <victim@example.com>",
        "a@b.com, c@d.com",
        "x" * 250 + "@example.com",
    ],
)
def test_invalid_addresses(addr):
    with pytest.raises(ToolValidationError):
        validate_email(addr)


def test_crlf_in_subject_is_rejected():
    assert validate_header_value("subject", "Lunch tomorrow?") == "Lunch tomorrow?"
    for bad in ("Hi\r\nBcc: attacker@evil.example", "Hi\nX-Injected: 1", "Hi\rX", "Hi\x00"):
        with pytest.raises(ToolValidationError, match="header injection"):
            validate_header_value("subject", bad)
