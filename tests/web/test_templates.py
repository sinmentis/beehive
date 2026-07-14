"""Template-level design and branding regression guards."""
from pathlib import Path
import re

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "templates"
_STATIC_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "static"


def test_no_template_references_the_old_product_name():
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        assert "News Center" not in content, f"{template_path.name} still says 'News Center'"


def test_no_template_uses_the_old_logo_emoji():
    # Checks for "📰 News Center" (the old logo's exact signature), not the bare 📰 emoji:
    # admin_add_source.html legitimately uses a standalone 📰 as an unrelated "Google News"
    # source-type icon in its type-selector UI, which must NOT be flagged by this guard.
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        assert "📰 News Center" not in content, (
            f"{template_path.name} still uses the old 📰 News Center logo"
        )


def test_base_template_uses_shared_design_system_and_brand_mark():
    content = (_TEMPLATES_DIR / "base.html").read_text()
    assert "蜂巢" in content
    assert 'href="/static/beehive.css"' in content
    assert 'href="/static/favicon.svg"' in content
    assert 'class="skip-link"' in content
    assert 'class="brand-mark"' in content
    assert "🐝" not in content


def test_shared_stylesheet_defines_responsive_dense_dashboard():
    content = (_STATIC_DIR / "beehive.css").read_text()
    assert "--accent:" in content
    assert "font-variant-numeric:tabular-nums" in content
    assert "--dashboard-row-height:1.625rem" in content
    assert ".signal-table" in content
    assert ".channel-strip" in content
    assert "@media (max-width:720px)" in content
    assert "grid-template-columns:1fr" in content
    assert ":focus-visible" in content
    assert ":lang(zh)" in content
    assert ".type-option:has(input:focus-visible)" in content
    assert "--muted-2:#838979" in content
    non_link_cells = re.search(
        r"\.signal-source,\.signal-engagement,\.signal-age\{([^}]*)\}",
        content,
    )
    assert non_link_cells is not None
    assert "display:" not in non_link_cells.group(1)
    compact_search = re.search(
        r'\.dashboard-search input\[type="search"\]\{([^}]*)\}',
        content,
    )
    assert compact_search is not None
    assert "min-height:0" in compact_search.group(1)
    for selector in (r"\.channel-strip-teaser", r"\.signal-comment summary"):
        target = re.search(rf"{selector}\{{([^}}]*)\}}", content)
        assert target is not None
        assert "width:1.5rem" in target.group(1)
        assert "height:1.5rem" in target.group(1)


def test_english_editorial_labels_declare_their_language():
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        labels = re.findall(r'<p class="(?:eyebrow|section-kicker)"[^>]*>', content)
        assert all('lang="en"' in label for label in labels), (
            f"{template_path.name} has English micro-copy without lang=en"
        )


def test_htmx_helpers_restore_focus_and_announce_feedback():
    content = (_STATIC_DIR / "beehive.js").read_text()
    assert "htmx:beforeRequest" in content
    assert "htmx:afterSwap" in content
    assert ".focus()" in content
    assert "feedback-status" in content
    assert "const message = feedbackMessage" in content


def test_channel_template_marks_english_count_and_reason_focus_target():
    content = (_TEMPLATES_DIR / "channel_drilldown.html").read_text()
    item_content = (_TEMPLATES_DIR / "_item_card.html").read_text()
    assert '<span lang="en">Top</span>' in content
    assert 'data-focus-key="item-{{ item.id }}-reason"' in item_content


def test_templates_avoid_inline_styles_and_nested_interactive_controls():
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        assert 'style="' not in content, f"{template_path.name} contains inline styles"
        assert not re.search(r"<a\b[^>]*>\s*<button\b", content), (
            f"{template_path.name} nests a button inside a link"
        )
