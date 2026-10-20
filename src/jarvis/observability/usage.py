"""Token and cost accounting.

Every API call is recorded in `usage.jsonl` and SQLite. The daily budget is informational: crossing the
warning line and the budget each prints one notice; nothing is switched off and Jarvis keeps answering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from jarvis.config import Config
from jarvis.i18n import t
from jarvis.observability import logging_setup
from jarvis.observability.db import Database
from jarvis.utils.timeutil import now_local


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int | None = None

    @property
    def prompt_tokens(self) -> int:
        """Everything the model read: uncached input plus cache reads and writes."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @classmethod
    def from_api(cls, usage) -> "TokenUsage":
        """Build from an SDK `Usage` object (missing fields count as 0)."""
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


# Error categories for the dashboard ("Failed requests by type"); `classify_error` works on the
# SDK exception, `classify_status` on the `api_calls.status` text of rows written before the column existed.
ERROR_CATEGORIES = ("timeout", "overloaded", "rate_limit", "auth", "network", "validation", "other")
_STATUS_CATEGORIES = {
    "429": "rate_limit",
    "529": "overloaded",
    "503": "overloaded",
    "401": "auth",
    "403": "auth",
    "400": "validation",
    "413": "validation",
    "422": "validation",
}


def classify_error(exc: Exception, phase: str | None = None) -> str:
    import anthropic  # noqa: PLC0415 - keep the import local to the one place that needs the hierarchy

    if phase or isinstance(exc, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "network"
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status in (429,) or name == "RateLimitError":
        return "rate_limit"
    if status in (529, 503) or name in ("OverloadedError", "ServiceUnavailableError"):
        return "overloaded"
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth"
    if status in (400, 413, 422) or name in ("BadRequestError", "UnprocessableEntityError", "RequestTooLargeError"):
        return "validation"
    return "other"


def classify_status(status: str | None) -> str | None:
    """`error:429` -> rate_limit, `error:APITimeoutError:read` -> timeout, `ok` -> None."""
    if not status or not status.startswith("error"):
        return None
    parts = status.split(":")
    code = parts[1] if len(parts) > 1 else ""
    if len(parts) > 2 or "Timeout" in code:
        return "timeout"
    if "Connection" in code:
        return "network"
    return _STATUS_CATEGORIES.get(code, "other")


def estimate_cost(config: Config, model: str, usage: TokenUsage) -> float:
    price = config.budget.prices.get(model)
    if price is None:
        return 0.0
    per_token_in = price.input / 1_000_000
    per_token_out = price.output / 1_000_000
    # TODO(setup): Haiku 5.5 has a higher rate above 100K prompt tokens; not modeled here.
    return (
        usage.input_tokens * per_token_in
        + usage.output_tokens * per_token_out
        + usage.cache_read_tokens * per_token_in * config.budget.cache_read_multiplier
        + usage.cache_write_tokens * per_token_in * config.budget.cache_write_multiplier
    )


class UsageTracker:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.last_call_id: int | None = None

    def record(
        self,
        turn_id: str,
        *,
        model: str,
        attempt: int,
        status: str,
        duration_ms: float | None,
        ttft_ms: float | None,
        usage: TokenUsage | None,
        is_delegate: bool = False,
        error_category: str | None = None,
        request_id: str | None = None,
        final_failure: bool = False,
    ) -> float:
        """Write one api_calls row and one usage.jsonl line. Returns the estimated cost; the row id of the
        new api_calls row is left in `last_call_id` (the Brain attaches the prompt composition to it)."""
        usage = usage or TokenUsage()
        cost = estimate_cost(self.config, model, usage) if status == "ok" else 0.0
        self.last_call_id = self.db.add_api_call(
            turn_id,
            model=model,
            attempt=attempt,
            status=status,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=cost,
            is_delegate=is_delegate,
            error_category=error_category,
            request_id=request_id,
            final_failure=final_failure,
        )
        logging_setup.usage(
            {
                "model": model,
                "attempt": attempt,
                "status": status,
                "error_category": error_category,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "thinking_tokens": usage.thinking_tokens,
                "estimated_cost_usd": round(cost, 6),
                "duration_ms": duration_ms,
                "ttft_ms": ttft_ms,
                "is_delegate": is_delegate,
            }
        )
        return cost

    def spent_today(self) -> float:
        return self.db.spent_since(start_of_local_day_utc())


def start_of_local_day_utc() -> str:
    """Local midnight expressed as the UTC ISO prefix used in api_calls.created_at."""
    local = now_local()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class BudgetStatus:
    spent_usd: float
    budget_usd: float
    warn_fraction: float

    @property
    def fraction(self) -> float:
        return self.spent_usd / self.budget_usd if self.budget_usd > 0 else 0.0

    @property
    def exceeded(self) -> bool:
        return self.budget_usd > 0 and self.spent_usd >= self.budget_usd

    @property
    def warn(self) -> bool:
        return self.budget_usd > 0 and self.fraction >= self.warn_fraction

    def line(self) -> str:
        return f"${self.spent_usd:.4f} of ${self.budget_usd:.2f} used today ({self.fraction:.0%})"


class BudgetTracker:
    """Daily budget from the api_calls table. A budget of 0 disables the checks."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.warned = False
        self.exceeded_announced = False

    def status(self) -> BudgetStatus:
        return BudgetStatus(
            spent_usd=self.db.spent_since(start_of_local_day_utc()),
            budget_usd=self.config.budget.daily_budget_usd,
            warn_fraction=self.config.budget.warn_at_fraction,
        )

    def exceeded(self) -> bool:
        return self.status().exceeded

    def notice(self) -> str | None:
        """A one-time message for the user when the warn or cutoff line is crossed, else None."""
        status = self.status()
        if status.exceeded and not self.exceeded_announced:
            self.exceeded_announced = True
            self.warned = True
            return t(
                "cli.budget.exceeded", spent=f"{status.spent_usd:.4f}", budget=f"{status.budget_usd:.2f}", percent=f"{status.fraction:.0%}"
            )
        if status.warn and not status.exceeded and not self.warned:
            self.warned = True
            return t(
                "cli.budget.warning", spent=f"{status.spent_usd:.4f}", budget=f"{status.budget_usd:.2f}", percent=f"{status.fraction:.0%}"
            )
        return None
