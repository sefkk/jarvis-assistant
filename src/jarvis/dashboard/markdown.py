"""A small Markdown renderer for the Persona and Profile preview ("sanitized markdown preview").
It understands headings, bullet and numbered lists, paragraphs, fenced code, `code`, **bold** and *italic*,
and nothing else: every character of the source is escaped first and only these known tags are emitted, so
no HTML, script or attribute from the file can reach the page."""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)")


def _inline(text: str) -> str:
    """Escaped text with code, bold and italic spans."""
    out = str(escape(text))
    out = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def render(text: str) -> Markup:
    html: list[str] = []
    paragraph: list[str] = []
    list_tag: str | None = None
    in_code = False
    code: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            html.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            html.append(f"</{list_tag}>")
            list_tag = None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if in_code:
            if line.strip().startswith("```"):
                html.append("<pre><code>" + "\n".join(str(escape(c)) for c in code) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush_paragraph()
            close_list()
            in_code = True
            continue
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            html.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        bullet = _BULLET_RE.match(line)
        number = _NUMBER_RE.match(line) if not bullet else None
        if bullet or number:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                html.append(f"<{tag}>")
                list_tag = tag
            html.append(f"<li>{_inline((bullet or number).group(1))}</li>")
            continue
        if list_tag and line.startswith((" ", "\t")):
            html[-1] = html[-1][:-5] + "<br>" + _inline(line.strip()) + "</li>"  # a continued list item
            continue
        close_list()
        paragraph.append(line)
    if in_code:
        html.append("<pre><code>" + "\n".join(str(escape(c)) for c in code) + "</code></pre>")
    flush_paragraph()
    close_list()
    return Markup("\n".join(html))
