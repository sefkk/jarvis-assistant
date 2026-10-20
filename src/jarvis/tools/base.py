"""Tool framework: one class per tool, Pydantic input model, risk level."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ValidationError

from jarvis.exceptions import ToolValidationError

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.security.guards import PathGuard, ShellGuard, UrlGuard


class RiskLevel(str, Enum):
    SAFE = "SAFE"  # runs directly
    CONFIRM = "CONFIRM"  # asks the user first
    BLOCKED = "BLOCKED"  # never runs; guards raise ToolBlockedError for specific inputs


@dataclass
class ImageAttachment:
    """An image a tool hands to the model (screenshots, clipboard images)."""

    media_type: str  # image/jpeg or image/png
    data_b64: str
    width: int
    height: int
    sha256: str

    def block(self) -> dict[str, Any]:
        return {"type": "image", "source": {"type": "base64", "media_type": self.media_type, "data": self.data_b64}}

    def reference(self) -> dict[str, Any]:
        """What the transcript store keeps instead of the bytes."""
        return {"type": "image_ref", "media_type": self.media_type, "width": self.width, "height": self.height, "sha256": self.sha256}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    images: list[ImageAttachment] = field(default_factory=list)


class EmptyInput(BaseModel):
    """For tools that take no arguments."""


def did_you_mean(term: str, *, retry: str) -> str:
    """The tool result that makes the model ask one short question instead of acting on a possibly misheard
    name. The question is "Did you mean 'X'?" in the main language ("'X' mi demek istedin?" in Turkish),
    the one the rules tell the model to ask."""
    from jarvis.i18n import t  # noqa: PLC0415

    question = t("tools.did_you_mean", term=term)
    return (
        f"Not done yet: the request was spoken and the name may be misheard. Ask the user exactly one short "
        f'question in their language, e.g. "{question}", and wait. If they say yes, {retry}. '
        "If they say no, ask them to say or spell the name again."
    )


@dataclass
class Runtime:
    """Live objects a tool such as `delegate` needs: set by the Assistant once it exists.

    `sink` is the current turn's output sink (for debug/progress lines) and `turn_id` the
    current turn; both change every turn.
    """

    client: Any
    db: Any
    usage_tracker: Any
    registry: Any
    budget: Any
    debug: bool = False
    channel: str = "cli"  # cli / telegram / voice: tools that depend on where the request came from read it
    sink: Any = None
    turn_id: str | None = None
    user_text: str | None = None  # the user's own message this turn (the taint rules refer to it)
    spoken: bool = False  # this turn's request is a speech transcript (voice service, Telegram voice note): names may be misheard
    taint: Any = None  # the current turn's TaintState, shared with sub-runs
    delegate_failures: dict[str, int] = field(default_factory=dict)  # turn_id -> failed attempts


@dataclass
class ToolContext:
    """What tools may need beyond their arguments: config, paths and the shared guards."""

    config: "Config"
    paths: "Paths"
    path_guard: "PathGuard"
    shell_guard: "ShellGuard"
    url_guard: "UrlGuard"
    runtime: Runtime | None = None

    @classmethod
    def build(cls, config: "Config", paths: "Paths") -> "ToolContext":
        from jarvis.security.guards import PathGuard, ShellGuard, UrlGuard

        path_guard = PathGuard(
            config.security.allowed_file_roots,
            config.security.denied_paths,
            extra_denied=[paths.data_dir, paths.logs_dir],
        )
        return cls(
            config=config,
            paths=paths,
            path_guard=path_guard,
            shell_guard=ShellGuard(config.security.shell_denylist, path_guard),
            url_guard=UrlGuard(),
        )


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]] = EmptyInput
    risk: ClassVar[RiskLevel] = RiskLevel.SAFE
    taints: ClassVar[bool] = False  # True when the output is untrusted content
    taint_source: ClassVar[str] = "tool"  # label in <untrusted source="...">
    confirm_when_tainted: ClassVar[bool] = False  # SAFE tool that still needs approval in a tainted turn
    # Delegate access: True = always available to the delegate (read, search, list),
    # False = never, None (default) = the user is asked the first time the delegate wants it and
    # can answer always / only this time / never; always and never are saved in
    # data/delegate_permissions.json.
    delegate_ok: ClassVar[bool | None] = None

    def __init__(self, ctx: ToolContext | None = None) -> None:
        self.ctx = ctx

    @classmethod
    def available(cls, ctx: ToolContext | None) -> bool:
        """False hides the tool from the model entirely (not offered, not callable), for example
        the football tools without an API key. Must be cheap and must not prompt."""
        return True

    @abstractmethod
    def run(self, args: BaseModel) -> ToolResult: ...

    # --- hooks the security layer calls ------------------------------------------------------

    def risk_for(self, args: BaseModel) -> RiskLevel:
        """The risk of this particular call. Default: the class-level `risk`. A tool whose risk
        depends on its arguments (a Shortcut marked confirm in the allowlist) overrides it."""
        return self.risk

    def taints_for(self, args: BaseModel) -> bool:
        """Whether this particular call's output is untrusted. Default: the class-level `taints`."""
        return self.taints

    def tainted_confirm_reason(self, args: BaseModel, user_text: str) -> str | None:
        """Why this call needs confirmation in a tainted turn, or None if it may run directly."""
        if self.confirm_when_tainted:
            return f"{self.name} changes state and the turn contains untrusted content"
        return None

    def confirm_preview(self, args: BaseModel) -> str | None:
        """Extra text for the confirmation prompt, for example the exact shell command."""
        return None

    # --- plumbing ---------------------------------------------------------------------------

    def parse_args(self, raw: Any) -> BaseModel:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ToolValidationError(f"{self.name}: arguments must be an object, got {type(raw).__name__}")
        try:
            return self.input_model.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors())
            raise ToolValidationError(f"{self.name}: invalid arguments ({problems})") from None

    def api_schema(self) -> dict[str, Any]:
        """The Anthropic tool definition, generated from the Pydantic model."""
        schema = _strip_titles(self.input_model.model_json_schema())
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return {"name": self.name, "description": self.description, "input_schema": schema}

    def require_ctx(self) -> ToolContext:
        if self.ctx is None:
            raise RuntimeError(f"{self.name} needs a ToolContext (config and guards)")
        return self.ctx


def _strip_titles(node: Any) -> Any:
    """Pydantic adds `"title": "<Name>"` to every object and property; the model does not need
    them. Only string-valued `title` keys are generated noise: a field *named* title appears as
    `"title": {<schema>}` under `properties` and must stay."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node
