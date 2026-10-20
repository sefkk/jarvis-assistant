"""Gmail tools: gmail_search_threads, gmail_read_thread, gmail_create_draft, and
`inbox_since`, the daily brief's list of what reached the inbox today.

OAuth scopes: gmail.readonly and gmail.compose. There is no send scope and no send function:
the most Jarvis can do is leave a draft in the Drafts folder for the user to review and send.
Mail content is untrusted, so every tool taints the turn.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from pydantic import BaseModel, Field, field_validator

from jarvis.exceptions import ToolValidationError
from jarvis.security.guards import validate_email, validate_header_value
from jarvis.security.sanitize import html_to_text
from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.utils import google_auth
from jarvis.utils.timeutil import format_local

THREAD_BODY_MAX_CHARS = 12000
MESSAGE_MAX_CHARS = 6000
SNIPPET_MAX_CHARS = 200
HEADER_MAX_CHARS = 300
SUBJECT_MAX_CHARS = 998
BODY_MAX_CHARS = 100_000
MAX_RECIPIENTS = 50
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
INBOX_MAX_MESSAGES = 30  # the brief lists at most this many of today's messages
INBOX_SUBJECT_MAX_CHARS = 120
# Gmail's own labels worth passing on; the categories tell a person's mail from bulk mail.
LABEL_TAGS = {
    "IMPORTANT": "important",
    "STARRED": "starred",
    "UNREAD": "unread",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_SOCIAL": "social",
}
BULK_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL")


# --- shared helpers ------------------------------------------------------------------------------


def _service() -> Any:
    return google_auth.build_service("gmail", "v1")


def _check_thread_id(thread_id: str) -> str:
    thread_id = (thread_id or "").strip()
    if not _THREAD_ID_RE.match(thread_id):
        raise ToolValidationError("thread_id is empty or malformed; use the id shown by gmail_search_threads")
    return thread_id


def _headers(message: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    payload = message.get("payload") or {}
    for h in payload.get("headers") or []:
        if isinstance(h, dict) and h.get("name"):
            name = str(h["name"]).lower()
            if name not in out:
                out[name] = str(h.get("value") or "")[:HEADER_MAX_CHARS]
    return out


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _when(message: dict[str, Any], headers: dict[str, str]) -> str:
    raw = message.get("internalDate")
    try:
        ms = int(raw)
        return format_local(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OverflowError, OSError):
        return _one_line(headers.get("date", ""))[:40]


def _decode_b64url(data: str) -> bytes:
    data = data.strip()
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def _charset(part: dict[str, Any]) -> str:
    for h in part.get("headers") or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == "content-type":
            m = re.search(r"charset=\"?([A-Za-z0-9_\-]+)\"?", str(h.get("value") or ""), re.IGNORECASE)
            if m:
                return m.group(1)
    return "utf-8"


def _part_text(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = _decode_b64url(str(data))
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _collect_parts(part: dict[str, Any], plain: list[str], html: list[str], attachments: list[str]) -> None:
    mime = str(part.get("mimeType") or "").lower()
    filename = str(part.get("filename") or "")
    if filename:
        attachments.append(filename[:120])
    elif mime == "text/plain":
        plain.append(_part_text(part))
    elif mime == "text/html":
        html.append(_part_text(part))
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _collect_parts(child, plain, html, attachments)


def extract_body(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """Message text and attachment names. text/plain wins; text/html is converted with html_to_text."""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    if isinstance(payload, dict):
        _collect_parts(payload, plain, html, attachments)
    text = "\n".join(p for p in plain if p.strip())
    if not text.strip() and html:
        text = "\n".join(html_to_text(h) for h in html if h.strip())
    return text.strip(), attachments


def _sender_name(sender: str) -> str:
    """'Alice Example <alice@example.com>' -> 'Alice Example'; a bare address stays as it is."""
    sender = _one_line(sender)
    name = sender.split("<", 1)[0].strip().strip('"').strip()
    return name or sender.strip("<> ")


@dataclass
class InboxMessage:
    received: datetime
    sender: str
    subject: str
    labels: list[str] = field(default_factory=list)

    @property
    def bulk(self) -> bool:
        return any(label in BULK_LABELS for label in self.labels)

    def line(self) -> str:
        tags = [LABEL_TAGS[label] for label in LABEL_TAGS if label in self.labels]
        tail = f" [{', '.join(tags)}]" if tags else ""
        return f"{format_local(self.received, with_date=False)} | from {self.sender} | {self.subject}{tail}"


def inbox_since(since: datetime, *, limit: int = INBOX_MAX_MESSAGES) -> tuple[list[InboxMessage], bool]:
    """The messages that reached the inbox after `since` (newest first, at most `limit`) and whether there
    are more. Gmail reads a plain date as midnight Pacific time, so the query carries epoch seconds."""
    service = _service()
    query = f"in:inbox after:{int(since.timestamp())}"
    listing = (
        google_auth.execute(service.users().messages().list(userId="me", q=query, maxResults=limit), "listing today's Gmail inbox") or {}
    )
    out: list[InboxMessage] = []
    for entry in (listing.get("messages") or [])[:limit]:
        message_id = str(entry.get("id") or "")
        if not message_id:
            continue
        detail = (
            google_auth.execute(
                service.users().messages().get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"]),
                "reading a Gmail message summary",
            )
            or {}
        )
        hdrs = _headers(detail)
        try:
            received = datetime.fromtimestamp(int(detail.get("internalDate")) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        subject = _one_line(hdrs.get("subject") or "(no subject)")[:INBOX_SUBJECT_MAX_CHARS]
        out.append(
            InboxMessage(
                received,
                _sender_name(hdrs.get("from", "")) or "(unknown sender)",
                subject,
                [str(label) for label in detail.get("labelIds") or []],
            )
        )
    out.sort(key=lambda m: m.received, reverse=True)
    return out, bool(listing.get("nextPageToken"))


def format_inbox(messages: list[InboxMessage], more: bool, since: datetime, until: datetime) -> str:
    """The brief's mail section: one line per message; promotions and social mail only counted."""
    span = f"{format_local(since, with_date=False)}-{format_local(until, with_date=False)}"
    if not messages:
        return f"No new mail in the inbox today ({span})."
    listed = [m for m in messages if not m.bulk]
    bulk = len(messages) - len(listed)
    head = f"{len(messages)}{'+' if more else ''} message(s) in the inbox today ({span}), newest first:"
    lines = [head, *(m.line() for m in listed)]
    if bulk:
        lines.append(f"+ {bulk} promotion/social message(s), not listed")
    return "\n".join(lines)


# --- tools --------------------------------------------------------------------------------------


class SearchThreadsInput(BaseModel):
    query: str = Field(description="Gmail search query, e.g. 'from:alice newer_than:7d' or 'invoice is:unread'.")
    max_results: int = Field(default=10, ge=1, le=50, description="Maximum number of threads.")


class GmailSearchThreads(Tool):
    name = "gmail_search_threads"
    description = (
        "Search the user's Gmail with Gmail query syntax. Returns one line per thread with its id, "
        "date, sender, subject and a snippet. Use gmail_read_thread to read one."
    )
    input_model = SearchThreadsInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "gmail"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, SearchThreadsInput)
        self.require_ctx()
        query = args.query.strip()
        if not query or "\x00" in query:
            raise ToolValidationError("query is empty")
        service = _service()
        listing = google_auth.execute(
            service.users().threads().list(userId="me", q=query, maxResults=args.max_results),
            "searching Gmail",
        )
        threads = (listing or {}).get("threads") or []
        if not threads:
            return ToolResult(f"No Gmail threads match {query!r}.")
        lines: list[str] = []
        for entry in threads[: args.max_results]:
            thread_id = str(entry.get("id") or "")
            if not thread_id:
                continue
            detail = google_auth.execute(
                service.users().threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=["Subject", "From", "Date"]),
                "reading a Gmail thread summary",
            )
            messages = [m for m in (detail or {}).get("messages") or [] if isinstance(m, dict)]
            first = messages[0] if messages else {}
            last = messages[-1] if messages else {}
            first_headers, last_headers = _headers(first), _headers(last)
            subject = _one_line(first_headers.get("subject") or last_headers.get("subject") or "(no subject)")
            sender = _one_line(last_headers.get("from", "")) or "(unknown sender)"
            snippet = _one_line(str(last.get("snippet") or entry.get("snippet") or ""))[:SNIPPET_MAX_CHARS]
            count = f", {len(messages)} messages" if len(messages) > 1 else ""
            lines.append(f"[thread {thread_id}] {_when(last, last_headers)} | from {sender} | {subject}{count}")
            if snippet:
                lines.append(f"    {snippet}")
        more = " (more available: raise max_results or refine the query)" if listing.get("nextPageToken") else ""
        return ToolResult(f"{len(threads)} Gmail thread(s) for {query!r}{more}:\n" + "\n".join(lines))


class ReadThreadInput(BaseModel):
    thread_id: str = Field(description="The thread id from gmail_search_threads.")
    max_messages: int = Field(default=10, ge=1, le=50, description="Read at most this many messages (oldest first).")


class GmailReadThread(Tool):
    name = "gmail_read_thread"
    description = "Read the messages of a Gmail thread (headers and text body, attachments listed by name)."
    input_model = ReadThreadInput
    risk = RiskLevel.SAFE
    delegate_ok = True
    taints = True
    taint_source = "gmail"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ReadThreadInput)
        ctx = self.require_ctx()
        cap = min(THREAD_BODY_MAX_CHARS, ctx.config.context.tool_result_max_chars)
        thread_id = _check_thread_id(args.thread_id)
        detail = google_auth.execute(_service().users().threads().get(userId="me", id=thread_id, format="full"), "reading the Gmail thread")
        messages = [m for m in (detail or {}).get("messages") or [] if isinstance(m, dict)]
        if not messages:
            return ToolResult(f"Thread {thread_id} has no messages.")
        shown = messages[: args.max_messages]
        chunks: list[str] = []
        for i, message in enumerate(shown, 1):
            headers = _headers(message)
            body, attachments = extract_body(message.get("payload") or {})
            if len(body) > MESSAGE_MAX_CHARS:
                body = body[:MESSAGE_MAX_CHARS] + f"\n[message truncated to {MESSAGE_MAX_CHARS} characters]"
            head = [f"--- message {i}/{len(messages)} | {_when(message, headers)}"]
            for name in ("from", "to", "cc", "subject"):
                if headers.get(name):
                    head.append(f"{name.capitalize()}: {_one_line(headers[name])}")
            if attachments:
                head.append("Attachments: " + ", ".join(attachments))
            chunks.append("\n".join(head) + "\n\n" + (body or "(no text body)"))
        text = f"Gmail thread {thread_id} ({len(messages)} message(s)):\n" + "\n\n".join(chunks)
        notes: list[str] = []
        if len(messages) > len(shown):
            notes.append(f"{len(messages) - len(shown)} older/newer message(s) not shown; raise max_messages")
        if len(text) > cap:
            text = text[:cap]
            notes.append(f"output truncated to {cap} characters")
        if notes:
            text += "\n[" + "; ".join(notes) + "]"
        return ToolResult(text)


class CreateDraftInput(BaseModel):
    to: list[str] = Field(description="Recipient email addresses.")
    subject: str = Field(description="Subject line (one line).")
    body: str = Field(description="Plain-text body.")
    cc: list[str] | None = Field(default=None, description="Optional Cc addresses.")
    bcc: list[str] | None = Field(default=None, description="Optional Bcc addresses.")

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value


class GmailCreateDraft(Tool):
    name = "gmail_create_draft"
    description = (
        "Create a DRAFT email in the user's Gmail Drafts folder. Nothing is sent: the user reviews and sends it "
        "in Gmail. Asks for confirmation."
    )
    input_model = CreateDraftInput
    risk = RiskLevel.CONFIRM
    taints = True
    taint_source = "gmail"

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CreateDraftInput)
        self.require_ctx()
        to = [validate_email(a) for a in args.to]
        cc = [validate_email(a) for a in (args.cc or [])]
        bcc = [validate_email(a) for a in (args.bcc or [])]
        if not to:
            raise ToolValidationError("at least one 'to' address is required")
        if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
            raise ToolValidationError(f"more than {MAX_RECIPIENTS} recipients")
        subject = validate_header_value("subject", args.subject).strip()
        if len(subject) > SUBJECT_MAX_CHARS:
            raise ToolValidationError("subject is too long")
        if "\x00" in args.body:
            raise ToolValidationError("body contains a NUL byte")
        if len(args.body) > BODY_MAX_CHARS:
            raise ToolValidationError(f"body longer than {BODY_MAX_CHARS} characters")

        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        msg.set_content(args.body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        created = google_auth.execute(
            _service().users().drafts().create(userId="me", body={"message": {"raw": raw}}),
            "creating the Gmail draft",
        )
        created = created if isinstance(created, dict) else {}
        draft_id = str(created.get("id") or "")
        ident = f" [draft id {draft_id}]" if draft_id else ""
        extra = (f"; cc {len(cc)}" if cc else "") + (f"; bcc {len(bcc)}" if bcc else "")
        return ToolResult(
            f"Created a DRAFT (not sent) to {', '.join(to)}{extra} with subject '{subject}'.{ident} "
            "It is in the Gmail Drafts folder for the user to review and send."
        )
