"""Inline SVG icons from the vendored Lucide subset (`static/vendor/lucide/`, lucide-static 1.48.0, ISC). They are
inlined rather than linked so they take the text color and cost no request; the stroke width comes from the
stylesheet. Only names that exist in the folder render; an unknown name is a template bug and raises."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

ICON_DIR = Path(__file__).resolve().parent / "static" / "vendor" / "lucide"

# Sidebar item -> icon (keys of app.NAV).
NAV_ICONS = {
    "overview": "layout-dashboard",
    "analytics": "chart-column",
    "logs": "scroll-text",
    "audit": "shield-check",
    "settings": "settings-2",
    "memory": "brain",
    "persona": "user-round",
    "history": "history",
}


@lru_cache(maxsize=None)
def icon(name: str, extra_class: str = "") -> Markup:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"bad icon name: {name!r}")
    svg = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S).strip()
    head, rest = svg.split(">", 1)  # only the <svg> tag loses its class and size; a <rect> keeps its width and height
    head = re.sub(r'\s(class|width|height)="[^"]*"', "", head)
    css = f"icon {extra_class}".strip()
    svg = head.replace("<svg", f'<svg class="{css}" aria-hidden="true" focusable="false"', 1) + ">" + rest
    return Markup(" ".join(svg.split()).replace("> <", "><"))
