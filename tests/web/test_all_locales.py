"""Representative "does every supported language actually render?" coverage. Every other test
file exercises the default English behavior plus a handful of explicit zh-CN cases; this file's
job is narrower and orthogonal: loop across every beehive.localization.SUPPORTED_LANGUAGES entry
and prove that (a) full page rendering never raises MissingTranslationError/other exceptions for
any locale, (b) <html lang="..."> always matches the saved platform language, and (c) the
formatting/label helpers used across pages produce a string for every locale without raising."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
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
from beehive.web.public import _engagement_label, _source_label
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
    client = TestClient(create_app(path, session_secret="test-secret"), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_dashboard_renders_in_every_supported_language(conn, language_code):
    path, c = conn
    save_language(c, language_code)
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
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
    insert_new(c, source_id, RawItem(
        external_id="1", title="A story", url="https://example.com/story",
        raw_metadata={"score": 10, "num_comments": 2}))
    update_ai_ranking(c, source_id, "1", score=90, summary="s", rationale="r")

    client = TestClient(create_app(path))
    resp = client.get(f"/channels/{channel_id}")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text


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
def test_admin_settings_page_renders_in_every_supported_language(authed_client, conn, language_code):
    _, c = conn
    save_language(c, language_code)

    resp = authed_client.get("/admin/")

    assert resp.status_code == 200
    assert f'<html lang="{language_code}">' in resp.text
    assert localizer_for(language_code).text(
        "web.admin.settings.channels_heading") in resp.text
    # The language selector itself must always list every language's own native name,
    # regardless of which language is currently active.
    for language in SUPPORTED_LANGUAGES:
        assert language.native_name in resp.text


@pytest.mark.parametrize("language_code", LANGUAGE_CODES)
def test_formatting_and_label_helpers_produce_text_in_every_supported_language(language_code):
    t = localizer_for(language_code)
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    sources = [{
        "last_fetch_at": "2026-07-13T07:00:00+00:00",
        "last_fetch_raw_count": 10,
        "last_fetch_new_count": 4,
    }]

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
