"""Auto-discovers Tool subclasses in `jarvis.tools.*` and produces the API schemas."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any, Iterator

from jarvis.tools.base import RiskLevel, Tool, ToolContext

log = logging.getLogger("jarvis.tools")

SKIP_MODULES = {"base", "registry"}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._modules: dict[str, str] = {}
        self.hidden: dict[str, str] = {}  # tool name -> module, for tools whose `available()` said no

    @classmethod
    def discover(cls, ctx: ToolContext | None = None, package: str = "jarvis.tools") -> "ToolRegistry":
        registry = cls()
        pkg = importlib.import_module(package)
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name in SKIP_MODULES or info.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"{package}.{info.name}")
            except Exception:  # a broken integration must not take Jarvis down
                log.exception("failed to import tool module %s", info.name)
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if obj is Tool or not issubclass(obj, Tool) or inspect.isabstract(obj):
                    continue
                if obj.__module__ != module.__name__ or not getattr(obj, "name", None):
                    continue
                try:
                    available = obj.available(ctx)
                except Exception:  # an availability probe must never take a tool module down
                    log.exception("%s.available() failed; offering the tool anyway", obj.name)
                    available = True
                if not available:
                    registry.hidden[obj.name] = info.name
                    log.info("tool %s hidden: not configured", obj.name)
                    continue
                registry.register(obj(ctx), module=info.name)
        return registry

    def register(self, tool: Tool, module: str = "") -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        self._modules[tool.name] = module

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def replace(self, tool: Tool) -> None:
        """Swap in another implementation under an existing name (evals use fixture-backed tools)."""
        if tool.name not in self._tools:
            raise KeyError(f"no tool named {tool.name} to replace")
        self._tools[tool.name] = tool

    def qualified_name(self, name: str) -> str:
        module = self._modules.get(name)
        return f"{module}.{name}" if module else name

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools[name] for name in self.names())

    def __len__(self) -> int:
        return len(self._tools)

    def subset(self, *, max_risk: RiskLevel | None = None, exclude: set[str] | None = None) -> "ToolRegistry":
        """A registry with only the tools at or below a risk level, minus `exclude` by name."""
        order = [RiskLevel.SAFE, RiskLevel.CONFIRM, RiskLevel.BLOCKED]
        exclude = exclude or set()
        sub = ToolRegistry()
        sub.hidden = dict(self.hidden)
        for tool in self:
            if tool.name in exclude:
                continue
            if max_risk is None or order.index(tool.risk) <= order.index(max_risk):
                sub.register(tool, module=self._modules.get(tool.name, ""))
        return sub

    def unmatched_patterns(self, patterns: list[str]) -> list[str]:
        """Patterns that match no registered tool (a typo silently removes a tool otherwise)."""
        qualified = {f"{self._modules.get(t.name, '')}.{t.name}" for t in self}
        qualified |= {f"{module}.{name}" for name, module in self.hidden.items()}  # hidden, not a typo
        modules = {self._modules.get(t.name, "") for t in self} | set(self.hidden.values())
        names = {t.name for t in self} | set(self.hidden)
        unmatched = []
        for pattern in patterns:
            if pattern.endswith(".*"):
                if pattern[:-2] not in modules:
                    unmatched.append(pattern)
            elif pattern not in qualified and pattern not in names:
                unmatched.append(pattern)
        return unmatched

    def subset_by_patterns(self, patterns: list[str]) -> "ToolRegistry":
        """Only the tools matching "module.tool" or "module.*" (remote profiles)."""
        allowed: set[str] = set()
        for tool in self:
            module = self._modules.get(tool.name, "")
            qualified = f"{module}.{tool.name}" if module else tool.name
            for pattern in patterns:
                if pattern == qualified or pattern == tool.name or (pattern.endswith(".*") and module == pattern[:-2]):
                    allowed.add(tool.name)
                    break
        return self.subset(exclude={t.name for t in self if t.name not in allowed})

    def api_tools(self) -> list[dict[str, Any]]:
        """Tool definitions in a stable order (important for prompt caching)."""
        return [tool.api_schema() for tool in self]
