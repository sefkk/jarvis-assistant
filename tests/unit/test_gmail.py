"""gmail tools against a fake googleapiclient service (no network). Nothing here can send mail."""

from __future__ import annotations

import base64
import email
import email.policy
import json

import pytest

from jarvis.exceptions import PermissionMissingError, ToolExecutionError, ToolValidationError
from jarvis.tools import gmail as gm
from jarvis.tools.base import RiskLevel, ToolContext
from jarvis.utils import google_auth

# --- fake service ---------------------------------------------------------------------------


class FakeService:
    """`service.users().threads().get(**kw)` -> request recorded as ("users.threads.get", kw).
    A call without kwargs is a sub-resource; a call with kwargs returns the canned response
    (a list is popped in order, a callable gets the kwargs, an exception is raised on execute)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict = {}

    def __getattr__(self, name):
        return _Resource(self, name)


class _Resource:
    def __init__(self, service: FakeService, path: str) -> None:
        self.service = service
        self.path = path

    def __call__(self, **kwargs):
        if not kwargs:
            return self
        self.service.calls.append((self.path, kwargs))
        outcome = self.service.responses.get(self.path, {})
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if callable(outcome):
            outcome = outcome(kwargs)
        return _Request(outcome)

    def __getattr__(self, name):
        return _Resource(self.service, f"{self.path}.{name}")


class _Request:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def execute(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def http_error(status: int, message: str):
    import httplib2
    from googleapiclient.errors import HttpError

    return HttpError(
        httplib2.Response({"status": status, "reason": "x"}),
        json.dumps({"error": {"message": message}}).encode(),
        uri="https://gmail.googleapis.com/gmail/v1/x",
    )


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def headers(**values: str) -> list[dict]:
    return [{"name": k.rstrip("_").replace("_", "-").title(), "value": v} for k, v in values.items()]


@pytest.fixture
def ctx(config, paths) -> ToolContext:
    return ToolContext.build(config, paths)


@pytest.fixture
def service(monkeypatch) -> FakeService:
    fake = FakeService()
    monkeypatch.setattr(google_auth, "build_service", lambda api, version: fake)
    return fake


def run(tool_cls, ctx, **args):
    tool = tool_cls(ctx)
    return tool.run(tool.parse_args(args))


# --- metadata ---------------------------------------------------------------------------------


def test_tool_metadata_and_no_send_anywhere():
    assert [t.name for t in (gm.GmailSearchThreads, gm.GmailReadThread, gm.GmailCreateDraft)] == [
        "gmail_search_threads",
        "gmail_read_thread",
        "gmail_create_draft",
    ]
    assert gm.GmailSearchThreads.risk == RiskLevel.SAFE and gm.GmailReadThread.risk == RiskLevel.SAFE
    assert gm.GmailCreateDraft.risk == RiskLevel.CONFIRM
    for t in (gm.GmailSearchThreads, gm.GmailReadThread, gm.GmailCreateDraft):
        assert t.taints and t.taint_source == "gmail"
    import inspect

    source = inspect.getsource(gm)
    assert ".send(" not in source and "messages().send" not in source and "gmail.send" not in source


# --- search -----------------------------------------------------------------------------------


def test_search_threads_lists_then_fetches_metadata(service, ctx):
    service.responses["users.threads.list"] = {"threads": [{"id": "t1", "snippet": "hi"}, {"id": "t2"}]}
    service.responses["users.threads.get"] = lambda kw: {
        "t1": {
            "id": "t1",
            "messages": [
                {
                    "id": "m1",
                    "internalDate": "1760947200000",
                    "snippet": "first",
                    "payload": {"headers": headers(subject="Invoice 42", from_="Alice <alice@example.com>")},
                },
                {
                    "id": "m2",
                    "internalDate": "1761033600000",
                    "snippet": "  reply  text ",
                    "payload": {"headers": headers(subject="Re: Invoice 42", from_="Bob <bob@example.com>")},
                },
            ],
        },
        "t2": {
            "id": "t2",
            "messages": [
                {"id": "m3", "internalDate": "1760947200000", "snippet": "solo", "payload": {"headers": headers(from_="c@example.com")}}
            ],
        },
    }[kw["id"]]
    result = run(gm.GmailSearchThreads, ctx, query="invoice", max_results=5)

    assert service.calls[0] == ("users.threads.list", {"userId": "me", "q": "invoice", "maxResults": 5})
    assert service.calls[1][0] == "users.threads.get"
    assert service.calls[1][1]["format"] == "metadata" and service.calls[1][1]["id"] == "t1"
    assert set(service.calls[1][1]["metadataHeaders"]) == {"Subject", "From", "Date"}
    lines = result.content.splitlines()
    assert lines[0] == "2 Gmail thread(s) for 'invoice':"
    assert lines[1].startswith("[thread t1] ") and "from Bob <bob@example.com>" in lines[1]
    assert "| Invoice 42, 2 messages" in lines[1]
    assert lines[2].strip() == "reply text"
    assert "[thread t2]" in lines[3] and "(no subject)" in lines[3]


def test_search_threads_empty_and_validation(service, ctx):
    service.responses["users.threads.list"] = {}
    assert run(gm.GmailSearchThreads, ctx, query="nothing").content == "No Gmail threads match 'nothing'."
    with pytest.raises(ToolValidationError):
        run(gm.GmailSearchThreads, ctx, query="   ")
    with pytest.raises(ToolValidationError):
        run(gm.GmailSearchThreads, ctx, query="x", max_results=0)


# --- read -------------------------------------------------------------------------------------


def thread_full(messages):
    return {"id": "t1", "messages": messages}


def test_read_thread_prefers_text_plain(service, ctx):
    service.responses["users.threads.get"] = thread_full(
        [
            {
                "id": "m1",
                "internalDate": "1760947200000",
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": headers(from_="alice@example.com", to="me@example.com", subject="Hello", cc="c@example.com"),
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": b64url("Plain body here\nline 2")}},
                        {"mimeType": "text/html", "body": {"data": b64url("<p>HTML body</p>")}},
                    ],
                },
            }
        ]
    )
    result = run(gm.GmailReadThread, ctx, thread_id="t1")
    assert service.calls == [("users.threads.get", {"userId": "me", "id": "t1", "format": "full"})]
    text = result.content
    assert text.startswith("Gmail thread t1 (1 message(s)):")
    assert "From: alice@example.com" in text and "To: me@example.com" in text and "Cc: c@example.com" in text
    assert "Subject: Hello" in text
    assert "Plain body here\nline 2" in text and "HTML body" not in text


def test_read_thread_falls_back_to_html_and_lists_attachments(service, ctx):
    html = (
        "<html><head><style>p{}</style><script>alert(1)</script></head><body><p>Visible <b>text</b></p>"
        "<div style='display:none'>hidden instructions</div></body></html>"
    )
    service.responses["users.threads.get"] = thread_full(
        [
            {
                "id": "m1",
                "internalDate": "1760947200000",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": headers(from_="a@example.com", subject="Report"),
                    "parts": [
                        {
                            "mimeType": "multipart/alternative",
                            "parts": [
                                {"mimeType": "text/html", "body": {"data": b64url(html)}},
                            ],
                        },
                        {"mimeType": "application/pdf", "filename": "report.pdf", "body": {"attachmentId": "x", "size": 10}},
                    ],
                },
            },
            {
                "id": "m2",
                "internalDate": "1761033600000",
                "payload": {"mimeType": "text/plain", "headers": headers(from_="b@example.com"), "body": {"data": b64url("Thanks!")}},
            },
        ]
    )
    text = run(gm.GmailReadThread, ctx, thread_id="t1").content
    assert "Visible text" in text and "hidden instructions" not in text and "alert" not in text
    assert "Attachments: report.pdf" in text
    assert "--- message 2/2" in text and "Thanks!" in text


def test_read_thread_truncates_and_limits_messages(service, ctx):
    big = "x" * 20000
    service.responses["users.threads.get"] = thread_full(
        [
            {
                "id": f"m{i}",
                "internalDate": "1760947200000",
                "payload": {"mimeType": "text/plain", "headers": headers(from_="a@example.com"), "body": {"data": b64url(big)}},
            }
            for i in range(3)
        ]
    )
    text = run(gm.GmailReadThread, ctx, thread_id="t1", max_messages=2).content
    assert len(text) <= gm.THREAD_BODY_MAX_CHARS + 200
    assert "truncated" in text and "1 older/newer message(s) not shown" in text


def test_read_thread_validation_and_errors(service, ctx):
    with pytest.raises(ToolValidationError, match="thread_id"):
        run(gm.GmailReadThread, ctx, thread_id="../x")
    assert service.calls == []
    service.responses["users.threads.get"] = http_error(404, "Requested entity was not found.")
    with pytest.raises(ToolExecutionError, match="not found"):
        run(gm.GmailReadThread, ctx, thread_id="t404")
    service.responses["users.threads.get"] = http_error(401, "Invalid Credentials")
    with pytest.raises(PermissionMissingError, match="jarvis google login"):
        run(gm.GmailReadThread, ctx, thread_id="t1")


# --- draft ------------------------------------------------------------------------------------


def test_create_draft_builds_mime_and_calls_drafts_create(service, ctx):
    service.responses["users.drafts.create"] = {"id": "d123", "message": {"id": "m9"}}
    result = run(
        gm.GmailCreateDraft,
        ctx,
        to=["alice@example.com", " bob@example.org "],
        subject="Hello there",
        body="Line one\nLine two with ünïcode",
        cc=["c@example.com"],
    )
    path, kw = service.calls[0]
    assert path == "users.drafts.create" and kw["userId"] == "me"
    raw = kw["body"]["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=email.policy.default)
    assert msg["To"] == "alice@example.com, bob@example.org"
    assert msg["Cc"] == "c@example.com" and msg["Bcc"] is None
    assert msg["Subject"] == "Hello there"
    assert msg.get_content().rstrip("\n") == "Line one\nLine two with ünïcode"
    assert "DRAFT" in result.content and "not sent" in result.content and "[draft id d123]" in result.content
    assert "sent to" not in result.content.lower().replace("not sent", "")


def test_create_draft_accepts_comma_separated_to(service, ctx):
    service.responses["users.drafts.create"] = {"id": "d1"}
    run(gm.GmailCreateDraft, ctx, to="a@example.com, b@example.com", subject="s", body="b")
    raw = service.calls[0][1]["body"]["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=email.policy.default)
    assert msg["To"] == "a@example.com, b@example.com"


def test_create_draft_rejects_crlf_subject_before_any_call(service, ctx):
    with pytest.raises(ToolValidationError, match="header injection"):
        run(gm.GmailCreateDraft, ctx, to=["a@example.com"], subject="Hi\r\nBcc: evil@example.com", body="x")
    with pytest.raises(ToolValidationError, match="header injection"):
        run(gm.GmailCreateDraft, ctx, to=["a@example.com"], subject="Hi\nX", body="x")
    assert service.calls == []


def test_create_draft_rejects_invalid_addresses(service, ctx):
    for bad in (["not an email"], ["a@example.com\r\nbcc: x@y.com"], ["a@@example.com"], []):
        with pytest.raises(ToolValidationError):
            run(gm.GmailCreateDraft, ctx, to=bad, subject="s", body="b")
    with pytest.raises(ToolValidationError, match="invalid email"):
        run(gm.GmailCreateDraft, ctx, to=["a@example.com"], cc=["nope"], subject="s", body="b")
    with pytest.raises(ToolValidationError, match="invalid email"):
        run(gm.GmailCreateDraft, ctx, to=["a@example.com"], bcc=["x@"], subject="s", body="b")
    assert service.calls == []


def test_create_draft_api_failure(service, ctx):
    service.responses["users.drafts.create"] = http_error(403, "Request had insufficient authentication scopes.")
    with pytest.raises(ToolExecutionError, match="403"):
        run(gm.GmailCreateDraft, ctx, to=["a@example.com"], subject="s", body="b")
    service.responses["users.drafts.create"] = TimeoutError()
    with pytest.raises(ToolExecutionError, match="network error"):
        run(gm.GmailCreateDraft, ctx, to=["a@example.com"], subject="s", body="b")


# --- today's inbox for the daily brief ------------------------------------------------------------


def test_inbox_since_queries_epoch_seconds_and_returns_newest_first_with_labels(service):
    from datetime import datetime, timezone

    since = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    service.responses["users.messages.list"] = {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}], "nextPageToken": "p2"}
    details = {
        "m1": {
            "id": "m1",
            "internalDate": str(int(since.timestamp() * 1000) + 3_600_000),
            "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
            "payload": {"headers": headers(from_='"Ayşe Test" <ayse@example.com>', subject="Yarınki toplantı\r\n saati")},
        },
        "m2": {
            "id": "m2",
            "internalDate": str(int(since.timestamp() * 1000) + 7_200_000),
            "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
            "payload": {"headers": headers(from_="deals@shop.example", subject="x" * 500)},
        },
        "m3": {"id": "m3", "internalDate": "not a number", "payload": {"headers": headers(subject="broken")}},
    }
    service.responses["users.messages.get"] = lambda kw: details[kw["id"]]
    messages, more = gm.inbox_since(since)

    path, kw = service.calls[0]
    assert (
        path == "users.messages.list"
        and kw["q"] == f"in:inbox after:{int(since.timestamp())}"
        and kw["maxResults"] == gm.INBOX_MAX_MESSAGES
    )
    assert all(kw.get("format") == "metadata" for p, kw in service.calls[1:])  # never the body
    assert more is True and [m.sender for m in messages] == ["deals@shop.example", "Ayşe Test"]  # newest first; m3 skipped
    assert messages[1].subject == "Yarınki toplantı saati" and len(messages[0].subject) == gm.INBOX_SUBJECT_MAX_CHARS
    assert messages[0].bulk and not messages[1].bulk

    text = gm.format_inbox(messages, more, since, since)
    assert text.splitlines()[0].startswith("2+ message(s) in the inbox today (")
    assert "| from Ayşe Test | Yarınki toplantı saati [important, unread]" in text
    assert "deals@shop" not in text and text.endswith("+ 1 promotion/social message(s), not listed")
    assert gm.format_inbox([], False, since, since).startswith("No new mail in the inbox today (")
