"""Representative "does every supported language actually render?" coverage. Every other test
file exercises the default English behavior plus a handful of explicit zh-CN cases; this file's
job is narrower and orthogonal: loop across every beehive.localization.SUPPORTED_LANGUAGES entry
and prove that (a) full page rendering never raises MissingTranslationError/other exceptions for
any locale, (b) <html lang="..."> always matches the saved platform language, and (c) the
formatting/label helpers used across pages produce a string for every locale without raising."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import (
    claim_deep_read,
    complete_deep_read_success,
    fail_deep_read,
    request_deep_read,
)
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sessions import create_session
from beehive.db.sources import create_source, record_fetch_success
from beehive.localization import SUPPORTED_LANGUAGES, localizer_for, save_language
from beehive.web.admin import _fetch_interval_label, _source_type_options
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from beehive.web.formatting import (
    fetch_stats_label,
    freshness_label,
    next_fetch_countdown,
    relative_time,
)
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.public import _auction_pricing_facts, _engagement_label, _source_label
from scripts.set_admin_password import set_admin_password

LANGUAGE_CODES = [language.code for language in SUPPORTED_LANGUAGES]


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def authed_client(conn):
    path, c = conn
    set_admin_password(path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(
        create_app(path, session_secret="test-secret"), follow_redirects=False
    )
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_dashboard_renders_in_every_supported_language(conn, language_code):
    path, c = conn
    save_language(c, language_code)
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(
        c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"}
    )
    insert_new(
        c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x")
    )
    update_ai_ranking(c, source_id, "t1", score=90, summary="s", rationale="r")
    record_fetch_success(c, source_id, "2026-07-09T00:00:00+00:00")

    client = TestClient(create_app(path))
    resp = client.get("/")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text
    assert "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_archive_renders_in_every_supported_language(conn, language_code):
    path, c = conn
    save_language(c, language_code)

    client = TestClient(create_app(path))
    resp = client.get("/archive")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_channel_drilldown_renders_in_every_supported_language(conn, language_code):
    path, c = conn
    save_language(c, language_code)
    channel_id = create_channel(c, "Tech", "developer news")
    source_id = create_source(c, channel_id, "hackernews_stories", {"feed": "top"})
    insert_new(
        c,
        source_id,
        RawItem(
            external_id="1",
            title="A story",
            url="https://example.com/story",
            raw_metadata={"score": 10, "num_comments": 2},
        ),
    )
    update_ai_ranking(c, source_id, "1", score=90, summary="s", rationale="r")

    client = TestClient(create_app(path))
    resp = client.get(f"/channels/{channel_id}")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_monitor_and_tracker_panels_render_in_every_supported_language(
    conn,
    language_code,
):
    path, c = conn
    save_language(c, language_code)

    monitor_id = create_channel(c, "Gear", "outdoor gear", kind="monitor")
    monitor_source_id = create_source(
        c,
        monitor_id,
        "shopify_collection",
        {"collection_url": "https://example.com/collections/gear"},
    )
    insert_new(
        c,
        monitor_source_id,
        RawItem(
            external_id="product",
            title="Jacket",
            url="https://example.com/jacket",
            raw_metadata={
                "price": 80.0,
                "compare_at_price": 100.0,
                "on_sale": True,
                "available": True,
                "vendor": "Example",
                "product_type": "Jackets",
            },
        ),
    )
    update_ai_ranking(
        c,
        monitor_source_id,
        "product",
        score=90,
        summary="s",
        rationale="r",
    )

    tracker_id = create_channel(c, "Auctions", "tools", kind="tracker")
    tracker_source_id = create_source(c, tracker_id, "all_about_auctions", {})
    insert_new(
        c,
        tracker_source_id,
        RawItem(
            external_id="lot",
            title="Drill",
            url="https://example.com/drill",
            raw_metadata={
                "auction_title": "Weekly auction",
                "closing_at": (
                    datetime.now(timezone.utc) + timedelta(hours=2)
                ).isoformat(),
                "currency_code": "NZD",
                "current_bid": 50.0,
            },
        ),
    )
    update_ai_ranking(
        c,
        tracker_source_id,
        "lot",
        score=90,
        summary="s",
        rationale="r",
    )

    client = TestClient(create_app(path))
    monitor_response = client.get(f"/channels/{monitor_id}")
    tracker_response = client.get(f"/channels/{tracker_id}")

    for response in (monitor_response, tracker_response):
        assert response.status_code == 200
        assert f'<html lang="{language_code}">' in response.text
        assert "{{" not in response.text
        assert "}}" not in response.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_empty_watchlist_renders_in_every_supported_language(
    conn,
    authed_client,
    language_code,
):
    _, c = conn
    save_language(c, language_code)

    resp = authed_client.get("/watchlist")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text
    assert "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_auction_pricing_facts_render_in_every_supported_language(language_code):
    facts = _auction_pricing_facts(
        {
            "source_type": "all_about_auctions",
            "raw_metadata": {
                "currency_code": "NZD",
                "current_bid": 500.0,
                "buyer_premium_rate": 0.17,
                "estimated_cost": 585.0,
                "rrp": 1040.0,
                "rrp_excludes_gst": True,
                "starting_price": 100.0,
                "estimate_low": 700.0,
                "estimate_high": 900.0,
                "sold_price": 850.0,
            },
        },
        localizer_for(language_code),
    )

    assert len(facts) == 6
    assert all(fact for fact in facts)


_DEEP_READ_NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
_DEEP_READ_RESULT = {
    "item_id": "1",
    "bottom_line": "Rates fell by 25 basis points.",
    "key_findings": ["Inflation cooled", "Wage growth held"],
    "important_figures": [{"value": "25bp", "label": "rate cut"}],
    "why_it_matters": "Borrowing costs will ease for households.",
    "limitations": "Based on a single central bank statement.",
}


def _create_deep_read_item(c):
    channel_id = create_channel(c, "Tech", "developer news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(
        c,
        source_id,
        RawItem(external_id="t1", title="A story", url="https://example.com/a"),
    )
    update_ai_ranking(c, source_id, "t1", score=90, summary="s", rationale="r")
    return c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_deep_read_brief_page_renders_ready_state_in_every_supported_language(
    conn, language_code
):
    path, c = conn
    save_language(c, language_code)
    item_id = _create_deep_read_item(c)
    request_deep_read(c, item_id, _DEEP_READ_NOW)
    claimed = claim_deep_read(c, item_id, _DEEP_READ_NOW, lease_seconds=1500)
    complete_deep_read_success(
        c,
        item_id,
        claimed.request_version,
        claimed.claim_token,
        json.dumps(_DEEP_READ_RESULT),
        language_code,
        _DEEP_READ_NOW,
        warning_code="content_incomplete",
    )

    client = TestClient(create_app(path))
    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text
    assert "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_deep_read_brief_page_renders_pending_state_in_every_supported_language(
    conn, language_code
):
    path, c = conn
    save_language(c, language_code)
    item_id = _create_deep_read_item(c)
    request_deep_read(c, item_id, _DEEP_READ_NOW)

    client = TestClient(create_app(path))
    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text
    assert "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_deep_read_brief_page_renders_failed_state_in_every_supported_language(
    conn,
    language_code,
):
    path, c = conn
    save_language(c, language_code)
    item_id = _create_deep_read_item(c)
    request_deep_read(c, item_id, _DEEP_READ_NOW)
    claimed = claim_deep_read(c, item_id, _DEEP_READ_NOW, lease_seconds=1500)
    fail_deep_read(
        c,
        item_id,
        claimed.request_version,
        claimed.claim_token,
        "fetch",
        "raw trace",
        _DEEP_READ_NOW,
    )

    client = TestClient(create_app(path))
    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text
    assert "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_deep_read_brief_page_renders_not_requested_state_in_every_supported_language(
    conn,
    authed_client,
    language_code,
):
    path, c = conn
    save_language(c, language_code)
    item_id = _create_deep_read_item(c)

    resp = authed_client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert "{{" not in resp.text
    assert "}}" not in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_not_found_page_renders_in_every_supported_language(conn, language_code):
    path, c = conn
    save_language(c, language_code)

    client = TestClient(create_app(path))
    resp = client.get("/does-not-exist")

    assert resp.status_code == 404
    assert f'<html lang="{language_code}">' in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_admin_login_page_renders_in_every_supported_language(conn, language_code):
    path, c = conn
    save_language(c, language_code)

    client = TestClient(create_app(path))
    resp = client.get("/admin/login")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_admin_settings_page_renders_in_every_supported_language(
    authed_client, conn, language_code
):
    _, c = conn
    save_language(c, language_code)

    channels_response = authed_client.get("/admin/")
    ai_response = authed_client.get("/admin/?tab=ai")

    assert channels_response.status_code == 200
    assert ai_response.status_code == 200
    assert f'<html lang="{language_code}">' in channels_response.text
    assert f'<html lang="{language_code}">' in ai_response.text
    assert (
        localizer_for(language_code).text("web.admin.settings.channels_heading")
        in channels_response.text
    )
    # The language selector itself must always list every language's own native name,
    # regardless of which language is currently active.
    for language in SUPPORTED_LANGUAGES:
        assert language.native_name in ai_response.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_formatting_and_label_helpers_produce_text_in_every_supported_language(
    language_code,
):
    t = localizer_for(language_code)
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    sources = [
        {
            "last_fetch_at": "2026-07-13T07:00:00+00:00",
            "last_fetch_raw_count": 10,
            "last_fetch_new_count": 4,
        }
    ]

    assert relative_time("2026-07-13T07:00:00+00:00", t)
    assert freshness_label(sources, t)
    assert fetch_stats_label(sources, t)
    # A channel with sources but nothing fetched yet is imminently due -- countdown must not
    # raise or return an empty string for any locale.
    assert next_fetch_countdown(sources, 24, now, t) is not None
    assert _fetch_interval_label(24, t)
    assert _fetch_interval_label(6, t)

    for option in _source_type_options(t):
        assert option["label"]

    hn_item = {
        "source_type": "hackernews_stories",
        "source_config": '{"feed": "top"}',
        "raw_metadata": {"score": 5, "num_comments": 1},
    }
    assert _source_label(hn_item, t)
    assert _engagement_label(hn_item, t)
    assert hackernews_source_label("hackernews_stories", {"feed": "top"}, t)

    reddit_item = {
        "source_type": "reddit_subreddit",
        "source_config": '{"subreddit": "x"}',
        "raw_metadata": {"score": 5, "num_comments": 1},
    }
    assert _source_label(reddit_item, t)
    assert _engagement_label(reddit_item, t)
