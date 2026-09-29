"""Template-level design and branding regression guards."""
from pathlib import Path
import re

from beehive.web import app as web_app

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "templates"
_STATIC_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "static"

_READING_PAGES = (
    "dashboard.html",
    "channel_editorial.html",
    "channel_monitor.html",
    "channel_tracker.html",
    "archive.html",
    "search.html",
    "deep_read_brief.html",
)


def _css_rule(css: str, selector: str) -> str:
    """The declarations of the rule whose selector list is exactly `selector`."""
    match = re.search(r"(?:^|[}\n])" + re.escape(selector) + r"\{([^}]*)\}", css)
    assert match is not None, selector
    return match.group(1)


def _css_block(css: str, prelude: str) -> str:
    """The body of the block opened by `prelude`, such as an @container query."""
    start = css.index(prelude + "{") + len(prelude) + 1
    depth = 1
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start:index]
    raise AssertionError(f"{prelude} is never closed")


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
    # Every page's document is the datasheet: its one stylesheet, the favicon and the script are
    # fingerprinted, and the brand is the hexagon mark with the shell's label beside it.
    content = (_TEMPLATES_DIR / "datasheet_base.html").read_text()
    reading = (_TEMPLATES_DIR / "reading_base.html").read_text()
    assert "t('common.product_name')" in content
    assert 'href="/static/admin.css?v={{ asset_version }}"' in content
    assert 'href="/static/favicon.svg?v={{ asset_version }}"' in content
    assert 'src="/static/beehive.js?v={{ asset_version }}"' in content
    assert 'class="skip-link"' in content
    assert '<a class="adm-brand" href="{{ shell_nav.home_href }}"' in content
    assert '<svg viewBox="0 0 24 24" aria-hidden="true">' in content
    assert "<span>{{ shell_nav.label }}</span>" in content
    assert '<nav aria-label="{{ shell_nav.toc_aria }}">' in content
    assert "{% block contract %}" in content and "{% block shell %}" in content
    assert "beehive.css" not in content
    assert "🐝" not in content
    # The reading shell is that document with the reading pages' class and rail footer.
    assert '{% extends "datasheet_base.html" %}' in reading
    assert "{% block body_class %}page-reading{% endblock %}" in reading
    assert "{% block rail_foot %}" in reading
    assert not (_TEMPLATES_DIR / "base.html").exists()


def test_shared_stylesheet_defines_the_reading_site_basics():
    # The reading pages are set in the datasheet, so admin.css carries their basics and the old
    # reading stylesheet is gone.
    assert not (_STATIC_DIR / "beehive.css").exists()
    content = (_STATIC_DIR / "admin.css").read_text()
    assert "--link:#7DB6FF" in content
    assert "--ink-3:#808891" in content
    assert "font-variant-numeric:tabular-nums" in content
    assert ".adm :focus-visible{outline:2px solid var(--link)" in content
    assert ":lang(zh)" in content
    # Narrow screens drop the rail above a single column.
    narrow_shell = re.search(
        r"\.adm-shell\{([^}]*)\}", _css_block(content, "@container adm (max-width:880px)")
    )
    assert narrow_shell is not None
    assert "grid-template-columns:1fr" in narrow_shell.group(1).split(";")
    # Every custom property the sheet reads is defined in it.
    used = set(re.findall(r"var\((--[\w-]+)", content))
    defined = set(re.findall(r"(--[\w-]+):", content))
    assert used <= defined, sorted(used - defined)


def test_home_is_the_channel_desk_in_the_reading_shell():
    template = (_TEMPLATES_DIR / "dashboard.html").read_text()
    reading = (_TEMPLATES_DIR / "reading_base.html").read_text()
    css = (_STATIC_DIR / "admin.css").read_text()

    assert '{% extends "reading_base.html" %}' in template
    assert '{% extends "datasheet_base.html" %}' in reading
    # The direction contract ships in the page, as the first thing in the body.
    assert "{% block contract %}\n<!-- Reading direction contract (impeccable seed ba4c87a4)" in reading
    assert 'class="desk-sec"' in template
    assert 'class="tbl tbl-feed"' in template
    assert "data-kb-search" in template
    assert 'id="kb-status"' in template and 'aria-live="polite"' in template
    for rule in (".toc .toc-n{", ".rd{", ".score{", ".desk-state{", ".tbl .c-sum{", ".tbl-feed tr{"):
        assert rule in css, rule
    # Nothing on the desk is cut short: long text wraps, and a lot's auction context is not
    # clamped the way the workspace clamps it.
    assert "text-overflow:ellipsis" not in css.split("/* Reading:", 1)[1]
    assert ".page-reading .lot-where{display:block;overflow:visible}" in css


def test_secondary_navigation_is_scoped_to_each_product_area():
    # Every reading page is set in the reading shell, whose rail lists Featured, each Channel and
    # the Archive: there is no tab shelf any more. Error pages are a single sheet with no rail.
    for template_name in _READING_PAGES:
        page = (_TEMPLATES_DIR / template_name).read_text()
        assert '{% extends "reading_base.html" %}' in page, template_name
    for template_name in ("error.html", "not_found.html"):
        page = (_TEMPLATES_DIR / template_name).read_text()
        assert '{% extends "datasheet_base.html" %}' in page, template_name
        assert "{% block shell %}" in page, template_name
        assert 'class="adm-login err-page"' in page, template_name
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        assert not re.search(r'extends\s+"base\.html"', content), template_path.name
        assert "secondary_navigation" not in content, template_path.name
        assert "_channel_shelf.html" not in content, template_path.name
    assert not (_TEMPLATES_DIR / "base.html").exists()
    assert not (_TEMPLATES_DIR / "_channel_shelf.html").exists()
    assert not (_STATIC_DIR / "beehive.css").exists()

    # Admin has its own shell: a contents rail instead of the reading site's header and tabs.
    # The rail and running head live in the shared datasheet document, which the admin, the
    # Owner's workspace (research, watch list) and the reading pages each extend with their own
    # contract and footer.
    admin = (_TEMPLATES_DIR / "admin_settings.html").read_text()
    assert '{% extends "admin_base.html" %}' in admin
    datasheet = (_TEMPLATES_DIR / "datasheet_base.html").read_text()
    assert 'class="toc"' in datasheet
    assert "{% block shell %}" in datasheet
    assert "admin.css" in datasheet and "beehive.css" not in datasheet
    for shell in ("admin_base.html", "workspace_base.html", "reading_base.html"):
        assert '{% extends "datasheet_base.html" %}' in (_TEMPLATES_DIR / shell).read_text()

    for template_name in (
        "watchlist.html",
        "research_list.html",
        "research_detail.html",
        "research_new.html",
        "research_run_preview.html",
        "research_source_form.html",
    ):
        workspace_page = (_TEMPLATES_DIR / template_name).read_text()
        assert '{% extends "workspace_base.html" %}' in workspace_page, template_name

    for template_name in (
        "admin_login.html",
        "admin_new_channel.html",
        "admin_edit_channel.html",
        "admin_add_source.html",
        "admin_source_test.html",
        "admin_new_email_group.html",
        "admin_edit_email_group.html",
        "admin_email_group_preview.html",
    ):
        admin_flow = (_TEMPLATES_DIR / template_name).read_text()
        assert '{% extends "admin_base.html" %}' in admin_flow


def test_reading_site_typography_is_readable_at_default_zoom():
    css = (_STATIC_DIR / "admin.css").read_text()
    reading = (_TEMPLATES_DIR / "reading_base.html").read_text()
    # Reading pages are set in the datasheet's type: 15px Archivo on a 1.55 line, the rail's
    # section links a step smaller, prose at a reading measure and a brief's answer a step larger.
    assert "{% block body_class %}page-reading{% endblock %}" in reading
    assert "font:400 .9375rem/1.55 var(--sans)" in _css_rule(css, ".adm")
    assert "font-size:.8125rem" in _css_rule(css, ".toc .sub a")
    assert "max-width:72ch" in _css_rule(css, ".read")
    assert "font-size:1.0625rem;line-height:1.75" in _css_rule(css, ".brief-body .answer")


def test_editorial_channel_keeps_compact_readability_contract():
    css = (_STATIC_DIR / "admin.css").read_text()
    template = (_TEMPLATES_DIR / "channel_editorial.html").read_text()

    # The page is the datasheet's head over numbered sections of shared story rows.
    assert '{% extends "reading_base.html" %}' in template
    assert '{% import "_reading_macros.html" as rd with context %}' in template
    assert '<p class="adm-meta" id="chan-meta">' in template
    assert '<p class="adm-meta chan-sources">' in template
    # Both sections sit in the region a read toggle re-renders, side by side on a wide sheet.
    assert (
        template.index('<div id="chan-stories" data-refocus-slot>')
        < template.index('<div class="cols">')
        < template.index('<section class="chan-sec" id="top"')
        < template.index('<section class="chan-sec" id="more"')
    )
    assert '<span class="no">{{ sec.top }}</span>' in template
    assert '<span class="no">{{ sec.more }}</span>' in template
    assert template.count('<table class="tbl tbl-feed">') == 2
    assert template.count("rd.story_row(story, 'chan-stories', oob, page.channel_name") == 2
    assert "channel.kind" not in template

    expected_sizes = {
        ".chan-sources": ".8125rem",
        ".desk-state": ".8125rem",
        ".tbl small.why": ".8125rem",
        ".cmt": ".78rem",
    }
    for selector, font_size in expected_sizes.items():
        assert f"font-size:{font_size}" in _css_rule(css, selector), selector
    # Nothing is cut short: a summary wraps instead of ending in an ellipsis.
    assert "overflow-wrap:anywhere" in _css_rule(css, ".tbl .c-sum")
    # The sheet fills a wide screen and the page's sections flow into columns of 60rem or more.
    assert "max-width" not in _css_rule(css, ".adm-page")
    assert (
        "grid-template-columns:repeat(auto-fit,minmax(min(100%,60rem),1fr))"
        in _css_rule(css, ".cols")
    )


def test_monitor_and_tracker_have_dedicated_typed_templates():
    monitor = (_TEMPLATES_DIR / "channel_monitor.html").read_text()
    tracker = (_TEMPLATES_DIR / "channel_tracker.html").read_text()
    macros = (_TEMPLATES_DIR / "_reading_macros.html").read_text()
    watch_control = (_TEMPLATES_DIR / "_tracker_watch_control.html").read_text()
    feedback_control = (_TEMPLATES_DIR / "_listing_feedback_control.html").read_text()

    # Each kind renders its own rows: listings with prices, lots with deadlines. The rows are
    # shared macros, so search shows a Channel's hits in the same rows as its page.
    assert "{% macro listing_row(listing) %}" in macros
    assert '<tr id="listing-{{ listing.id }}"' in macros
    assert "{% macro lot_row(lot) %}" in macros
    assert '<tr id="lot-{{ lot.id }}"' in macros
    assert '{% include "_tracker_watch_control.html" %}' in macros
    assert macros.count('{% include "_listing_feedback_control.html" %}') == 2
    assert "rd.listing_table(page.items)" in monitor
    assert "rd.lot_table(lots)" in tracker
    for content in (monitor, tracker):
        assert '{% import "_reading_macros.html" as rd with context %}' in content
    for content in (monitor, tracker, macros, watch_control, feedback_control):
        assert "raw_metadata" not in content
        assert "channel.kind" not in content
    for gone in ("_monitor_item.html", "_tracker_item.html"):
        assert not (_TEMPLATES_DIR / gone).exists(), gone


def test_channel_scripts_use_the_static_asset_fingerprint():
    for template_name in ("channel_editorial.html", "channel_monitor.html", "channel_tracker.html"):
        content = (_TEMPLATES_DIR / template_name).read_text()
        assert 'src="/static/htmx.min.js?v={{ asset_version }}"' in content, template_name
    # The channel pages load the interaction helper and the stylesheet through the shell.
    datasheet = (_TEMPLATES_DIR / "datasheet_base.html").read_text()
    assert 'src="/static/beehive.js?v={{ asset_version }}"' in datasheet
    assert 'href="/static/admin.css?v={{ asset_version }}"' in datasheet


def test_dashboard_script_implements_displayed_keyboard_shortcuts():
    content = (_STATIC_DIR / "beehive.js").read_text()
    assert 'key === "/" || key === "f"' in content
    assert 'key === "j" || key === "k"' in content
    # Enter opens a story only while its row itself has focus, so it still presses buttons.
    assert '(key === "enter" && row && event.target === row)' in content
    # After a read toggle re-renders a row, j and k move on from the row that holds focus.
    assert 'document.activeElement?.closest?.("tr.kb-row")' in content
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
    assert "announce(feedbackMessage)" in content
    # Stopping a watch re-renders the list, so the focus target is remembered by id.
    assert "fallbackFocusSelector" in content


def test_htmx_focus_and_feedback_follow_the_button_that_submitted_a_form():
    content = (_STATIC_DIR / "beehive.js").read_text()
    before_request = content[
        content.index('"htmx:beforeRequest"') : content.index('"htmx:afterSwap"')
    ]
    # A form posted by htmx is the request's element, but the focus key and the announcement sit
    # on the button that submitted it, when that button is the form's own.
    assert "event.detail.requestConfig?.triggeringEvent?.submitter" in before_request
    assert "event.detail.elt.contains(submitter)" in before_request
    assert 'focusKey = element.dataset.focusKey || "";' in before_request
    assert 'feedbackMessage = element.dataset.feedbackMessage || "";' in before_request


def test_keyboard_search_finds_the_search_field_on_each_key_press():
    content = (_STATIC_DIR / "beehive.js").read_text()
    keydown = content[content.index('document.addEventListener("keydown"') :]
    # An htmx swap can replace the search field, so it is looked up when a key is pressed rather
    # than kept from page load.
    assert 'const findSearch = () => document.querySelector("[data-kb-search]");' in content
    assert 'const search = document.querySelector("[data-kb-search]")' not in content
    assert keydown.index("const search = findSearch();") < keydown.index("search.focus();")


def test_wide_reading_tables_set_their_rows_in_newspaper_columns():
    css = (_STATIC_DIR / "admin.css").read_text()
    # Each reading table's box is the container, so the table's own width, not the screen's,
    # decides when its rows flow into columns.
    assert "container:ledger/inline-size" in _css_rule(css, ".page-reading .tbl-wrap")
    query = re.search(r"@container ledger \(min-width:([\d.]+)rem\)", css)
    assert query is not None
    ledger = _css_block(css, query.group(0))
    column = re.search(r"columns:4 ([\d.]+)rem", ledger)
    gap = re.search(r"column-gap:([\d.]+)rem", ledger)
    assert column is not None and gap is not None
    # The rows switch to columns exactly when two full columns and the gap between them fit.
    assert float(query.group(1)) == 2 * float(column.group(1)) + float(gap.group(1))
    assert ".page-reading :is(.tbl-feed,.tbl-stack) thead{display:none}" in ledger
    # A row never splits across two columns.
    assert "break-inside:avoid" in ledger


def test_channel_template_translates_its_counts_and_marks_the_reason_focus_target():
    content = (_TEMPLATES_DIR / "channel_editorial.html").read_text()
    macros = (_TEMPLATES_DIR / "_reading_macros.html").read_text()
    # Section headings and their counts are translated, so no bare English label is left to mark
    # with lang="en".
    assert "t('web.channel.priority_heading')" in content
    assert "t('web.channel.folded_count', count=page.highlighted|length)" in content
    assert "t('web.channel.folded_count', count=page.folded_pagination.total)" in content
    assert not re.search(r">\s*Top\s*<", content)
    # A down vote's reason form keeps focus on its save button when the row re-renders.
    assert 'data-focus-key="reason-{{ story.id }}"' in macros


def test_templates_avoid_inline_styles_and_nested_interactive_controls():
    for template_path in _TEMPLATES_DIR.glob("*.html"):
        content = template_path.read_text()
        assert 'style="' not in content, f"{template_path.name} contains inline styles"
        assert not re.search(r"<a\b[^>]*>\s*<button\b", content), (
            f"{template_path.name} nests a button inside a link"
        )
