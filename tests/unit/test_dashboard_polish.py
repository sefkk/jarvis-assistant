"""The dashboard's UI is English only (templates, strings, scripts), every page has an empty
state, the loading indicator exists, and the vendored libraries are the pinned versions."""

from __future__ import annotations

import re
from pathlib import Path

from jarvis.dashboard.app import STATIC_DIR, TEMPLATES_DIR
from jarvis.dashboard.ui_text import UI, all_strings

TURKISH_LETTERS = re.compile(r"[ğĞşŞıİçÇöÖüÜ]")
# Common Turkish words that use only ASCII letters, matched as whole words in lower case.
TURKISH_WORDS = re.compile(
    r"\b(ve|veya|ayarlar|kaydet|iptal|evet|hayir|sil|ekle|tamam|bugun|dakika|saat|hata|yok|var|sesli|hafiza|kisi)\b", re.IGNORECASE
)


def _text_nodes(html: str) -> str:
    """What the browser shows: tag contents and the attribute values a user reads (title, placeholder, aria-label)."""
    shown = re.sub(r"<[^>]+>", " ", html)
    for attr in ("title", "placeholder", "aria-label", "value"):
        shown += " " + " ".join(re.findall(rf'{attr}="([^"]*)"', html))
    return shown


def test_ui_strings_and_templates_are_english_only():
    for name, text in all_strings().items():
        assert not TURKISH_LETTERS.search(text) and not TURKISH_WORDS.search(text), (name, text)
    for template in sorted(Path(TEMPLATES_DIR).rglob("*.html")):
        source = template.read_text(encoding="utf-8")
        visible = _text_nodes(re.sub(r"\{[{%#].*?[}%#]\}", " ", source, flags=re.S))  # Jinja expressions carry data, not UI text
        assert not TURKISH_LETTERS.search(visible), (template.name, TURKISH_LETTERS.search(visible).group(0))
        assert not TURKISH_WORDS.search(visible), (template.name, TURKISH_WORDS.search(visible).group(0))
    for script in (STATIC_DIR / "app.js", STATIC_DIR / "analytics.js"):
        assert not TURKISH_LETTERS.search(script.read_text(encoding="utf-8")), script.name
    for source in sorted(Path(STATIC_DIR).parent.glob("*.py")):  # the dashboard package itself: code, docstrings, messages
        text = source.read_text(encoding="utf-8")
        assert not TURKISH_LETTERS.search(text), (source.name, TURKISH_LETTERS.search(text).group(0))
        assert not TURKISH_WORDS.search(text), (source.name, TURKISH_WORDS.search(text).group(0))


def test_every_page_template_has_an_empty_state_or_data_guard():
    pages = {
        "overview.html": (),
        "partials/today_recent.html": ("T.EMPTY",),
        "analytics.html": ("T.NO_DATA",),
        "transcripts/index.html": ("T.EMPTY", "T.NO_DATA"),
        "transcripts/session.html": ("T.EMPTY",),
        "transcripts/turn.html": ("T.EMPTY",),
        "audit.html": ("T.NO_DATA", "T.AUDIT_FILE_MISSING"),
        "settings.html": (),
        "memory.html": (),
        "partials/memory_list.html": ("T.EMPTY",),
        "history.html": ("T.EMPTY",),
        "partials/cart_drawer.html": ("T.NO_CHANGES",),
    }
    for name, markers in pages.items():
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in source, (name, marker)


def test_loading_states_exist():
    base = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert 'id="loading-bar"' in base and "T.LOADING" in base and UI.LOADING == "Loading…"
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".htmx-indicator" in css and ".loading-bar" in css and ".skeleton" in css and "prefers-reduced-motion" in css
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "htmx:beforeRequest" in js and "htmx:afterRequest" in js


def test_vendored_versions_are_pinned():
    htmx = (STATIC_DIR / "vendor" / "htmx.min.js").read_text(encoding="utf-8")
    assert '"2.0.11"' in htmx or "2.0.11" in htmx
    chart = (STATIC_DIR / "vendor" / "chart.umd.js").read_text(encoding="utf-8")[:400]
    assert "Chart.js v4.5.1" in chart
    assert (STATIC_DIR / "vendor" / "alpine-csp.min.js").stat().st_size > 10_000
    for license_file in ("htmx-LICENSE", "alpine-LICENSE.md", "chart-LICENSE.md", "lucide-LICENSE"):
        assert (STATIC_DIR / "vendor" / license_file).is_file()
    assert "lucide-static v1.48.0" in (STATIC_DIR / "vendor" / "lucide" / "x.svg").read_text(encoding="utf-8")


def test_design_tokens_cover_both_themes_and_the_toggle():
    """One token file: every color is a light-dark() pair, the system decides unless data-theme pins a theme."""
    tokens = (STATIC_DIR / "tokens.css").read_text(encoding="utf-8")
    assert "color-scheme: light dark" in tokens
    assert ':root[data-theme="light"] { color-scheme: light; }' in tokens and ':root[data-theme="dark"] { color-scheme: dark; }' in tokens
    for name in ("--bg", "--glass", "--text", "--accent", "--success", "--warning", "--danger", "--info", "--chart-grid"):
        assert re.search(rf"{name}: light-dark\(", tokens), name
    assert re.search(r"--chart-1: #0071e3;", tokens) and re.search(r"--chart-10: #bf5af2;", tokens)  # the original chart colors
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css.replace("%23", "")), "colors belong in tokens.css"
    base = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert base.index("tokens.css") < base.index("app.css") and """<script src="{{ asset('theme.js') }}"></script>""" in base
    assert base.count("data-theme-choice") == 3


def test_icons_are_inline_vendored_svgs():
    from jarvis.dashboard.app import NAV
    from jarvis.dashboard.icons import NAV_ICONS, icon

    assert set(NAV_ICONS) == {key for key, _, _ in NAV}
    svg = str(icon("layout-dashboard"))
    assert svg.startswith('<svg class="icon" aria-hidden="true"') and svg.count('<rect width="7"') == 4  # shapes keep their size
    assert "<!--" not in svg and 'width="24"' not in svg
    for bad in ("../app", "X", ""):
        try:
            icon(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_static_urls_carry_a_content_hash():
    """A changed stylesheet or script gets a new URL, so the browser cannot keep running an old cached copy."""
    from jarvis.dashboard.app import asset_url

    url = asset_url("app.css")
    assert re.fullmatch(r"/static/app\.css\?v=[0-9a-f]{10}", url)
    for bad in ("../app.py", "nope.css"):
        try:
            asset_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    for template in sorted(Path(TEMPLATES_DIR).rglob("*.html")):
        assert '"/static/' not in template.read_text(encoding="utf-8"), template.name  # every asset goes through asset()
