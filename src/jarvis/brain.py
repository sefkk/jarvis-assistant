"""Claude API loop: streaming, tool calls, own retries, iteration cap.

The Brain does not own history. The caller passes the `messages` list and the Brain
appends assistant turns and tool results to it in place, so that a thinking block is
always kept exactly as the API returned it and a tool_use is never split from its
tool_result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from jarvis.config import Config
from jarvis.exceptions import (
    BudgetExceededError,
    JarvisAPIError,
    PermissionMissingError,
    ToolBlockedError,
    ToolDeniedError,
    ToolError,
    ToolExecutionError,
    ToolValidationError,
)
from jarvis.i18n import t
from jarvis.io.base import OutputSink, compose_reply
from jarvis.observability import composition, logging_setup
from jarvis.observability.composition import TokenCounter
from jarvis.observability.db import Database
from jarvis.observability.timing import TurnTimer
from jarvis.observability.usage import TokenUsage, UsageTracker, classify_error
from jarvis.security import confirm, sanitize
from jarvis.security.taint import TaintState
from jarvis.tools.registry import ToolRegistry
from jarvis.utils.timeutil import monotonic_ms

log = logging.getLogger("jarvis.brain")

RESULT_SUMMARY_CHARS = 300
# Result blocks of Anthropic's server-side web tools; their content came from the open web.
SERVER_RESULT_BLOCKS = ("web_search_tool_result", "web_fetch_tool_result")
# Every other content block (tool_use, server_tool_use, a server tool result) splits the reply text.
_TEXT_BLOCK_TYPES = ("", "text", "thinking", "redacted_thinking")
# When thinking is on, the reasoning shares max_tokens with the visible reply.
THINKING_MIN_OUTPUT_TOKENS = 4096

# Thinking "on" is adaptive on every current model; `display` makes the summary available so
# it can be stored in SQLite and shown in --debug. Thinking "off" is spelled differently per
# model family, and some families cannot turn it off at all.
THINKING_ON: dict[str, Any] = {"type": "adaptive", "display": "summarized"}
# model id prefix -> the parameter that means "off" (None = omit `thinking`, which on these
# older models means no thinking)
THINKING_OFF_BY_FAMILY: list[tuple[str, dict[str, Any] | None]] = [
    ("claude-haiku-5-5", {"type": "disabled"}),
    ("claude-sonnet-5-5", {"type": "between_tools"}),
]
# Families where thinking is always on: "off" falls back to adaptive with a warning.
THINKING_ALWAYS_ON = ("claude-opus-5-5", "claude-fable", "claude-mythos")


def resolve_thinking_param(model: str, thinking: bool) -> dict[str, Any]:
    """Translate the config boolean into the `thinking` parameter this model accepts."""
    if thinking:
        return dict(THINKING_ON)
    for prefix, param in THINKING_OFF_BY_FAMILY:
        if model.startswith(prefix):
            return dict(param) if param else {}
    if model.startswith(THINKING_ALWAYS_ON):
        log.warning("%s cannot turn thinking off; running with adaptive thinking (lower the effort instead)", model)
        return dict(THINKING_ON)
    return {}


@dataclass
class ApiCallResult:
    message: anthropic.types.Message
    attempts: int
    duration_ms: float
    ttft_ms: float | None


@dataclass
class TurnOutcome:
    text: str = ""
    tools_used: list[str] = field(default_factory=list)
    taint: TaintState = field(default_factory=TaintState)
    api_calls: int = 0
    stop_reason: str | None = None
    hit_iteration_cap: bool = False
    usage: TokenUsage = field(default_factory=TokenUsage)
    last_prompt_tokens: int = 0  # size of the whole prompt on the last call; drives compaction
    first_prompt_tokens: int = 0  # size of the whole prompt on the turn's first call

    @property
    def tainted(self) -> bool:
        return self.taint.tainted

    @property
    def taint_sources(self) -> list[str]:
        return self.taint.sources


class Brain:
    def __init__(
        self,
        client: anthropic.Anthropic,
        config: Config,
        registry: ToolRegistry,
        db: Database,
        usage_tracker: UsageTracker,
        *,
        model: str | None = None,
        thinking: bool | None = None,
        effort: str | None = None,
        max_output_tokens: int | None = None,
        is_delegate: bool = False,
        debug: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.client = client
        self.config = config
        self.registry = registry
        self.db = db
        self.usage_tracker = usage_tracker
        self.model = model or config.models.main_model
        self.thinking = config.models.main_thinking if thinking is None else thinking
        self.effort = effort or config.models.main_effort
        self.max_output_tokens = max_output_tokens or config.models.max_output_tokens
        self.is_delegate = is_delegate
        self.debug = debug
        self._sleep = sleep
        self._rng = rng
        self._image_refs: dict[str, dict[str, Any]] = {}
        self._pending_compositions: list[tuple[int, dict[str, Any], TokenUsage, list[dict[str, Any]]]] = []
        self.counter = TokenCounter(client, db)  # prompt composition
        self.thinking_param = resolve_thinking_param(self.model, self.thinking)
        self.effective_max_tokens = self.max_output_tokens
        if self.thinking_param.get("type") == "adaptive" and self.max_output_tokens < THINKING_MIN_OUTPUT_TOKENS:
            # Thinking tokens count against max_tokens; leave room for the actual reply.
            self.effective_max_tokens = THINKING_MIN_OUTPUT_TOKENS
            log.warning(
                "%s: thinking is on, raising max_tokens from %d to %d so reasoning cannot swallow the reply",
                self.model,
                self.max_output_tokens,
                THINKING_MIN_OUTPUT_TOKENS,
            )

    # --- request assembly ---------------------------------------------------------------

    def _request_params(
        self,
        system: Any,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.effective_max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": self.effort},
            # Automatic caching of the conversation tail; the explicit breakpoints live in `system`.
            "cache_control": {"type": "ephemeral"},
        }
        if self.thinking_param:
            params["thinking"] = self.thinking_param
        tools = self.registry.api_tools() if tools is None else tools
        if tools:
            params["tools"] = [_server_tool_for_model(t, self.model) for t in tools]
        if tool_choice:
            params["tool_choice"] = tool_choice
        return params

    # --- one API call with retries ------------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
            return True
        if isinstance(exc, anthropic.APIStatusError):
            return exc.status_code == 429 or exc.status_code >= 500
        return False

    def _backoff_seconds(self, attempt: int) -> float:
        base = self.config.loop.api_backoff_base_seconds
        return min(base * (2 ** (attempt - 1)) + self._rng() * base, 30.0)

    @staticmethod
    def timeout_phase(exc: Exception, *, ttft_ms: float | None) -> str | None:
        """Which of our own limits an `APITimeoutError` hit (None for other errors).

        connect: no TCP + TLS connection within api_connect_timeout_s (the request never left);
        first_token: connected and sent, but nothing came back within api_read_timeout_s;
        stall: the reply had started and then went silent for api_read_timeout_s;
        write: the request body could not be sent; pool: no free connection in time.
        """
        if not isinstance(exc, anthropic.APITimeoutError):
            return None
        cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else ""
        if "ConnectTimeout" in cause:
            return "connect"
        if "ReadTimeout" in cause:
            return "stall" if ttft_ms is not None else "first_token"
        if "WriteTimeout" in cause:
            return "write"
        if "PoolTimeout" in cause:
            return "pool"
        return "stall" if ttft_ms is not None else "connect"

    def stream_message(
        self,
        *,
        system: Any,
        messages: list[dict[str, Any]],
        sink: OutputSink,
        turn_id: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> ApiCallResult:
        params = self._request_params(system, messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens)
        max_attempts = self.config.loop.api_max_attempts
        if self.debug:
            # --debug: full payloads, visible whatever the configured log level.
            log.info("api request", extra={"data": {"params": params}})

        written = ""  # text already shown to the user; a retry must not show it twice
        loop_cfg = self.config.loop
        for attempt in range(1, max_attempts + 1):
            start = monotonic_ms()
            ttft: float | None = None
            last_event = start  # the stalled-stream clock: silence since the last event (or the start)
            events = 0
            streamed = ""
            diverged = False
            try:
                with self.client.messages.stream(**params) as stream:
                    for event in stream:
                        events += 1
                        last_event = monotonic_ms()
                        if event.type == "text":
                            if ttft is None:
                                ttft = monotonic_ms() - start
                            streamed += event.text
                            if not written:
                                sink.write_delta(event.text)
                            elif diverged:
                                sink.write_delta(event.text)
                            elif written.startswith(streamed):
                                pass  # still inside the text the user already saw
                            elif streamed.startswith(written):
                                sink.write_delta(streamed[len(written) :])  # the new part only
                                written = streamed
                            else:
                                diverged = True
                                sink.notice(t("cli.notice.reply_restarted"))
                                sink.write_delta(streamed)
                        elif event.type == "thinking":
                            if ttft is None:
                                ttft = monotonic_ms() - start
                            if event.thinking:
                                sink.debug(event.thinking)
                        elif event.type == "content_block_start":
                            if ttft is None:
                                ttft = monotonic_ms() - start
                            block_type = getattr(getattr(event, "content_block", None), "type", "")
                            # A tool block ends a text segment; a retry replaying text the user saw
                            # already must not end it a second time.
                            if block_type not in _TEXT_BLOCK_TYPES and (not written or diverged or len(streamed) > len(written)):
                                sink.segment_break()
                    message = stream.get_final_message()
            except Exception as exc:  # classified below
                now = monotonic_ms()
                duration = now - start
                status_code = getattr(exc, "status_code", None)
                error_type = getattr(exc, "type", None) or type(exc).__name__
                request_id = getattr(exc, "request_id", None)
                retryable = self._is_retryable(exc)
                phase = self.timeout_phase(exc, ttft_ms=ttft)
                self.usage_tracker.record(
                    turn_id,
                    model=self.model,
                    attempt=attempt,
                    status=f"error:{status_code or error_type}" + (f":{phase}" if phase else ""),
                    duration_ms=duration,
                    ttft_ms=ttft,
                    usage=None,
                    is_delegate=self.is_delegate,
                    error_category=classify_error(exc, phase),
                    request_id=str(request_id) if request_id else None,
                    final_failure=not retryable or attempt >= max_attempts,
                )
                log.warning(
                    "api call failed (attempt %d/%d, retryable=%s%s): %s",
                    attempt,
                    max_attempts,
                    retryable,
                    f", timeout phase={phase}" if phase else "",
                    exc,
                    extra={
                        "data": {
                            "status_code": status_code,
                            "error_type": error_type,
                            "request_id": request_id,
                            "exception": type(exc).__name__,
                            "cause": type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
                            "phase": phase,
                            "duration_ms": round(duration),
                            "ttft_ms": round(ttft) if ttft is not None else None,
                            "events": events,
                            "silent_ms": round(now - last_event),
                            "timeouts_s": {"connect": loop_cfg.api_connect_timeout_s, "read": loop_cfg.api_read_timeout_s},
                        }
                    },
                )
                if not isinstance(exc, anthropic.APIError):
                    raise  # not an API failure (e.g. a bug in the sink): surface it
                if not retryable or attempt >= max_attempts:
                    raise JarvisAPIError(
                        f"{error_type}: {getattr(exc, 'message', str(exc))}",
                        status_code=status_code,
                        error_type=str(error_type),
                        request_id=request_id,
                        attempts=attempt,
                        phase=phase,
                        connect_timeout_s=loop_cfg.api_connect_timeout_s,
                        read_timeout_s=loop_cfg.api_read_timeout_s,
                    ) from exc
                delay = self._backoff_seconds(attempt)
                retry = {"delay": f"{delay:.1f}", "attempt": attempt + 1, "max": max_attempts}
                if phase:
                    limit = loop_cfg.api_connect_timeout_s if phase in ("connect", "pool") else loop_cfg.api_read_timeout_s
                    sink.notice(t("cli.notice.retry_timeout", phase=phase, limit=f"{limit:g}", **retry))
                else:
                    sink.notice(t("cli.notice.retry_error", detail=status_code or error_type, **retry))
                if streamed and not diverged:
                    written = max(written, streamed, key=len)
                self._sleep(delay)
                continue

            duration = monotonic_ms() - start
            usage = TokenUsage.from_api(message.usage)
            self.usage_tracker.record(
                turn_id,
                model=self.model,
                attempt=attempt,
                status="ok",
                duration_ms=duration,
                ttft_ms=ttft,
                usage=usage,
                is_delegate=self.is_delegate,
                request_id=str(getattr(message, "_request_id", None) or "") or None,
            )
            call_id = getattr(self.usage_tracker, "last_call_id", None)
            if call_id is not None:
                # Analyzed after the turn (`flush_compositions`), never on the streaming path.
                self._pending_compositions.append(
                    (call_id, {**params, "messages": list(params["messages"])}, usage, [_block_to_param(b) for b in message.content])
                )
            if self.debug:
                log.info(
                    "api response",
                    extra={
                        "data": {
                            "message": message.model_dump(mode="json"),
                            "request_id": getattr(message, "_request_id", None),
                        }
                    },
                )
            return ApiCallResult(message=message, attempts=attempt, duration_ms=duration, ttft_ms=ttft)
        raise AssertionError("unreachable")

    # --- the tool loop ----------------------------------------------------------------------

    def run_turn(
        self,
        *,
        system: Any,
        messages: list[dict[str, Any]],
        sink: OutputSink,
        turn_id: str,
        timer: TurnTimer,
        user_text: str = "",
        taint: TaintState | None = None,
    ) -> TurnOutcome:
        outcome = TurnOutcome()
        if taint is not None:
            # A sub-run (delegate) inherits the taint of the turn it runs in; a main turn can
            # also start tainted (a forwarded Telegram message).
            outcome.taint = TaintState(tainted=taint.tainted, sources=list(taint.sources))
        if self._runtime() is not None and not self.is_delegate:
            # Expose the main turn's taint to tools (the delegate inherits it from here),
            # whether or not the turn started tainted.
            self._runtime().taint = outcome.taint
        if outcome.taint.tainted:
            _notify_taint(sink, outcome.taint)
        max_iter = self.config.loop.max_tool_iterations
        segments: list[str] = [""]  # the turn's text, split at every tool block (io.base.compose_reply)

        for iteration in range(1, max_iter + 1):
            with timer.stage("api_call"):
                result = self.stream_message(system=system, messages=messages, sink=sink, turn_id=turn_id)
            if result.ttft_ms is not None:
                timer.record("ttft", result.ttft_ms)
            message = result.message
            outcome.api_calls += 1
            outcome.stop_reason = message.stop_reason
            _accumulate(outcome.usage, message.usage)
            outcome.last_prompt_tokens = TokenUsage.from_api(message.usage).prompt_tokens
            if iteration == 1:
                outcome.first_prompt_tokens = outcome.last_prompt_tokens

            content = [_block_to_param(block) for block in message.content]
            messages.append({"role": "assistant", "content": content})
            self._save_message(turn_id, "assistant", content)
            for block in message.content:
                if block.type == "text":
                    segments[-1] += block.text
                elif block.type not in _TEXT_BLOCK_TYPES and segments[-1]:
                    segments.append("")
            self._note_server_tools(message, outcome, sink)

            tool_uses = [block for block in message.content if block.type == "tool_use"]
            if message.stop_reason == "refusal":
                sink.notice(t("cli.notice.refusal"))
                break
            if message.stop_reason == "max_tokens":
                sink.notice(t("cli.notice.cut_off_tool" if tool_uses else "cli.notice.cut_off"))
                if tool_uses:
                    # Every tool_use needs a tool_result or the next request is rejected.
                    cut = [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "not executed: the response was cut off at max_tokens",
                            "is_error": True,
                        }
                        for block in tool_uses
                    ]
                    messages.append({"role": "user", "content": cut})
                    self._save_message(turn_id, "user", cut)
                break
            if message.stop_reason == "pause_turn":
                continue
            if not tool_uses:
                break

            results = [self._execute_tool(block, sink, turn_id, timer, outcome, user_text) for block in tool_uses]
            messages.append({"role": "user", "content": results})
            self._save_message(turn_id, "user", results)

            if iteration == max_iter:
                outcome.hit_iteration_cap = True
                sink.notice(t("cli.notice.iteration_cap", max=max_iter))
                break

        outcome.text = compose_reply(segments)
        self.flush_compositions()
        return outcome

    def flush_compositions(self) -> int:
        """Store the prompt composition of every API call made since the last flush (run_turn calls this at
        its end, so the token counting never delays the reply). Never raises. Returns the rows written."""
        pending, self._pending_compositions = self._pending_compositions, []
        written = 0
        for call_id, params, usage, content in pending:
            try:
                result = composition.compose(params, usage.prompt_tokens, content, usage.output_tokens, self.counter)
                self.db.set_api_call_composition(call_id, result, result.get(composition.THINKING))
                written += 1
            except Exception:  # analytics must never break a turn
                log.warning("prompt composition for api call %s failed", call_id, exc_info=True)
        return written

    def _note_server_tools(self, message, outcome: TurnOutcome, sink: OutputSink) -> None:
        """Server-side tools (web search) run on Anthropic's side; their results are untrusted."""
        for block in message.content:
            if block.type == "server_tool_use":
                outcome.tools_used.append(block.name)
                sink.notice(f"• {block.name} (server)")
                logging_setup.audit(
                    {
                        "tool": block.name,
                        "args": getattr(block, "input", None),
                        "risk": "SAFE",
                        "tainted": outcome.taint.tainted,
                        "taint_sources": list(outcome.taint.sources),
                        "taints_turn": True,
                        "decision": "server",
                        "duration_ms": None,
                        "result_summary": None,
                        "is_delegate": self.is_delegate,
                    }
                )
            elif block.type in SERVER_RESULT_BLOCKS:
                outcome.taint.mark("web")
                _notify_taint(sink, outcome.taint)

    def _runtime(self):
        """The Runtime handle shared through the tools' context, if any."""
        for tool in self.registry:
            if tool.ctx is not None:
                return tool.ctx.runtime
        return None

    def _save_message(self, turn_id: str, role: str, content: Any) -> None:
        if self.config.general.save_transcripts:
            if not self.config.screen.store_images:
                content = strip_image_data(content, self._image_refs)
            self.db.add_message(turn_id, role, content)

    # --- tool execution -------------------------------------------------------------------

    def _execute_tool(
        self,
        block,
        sink: OutputSink,
        turn_id: str,
        timer: TurnTimer,
        outcome: TurnOutcome,
        user_text: str = "",
    ) -> dict[str, Any]:
        name = block.name
        raw_args = block.input if isinstance(block.input, dict) else {}
        tool = self.registry.get(name)
        risk = tool.risk.value if tool else "UNKNOWN"
        taints = bool(tool and tool.taints)
        taint = outcome.taint
        # The audit line records the state under which the decision was made.
        tainted_before, sources_before = taint.tainted, list(taint.sources)
        decision = "error"
        error: str | None = None
        content = ""
        is_error = False
        images: list = []
        start = monotonic_ms()

        try:
            if tool is None:
                raise ToolValidationError(f"unknown tool: {name}")
            args = tool.parse_args(raw_args)
            # Risk and taint can depend on the arguments (a Shortcut's allowlist entry).
            risk = tool.risk_for(args).value
            taints = tool.taints_for(args)
            decision = confirm.gate(tool, args, taint=taint, sink=sink, user_text=user_text)
            sink.notice(f"• {name}")
            if taints:
                # Whatever comes back (content or an error quoting it) came from an untrusted source.
                taint.mark(tool.taint_source)
                _notify_taint(sink, taint)
            with timer.stage(f"tool:{name}"):
                result = tool.run(args)
            is_error = result.is_error
            images = list(result.images)
            # Every result is normalized (NFKC, invisible and control characters removed);
            # untrusted ones are additionally capped and wrapped.
            content = self._truncate(sanitize.for_model(result.content))
            if taints:
                content = sanitize.wrap_untrusted(content, tool.taint_source)
        except ToolValidationError as exc:
            decision, error, is_error = "validation_error", str(exc), True
        except ToolBlockedError as exc:
            decision, error, is_error = "blocked", str(exc), True
        except ToolDeniedError as exc:
            decision, error, is_error = "denied", str(exc), True
        except (ToolError, PermissionMissingError, BudgetExceededError) as exc:
            decision, error, is_error = "error", str(exc), True
        except Exception as exc:  # tools never crash Jarvis
            log.exception("tool %s crashed", name)
            wrapped = ToolExecutionError(f"{name} failed: {type(exc).__name__}: {exc}")
            decision, error, is_error = "error", str(wrapped), True

        duration = monotonic_ms() - start
        if error:
            # Error text can quote external input (paths, stderr); keep it clean but unwrapped.
            content = self._truncate(sanitize.for_model(error))

        outcome.tools_used.append(name)
        self.db.add_tool_call(
            turn_id,
            tool=name,
            args=raw_args,
            risk=risk,
            decision=decision,
            duration_ms=duration,
            error=error,
            is_delegate=self.is_delegate,
        )
        logging_setup.audit(
            {
                "tool": name,
                "args": raw_args,
                "risk": risk,
                "tainted": tainted_before,
                "taint_sources": sources_before,
                # true whenever a tainting tool actually ran, even if it then failed (its error can
                # quote outside content), matching when `taint.mark` is called above
                "taints_turn": bool(taints and decision not in ("validation_error", "blocked", "denied")),
                "decision": decision,
                "duration_ms": round(duration, 1),
                "result_summary": {"length": len(content), "head": content[:RESULT_SUMMARY_CHARS]},
                "is_delegate": self.is_delegate,
            }
        )
        if self.debug:
            sink.debug(f"{name} -> {decision} in {duration:.0f} ms: {content[:200]}")

        tool_result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if images:
            # Image first, then the text (the model reads images best before the text).
            tool_result["content"] = [*(img.block() for img in images), {"type": "text", "text": content}]
            for img in images:  # what the transcript keeps instead of the pixels
                self._image_refs[_b64_key(img.data_b64) or ""] = img.reference()
        if is_error:
            tool_result["is_error"] = True
        return tool_result

    def _truncate(self, content: str) -> str:
        limit = self.config.context.tool_result_max_chars
        if len(content) > limit:
            return content[:limit] + f"\n[truncated: {len(content) - limit} more characters]"
        return content


def _notify_taint(sink: OutputSink, taint: TaintState) -> None:
    """Tell the sink the turn is tainted. A sink bug must not stop the turn."""
    try:
        sink.taint_changed(list(taint.sources))
    except Exception:
        log.exception("sink.taint_changed failed")


def _b64_key(data: Any) -> str | None:
    return hashlib.sha256(data.encode("ascii", "ignore")).hexdigest() if isinstance(data, str) else None


def strip_image_data(content: Any, references: dict[str, dict[str, Any]] | None = None) -> Any:
    """Replace base64 image blocks (anywhere in a message content tree) with a small reference.

    `references` maps the base64 text's hash to the attachment's own reference (raw-byte sha256,
    width, height); without it a generic reference is stored.
    """
    if isinstance(content, list):
        return [strip_image_data(c, references) for c in content]
    if isinstance(content, dict):
        if content.get("type") == "image" and isinstance(content.get("source"), dict):
            source = content["source"]
            key = _b64_key(source.get("data", ""))
            if references and key in references:
                return dict(references[key])
            return {"type": "image_ref", "media_type": source.get("media_type"), "sha256": key, "width": None, "height": None}
        return {k: strip_image_data(v, references) for k, v in content.items()}
    return content


def _server_tool_for_model(tool: dict[str, Any], model: str) -> dict[str, Any]:
    """Server tool versions depend on the model that sends the request (the delegate may differ)."""
    if tool.get("name") == "web_search" and str(tool.get("type", "")).startswith("web_search_"):
        from jarvis.tools.web import web_search_tool_type

        return {**tool, "type": web_search_tool_type(model)}
    return tool


def _block_to_param(block) -> dict[str, Any]:
    """A response block as a request param. Thinking blocks are passed back unchanged."""
    return block.model_dump(mode="json", exclude_none=True)


def _accumulate(total: TokenUsage, usage) -> None:
    part = TokenUsage.from_api(usage)
    total.input_tokens += part.input_tokens
    total.output_tokens += part.output_tokens
    total.cache_read_tokens += part.cache_read_tokens
    total.cache_write_tokens += part.cache_write_tokens


def pretty_args(args: dict[str, Any]) -> str:
    return json.dumps(args, indent=2, ensure_ascii=False)
