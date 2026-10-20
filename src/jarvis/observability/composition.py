"""Prompt composition per API call: how many input tokens went to the persona,
the rules, the profile, the memory, the tool definitions, the conversation history, the tool results and
the user's own message, plus an estimate of the thinking tokens in the reply.

The stable blocks (persona, rules, profile, memory, tool definitions) are counted with the Anthropic
token-counting endpoint once per content version and model (free, separate rate limit; the count is an
estimate by Anthropic's own words) and cached in the `kv` table, so a block costs one request only when
it changes. The parts that change with every call (history, tool results, the user's message) are
estimated locally from their length and then scaled so that all components add up to the prompt tokens
the API actually billed. When the endpoint is unavailable everything is estimated locally.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

log = logging.getLogger("jarvis.composition")

PERSONA, RULES, PROFILE, MEMORY = "persona", "rules", "profile", "memory"
TOOLS, HISTORY, TOOL_RESULTS, USER_INPUT, THINKING = "tool_definitions", "conversation_history", "tool_results", "user_input", "thinking"
COMPONENTS = (PERSONA, RULES, PROFILE, MEMORY, TOOLS, HISTORY, TOOL_RESULTS, USER_INPUT)
SYSTEM_TAGS = {"<persona>": PERSONA, "<rules>": RULES, "<user_profile>": PROFILE, "<memory>": MEMORY}

CHARS_PER_TOKEN = 3.5  # Turkish text and JSON tokenize worse than English prose; the scaling step corrects the rest
IMAGE_TOKENS = 1200  # a screenshot's worth; the real cost depends on its size
KV_PREFIX = "tokcount:"
API_RETRY_AFTER_S = 120.0  # after a failed count request: estimate locally for this long before trying the API again
MAX_API_CALLS_PER_FLUSH = 8


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0


def text_of(content: Any) -> str:
    """Flatten a message's content (text, tool_use, tool_result, thinking, image blocks) into the text that
    the tokenizer would see; images become a fixed-size marker."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text_of(block) for block in content)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind == "text":
            return str(content.get("text", ""))
        if kind == "thinking":
            return str(content.get("thinking", ""))
        if kind == "tool_use":
            return str(content.get("name", "")) + " " + json.dumps(content.get("input", {}), ensure_ascii=False)
        if kind == "tool_result":
            return text_of(content.get("content"))
        if kind == "image":
            return "x" * int(IMAGE_TOKENS * CHARS_PER_TOKEN)
        return json.dumps(content, ensure_ascii=False)
    return str(content)


class TokenCounter:
    """Counts a text block or a tool list for a model, through the API when possible, cached forever per
    content hash (in memory and in the `kv` table when a database is given)."""

    def __init__(self, client: Any, db: Any = None, *, use_api: bool = True, clock=time.monotonic) -> None:
        self.client = client
        self.db = db
        self.use_api = use_api
        self.clock = clock
        self._memory: dict[str, int] = {}
        self._api_blocked_until: float = 0.0
        self.api_calls = 0

    # --- public ---

    def count_text(self, model: str, text: str, *, budget: list[int] | None = None) -> tuple[int, bool]:
        """(tokens, exact): exact is True when the number came from the API (now or cached)."""
        return self._count(model, "text", text, lambda: self._api_count(model, text=text), estimate_tokens(text), budget)

    def count_tools(self, model: str, tools: list[dict[str, Any]], *, budget: list[int] | None = None) -> tuple[int, bool]:
        client_tools = [t for t in tools if "input_schema" in t]  # server tools (web search) are rejected by the endpoint
        server_estimate = sum(estimate_tokens(json.dumps(t, ensure_ascii=False)) for t in tools if "input_schema" not in t)
        if not client_tools:
            return server_estimate, False
        payload = json.dumps(client_tools, ensure_ascii=False, sort_keys=True)
        tokens, exact = self._count(
            model, "tools", payload, lambda: self._api_count(model, tools=client_tools), estimate_tokens(payload), budget
        )
        return tokens + server_estimate, exact

    # --- internals ---

    def _key(self, model: str, kind: str, payload: str) -> str:
        digest = hashlib.sha256(f"{model}\0{kind}\0{payload}".encode("utf-8")).hexdigest()[:32]
        return f"{KV_PREFIX}{digest}"

    def _count(self, model, kind, payload, api_call, estimate, budget) -> tuple[int, bool]:
        key = self._key(model, kind, payload)
        cached = self._memory.get(key)
        if cached is None and self.db is not None:
            try:
                raw = self.db.kv_get(key)
                cached = int(raw) if raw is not None else None
            except Exception:
                cached = None
            if cached is not None:
                self._memory[key] = cached
        if cached is not None:
            return cached, True
        if not self._api_available() or (budget is not None and budget[0] <= 0):
            return estimate, False
        if budget is not None:
            budget[0] -= 1
        try:
            tokens = api_call()
        except Exception as exc:  # network, auth, rate limit: estimate for a while instead of blocking turns
            self._api_blocked_until = self.clock() + API_RETRY_AFTER_S
            log.info("token counting unavailable (%s: %s); estimating locally for %.0f s", type(exc).__name__, exc, API_RETRY_AFTER_S)
            return estimate, False
        self._memory[key] = tokens
        if self.db is not None:
            try:
                self.db.kv_set(key, str(tokens))
            except Exception:
                log.debug("could not cache the token count", exc_info=True)
        return tokens, True

    def _api_available(self) -> bool:
        if not self.use_api or self.client is None:
            return False
        messages = getattr(self.client, "messages", None)
        if messages is None or not callable(getattr(messages, "count_tokens", None)):
            return False
        return self.clock() >= self._api_blocked_until

    def _api_count(self, model: str, *, text: str | None = None, tools: list[dict[str, Any]] | None = None) -> int:
        self.api_calls += 1
        probe = [{"role": "user", "content": "."}]
        if tools is not None:
            with_tools = self.client.messages.count_tokens(model=model, tools=tools, messages=probe).input_tokens
            without = self.client.messages.count_tokens(model=model, messages=probe).input_tokens
            return max(0, int(with_tools) - int(without))
        return int(self.client.messages.count_tokens(model=model, messages=[{"role": "user", "content": text or "."}]).input_tokens)


def _system_blocks(system: Any) -> dict[str, str]:
    """The four XML-tagged system blocks by component; anything untagged counts as rules."""
    out: dict[str, str] = {}
    blocks = system if isinstance(system, list) else [{"type": "text", "text": str(system or "")}]
    for block in blocks:
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        component = RULES
        head = text.lstrip()[:20]
        for tag, name in SYSTEM_TAGS.items():
            if head.startswith(tag):
                component = name
                break
        out[component] = out.get(component, "") + text
    return out


def compose(
    params: dict[str, Any], prompt_tokens: int, message_content: Any, output_tokens: int, counter: TokenCounter | None
) -> dict[str, Any]:
    """The composition of one API call. `params` is the request (system, messages, tools, model);
    `prompt_tokens` the input tokens the API billed (uncached + cache reads + cache writes)."""
    model = str(params.get("model", ""))
    budget = [MAX_API_CALLS_PER_FLUSH]
    exact = True
    fixed: dict[str, int] = {}
    for component, text in _system_blocks(params.get("system")).items():
        tokens, is_exact = counter.count_text(model, text, budget=budget) if counter else (estimate_tokens(text), False)
        fixed[component] = fixed.get(component, 0) + tokens
        exact = exact and is_exact
    tools = params.get("tools") or []
    if tools:
        tokens, is_exact = counter.count_tools(model, tools, budget=budget) if counter else (estimate_tokens(json.dumps(tools)), False)
        fixed[TOOLS] = tokens
        exact = exact and is_exact

    messages = list(params.get("messages") or [])
    variable = {HISTORY: 0, TOOL_RESULTS: 0, USER_INPUT: 0}
    current_user_index = None
    if messages and messages[-1].get("role") == "user" and not _is_tool_result_message(messages[-1]):
        current_user_index = len(messages) - 1
    for index, message in enumerate(messages):
        content = message.get("content")
        if index == current_user_index:
            variable[USER_INPUT] += estimate_tokens(text_of(content))
            continue
        if isinstance(content, list):
            for block in content:
                target = TOOL_RESULTS if isinstance(block, dict) and block.get("type") == "tool_result" else HISTORY
                variable[target] += estimate_tokens(text_of(block))
        else:
            variable[HISTORY] += estimate_tokens(text_of(content))

    total = max(0, int(prompt_tokens or 0))
    fixed_sum = sum(fixed.values())
    if total and fixed_sum > total:  # the counts overshoot the bill (older cached blocks): shrink them to fit
        factor = total / fixed_sum
        fixed = {k: int(round(v * factor)) for k, v in fixed.items()}
        fixed_sum = sum(fixed.values())
        exact = False
    remaining = max(0, total - fixed_sum) if total else sum(variable.values())
    variable_sum = sum(variable.values())
    if variable_sum:
        scaled = {k: int(round(v * remaining / variable_sum)) for k, v in variable.items()}
    else:
        scaled = {HISTORY: 0, TOOL_RESULTS: 0, USER_INPUT: remaining}
    composition = {component: 0 for component in COMPONENTS}
    composition.update(fixed)
    composition.update(scaled)
    thinking = sum(estimate_tokens(text_of(b)) for b in (message_content or []) if isinstance(b, dict) and b.get("type") == "thinking")
    composition[THINKING] = min(thinking, int(output_tokens or 0)) if output_tokens else thinking
    composition["prompt_tokens"] = total
    composition["exact"] = bool(exact)
    return composition


def _is_tool_result_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
