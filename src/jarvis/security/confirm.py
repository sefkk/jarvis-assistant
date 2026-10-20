"""Confirmation flow. Decides whether a tool call needs approval and asks."""

from __future__ import annotations

from pydantic import BaseModel

from jarvis.exceptions import ToolBlockedError, ToolDeniedError
from jarvis.io.base import ConfirmRequest, OutputSink
from jarvis.security.taint import TaintState
from jarvis.tools.base import RiskLevel, Tool


def gate(tool: Tool, args: BaseModel, *, taint: TaintState, sink: OutputSink, user_text: str) -> str:
    """Return the audit decision ("ran" or "confirmed") or raise ToolBlockedError / ToolDeniedError.

    Confirmation is required when the tool is CONFIRM, or when the turn is tainted and the tool
    says the call is sensitive in that situation (for example `remember`, or `fetch_page` with
    a URL the user never mentioned).
    """
    risk = tool.risk_for(args)
    if risk == RiskLevel.BLOCKED:
        raise ToolBlockedError(f"{tool.name} is blocked")

    needs_confirmation = risk == RiskLevel.CONFIRM
    reason: str | None = None
    if taint.tainted:
        reason = tool.tainted_confirm_reason(args, user_text)
        if reason:
            needs_confirmation = True
    if not needs_confirmation:
        return "ran"

    request = ConfirmRequest(
        tool=tool.name,
        args=args.model_dump(mode="json"),
        risk=risk.value,
        tainted=taint.tainted,
        taint_sources=list(taint.sources),
        taint_warning=taint.warning() if taint.tainted else None,
        reason=reason,
        command_preview=tool.confirm_preview(args),
    )
    outcome = sink.confirm_detailed(request)
    if not outcome.approved:
        # The model sees the real decision and reason (and the user's answer), so it can report
        # it exactly instead of a bare "not approved" (rules.md, Tools and honesty).
        raise ToolDeniedError(outcome.model_text(tool.name))
    return "confirmed"
