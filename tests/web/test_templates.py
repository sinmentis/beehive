"""Template-level design and branding regression guards."""
from pathlib import Path
import re

from beehive.web import app as web_app

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
    assert "t('common.product_name')" in content
    assert 'href="/static/beehive.css?v={{ asset_version }}"' in content
    assert 'href="/static/favicon.svg?v={{ asset_version }}"' in content
    assert 'class="skip-link"' in content
    assert 'class="brand-mark"' in content
    assert "{% block brand_context %}" in content
    assert "{% block secondary_navigation %}" in content
    assert "🐝" not in content


def test_shared_stylesheet_defines_responsive_dense_dashboard():
    content = (_STATIC_DIR / "beehive.css").read_text()
    assert "--accent:" in content
    assert "font-variant-numeric:tabular-nums" in content
    assert "--dashboard-row-height:1.625rem" in content
    assert ".signal-table" in content
    assert ".dashboard-channel-teaser" not in content
    assert "@media (max-width:720px)" in content
    assert "grid-template-columns:1fr" in content
    assert ":focus-visible" in content
    assert ":lang(zh)" in content
    assert ".type-option:has(input:focus-visible)" in content
    assert "--muted-2:#686e64" in content
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
    target = re.search(r"\.signal-comment summary\{([^}]*)\}", content)
    assert target is not None
    assert "width:1.5rem" in target.group(1)
    assert "height:1.5rem" in target.group(1)


def test_dashboard_matches_selected_a2_pixel_contract():
    css = (_STATIC_DIR / "beehive.css").read_text()
    template = (_TEMPLATES_DIR / "dashboard.html").read_text()
    channel_shelf = (_TEMPLATES_DIR / "_channel_shelf.html").read_text()

    assert "--header-height:5.65rem" in css
    assert "--muted:#8b9085" in css
    assert "--muted-2:#686e64" in css

    toolbar = re.search(r"\.page-dashboard \.dashboard-toolbar\{([^}]*)\}", css)
    assert toolbar is not None
    assert "height:2.75rem" in toolbar.group(1)
    assert "padding:0 .9rem" in toolbar.group(1)

    table_heading = re.search(r"\.signal-table th\{([^}]*)\}", css)
    assert table_heading is not None
    assert "height:1.1875rem" in table_heading.group(1)

    age_column = re.search(r"\.signal-age-col\{([^}]*)\}", css)
    assert age_column is not None
    assert "width:4.5rem" in age_column.group(1)

    assert ".channel-strip{" not in css
    assert ".signal-row.is-dim{opacity:" not in css
    assert 'class="channel-strip"' not in template
    assert '{% include "_channel_shelf.html" %}' in template
    assert 'class="channel-shelf"' in channel_shelf
    assert 'class="dashboard-channel-teaser"' not in channel_shelf
    assert 'class="dashboard-search-shortcut"' in template
    assert 'id="dashboard-selection-status"' in template
    assert 'aria-live="polite"' in template


def test_secondary_navigation_is_scoped_to_each_product_area():
    dashboard = (_TEMPLATES_DIR / "dashboard.html").read_text()
    channel = (_TEMPLATES_DIR / "channel_drilldown.html").read_text()
    archive = (_TEMPLATES_DIR / "archive.html").read_text()
    admin = (_TEMPLATES_DIR / "admin_settings.html").read_text()

    assert "{% block secondary_navigation %}" in dashboard
    assert "{% block secondary_navigation %}" in channel
    assert '{% include "_channel_shelf.html" %}' in dashboard
    assert '{% include "_channel_shelf.html" %}' in channel

    assert "{% block secondary_navigation %}" not in archive
    assert '{% include "_channel_shelf.html" %}' not in archive
    assert 'class="brand-context"' in archive

    assert "{% block secondary_navigation %}" in admin
    assert admin.count('class="admin-tabs"') == 1
    assert '{% include "_channel_shelf.html" %}' not in admin
    assert 'class="brand-context"' in admin

    for template_name in (
        "admin_login.html",
        "admin_new_channel.html",
        "admin_edit_channel.html",
        "admin_add_source.html",
    ):
        admin_flow = (_TEMPLATES_DIR / template_name).read_text()
        assert 'class="brand-context"' in admin_flow


def test_dashboard_typography_is_readable_at_default_zoom():
    css = (_STATIC_DIR / "beehive.css").read_text()
    expected_sizes = {
        r"\.channel-shelf-link": ".72rem",
        r'\.dashboard-search input\[type="search"\]': ".6875rem",
        r"\.signal-table th": ".625rem",
        r"\.signal-table td": ".8125rem",
        r"(?m)^\.signal-summary": ".8125rem",
        r"\.signal-statusbar": ".625rem",
    }
    for selector, font_size in expected_sizes.items():
        declaration = re.search(rf"{selector}\{{([^}}]*)\}}", css)
        assert declaration is not None
        assert f"font-size:{font_size}" in declaration.group(1)


def test_channel_matches_dashboard_dense_visual_contract():
    css = (_STATIC_DIR / "beehive.css").read_text()
    template = (_TEMPLATES_DIR / "channel_drilldown.html").read_text()

    assert "--header-height:5.65rem" in css
    assert "--muted:#8b9085" in css
    assert "--muted-2:#686e64" in css

    header = re.search(r"\.page-channel \.site-header\{([^}]*)\}", css)
    assert header is not None
    assert "height:var(--header-height)" in header.group(1)

    shell = re.search(r"\.channel-shell\{([^}]*)\}", css)
    assert shell is not None
    assert "width:100%" in shell.group(1)
    assert "padding:0" in shell.group(1)

    toolbar = re.search(r"\.channel-toolbar\{([^}]*)\}", css)
    assert toolbar is not None
    assert "min-height:3rem" in toolbar.group(1)
    assert "padding:.375rem .6875rem" in toolbar.group(1)

    expected_sizes = {
        r"\.channel-title": "1rem",
        r"\.channel-statline": ".6875rem",
        r"\.channel-section-heading h2": ".8125rem",
        r"\.page-channel \.item-title": ".8125rem",
        r"\.page-channel \.best-comment": ".8125rem",
        r"\.page-channel \.folded-title": ".8125rem",
    }
    for selector, font_size in expected_sizes.items():
        declaration = re.search(rf"{selector}\{{([^}}]*)\}}", css)
        assert declaration is not None
        assert f"font-size:{font_size}" in declaration.group(1)

    assert '{% block body_class %}page-channel{% endblock %}' in template
    assert '{% include "_channel_shelf.html" %}' in template
    assert 'class="channel-toolbar"' in template
    assert 'class="channel-section-heading"' in template


def test_channel_scripts_use_the_static_asset_fingerprint():
    content = (_TEMPLATES_DIR / "channel_drilldown.html").read_text()
    assert 'src="/static/htmx.min.js?v={{ asset_version }}"' in content
    assert 'src="/static/beehive.js?v={{ asset_version }}"' in content


def test_dashboard_script_implements_displayed_keyboard_shortcuts():
    content = (_STATIC_DIR / "beehive.js").read_text()
    assert 'key === "/" || key === "f"' in content
    assert 'key === "j" || key === "k"' in content
    assert 'key === "enter" && selectedRowHasFocus' in content
    assert "selectionStatus.textContent" in content
    assert "scrollIntoView" in content
    assert "/__(CHANNEL|SCORE|SUMMARY)__/g" in content
    assert '.replace("__CHANNEL__", channel)' not in content


def test_static_asset_version_changes_when_asset_bytes_change(tmp_path, monkeypatch):
    asset = tmp_path / "asset.css"
    asset.write_text("first")
    monkeypatch.setattr(web_app, "_STATIC_DIR", tmp_path)
    first = web_app._static_asset_version()

    asset.write_text("second")

    assert web_app._static_asset_version() != first


def test_english_editorial_labels_declare_their_language():
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        labels = re.findall(
            r'<p class="(?:eyebrow|section-kicker)"([^>]*)>(.*?)</p>',
            content,
        )
        for attributes, body in labels:
            if "{{ t(" not in body:
                assert 'lang="en"' in attributes, (
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
