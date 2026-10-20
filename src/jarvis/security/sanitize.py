"""Sanitization at the three boundaries.

- untrusted content -> model   : `for_model`, `html_to_text`, `wrap_untrusted`
- anything -> terminal         : `for_terminal`
- the user's own input         : `user_input`
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

USER_INPUT_MAX_CHARS = 50_000

# ANSI/VT escape sequences: CSI (ESC [ ... final byte), OSC (ESC ] ... BEL or ESC \),
# and the remaining two-character escapes.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS / SOS / PM / APC strings
    r"|\x1b[@-Z\\-_]"  # other 7-bit escapes
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
# C0 controls except tab/newline/carriage return, plus DEL and C1 controls.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
# Invisible characters that can hide instructions from a human reader.
_INVISIBLE_RE = re.compile(
    "["
    "​-‏"  # zero-width space/joiners, LRM, RLM
    "  "  # line/paragraph separators
    "‪-‮"  # bidi embeddings and overrides
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # bidi isolates
    "﻿"  # BOM / zero-width no-break space
    "\U000e0000-\U000e007f"  # Unicode tag characters
    "\U000e0100-\U000e01ef"  # variation selectors supplement
    "]"
)
_UNTRUSTED_CLOSE_RE = re.compile(r"<\s*/\s*untrusted", re.IGNORECASE)
_UNTRUSTED_OPEN_RE = re.compile(r"<\s*untrusted\b", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def strip_controls(text: str) -> str:
    return _CONTROL_RE.sub("", text)


def for_terminal(text: str) -> str:
    """Anything printed to the terminal: no escape sequences, no control characters."""
    return strip_controls(strip_ansi(text))


def user_input(text: str) -> str:
    """The user's own text is trusted: only control characters are removed and a length cap applied."""
    cleaned = strip_controls(strip_ansi(text.replace("\r\n", "\n").replace("\r", "\n")))
    if len(cleaned) > USER_INPUT_MAX_CHARS:
        cleaned = cleaned[:USER_INPUT_MAX_CHARS]
    return cleaned


def for_model(text: str) -> str:
    """Untrusted text before it goes to the model: normalized, no invisible or control characters."""
    text = unicodedata.normalize("NFKC", text)
    text = strip_ansi(text)
    text = _INVISIBLE_RE.sub("", text)
    text = strip_controls(text.replace("\r\n", "\n").replace("\r", "\n"))
    return text


def wrap_untrusted(text: str, source: str) -> str:
    """Wrap untrusted content so the model treats it as data. Nested tags are neutralized."""
    text = _UNTRUSTED_CLOSE_RE.sub("[/untrusted", text)
    text = _UNTRUSTED_OPEN_RE.sub("[untrusted", text)
    source = re.sub(r"[^a-z0-9_\-]", "", source.lower()) or "unknown"
    return f'<untrusted source="{source}">\n{text}\n</untrusted>'


# --- HTML -> text ------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "iframe", "object", "svg"}
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "table",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
    "hr",
    "nav",
    "aside",
    "form",
}
# Block tags that also end with a line break (list items only break before, so lists stay compact).
_BLOCK_END_TAGS = _BLOCK_TAGS - {"li", "br", "hr", "tr"}
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[^.\d]|$)"
    r"|font-size\s*:\s*0*(?:[0-2](?:\.\d+)?px|0(?:\.\d+)?(?:em|rem|pt|%)|0)\b"
    r"|(?:height|width|max-height|max-width)\s*:\s*0(?:px)?(?:[^.\d]|$)"
    r"|(?:left|top)\s*:\s*-\d{3,}px|text-indent\s*:\s*-\d{3,}px",
    re.IGNORECASE,
)


_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CLASS_SELECTOR_RE = re.compile(r"^(?:[a-z0-9]+)?\.([\w-]+)$", re.IGNORECASE)
_ID_SELECTOR_RE = re.compile(r"^(?:[a-z0-9]+)?#([\w-]+)$", re.IGNORECASE)


def hidden_selectors(css: str) -> tuple[set[str], set[str]]:
    """Class names and ids that a stylesheet hides (simple selectors only, e.g. `.hidden`, `#x`)."""
    classes: set[str] = set()
    ids: set[str] = set()
    for selectors, declarations in _CSS_RULE_RE.findall(css):
        if not _HIDDEN_STYLE_RE.search(declarations):
            continue
        for selector in selectors.split(","):
            selector = selector.strip()
            if m := _CLASS_SELECTOR_RE.match(selector):
                classes.add(m.group(1))
            elif m := _ID_SELECTOR_RE.match(selector):
                ids.add(m.group(1))
    return classes, ids


def _is_hidden(attrs: list[tuple[str, str | None]], hidden_classes: set[str] = frozenset(), hidden_ids: set[str] = frozenset()) -> bool:
    for name, value in attrs:
        name = name.lower()
        if name == "hidden":
            return True
        if name == "aria-hidden" and (value or "").strip().lower() == "true":
            return True
        if name == "style" and value and _HIDDEN_STYLE_RE.search(value):
            return True
        if name == "type" and (value or "").lower() == "hidden":
            return True
        if name == "class" and value and hidden_classes and set(value.split()) & hidden_classes:
            return True
        if name == "id" and value and value.strip() in hidden_ids:
            return True
    return False


class _TextExtractor(HTMLParser):
    def __init__(self, hidden_classes: set[str] = frozenset(), hidden_ids: set[str] = frozenset()) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_stack: list[str] = []  # tags whose content is being dropped
        self._hidden_classes = hidden_classes
        self._hidden_ids = hidden_ids

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if tag not in _VOID_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _SKIP_TAGS or _is_hidden(attrs, self._hidden_classes, self._hidden_ids):
            if tag not in _VOID_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.lower().startswith("javascript:"):
                self._pending_href = href
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[image: {alt}]")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            elif tag in self._skip_stack:
                # close up to the matching tag (tolerate sloppy HTML)
                while self._skip_stack and self._skip_stack.pop() != tag:
                    pass
            return
        if tag in _BLOCK_END_TAGS:
            self.parts.append("\n")
        if tag == "a" and getattr(self, "_pending_href", None):
            self.parts.append(f" ({self._pending_href})")
            self._pending_href = None

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in raw.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)


def html_to_text(html: str) -> str:
    """Visible text only: scripts, styles and hidden elements are dropped first.

    Hidden means an inline style, a `hidden`/`aria-hidden` attribute, or a class/id that a
    `<style>` block in the page hides (simple selectors). External stylesheets are not fetched,
    so hiding through them is not detected (TODO(setup): accept that or fetch same-origin CSS).
    """
    classes: set[str] = set()
    ids: set[str] = set()
    for block in _STYLE_BLOCK_RE.findall(html):
        c, i = hidden_selectors(block)
        classes |= c
        ids |= i
    parser = _TextExtractor(classes, ids)
    parser.feed(html)
    parser.close()
    return parser.text()
