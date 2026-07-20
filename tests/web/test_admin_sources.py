import json
import re

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
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
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


def test_new_source_form_requires_session(db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
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


def test_create_source_succeeds_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/sources/new",
                               data={"type": "reddit_subreddit", "subreddit": "PersonalFinanceNZ",
                                     "csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/channels/{channel_id}/edit"

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM sources WHERE channel_id = ?", (channel_id,)).fetchone()
    assert row["type"] == "reddit_subreddit"
    assert json.loads(row["config"])["subreddit"] == "PersonalFinanceNZ"


def test_create_source_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/sources/new",
                               data={"type": "reddit_subreddit", "subreddit": "x",
                                     "csrf_token": "wrong"})
    assert resp.status_code == 403


def test_delete_source_removes_it_and_redirects_to_parent_channel(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(f"/admin/sources/{source_id}/delete",
                               data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/channels/{channel_id}/edit"

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE id = ?",
                         (source_id,)).fetchone()[0] == 0


def test_delete_source_404_for_missing_source(authed_client):
    resp = authed_client.post("/admin/sources/999/delete", data={"csrf_token": "csrf1"})
    assert resp.status_code == 404


def test_delete_source_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.close()

    resp = authed_client.post(f"/admin/sources/{source_id}/delete",
                               data={"csrf_token": "wrong"})
    assert resp.status_code == 403

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE id = ?",
                         (source_id,)).fetchone()[0] == 1


def test_create_google_news_source_succeeds_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/sources/new",
                               data={"type": "google_news_query", "query": "New Zealand economy",
                                     "csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/channels/{channel_id}/edit"

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM sources WHERE channel_id = ?", (channel_id,)).fetchone()
    assert row["type"] == "google_news_query"
    assert json.loads(row["config"])["query"] == "New Zealand economy"


def test_create_source_rejects_empty_subreddit_with_form_error(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/sources/new",
                               data={"type": "reddit_subreddit", "subreddit": "",
                                     "csrf_token": "csrf1"})
    assert resp.status_code == 400
    assert "subreddit" in resp.text

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE channel_id = ?",
                         (channel_id,)).fetchone()[0] == 0


def test_create_source_rejects_empty_query_with_form_error(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/sources/new",
                               data={"type": "google_news_query", "query": "",
                                     "csrf_token": "csrf1"})
    assert resp.status_code == 400
    assert "query" in resp.text

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE channel_id = ?",
                         (channel_id,)).fetchone()[0] == 0


def test_create_source_rejects_unknown_type(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/sources/new",
                               data={"type": "bogus_type", "csrf_token": "csrf1"})
    assert resp.status_code == 400
    assert "unknown Source type" in resp.text

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE channel_id = ?",
                         (channel_id,)).fetchone()[0] == 0


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
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()[0] == 0


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


def test_create_shopify_collection_source_succeeds_and_redirects(authed_client, db_path):
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
    assert resp.headers["location"] == f"/admin/channels/{channel_id}/edit"

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM sources WHERE channel_id = ?", (channel_id,)).fetchone()
    assert row["type"] == "shopify_collection"
    assert json.loads(row["config"]) == {
        "collection_url": "https://arcteryx.co.nz/collections/outlet",
    }


def test_create_shopify_collection_source_rejects_empty_url(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    conn.close()

    resp = authed_client.post(
        f"/admin/channels/{channel_id}/sources/new",
        data={"type": "shopify_collection", "shopify_collection_url": "", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "Please enter a collection URL" in resp.text

    conn = connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0] == 0


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
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0] == 0


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


def test_create_land_sea_collection_source_succeeds_and_redirects(authed_client, db_path):
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
    assert resp.headers["location"] == f"/admin/channels/{channel_id}/edit"

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM sources WHERE channel_id = ?", (channel_id,)).fetchone()
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
        data={"type": "land_sea_collection", "land_sea_collection_url": "", "csrf_token": "csrf1"},
    )
    assert resp.status_code == 400
    assert "Please enter a listing URL" in resp.text

    conn = connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0] == 0


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
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0] == 0


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
