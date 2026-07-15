"""Design/UI regression coverage for the deep-read todo: the compact action control that appears
on every ranked-item list surface (Dashboard rows, highlighted/folded Channel items, Archive
results) plus the fully redesigned dedicated brief page and its HTMX status partial. Complements
tests/web/test_deep_read_routes.py (route/auth/view-model behavior) and
tests/web/test_templates.py (site-wide template guards) -- this file is narrower: it proves every
surface actually renders the right control for the right state, that no template nests one
interactive control inside another, that the pending state is a structural skeleton (not a
spinner), and that the CSS backs the responsive/reduced-motion/touch-target claims."""
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
    client = TestClient(create_app(path, session_secret="test-secret"), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
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


# ============================================================================
# Every ranked-item surface renders the shared action control
# ============================================================================

def test_shared_action_partial_is_wired_into_every_ranked_item_surface():
    dashboard = (_TEMPLATES_DIR / "dashboard.html").read_text()
    item_card = (_TEMPLATES_DIR / "_item_card.html").read_text()
    channel = (_TEMPLATES_DIR / "channel_drilldown.html").read_text()
    archive = (_TEMPLATES_DIR / "archive.html").read_text()

    assert '{% include "_deep_read_action.html" %}' in dashboard
    assert '{% include "_deep_read_action.html" %}' in item_card
    assert '{% include "_deep_read_action.html" %}' in channel
    assert '{% include "_deep_read_action.html" %}' in archive

    # Dashboard rows and folded Channel items are dense: the control must stay hidden at rest.
    assert 'deep_read_variant = "dense"' in dashboard
    assert 'deep_read_variant = "dense"' in channel
    # Highlighted Channel cards and Archive rows have room to keep it always visible.
    assert 'deep_read_variant = "card"' in item_card
    assert 'deep_read_variant = "side"' in archive


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
    template = (_TEMPLATES_DIR / "dashboard.html").read_text()
    summary_cell = re.search(r'<td class="signal-summary-cell">(.*?)</td>', template, re.DOTALL)
    assert summary_cell is not None
    cell_body = summary_cell.group(1)
    summary_link_end = cell_body.index("</a>")
    action_include_index = cell_body.index('{% include "_deep_read_action.html" %}')
    assert action_include_index > summary_link_end, (
        "the deep-read action must be a sibling of the summary link, never nested inside it"
    )


def test_folded_item_action_sits_outside_the_title_link():
    template = (_TEMPLATES_DIR / "channel_drilldown.html").read_text()
    folded_article = re.search(r'<article class="folded-item">(.*?)</article>', template, re.DOTALL)
    assert folded_article is not None
    body = folded_article.group(1)
    title_link_end = body.index("</a>")
    action_include_index = body.index('{% include "_deep_read_action.html" %}')
    assert action_include_index > title_link_end


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
    assert '<h1 class="deep-read-title" id="deep-read-heading">s</h1>' in text
    assert 'class="deep-read-back"' in text
    assert 'class="deep-read-meta"' in text
    assert 'class="deep-read-source-link"' in text
    assert text.count("<h2") == 5
    for heading_id in (
        "deep-read-bottom-line-heading",
        "deep-read-key-findings-heading",
        "deep-read-important-figures-heading",
        "deep-read-why-it-matters-heading",
        "deep-read-limitations-heading",
    ):
        assert f'id="{heading_id}"' in text
        assert f'aria-labelledby="{heading_id}"' in text
    assert 'class="deep-read-figure-value"' in text
    assert 'class="deep-read-provenance"' in text
    assert "Generated" in text


def test_brief_page_pending_state_uses_structural_skeleton_not_a_spinner(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    text = resp.text
    assert 'class="deep-read-skeleton" aria-hidden="true"' in text
    assert "skeleton-line" in text
    assert "skeleton-figure" in text
    assert "spinner" not in text.lower()
    assert 'role="status"' in text
    assert 'aria-live="polite"' in text


def test_brief_page_omits_empty_limitations_section(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, {**_READY_RESULT, "limitations": ""})

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert "deep-read-limitations-heading" not in resp.text


def test_brief_page_not_requested_offers_generation_to_owner_only(conn, authed_client, client):
    _, c = conn
    _create_ranked_item(c)
    item_id = c.execute("SELECT id FROM items").fetchone()[0]

    owner_resp = authed_client.get(f"/items/{item_id}/brief")
    anon_resp = client.get(f"/items/{item_id}/brief")

    assert 'class="deep-read-owner-controls"' in owner_resp.text
    assert "csrf_token" in owner_resp.text
    assert 'class="deep-read-owner-controls"' not in anon_resp.text


def test_brief_page_failed_state_offers_retry_to_owner_only(conn, authed_client, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id)

    owner_resp = authed_client.get(f"/items/{item_id}/brief")
    anon_resp = client.get(f"/items/{item_id}/brief")

    assert 'class="deep-read-status deep-read-status-failed"' in owner_resp.text
    assert "raw trace" not in owner_resp.text
    assert "raw trace" not in anon_resp.text
    assert 'name="regenerate" value="true"' in owner_resp.text
    assert 'name="regenerate" value="true"' not in anon_resp.text


# ============================================================================
# HTMX polling stops on terminal states; live regions present
# ============================================================================

def test_status_partial_only_polls_while_pending():
    content = (_TEMPLATES_DIR / "_deep_read_status.html").read_text()
    branches = content.split("{% elif")
    pending_branch = branches[0]
    assert "hx-get=" in pending_branch
    assert 'hx-trigger="every 3s"' in pending_branch
    assert 'hx-swap="outerHTML"' in pending_branch
    for terminal_branch in branches[1:]:
        assert "hx-get=" not in terminal_branch
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

def test_css_defines_deep_read_responsive_breakpoints_and_touch_targets():
    css = (_STATIC_DIR / "beehive.css").read_text()

    breakpoint_760 = re.search(r"@media \(max-width:760px\)\{(.*?)\n\}", css, re.DOTALL)
    assert breakpoint_760 is not None
    assert ".deep-read-" in breakpoint_760.group(1)

    breakpoint_720_blocks = re.findall(
        r"@media \(max-width:720px\)\{(.*?)\n\}", css, re.DOTALL)
    assert any(".deep-read-" in block for block in breakpoint_720_blocks)
    assert any("folded-item" in block and "deep-read-action" in block for block in breakpoint_720_blocks)

    touch_block = re.search(
        r"@media \(hover:none\),\(pointer:coarse\)\{(.*?)\n\}", css, re.DOTALL)
    assert touch_block is not None
    assert ".deep-read-chip{min-height:2.75rem" in touch_block.group(1)
    assert ".deep-read-owner-controls .deep-read-form" in css
    assert ".deep-read-status-failed .deep-read-form" in css
    assert ".deep-read-status-failed .btn{width:100%;min-height:2.75rem}" in css


def test_css_avoids_gradients_and_inline_styles_in_deep_read_rules():
    css = (_STATIC_DIR / "beehive.css").read_text()
    compact_action_start = css.index("/* Deep read: compact action control")
    compact_action_css = css[compact_action_start:css.index("\n.filters{")]
    assert "gradient(" not in compact_action_css

    brief_page_start = css.index("/* Deep read: dedicated brief page")
    brief_page_css = css[brief_page_start:]
    assert "gradient(" not in brief_page_css


def test_skeleton_animation_is_covered_by_reduced_motion_override():
    css = (_STATIC_DIR / "beehive.css").read_text()
    assert "@media (prefers-reduced-motion:reduce)" in css
    reduced_motion = re.search(
        r"@media \(prefers-reduced-motion:reduce\)\{(.*?)\n\}", css, re.DOTALL)
    assert reduced_motion is not None
    assert "animation-duration:.01ms!important" in reduced_motion.group(1)
    assert ".skeleton-line{" in css
    assert "animation:skeleton-pulse" in css
