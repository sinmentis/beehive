import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.collector.manual_trigger import request_channel_fetch
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
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
from beehive.web import admin as admin_routes
from scripts.set_admin_password import set_admin_password


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    set_admin_password(path, "correct-password")
    return path


@pytest.fixture
def authed_client(db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    client = TestClient(
        create_app(db_path, session_secret="test-secret"), follow_redirects=False
    )
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


def test_new_source_form_requires_session(db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    client = TestClient(
        create_app(db_path, session_secret="test-secret"), follow_redirects=False
    )
    resp = client.get(f"/admin/channels/{channel_id}/sources/new")
    assert resp.status_code == 303


def test_new_source_form_404_for_missing_channel(authed_client):
    resp = authed_client.get("/admin/channels/999/sources/new")
    assert resp.status_code == 404


def test_new_source_form_shows_channel_name(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    resp = authed_client.get(f"/admin/channels/{channel_id}/sources/new")
    assert resp.status_code == 200
    assert "NZ Finance" in resp.text


def test_source_test_uses_bounded_preview_without_persisting_items(
    authed_client,
    db_path,
    monkeypatch,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Auctions", "tools", kind="tracker")
    source_id = create_source(conn, channel_id, "all_about_auctions", {})
    conn.close()
    calls = []

    class PreviewConnector:
        def fetch_preview(self, config, *, limit):
            calls.append((config, limit))
            return [
                RawItem(
                    external_id="lot-1",
                    title="Workshop tools",
                    url="https://example.com/lot-1",
                )
            ]

    monkeypatch.setattr(admin_routes, "get_connector", lambda source_type: PreviewConnector())

    resp = authed_client.post(
        f"/admin/sources/{source_id}/test",
        data={"csrf_token": "csrf1"},
    )

    assert resp.status_code == 200
    assert "Workshop tools" in resp.text
    assert calls == [({}, 10)]
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert get_source(conn, source_id)["last_attempt_at"] is None
    activity = authed_client.get("/admin/?tab=system")
    assert "Fetched 1 live items in" in activity.text
    assert "0 Sources · 1 items" not in activity.text


def test_create_source_succeeds_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "reddit_subreddit",
            "subreddit": "PersonalFinanceNZ",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/admin/channels/{channel_id}/edit?source_saved=1"
    )

    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    assert row["type"] == "reddit_subreddit"
    assert json.loads(row["config"])["subreddit"] == "PersonalFinanceNZ"


def test_create_source_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "reddit_subreddit", "subreddit": "x", "csrf_token": "wrong"},
    )
    assert resp.status_code == 403


def test_delete_source_removes_it_and_redirects_to_parent_channel(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/delete",
        data={"csrf_token": "csrf1", "confirmation": "reddit_subreddit"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(
        f"/admin/channels/{channel_id}/edit?source_removed=1&undo_action="
    )

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (source_id,)
        ).fetchone()[0]
        == 0
    )


def test_delete_source_404_for_missing_source(authed_client):
    resp = authed_client.post(
        "/admin/sources/999/delete",
        data={"csrf_token": "csrf1", "confirmation": "Missing"},
    )
    assert resp.status_code == 404


def test_delete_source_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/delete",
        data={"csrf_token": "wrong", "confirmation": "reddit_subreddit"},
    )
    assert resp.status_code == 403

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (source_id,)
        ).fetchone()[0]
        == 1
    )


def test_create_google_news_source_succeeds_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "google_news_query",
            "query": "New Zealand economy",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/admin/channels/{channel_id}/edit?source_saved=1"
    )

    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    assert row["type"] == "google_news_query"
    assert json.loads(row["config"])["query"] == "New Zealand economy"


def test_create_source_rejects_empty_subreddit_with_form_error(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "reddit_subreddit", "subreddit": "", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "subreddit" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_create_source_rejects_empty_query_with_form_error(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "google_news_query", "query": "", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "query" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_create_source_rejects_unknown_type(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "bogus_type", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "unknown Source type" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_new_source_form_shows_both_hackernews_types_with_consistent_icon(
    authed_client,
    db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/sources/new")

    assert resp.status_code == 200
    assert 'value="hackernews_stories"' in resp.text
    assert 'value="hackernews_query"' in resp.text
    assert "Hacker News — Feed" in resp.text
    assert "Hacker News — Keyword search" in resp.text
    assert resp.text.count("🟧") >= 2


@pytest.mark.parametrize(
    ("feed", "expected"),
    [
        ("top", "top"),
        ("best", "best"),
        ("new", "new"),
        ("ask", "ask"),
        ("show", "show"),
        ("job", "job"),
    ],
)
def test_create_hackernews_stories_source_persists_feed(
    authed_client,
    db_path,
    feed,
    expected,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "hackernews_stories",
            "hn_feed": feed,
            "csrf_token": "csrf1",
        },
    )

    assert resp.status_code == 303
    conn = connect(db_path)
    row = conn.execute(
        "SELECT type, config FROM sources WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert row["type"] == "hackernews_stories"
    assert json.loads(row["config"]) == {"feed": expected}


@pytest.mark.parametrize("sort", ["relevance", "recent"])
def test_create_hackernews_query_source_persists_query_and_sort(
    authed_client,
    db_path,
    sort,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "hackernews_query",
            "hn_query": "local-first",
            "hn_sort": sort,
            "csrf_token": "csrf1",
        },
    )

    assert resp.status_code == 303
    conn = connect(db_path)
    row = conn.execute(
        "SELECT type, config FROM sources WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert row["type"] == "hackernews_query"
    assert json.loads(row["config"]) == {
        "query": "local-first",
        "sort": sort,
    }


@pytest.mark.parametrize(
    "data, expected_message",
    [
        (
            {"type": "hackernews_stories", "hn_feed": "front_page"},
            "Please choose a valid Hacker News feed",
        ),
        (
            {"type": "hackernews_query", "hn_query": "", "hn_sort": "relevance"},
            "Please enter a Hacker News search keyword",
        ),
        (
            {"type": "hackernews_query", "hn_query": "python", "hn_sort": "popular"},
            "Please choose a valid Hacker News sort order",
        ),
    ],
)
def test_create_hackernews_source_rejects_invalid_config(
    authed_client,
    db_path,
    data,
    expected_message,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    conn.close()
    data = {**data, "csrf_token": "csrf1"}

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data=data,
    )

    assert resp.status_code == 400
    assert expected_message in resp.text
    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()[0]
        == 0
    )


def test_invalid_hackernews_query_preserves_selected_type_and_entered_query(
    authed_client,
    db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "hackernews_query",
            "hn_query": "python",
            "hn_sort": "popular",
            "csrf_token": "csrf1",
        },
    )

    assert 'value="hackernews_query"' in resp.text
    assert 'value="python"' in resp.text
    assert re.search(r'id="type-hn-query"\s+checked', resp.text)


def test_add_source_page_lists_three_official_options(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Official", "profile")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text
    assert 'value="rbnz_news"' in html
    assert 'value="nz_government_news"' in html
    assert 'value="federal_reserve_news"' in html
    assert "RBNZ — News releases" in html
    assert "NZ Government — Announcements" in html
    assert "Federal Reserve — News releases" in html
    assert "🏦" in html and "🇳🇿" in html and "🏛️" in html


def test_official_source_persists_with_empty_config(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Official", "profile")
    conn.close()

    response = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "rbnz_news", "csrf_token": "csrf1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(db_path)
    source = conn.execute(
        "SELECT type, config FROM sources WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert source["type"] == "rbnz_news"
    assert source["config"] == "{}"


def test_edit_channel_shows_official_label_and_icon(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Official", "profile")
    create_source(conn, channel_id, "federal_reserve_news", {})
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert "Federal Reserve" in html
    assert "🏛️" in html


def test_new_source_form_lists_shopify_collection_option(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text
    assert 'value="shopify_collection"' in html
    assert "Shopify — Collection watch" in html
    assert "🛍️" in html
    assert 'id="shopify-collection-url"' in html
    assert 'id="shopify-collection-vendors"' in html


def test_create_shopify_collection_source_succeeds_and_redirects(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "shopify_collection",
            "shopify_collection_url": "https://arcteryx.co.nz/collections/outlet",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/admin/channels/{channel_id}/edit?source_saved=1"
    )

    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    assert row["type"] == "shopify_collection"
    assert json.loads(row["config"]) == {
        "collection_url": "https://arcteryx.co.nz/collections/outlet",
    }


def test_create_shopify_collection_source_persists_vendor_list(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "shopify_collection",
            "shopify_collection_url": "https://arcteryx.co.nz/collections/outlet",
            "shopify_collection_vendors": "Arc'teryx,  Patagonia ,,Salomon",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303

    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    assert json.loads(row["config"]) == {
        "collection_url": "https://arcteryx.co.nz/collections/outlet",
        "vendors": ["Arc'teryx", "Patagonia", "Salomon"],
    }


def test_create_shopify_collection_source_rejects_empty_url(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "shopify_collection",
            "shopify_collection_url": "",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 400
    assert "Please enter a collection URL" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_create_shopify_collection_source_rejects_invalid_url(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "shopify_collection",
            "shopify_collection_url": "not-a-url",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 400
    assert "Please enter a valid http(s) URL" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_edit_channel_shows_shopify_collection_label_and_icon(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    create_source(
        conn,
        channel_id,
        "shopify_collection",
        {"collection_url": "https://arcteryx.co.nz/collections/outlet"},
    )
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert "arcteryx.co.nz/collections/outlet" in html
    assert "🛍️" in html


def test_new_source_form_lists_land_sea_collection_option(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text
    assert 'value="land_sea_collection"' in html
    assert "Land &amp; Sea — Listing watch" in html
    assert "🌊" in html
    assert 'id="land-sea-collection-url"' in html


def test_create_land_sea_collection_source_succeeds_and_redirects(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "land_sea_collection",
            "land_sea_collection_url": "https://www.land-sea.co.nz/sale",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/admin/channels/{channel_id}/edit?source_saved=1"
    )

    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    assert row["type"] == "land_sea_collection"
    assert json.loads(row["config"]) == {
        "collection_url": "https://www.land-sea.co.nz/sale",
    }


def test_create_land_sea_collection_source_rejects_empty_url(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "land_sea_collection",
            "land_sea_collection_url": "",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 400
    assert "Please enter a listing URL" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_create_land_sea_collection_source_rejects_invalid_url(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "land_sea_collection",
            "land_sea_collection_url": "not-a-url",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 400
    assert "Please enter a valid http(s) URL" in resp.text

    conn = connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        == 0
    )


def test_edit_channel_shows_land_sea_collection_label_and_icon(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    create_source(
        conn,
        channel_id,
        "land_sea_collection",
        {"collection_url": "https://www.land-sea.co.nz/sale"},
    )
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert "land-sea.co.nz/sale" in html
    assert "🌊" in html


def test_edit_channel_copy_button_exposes_full_land_sea_filter_url(
    authed_client, db_path
):
    filtered_url = "https://www.land-sea.co.nz/outlet/mens-outlet?availability=in-stock&brands=85,97,170,102,169"
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    create_source(
        conn, channel_id, "land_sea_collection", {"collection_url": filtered_url}
    )
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    # The visible label is still truncated to netloc+path (no query string)...
    assert "land-sea.co.nz/outlet/mens-outlet" in html
    assert "availability=in-stock" not in html.split('data-copy-value="')[0]
    # ...but the copy button's data-copy-value carries the full filtered URL (HTML-escaped).
    escaped_url = filtered_url.replace("&", "&amp;")
    assert f'data-copy-value="{escaped_url}"' in html


def test_edit_channel_copy_button_exposes_full_shopify_filter_url(
    authed_client, db_path
):
    filtered_url = "https://arcteryx.co.nz/collections/outlet?vendor=Arc%27teryx"
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    create_source(
        conn, channel_id, "shopify_collection", {"collection_url": filtered_url}
    )
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert f'data-copy-value="{filtered_url}"' in html


def test_edit_channel_copy_button_falls_back_to_label_for_non_url_sources(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "personalfinancenz"}
    )
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert 'data-copy-value="r/personalfinancenz"' in html


def test_new_source_form_lists_all_about_auctions_option(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Auction watch", "Makita tools", kind="tracker")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text

    assert 'value="all_about_auctions"' in html
    assert "All About Auctions" in html
    assert 'id="type-all-about-auctions"' in html
    assert '<span class="source-icon">AA</span>' in html


def test_create_all_about_auctions_source_persists_empty_config(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Auction watch", "Makita tools", kind="tracker")
    conn.close()

    response = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "all_about_auctions", "csrf_token": "csrf1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    conn = connect(db_path)
    source = conn.execute(
        "SELECT type, config FROM sources WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert source["type"] == "all_about_auctions"
    assert source["config"] == "{}"


def test_edit_channel_shows_all_about_auctions_label_and_icon(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Auction watch", "Makita tools", kind="tracker")
    create_source(conn, channel_id, "all_about_auctions", {})
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text

    assert "All About Auctions" in html
    assert '<span class="source-name">AA All About Auctions</span>' in html


def test_add_source_page_for_monitor_channel_lists_only_monitor_types(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text

    assert 'value="shopify_collection"' in html
    assert 'value="land_sea_collection"' in html
    # Editorial and tracker Source types are not offered on a monitor Channel.
    assert 'value="reddit_subreddit"' not in html
    assert 'value="all_about_auctions"' not in html
    # The Phase 3 (twitter) editorial placeholder is hidden for a monitor Channel too.
    assert "Phase 3" not in html
    # The first compatible type is pre-selected by default.
    assert re.search(
        r'value="shopify_collection"\s+id="type-shopify"\s+checked', html
    )


def test_add_source_page_for_tracker_channel_lists_only_all_about_auctions(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Auction watch", "Makita tools", kind="tracker")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text

    assert 'value="all_about_auctions"' in html
    assert 'value="reddit_subreddit"' not in html
    assert 'value="shopify_collection"' not in html
    assert "Phase 3" not in html


def test_add_source_page_for_editorial_channel_still_lists_editorial_types(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/sources/new").text

    assert 'value="reddit_subreddit"' in html
    assert 'value="hackernews_stories"' in html
    assert 'value="shopify_collection"' not in html
    assert 'value="all_about_auctions"' not in html
    # The editorial Phase 3 (twitter) placeholder is still shown on an editorial Channel.
    assert "Phase 3" in html


def test_create_source_rejects_incompatible_type_with_localized_400(
    authed_client, db_path
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "reddit_subreddit", "subreddit": "x", "csrf_token": "csrf1"},
    )

    assert resp.status_code == 400
    assert "added to this channel type." in resp.text

    conn = connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0]
    assert count == 0


# --- Source edit -----------------------------------------------------------------------------

def test_edit_source_form_requires_session(db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()
    client = TestClient(
        create_app(db_path, session_secret="test-secret"), follow_redirects=False
    )
    resp = client.get(f"/admin/sources/{source_id}/edit")
    assert resp.status_code == 303


def test_edit_source_form_404_for_missing_source(authed_client):
    resp = authed_client.get("/admin/sources/999/edit")
    assert resp.status_code == 404


def test_edit_source_form_prefills_existing_config_and_name(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"},
        name="Kiwi money")
    conn.close()

    html = authed_client.get(f"/admin/sources/{source_id}/edit").text

    assert 'value="PersonalFinanceNZ"' in html
    assert 'value="Kiwi money"' in html
    assert re.search(r'value="reddit_subreddit"\s+id="type-reddit"\s+checked', html)


def test_edit_source_updates_config_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "old"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/edit",
        data={
            "type": "reddit_subreddit",
            "subreddit": "NewValue",
            "source_name": "Renamed",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/admin/channels/{channel_id}/edit?source_saved=1"
    )

    conn = connect(db_path)
    row = get_source(conn, source_id)
    conn.close()
    assert json.loads(row["config"])["subreddit"] == "NewValue"
    assert row["name"] == "Renamed"


def test_edit_source_can_switch_to_another_compatible_type(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "old"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/edit",
        data={"type": "google_news_query", "query": "kiwis", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 303

    conn = connect(db_path)
    row = get_source(conn, source_id)
    conn.close()
    assert row["type"] == "google_news_query"
    assert json.loads(row["config"])["query"] == "kiwis"


def test_edit_source_rejects_incompatible_type_with_localized_400(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    source_id = create_source(
        conn, channel_id, "shopify_collection", {"collection_url": "https://s/collections/x"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/edit",
        data={"type": "reddit_subreddit", "subreddit": "x", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "added to this channel type." in resp.text

    conn = connect(db_path)
    assert get_source(conn, source_id)["type"] == "shopify_collection"
    conn.close()


def test_edit_source_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "old"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/edit",
        data={"type": "reddit_subreddit", "subreddit": "new", "csrf_token": "wrong"},
    )
    assert resp.status_code == 403

    conn = connect(db_path)
    assert json.loads(get_source(conn, source_id)["config"])["subreddit"] == "old"
    conn.close()


def test_edit_source_saving_unchanged_is_not_a_self_duplicate(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/edit",
        data={"type": "reddit_subreddit", "subreddit": "x", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 303


# --- Duplicate guard -------------------------------------------------------------------------

def test_create_source_rejects_duplicate_with_localized_400(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "dup"})
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "reddit_subreddit", "subreddit": "dup", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "already has a source" in resp.text

    conn = connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_edit_source_rejects_duplicate_of_another_source(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "a"})
    editable_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "b"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{editable_id}/edit",
        data={"type": "reddit_subreddit", "subreddit": "a", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "already has a source" in resp.text

    conn = connect(db_path)
    assert json.loads(get_source(conn, editable_id)["config"])["subreddit"] == "b"
    conn.close()


def test_create_source_persists_optional_display_name(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={
            "type": "reddit_subreddit",
            "subreddit": "x",
            "source_name": "My label",
            "csrf_token": "csrf1",
        },
    )
    assert resp.status_code == 303

    conn = connect(db_path)
    row = conn.execute(
        "SELECT name FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    conn.close()
    assert row["name"] == "My label"


# --- Pause / resume --------------------------------------------------------------------------

def test_pause_source_sets_paused_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/pause", data={"csrf_token": "csrf1"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/channels/{channel_id}/edit"

    conn = connect(db_path)
    assert get_source(conn, source_id)["paused_at"] is not None
    conn.close()


def test_resume_source_clears_paused_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    set_source_paused(conn, source_id, True, now_iso="2026-07-01T00:00:00")
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/resume", data={"csrf_token": "csrf1"}
    )
    assert resp.status_code == 303

    conn = connect(db_path)
    assert get_source(conn, source_id)["paused_at"] is None
    conn.close()


def test_pause_source_404_for_missing_source(authed_client):
    resp = authed_client.post("/admin/sources/999/pause", data={"csrf_token": "csrf1"})
    assert resp.status_code == 404


def test_pause_source_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(
        f"/admin/sources/{source_id}/pause", data={"csrf_token": "wrong"}
    )
    assert resp.status_code == 403

    conn = connect(db_path)
    assert get_source(conn, source_id)["paused_at"] is None
    conn.close()


def test_edit_channel_shows_paused_status_and_resume_control(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    set_source_paused(conn, source_id, True, now_iso="2026-07-01T00:00:00")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text

    assert "Paused" in html
    assert f"/admin/sources/{source_id}/resume" in html


# --- Per-source observability ----------------------------------------------------------------

def test_edit_channel_shows_source_fetch_observability(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00", raw_count=30, new_count=4)
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text

    assert f"/admin/sources/{source_id}/edit" in html  # edit link per row
    assert "4 new of 30" in html  # raw/new counts


def test_edit_channel_shows_current_source_error(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_error(conn, source_id, "boom upstream", "2026-07-09T03:00:00")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert "boom upstream" in html


def test_edit_channel_empty_sources_offers_add_source_cta(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert f"/admin/channels/{channel_id}/sources/new" in html


# --- Stale manual-fetch recovery -------------------------------------------------------------

def _write_stale_inflight_marker(data_dir, channel_id):
    request_channel_fetch(data_dir, channel_id)
    watched = os.path.join(data_dir, "fetch_trigger_channel_id")
    inflight = watched + ".inflight"
    os.replace(watched, inflight)
    os.utime(inflight, (100, 100))  # far in the past -> stale
    return inflight


def test_edit_channel_hides_recovery_when_no_stale_marker(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert "recover-stale-fetch" not in html


def test_edit_channel_shows_recovery_when_marker_is_stale(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    _write_stale_inflight_marker(os.path.dirname(db_path), channel_id)

    html = authed_client.get(f"/admin/channels/{channel_id}/edit").text
    assert f"/admin/channels/{channel_id}/recover-stale-fetch" in html


def test_recover_stale_fetch_clears_marker_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    inflight = _write_stale_inflight_marker(os.path.dirname(db_path), channel_id)

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/recover-stale-fetch",
        data={"csrf_token": "csrf1"},
    )
    assert resp.status_code == 303
    assert not os.path.exists(inflight)


def test_recover_stale_fetch_404_for_missing_channel(authed_client):
    resp = authed_client.post(
        "/admin/channels/999/recover-stale-fetch", data={"csrf_token": "csrf1"}
    )
    assert resp.status_code == 404


def test_recover_stale_fetch_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    inflight = _write_stale_inflight_marker(os.path.dirname(db_path), channel_id)

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/recover-stale-fetch",
        data={"csrf_token": "wrong"},
    )
    assert resp.status_code == 403
    assert os.path.exists(inflight)  # untouched when CSRF fails
