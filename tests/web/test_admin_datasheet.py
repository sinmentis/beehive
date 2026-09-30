"""The four-chapter admin: chapter routing, the contents rail's attention counts, the attention
list, how a Source is confirmed for removal and named in the activity log, and where pausing or
resuming a Source returns to."""
import html
import re

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.admin_actions import record_admin_action
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import assign_channel, create_email_group
from beehive.db.sessions import create_session
from beehive.db.sources import (
    create_source,
    get_source,
    record_fetch_error,
    record_fetch_success,
    set_source_paused,
)
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

SECRET = "test-secret-at-least-32-characters-long"
BIVOUAC = "https://www.bivouac.co.nz/collections/clearance"
PATAGONIA = "https://www.patagonia.co.nz/collections/sale"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    set_admin_password(path, "correct-password")
    return path


@pytest.fixture
def client(db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    test_client = TestClient(create_app(db_path, session_secret=SECRET), follow_redirects=False)
    test_client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", SECRET))
    return test_client


def _shop(conn, channel_id, url=BIVOUAC, name=None):
    return create_source(conn, channel_id, "shopify_collection", {"collection_url": url}, name=name)


def _outdoor_with_problems(db_path):
    """A monitor Channel with one paused and one failing Source."""
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    paused_id = _shop(conn, channel_id)
    set_source_paused(conn, paused_id, True, now_iso="2026-09-28T00:00:00+00:00")
    failing_id = _shop(conn, channel_id, PATAGONIA)
    record_fetch_error(
        conn, failing_id, "HTTP Error 403: Forbidden", "2026-09-28T01:00:00+00:00",
        error_kind="access_denied",
    )
    conn.close()
    return channel_id, paused_id, failing_id


@pytest.mark.parametrize(
    ("tab", "chapter"),
    [
        ("channels", "channels"),
        ("groups", "groups"),
        ("settings", "settings"),
        ("system", "system"),
        ("ai", "settings"),
        ("delivery", "groups"),
        ("nonsense", "channels"),
    ],
)
def test_old_and_new_tab_names_open_the_matching_chapter(client, tab, chapter):
    response = client.get(f"/admin/?tab={tab}")

    assert response.status_code == 200
    assert f'href="/admin/?tab={chapter}" aria-current="page"' in response.text


def test_every_admin_page_renders_inside_the_admin_shell(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    group_id = create_email_group(conn, "Daily", "Daily · {date}")
    conn.close()

    for path in (
        "/admin/",
        "/admin/channels/new",
        f"/admin/channels/{channel_id}/edit",
        f"/admin/channels/{channel_id}/sources/new",
        "/admin/email-groups/new",
        f"/admin/email-groups/{group_id}/edit",
        f"/admin/email-groups/{group_id}/preview",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert 'class="adm-shell"' in page.text, path
        assert "Auckland time " in page.text, path
        assert "/static/admin.css?v=" in page.text, path


def test_contents_rail_counts_sources_that_need_attention(client, db_path):
    channel_id, _, _ = _outdoor_with_problems(db_path)

    page = client.get(f"/admin/channels/{channel_id}/edit").text

    assert "2 items need attention" in page


def test_attention_list_shows_failures_before_paused_sources(client, db_path):
    _outdoor_with_problems(db_path)

    page = client.get("/admin/").text

    failed = page.index("www.patagonia.co.nz/collections/sale (Outdoor) failed to fetch.")
    paused = page.index("www.bivouac.co.nz/collections/clearance (Outdoor) is paused.")
    assert failed < paused
    assert "The site refused access" in page
    # Resuming from the list comes back to the list.
    assert 'name="return_url" value="/admin/?tab=channels"' in page
    assert "2 sources · 1 failing" in page


def test_attention_list_says_so_when_nothing_needs_the_owner(client, db_path, monkeypatch):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "owner@example.com")
    conn = connect(db_path)
    create_channel(conn, "Quiet", "profile")
    conn.close()

    page = client.get("/admin/").text

    assert "Nothing needs your attention right now." in page
    assert 'class="toc-count"' not in page


def test_email_group_without_a_recipient_is_flagged_where_it_is_counted(client, db_path):
    conn = connect(db_path)
    create_email_group(conn, "Weekly", "Weekly · {date}")
    conn.close()

    home = client.get("/admin/").text
    groups = client.get("/admin/?tab=groups").text

    assert '<span>Email groups</span><span id="toc-count-groups"><span class="toc-count"' in home
    assert "Weekly has no recipient, so it cannot send." in groups


def test_resume_returns_to_the_page_that_asked(client, db_path):
    _, paused_id, _ = _outdoor_with_problems(db_path)

    response = client.post(
        f"/admin/sources/{paused_id}/resume",
        data={"csrf_token": "csrf1", "return_url": "/admin/?tab=channels"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/?tab=channels"
    conn = connect(db_path)
    assert get_source(conn, paused_id)["paused_at"] is None


def test_pause_ignores_an_off_site_return_url(client, db_path):
    channel_id, _, failing_id = _outdoor_with_problems(db_path)

    response = client.post(
        f"/admin/sources/{failing_id}/pause",
        data={"csrf_token": "csrf1", "return_url": "https://evil.example/"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/channels/{channel_id}/edit"


def test_removing_a_shop_source_is_confirmed_with_its_domain(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    source_id = _shop(conn, channel_id)
    conn.close()

    page = client.get(f"/admin/channels/{channel_id}/edit").text
    assert 'Type "bivouac.co.nz" to confirm.' in html.unescape(page)

    wrong = client.post(
        f"/admin/sources/{source_id}/delete",
        data={"csrf_token": "csrf1", "confirmation": "shopify_collection"},
    )
    assert wrong.status_code == 409

    right = client.post(
        f"/admin/sources/{source_id}/delete",
        data={"csrf_token": "csrf1", "confirmation": "  Bivouac.co.NZ "},
    )
    assert right.status_code == 303
    conn = connect(db_path)
    assert get_source(conn, source_id) is None


def test_a_named_source_is_confirmed_with_its_name(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Designer", "deals", kind="monitor")
    source_id = create_source(
        conn, channel_id, "international_clearance",
        {"retailer": "mytheresa", "minimum_discount_percent": 70}, name="Mytheresa",
    )
    conn.close()

    response = client.post(
        f"/admin/sources/{source_id}/delete",
        data={"csrf_token": "csrf1", "confirmation": "mytheresa"},
    )

    assert response.status_code == 303


def test_activity_log_names_sources_by_their_current_label(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    source_id = _shop(conn, channel_id)
    # Rows written before this change stored the connector type as the label.
    record_admin_action(
        conn, action_type="source_created", target_type="source", target_id=source_id,
        target_label="shopify_collection", detail={"channel_id": channel_id},
    )
    conn.close()

    page = client.get("/admin/?tab=system").text

    assert "Added Source www.bivouac.co.nz/collections/clearance" in page
    assert "Added Source shopify_collection" not in page
    assert "No content was removed." not in page
    assert '<span class="muted">—</span>' in page


def test_creating_a_source_records_its_display_label(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    conn.close()

    response = client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "shopify_collection", "shopify_collection_url": BIVOUAC, "csrf_token": "csrf1"},
    )

    assert response.status_code == 303
    conn = connect(db_path)
    label = conn.execute(
        "SELECT target_label FROM admin_actions WHERE action_type = 'source_created'"
    ).fetchone()[0]
    assert label == "www.bivouac.co.nz/collections/clearance"


def test_groups_chapter_lists_channels_outside_every_email_group(client, db_path):
    conn = connect(db_path)
    grouped = create_channel(conn, "Grouped", "profile")
    create_channel(conn, "Loose", "profile")
    group_id = create_email_group(conn, "Daily", "Daily · {date}")
    assign_channel(conn, group_id, grouped)
    conn.close()

    page = client.get("/admin/?tab=groups").text

    assert "Not in any email group, so not in digest emails: Loose." in page
    assert 'id="default-digest-email"' in page


def test_a_never_fetched_source_is_not_hidden_behind_all_ok(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    fetched = _shop(conn, channel_id)
    record_fetch_success(conn, fetched, "2026-09-28T01:00:00+00:00", raw_count=10, new_count=2)
    _shop(conn, channel_id, PATAGONIA)
    conn.close()

    page = client.get("/admin/").text

    assert "2 sources · 1 not fetched yet" in page
    assert "all OK" not in page


def test_a_rejected_save_keeps_the_form_marked_unsaved(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    conn.close()

    response = client.post(
        f"/admin/channels/{channel_id}/edit",
        data={
            "csrf_token": "csrf1", "name": "Outdoor", "profile": "deals",
            "fetch_schedule_mode": "interval", "fetch_interval_hours": "3",
            "highlight_count": "8", "minimum_score": "0",
            "digest_email": "one@example.com,two@example.com",
        },
    )

    assert response.status_code == 400
    assert "data-dirty-track data-dirty-force" in response.text
    assert "These changes were not saved." in response.text


def test_hacker_news_feed_confirmation_does_not_depend_on_language(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    source_id = create_source(conn, channel_id, "hackernews_stories", {"feed": "best"})
    conn.close()

    page = client.get(f"/admin/channels/{channel_id}/edit").text
    assert 'Type "hn/best" to confirm.' in html.unescape(page)

    response = client.post(
        f"/admin/sources/{source_id}/delete", data={"csrf_token": "csrf1", "confirmation": "hn/best"}
    )
    assert response.status_code == 303


def test_rate_limited_failures_are_named_and_the_raw_error_is_folded(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Designer", "deals", kind="monitor")
    source_id = create_source(
        conn, channel_id, "international_clearance",
        {"retailer": "mytheresa", "minimum_discount_percent": 70}, name="Mytheresa",
    )
    record_fetch_error(
        conn, source_id, "Mytheresa GraphQL error: product listing page too many requests",
        "2026-09-28T01:00:00+00:00",
    )
    conn.close()

    page = client.get("/admin/").text

    assert "The site is limiting requests" in page
    assert '<details class="raw-error"><summary>Raw error</summary>' in page


def test_a_first_monitor_fetch_is_footnoted_as_a_baseline(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    source_id = _shop(conn, channel_id)
    record_fetch_success(conn, source_id, "2026-09-28T01:00:00+00:00", raw_count=1225, new_count=1225)
    conn.close()

    page = client.get(f"/admin/channels/{channel_id}/edit").text

    assert 'data-label="New">1,225<span class="fn-ref">(1)</span></td>' in page
    assert "A first fetch only records a baseline" in page


def test_a_failed_source_test_shows_no_preview_section(client, db_path, monkeypatch):
    from beehive.web.admin import sources as admin_routes

    class Broken:
        def fetch(self, config):
            raise RuntimeError("upstream exploded")

    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    source_id = create_source(conn, channel_id, "hackernews_stories", {"feed": "top"})
    conn.close()
    monkeypatch.setattr(admin_routes, "get_connector", lambda source_type: Broken())

    response = client.post(f"/admin/sources/{source_id}/test", data={"csrf_token": "csrf1"})

    assert response.status_code == 502
    assert "upstream exploded" in response.text
    assert 'id="preview"' not in response.text


def test_weekday_chips_use_short_labels_with_full_names_for_assistive_tech(client):
    page = client.get("/admin/email-groups/new").text

    assert 'aria-label="Monday"' in page
    assert ">Mon</label>" in page


def test_source_edit_page_names_the_source_and_its_channel(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    source_id = _shop(conn, channel_id)
    conn.close()

    page = html.unescape(client.get(f"/admin/sources/{source_id}/edit").text)

    assert '<p class="adm-meta">' in page
    assert " · in Outdoor</p>" in page


def test_long_values_are_wrapping_one_line_fields_so_nothing_is_cut_off(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    source_id = _shop(conn, channel_id, "https://www.bivouac.co.nz/collections/clearance?sort=price&page=1")
    conn.close()

    page = client.get(f"/admin/sources/{source_id}/edit").text

    field = re.search(r'<textarea class="inp inp-line" id="shopify-collection-url"[^>]*>([^<]*)</textarea>', page)
    assert field is not None
    assert 'name="shopify_collection_url" rows="1" data-single-line' in field.group(0)
    assert 'inputmode="url"' in field.group(0)
    assert 'spellcheck="false" autocapitalize="off"' in field.group(0)
    assert field.group(1) == "https://www.bivouac.co.nz/collections/clearance?sort=price&amp;page=1"
    assert '<textarea class="inp inp-line" id="source-name"' in page
    assert 'id="channel-name"' not in page


def test_table_cells_break_between_units_never_inside_one(client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outdoor", "deals", kind="monitor")
    source_id = _shop(conn, channel_id, "https://www.furtherfaster.co.nz/collections/clearance")
    record_fetch_success(conn, source_id, "2026-09-28T01:00:00+00:00", raw_count=3772, new_count=1945)
    conn.execute("UPDATE email_groups SET recipient_email = ?", ("personal-finance-nz@example.com",))
    conn.commit()
    conn.close()

    channels = client.get("/admin/?tab=channels").text
    assert '<small><span class="nw">3,772 items</span> · <span class="nw">1,945 new</span></small>' in channels
    assert re.search(r'<small title="Next fetch [^"]+">Next: <span class="nw">[^<]+</span></small>', channels)

    groups = client.get("/admin/?tab=groups").text
    assert '<td class="c-wrap" data-label="Recipient"><span class="nw">personal-finance-nz@example.com</span></td>' in groups

    edit = client.get(f"/admin/channels/{channel_id}/edit").text
    assert "<b>www.furtherfaster.co.nz</b><wbr>/collections<wbr>/clearance</span>" in edit


def test_group_list_uses_short_schedule_labels(client, db_path):
    conn = connect(db_path)
    daily = create_email_group(conn, "Daily", "Daily · {date}")
    picked = create_email_group(conn, "Picked", "Picked · {date}")
    conn.execute(
        "UPDATE email_groups SET schedule_mode = 'calendar', schedule_time = '09:00',"
        " schedule_timezone = 'Pacific/Auckland', schedule_weekdays = ? WHERE id = ?",
        ("0,1,2,3,4,5,6", daily),
    )
    conn.execute(
        "UPDATE email_groups SET schedule_mode = 'calendar', schedule_time = '07:30',"
        " schedule_timezone = 'Pacific/Auckland', schedule_weekdays = ? WHERE id = ?",
        ("0,2", picked),
    )
    conn.commit()
    conn.close()

    page = client.get("/admin/?tab=groups").text

    assert '<span class="nw">Daily at 09:00</span>' in page
    assert '<span class="nw">Mon, Wed at 07:30</span>' in page
