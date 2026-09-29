"""Design/UI regression coverage for the deep-read todo: the compact action control that appears
on every ranked-item list surface (the shared story row on the home page, the Channel sections and
the Archive) plus the dedicated brief page and its HTMX status partial. Complements
tests/web/test_deep_read_routes.py (route/auth/view-model behavior) and
tests/web/test_templates.py (site-wide template guards) -- this file is narrower: it proves every
surface actually renders the right control for the right state, that no template nests one
interactive control inside another, that the pending state is a structural skeleton (not a
spinner), and that the CSS backs the responsive/reduced-motion/target-size claims."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import claim_deep_read, complete_deep_read_success, fail_deep_read, request_deep_read
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

_NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "templates"
_STATIC_DIR = Path(__file__).parent.parent.parent / "src" / "beehive" / "web" / "static"

_READY_RESULT = {
    "item_id": "1",
    "bottom_line": "Rates fell by 25 basis points.",
    "key_findings": ["Inflation cooled", "Wage growth held"],
    "important_figures": [{"value": "25bp", "label": "rate cut"}],
    "why_it_matters": "Borrowing costs will ease for households.",
    "limitations": "Based on a single central bank statement.",
}


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def client(conn):
    path, _ = conn
    return TestClient(create_app(path), follow_redirects=False)


@pytest.fixture
def authed_client(conn):
    path, c = conn
    set_admin_password(path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(create_app(path, session_secret="test-secret-at-least-32-characters-long"), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret-at-least-32-characters-long"))
    return client


def _create_ranked_item(c, *, channel_name="Tech", score=90):
    channel_id = create_channel(c, channel_name, "developer news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="A story", url="https://example.com/a"))
    update_ai_ranking(c, source_id, "t1", score=score, summary="s", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    return channel_id, item_id


def _complete_ready(c, item_id, result=None):
    claimed = claim_deep_read(c, item_id, _NOW, lease_seconds=1500)
    complete_deep_read_success(
        c, item_id, claimed.request_version, claimed.claim_token,
        json.dumps(result or _READY_RESULT), "en", _NOW)


def _fail(c, item_id):
    claimed = claim_deep_read(c, item_id, _NOW, lease_seconds=1500)
    fail_deep_read(c, item_id, claimed.request_version, claimed.claim_token,
                    "fetch", "raw trace", _NOW)


def _css_rule(css, selector):
    """The declarations of the rule whose selector list is exactly `selector`."""
    match = re.search(r"(?:^|[}\n])" + re.escape(selector) + r"\{([^}]*)\}", css)
    assert match is not None, selector
    return match.group(1)


def _css_block(css, prelude):
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


def _story_row_macro():
    macros = (_TEMPLATES_DIR / "_reading_macros.html").read_text()
    start = macros.index("{% macro story_row(")
    return macros[start:macros.index("{% endmacro %}", start)]


# ============================================================================
# Every ranked-item surface renders the shared action control
# ============================================================================

def test_shared_action_partial_is_wired_into_every_ranked_item_surface():
    row = _story_row_macro()
    # One story row serves every ranked-item list, and its action column carries the control.
    ops_cell = re.search(r'<td class="c-ops">(.*?)</td>', row, re.DOTALL)
    assert ops_cell is not None
    assert '{%- set item = story %}{% include "_deep_read_action.html" %}' in ops_cell.group(1)
    for template_name in ("dashboard.html", "channel_editorial.html", "archive.html"):
        template = (_TEMPLATES_DIR / template_name).read_text()
        assert '{% import "_reading_macros.html" as rd with context %}' in template, template_name
        assert "rd.story_row(" in template, template_name
        # The action is an ordinary link in its own column everywhere: no per-surface variant.
        assert "deep_read_variant" not in template, template_name
    assert "deep_read_variant" not in row
    # Highlighted and folded Channel stories are the same row, one call per section.
    channel = (_TEMPLATES_DIR / "channel_editorial.html").read_text()
    assert channel.count("rd.story_row(") == 2
    for gone in ("_item_card.html", "_folded_item.html"):
        assert not (_TEMPLATES_DIR / gone).exists(), gone


def test_dashboard_row_shows_owner_start_button_when_not_yet_requested(conn, authed_client):
    _, c = conn
    _create_ranked_item(c)

    resp = authed_client.get("/")

    assert resp.status_code == 200
    assert 'class="deep-read-chip deep-read-chip-start"' in resp.text
    assert '<input type="hidden" name="csrf_token" value="csrf1">' in resp.text
    assert '<input type="hidden" name="origin" value="dashboard">' in resp.text


def test_dashboard_row_shows_nothing_for_anonymous_not_yet_requested(conn, client):
    _, c = conn
    _create_ranked_item(c)

    resp = client.get("/")

    assert resp.status_code == 200
    assert "deep-read-chip-start" not in resp.text
    assert "deep-read-chip-pending" not in resp.text
    assert "deep-read-chip-open" not in resp.text
    assert "deep-read-chip-retry" not in resp.text


def test_dashboard_row_shows_pending_link_only_to_owner(conn, authed_client, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    owner_resp = authed_client.get("/")
    anon_resp = client.get("/")

    assert f'href="/items/{item_id}/brief?origin=dashboard"' in owner_resp.text
    assert 'class="deep-read-chip deep-read-chip-pending"' in owner_resp.text
    assert "deep-read-chip-pending" not in anon_resp.text


def test_dashboard_row_shows_open_brief_to_everyone_when_ready(conn, authed_client, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    owner_resp = authed_client.get("/")
    anon_resp = client.get("/")

    for resp in (owner_resp, anon_resp):
        assert 'class="deep-read-chip deep-read-chip-open"' in resp.text
        assert f'href="/items/{item_id}/brief?origin=dashboard"' in resp.text


def test_dashboard_row_shows_retry_only_to_owner_when_failed(conn, authed_client, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id)

    owner_resp = authed_client.get("/")
    anon_resp = client.get("/")

    assert 'class="deep-read-chip deep-read-chip-retry"' in owner_resp.text
    assert '<input type="hidden" name="regenerate" value="true">' in owner_resp.text
    assert "deep-read-chip-retry" not in anon_resp.text


def test_channel_highlighted_and_folded_items_carry_the_action(conn, authed_client):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = authed_client.get(f"/channels/{channel_id}")

    assert resp.status_code == 200
    assert f'href="/items/{item_id}/brief?origin=channel&amp;channel_id={channel_id}"' in resp.text


def test_archive_row_carries_the_action(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = authed_client.get("/archive")

    assert resp.status_code == 200
    assert f'href="/items/{item_id}/brief?origin=archive"' in resp.text


# ============================================================================
# No nested interactive controls, real forms (not query strings/htmx) for mutations
# ============================================================================

def test_action_partial_never_nests_interactive_controls():
    content = (_TEMPLATES_DIR / "_deep_read_action.html").read_text()
    assert '<div class="deep-read-action' in content
    assert '<span class="deep-read-action' not in content
    assert not re.search(r"<a\b[^>]*>[^<]*<(a|button|form)\b", content)
    assert not re.search(r"<button\b[^>]*>[^<]*<(a|button)\b", content)
    # Every owner mutation is a real <form method="post">, never an hx-post/query-string mutation.
    assert content.count("<form") == content.count("method=\"post\"")
    assert "hx-post" not in content
    assert "hx-get" not in content


def test_action_partial_hidden_fields_are_allowlisted_and_never_a_free_text_url():
    content = (_TEMPLATES_DIR / "_deep_read_action.html").read_text()
    assert 'name="csrf_token" value="{{ dr.csrf_token }}"' in content
    assert 'name="origin" value="{{ dr.origin }}"' in content
    assert 'name="channel_id" value="{{ dr.channel_id }}"' in content
    assert "request.query_params" not in content
    assert "request.url" not in content


def test_dashboard_row_action_sits_outside_the_summary_link():
    # The home page's rows are the shared story row.
    assert "rd.story_row(" in (_TEMPLATES_DIR / "dashboard.html").read_text()
    row = _story_row_macro()
    summary_cell = re.search(r'<td class="c-sum">(.*?)</td>', row, re.DOTALL)
    assert summary_cell is not None
    assert "_deep_read_action.html" not in summary_cell.group(1), (
        "the deep-read action must be a sibling of the summary link, never nested inside it"
    )
    assert row.index('<td class="c-sum">') < row.index('{% include "_deep_read_action.html" %}')


def test_folded_item_action_sits_outside_the_title_link(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "Tech", "developer news", highlight_count=1)
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    for score, external_id in ((95, "top"), (94, "folded")):
        insert_new(c, source_id, RawItem(
            external_id=external_id, title=external_id, url=f"https://example.com/{external_id}"))
        update_ai_ranking(c, source_id, external_id, score=score,
                          summary=f"{external_id} summary", rationale="r")
    folded_id = c.execute("SELECT id FROM items WHERE external_id='folded'").fetchone()[0]

    resp = authed_client.get(f"/channels/{channel_id}")

    assert resp.status_code == 200
    folded_section = resp.text[resp.text.index('<section class="chan-sec" id="more"'):]
    row = re.search(
        rf'<tr class="kb-row" id="story-{folded_id}".*?</tr>', folded_section, re.DOTALL)
    assert row is not None
    title_link = re.search(r"<a [^>]*data-kb-open>(.*?)</a>", row.group(0), re.DOTALL)
    assert title_link is not None
    assert "folded summary" in title_link.group(1)
    assert "deep-read" not in title_link.group(1)
    assert row.group(0).index(title_link.group(0)) < row.group(0).index('class="deep-read-action"')
    assert 'class="deep-read-chip deep-read-chip-start"' in row.group(0)


def test_channel_item_action_follows_the_age_in_the_metadata_row():
    row = _story_row_macro()
    age_index = row.index('<td class="c-age"')
    ops_index = row.index('<td class="c-ops">')
    action_include_index = row.index('{% include "_deep_read_action.html" %}')
    assert age_index < ops_index < action_include_index
    assert "deep_read_variant" not in row


# ============================================================================
# Dedicated brief page: semantic structure
# ============================================================================

def test_brief_page_ready_state_has_conclusion_first_heading_and_all_required_sections(
    conn, client,
):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    text = resp.text
    assert '<h1 id="deep-read-heading">s</h1>' in text
    # The running head is the way back: the chapter the brief came from, then the brief itself.
    assert '<a href="/">1 Featured</a>' in text
    assert '<span aria-current="page">Deep read</span>' in text
    assert "<span>Source: r/x</span>" in text
    assert (
        '<a class="lnk" href="https://example.com/a" target="_blank" rel="noopener noreferrer">'
        "Read the original source"
    ) in text
    article = re.search(
        r'<article class="read brief-body" aria-labelledby="deep-read-heading">(.*?)</article>',
        text, re.DOTALL)
    assert article is not None
    body = article.group(1)
    # Conclusion first: the bottom line is the first heading, answered right beneath it. Each
    # section is numbered under the chapter the brief sits in, and a list counts its items.
    assert re.findall(r'<h2 id="(brief-[a-z-]+)"><span class="no">([0-9.]+)</span><span>(.*?)</span>', body) == [
        ("brief-bottom-line", "1.1", "Bottom line"),
        ("brief-findings", "1.2", "Key findings"),
        ("brief-why", "1.3", "Why it matters"),
    ]
    assert re.search(
        r'<h2 id="brief-bottom-line"><span class="no">1\.1</span><span>Bottom line</span></h2>\s*'
        r'<p class="answer">Rates fell by 25 basis points\.</p>', body)
    assert '<span class="tools"><span class="desk-state nw">2 items</span></span></h2>' in body
    assert "<ol><li>Inflation cooled</li><li>Wage growth held</li></ol>" in body
    assert "<p>Borrowing costs will ease for households.</p>" in body
    assert "<b>Limitations</b> Based on a single central bank statement." in body
    side = re.search(
        r'<aside class="nb-side" aria-label="Important figures">(.*?)</aside>', text, re.DOTALL)
    assert side is not None
    assert (
        '<h2><span class="no">1.4</span><span>Important figures</span>'
        '<span class="tools">1 item</span></h2>'
    ) in side.group(1)
    assert '<ul class="figs"><li><b>25bp</b><span>rate cut</span></li></ul>' in side.group(1)
    assert '<h2><span class="no">1.5</span><span>Source</span></h2>' in side.group(1)
    assert "Generated" in side.group(1)
    assert text.count("<h2") == 5
    # The rail lists the brief's sections under its chapter.
    for number, label, anchor in (
        ("1.1", "Bottom line", "#brief-bottom-line"),
        ("1.2", "Key findings", "#brief-findings"),
        ("1.3", "Why it matters", "#brief-why"),
        ("1.4", "Important figures", "#brief-figures"),
        ("1.5", "Source", "#brief-source"),
    ):
        assert f'<a href="{anchor}"><span class="no">{number}</span><span>{label}</span></a>' in text


def test_brief_page_pending_state_uses_structural_skeleton_not_a_spinner(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    text = resp.text
    status = re.search(
        r'<section id="deep-read-status" class="brief-status"(.*?)</section>', text, re.DOTALL)
    assert status is not None
    assert 'role="status" aria-live="polite"' in status.group(1)
    assert f'hx-get="/items/{item_id}/brief/status" hx-trigger="every 3s"' in status.group(1)
    assert (
        '<div class="skel brief-skel" aria-hidden="true">' + "<span></span>" * 5 + "</div>"
    ) in status.group(1)
    assert "spinner" not in text.lower()


def test_brief_page_omits_empty_limitations_section(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, {**_READY_RESULT, "limitations": ""})

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert "<b>Limitations</b>" not in resp.text
    assert '<h2 id="brief-why"><span class="no">1.3</span><span>Why it matters</span></h2>' in resp.text


@pytest.mark.parametrize(
    ("query", "chapter"),
    [
        ("?origin=channel&channel_id={channel_id}", "3"),
        ("?origin=archive", "4"),
        ("?origin=dashboard", "1"),
        ("", "1"),
    ],
)
def test_brief_sections_are_numbered_under_the_chapter_it_was_opened_from(
    conn, client, query, chapter
):
    _, c = conn
    # The rail reads 1 Featured, 2 Finance, 3 Tech (the story's Channel), 4 Archive.
    create_channel(c, "Finance", "economic news")
    channel_id, item_id = _create_ranked_item(c, channel_name="Tech")
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief" + query.format(channel_id=channel_id))

    assert resp.status_code == 200
    assert [number for number, _, _ in resp.context["toc_sections"]] == [
        f"{chapter}.{section}" for section in range(1, 6)
    ]
    assert f'<h2 id="brief-bottom-line"><span class="no">{chapter}.1</span>' in resp.text
    assert (
        f'<a href="#brief-source"><span class="no">{chapter}.5</span><span>Source</span></a>'
    ) in resp.text


@pytest.mark.parametrize(
    ("is_owner", "state", "query", "number", "word"),
    [
        # The Owner can start a brief that was never requested; to a reader it is unavailable.
        (True, "not_requested", "?origin=channel&channel_id={channel_id}", "2.1", "Not started"),
        (False, "not_requested", "", "1.1", "Unavailable"),
        (True, "failed", "?origin=archive", "3.1", "Failed"),
        (True, "pending", "?origin=channel&channel_id={channel_id}", "2.1", "Being written"),
        (False, "processing", "", "1.1", "Being written"),
    ],
)
def test_a_brief_that_is_not_ready_is_one_numbered_status_section(
    conn, client, authed_client, is_owner, state, query, number, word
):
    _, c = conn
    # The rail reads 1 Featured, 2 Tech (the story's Channel), 3 Archive.
    channel_id, item_id = _create_ranked_item(c)
    if state != "not_requested":
        request_deep_read(c, item_id, _NOW)
    if state == "processing":
        claim_deep_read(c, item_id, _NOW, lease_seconds=1500)
    if state == "failed":
        _fail(c, item_id)
    reader = authed_client if is_owner else client
    query = query.format(channel_id=channel_id)

    page = reader.get(f"/items/{item_id}/brief{query}")
    poll = reader.get(f"/items/{item_id}/brief/status{query}")

    assert page.status_code == poll.status_code == 200
    # The section is numbered under the brief's chapter and listed in the rail, in place of a
    # finished brief's five.
    assert page.context["toc_sections"] == [(number, "Deep read", "#deep-read-status")]
    assert (
        f'<a href="#deep-read-status"><span class="no">{number}</span><span>Deep read</span></a>'
    ) in page.text
    assert 'id="brief-bottom-line"' not in page.text
    headings = []
    for response in (page, poll):
        section = re.search(
            r'<section id="deep-read-status"([^>]*)>\s*(<h2 id="deep-read-status-h">.*?</h2>)'
            r"(.*?)</section>",
            response.text,
            re.DOTALL,
        )
        assert section is not None
        attributes, heading, body = section.groups()
        # Its one heading, first in it, carries the number and the state in words.
        assert f'<span class="no">{number}</span><span>Deep read</span>' in heading
        assert f'<span class="desk-state nw">{word}</span>' in heading
        assert "<h2" not in body
        # Only a brief still being written polls for its state.
        assert ("hx-get=" in attributes) is (state in ("pending", "processing"))
        headings.append(heading)
    # Each poll re-renders the heading as the page shows it, so its state never goes stale.
    assert headings[0] == headings[1]


def test_brief_figures_heading_has_no_count_without_figures(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, {**_READY_RESULT, "important_figures": []})

    resp = client.get(f"/items/{item_id}/brief")

    # The section keeps its number and its place in the rail, and says there is nothing to count.
    assert '<h2><span class="no">1.4</span><span>Important figures</span></h2>' in resp.text
    assert "No notable figures in this article." in resp.text
    assert (
        '<a href="#brief-figures"><span class="no">1.4</span><span>Important figures</span></a>'
    ) in resp.text


def test_brief_page_not_requested_offers_generation_to_owner_only(conn, authed_client, client):
    _, c = conn
    _create_ranked_item(c)
    item_id = c.execute("SELECT id FROM items").fetchone()[0]

    owner_resp = authed_client.get(f"/items/{item_id}/brief")
    anon_resp = client.get(f"/items/{item_id}/brief")

    owner_controls = '<div class="adm-actions" role="group" aria-label="Deep read owner controls">'
    start_form = f'<form method="post" action="/items/{item_id}/deep-read">'
    assert owner_controls in owner_resp.text
    assert start_form in owner_resp.text
    assert '<input type="hidden" name="csrf_token" value="csrf1">' in owner_resp.text
    assert 'aria-label="Deep read: s">Deep read</button>' in owner_resp.text
    assert '<div class="empty">No deep read yet.' in owner_resp.text
    assert owner_controls not in anon_resp.text
    assert start_form not in anon_resp.text
    assert "csrf_token" not in anon_resp.text
    assert '<div class="empty">Deep read isn&#39;t available for this item right now.</div>' in (
        anon_resp.text)


def test_brief_page_failed_state_offers_retry_to_owner_only(conn, authed_client, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id)

    owner_resp = authed_client.get(f"/items/{item_id}/brief")
    anon_resp = client.get(f"/items/{item_id}/brief")

    for resp in (owner_resp, anon_resp):
        failed = re.search(
            r'<section id="deep-read-status" class="brief-status"[^>]*>(.*?)</section>',
            resp.text, re.DOTALL)
        assert failed is not None
        assert '<div class="note note-danger">' in failed.group(1)
        assert "<b>Why this failed</b>" in failed.group(1)
        assert "raw trace" not in resp.text
    assert "<small>Try again. If it keeps failing, open the original source above.</small>" in (
        owner_resp.text)
    assert 'name="regenerate" value="true"' in owner_resp.text
    assert 'aria-label="Retry deep read: s">Retry</button>' in owner_resp.text
    assert 'name="regenerate" value="true"' not in anon_resp.text
    assert "Retry</button>" not in anon_resp.text


# ============================================================================
# HTMX polling stops on terminal states; live regions present
# ============================================================================

def test_status_partial_only_polls_while_pending():
    content = (_TEMPLATES_DIR / "_deep_read_status.html").read_text()
    # The one polling attribute set is guarded by the pending state; terminal states never poll.
    assert content.count("hx-get=") == 1
    assert (
        '{%- if is_pending %} hx-get="{{ status_url }}" hx-trigger="every 3s" '
        'hx-swap="outerHTML"{% endif %}'
    ) in content
    assert content.count('role="status"') == content.count('aria-live="polite"')
    assert 'class="sr-only"' in content


def test_deep_read_brief_page_reuses_the_status_partial_for_non_ready_states():
    content = (_TEMPLATES_DIR / "deep_read_brief.html").read_text()
    assert '{% include "_deep_read_status.html" %}' in content
    # The ready-state article is the only branch NOT delegated to the shared partial.
    assert content.count('{% include "_deep_read_status.html" %}') == 1


# ============================================================================
# Responsive styles and touch targets
# ============================================================================

def test_css_defines_deep_read_responsive_layout_and_target_sizes():
    css = (_STATIC_DIR / "admin.css").read_text()

    # Mid widths: the brief's side column (figures and source) drops below the text.
    mid = _css_block(css, "@container adm (max-width:1180px)")
    assert ".nb{grid-template-columns:minmax(0,1fr)}" in mid
    assert ".nb-side{position:static;margin-top:28px}" in mid

    # Narrow screens: a story's summary takes the whole line and its deep-read action wraps
    # beneath it; a failed brief's retry drops under the explanation.
    narrow = _css_block(css, "@container adm (max-width:880px)")
    assert ".tbl-feed .c-sum{flex:1 1 100%;min-width:0}" in narrow
    assert ".tbl-feed .c-ops{width:auto;font-size:.78rem}" in narrow
    assert ".note-row{grid-template-columns:1fr}" in narrow
    assert ".note-act{justify-content:flex-start;padding-top:0}" in narrow

    # Buttons and toggles keep a target of at least 24px.
    assert "min-height:32px" in _css_rule(css, ".btn")
    assert "min-height:26px" in _css_rule(css, ".btn-sm")
    assert "min-height:26px" in _css_rule(css, ".tg")

    # The deep-read action reads as the datasheet's link, in the sheet's own font.
    chip = _css_rule(css, ".adm .deep-read-chip")
    assert "color:var(--link)" in chip
    assert "font:inherit" in chip


def test_css_avoids_gradients_and_inline_styles_in_deep_read_rules():
    css = (_STATIC_DIR / "admin.css").read_text()
    # The datasheet is flat throughout, the deep-read rules included.
    assert ".adm .deep-read-chip{" in css
    assert ".brief-body .answer{" in css
    assert "gradient(" not in css
    for template_name in (
        "_deep_read_action.html", "_deep_read_status.html", "deep_read_brief.html"
    ):
        assert 'style="' not in (_TEMPLATES_DIR / template_name).read_text(), template_name


def test_skeleton_animation_is_covered_by_reduced_motion_override():
    css = (_STATIC_DIR / "admin.css").read_text()
    status = (_TEMPLATES_DIR / "_deep_read_status.html").read_text()
    assert '<div class="skel brief-skel" aria-hidden="true">' in status
    assert "animation:skel " in _css_rule(css, ".skel span")
    assert "@keyframes skel{" in css
    reduced_motion = _css_block(css, "@media (prefers-reduced-motion:reduce)")
    assert ".skel span{animation:none}" in reduced_motion
    assert ".adm *{transition:none!important}" in reduced_motion
