"""Per-turn taint tracking.

A turn is tainted as soon as any tool with `taints = True` returns content. Tools decide
through `tainted_confirm_reason()` whether a tainted turn turns them into a confirmed action.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaintState:
    tainted: bool = False
    sources: list[str] = field(default_factory=list)

    def mark(self, source: str) -> None:
        self.tainted = True
        if source not in self.sources:
            self.sources.append(source)

    def warning(self) -> str:
        sources = ", ".join(self.sources) or "an unknown source"
        return f"This action was triggered after reading untrusted content from {sources}."
