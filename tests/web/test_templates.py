"""Regression guard for the News Center -> 蜂巢 rename: reads every template FILE directly
(not rendered HTML) and confirms none still contain the old product name or logo emoji. A
file-content check catches templates that are never exercised by a page-render test too."""
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "templates"


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


def test_base_template_shows_new_product_name_and_logo():
    content = (_TEMPLATES_DIR / "base.html").read_text()
    assert "蜂巢" in content
    assert "🐝" in content
