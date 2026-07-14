import html
import re
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
from beehive.db.votes import upsert_vote
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def client(conn):
    path, _ = conn
    return TestClient(create_app(path))


@pytest.fixture
def db_path(conn):
    path, _ = conn
    return path


@pytest.fixture
def authed_client(db_path, conn):
    _, c = conn
    set_admin_password(db_path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


def test_dashboard_channel_tab_keeps_secondary_teaser_link(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")
    record_fetch_success(c, source_id, "2026-07-09T00:00:00+00:00")

    resp = client.get("/")
    assert resp.status_code == 200
    assert "NZ Finance" in resp.text
    assert "RBNZ 降息" in resp.text
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    assert (
        f'class="dashboard-channel-teaser" href="/items/{item_id}/open" '
        'target="_blank" rel="noopener noreferrer"'
    ) in resp.text
    assert 'aria-label="打开 NZ Finance 最新信号：RBNZ 降息（在新窗口打开）"' in resp.text


def test_dashboard_renders_fingerprinted_static_assets(client):
    resp = client.get("/")
    assert resp.status_code == 200
    match = re.search(r'href="/static/beehive\.css\?v=([0-9a-f]{12})"', resp.text)
    assert match is not None
    assert f'src="/static/beehive.js?v={match.group(1)}"' in resp.text


def test_dashboard_shows_unread_count(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="A", url="https://x"))
    insert_new(c, source_id, RawItem(external_id="t2", title="B", url="https://y"))

    resp = client.get("/")
    assert " · 2 新</span>" in resp.text


def test_dashboard_renders_with_no_channels(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "从第一个 Channel 开始" in resp.text
    assert " · 0 新</span>" in resp.text


def test_create_app_bootstraps_schema_on_fresh_db(tmp_path):
    """Regression test: a freshly-deployed beehive-data.volume has no tables until
    something calls init_schema. create_app must bootstrap the schema itself so a brand
    new DB file (never passed through the conn/init_schema fixture above) still serves a
    working Dashboard instead of crashing with "no such table: channels"."""
    fresh_path = str(tmp_path / "brand_new.db")
    fresh_client = TestClient(create_app(fresh_path))
    resp = fresh_client.get("/")
    assert resp.status_code == 200
    assert "从第一个 Channel 开始" in resp.text
    assert " · 0 新</span>" in resp.text


def test_channel_drilldown_shows_item_with_source_badge_and_link(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(
        external_id="t1", title="Rates fall", url="https://reddit.com/r/x/comments/t1",
        raw_metadata={"score": 412, "num_comments": 189}))
    update_ai_ranking(c, source_id, "t1", score=91, summary="RBNZ 降息", rationale="匹配利率变化")

    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    assert "r/PersonalFinanceNZ" in resp.text
    assert "RBNZ 降息" in resp.text
    assert "匹配利率变化" in resp.text
    assert "412" in resp.text and "189" in resp.text
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    assert f'href="/items/{item_id}/open"' in resp.text
    assert resp.text.count(f'href="/items/{item_id}/open"') == 2


def test_channel_drilldown_shows_google_news_source_label_and_summary(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech News", "AI industry news")
    source_id = create_source(c, channel_id, "google_news_query", {"query": "OpenAI"})
    insert_new(c, source_id, RawItem(external_id="g1", title="A story",
                                      url="https://news.google.com/rss/articles/abc?oc=5"))
    update_ai_ranking(c, source_id, "g1", score=91, summary="摘要", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    # Jinja auto-escapes the label's double quotes, so compare against the unescaped HTML.
    assert '"OpenAI"' in html.unescape(resp.text)


def test_channel_drilldown_shows_google_news_item_with_publisher_engagement_label(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech News", "AI industry news")
    source_id = create_source(c, channel_id, "google_news_query", {"query": "OpenAI"})
    insert_new(c, source_id, RawItem(
        external_id="g1", title="Apple sues OpenAI - BBC",
        url="https://news.google.com/rss/articles/abc123?oc=5",
        raw_metadata={"source_name": "Reuters"}))
    update_ai_ranking(c, source_id, "g1", score=91, summary="苹果起诉OpenAI", rationale="重大科技新闻")

    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    assert "Reuters" in resp.text
    assert "0赞 0评论" not in resp.text


def test_channel_drilldown_google_news_item_with_no_source_name_shows_no_engagement_label(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech News", "AI industry news")
    source_id = create_source(c, channel_id, "google_news_query", {"query": "OpenAI"})
    insert_new(c, source_id, RawItem(
        external_id="g1", title="Some story", url="https://news.google.com/rss/articles/abc?oc=5",
        raw_metadata={}))
    update_ai_ranking(c, source_id, "g1", score=91, summary="摘要", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    assert "0赞 0评论" not in resp.text


def test_channel_drilldown_splits_highlighted_and_folded(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    for i in range(10):
        insert_new(c, source_id, RawItem(external_id=f"t{i}", title=f"Item {i}",
                                          url=f"https://x/{i}", raw_metadata={}))
        update_ai_ranking(c, source_id, f"t{i}", score=100 - i, summary=f"summary {i}", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    assert "summary 0" in resp.text  # rank 0 is in the highlighted tier
    assert "summary 9" in resp.text  # rank 9 is folded but still rendered as a one-liner


def test_channel_drilldown_folded_item_links_to_the_original_post_in_a_new_tab(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    for i in range(10):
        insert_new(c, source_id, RawItem(external_id=f"t{i}", title=f"Item {i}",
                                          url=f"https://reddit.com/{i}", raw_metadata={}))
        update_ai_ranking(c, source_id, f"t{i}", score=100 - i, summary=f"summary {i}", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    # rank 9 (score 91) is in the folded tier (HIGHLIGHT_COUNT=8 caps the highlighted tier at
    # the top 8); its summary text must link out to the original post like every other post's
    # title/summary does since sub-project B, not stay plain unlinked text.
    item_id = c.execute("SELECT id FROM items WHERE external_id='t9'").fetchone()[0]
    assert f'href="/items/{item_id}/open" target="_blank" rel="noopener noreferrer"' in resp.text


def test_channel_drilldown_folded_google_news_item_shows_publisher_not_zero_score(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech News", "AI industry news")
    source_id = create_source(c, channel_id, "google_news_query", {"query": "OpenAI"})
    for i in range(10):
        insert_new(c, source_id, RawItem(
            external_id=f"g{i}", title=f"Story {i}",
            url=f"https://news.google.com/rss/articles/{i}?oc=5",
            raw_metadata={"source_name": "Reuters"} if i == 9 else {}))
        update_ai_ranking(c, source_id, f"g{i}", score=100 - i, summary=f"summary {i}", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    # rank 9 (score 91) lands in the folded tier (HIGHLIGHT_COUNT=8 caps the highlighted tier at the
    # top 8). A Google News item has no "score", so the folded tier must render its publisher via
    # item.engagement_label, never the old hardcoded, meaningless "(0赞)".
    assert resp.status_code == 200
    assert "Reuters" in resp.text
    assert "(0赞)" not in resp.text


def test_channel_drilldown_shows_hackernews_feed_label_summary_and_engagement(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech", "developer news")
    source_id = create_source(c, channel_id, "hackernews_stories", {"feed": "top"})
    insert_new(
        c,
        source_id,
        RawItem(
            external_id="48888193",
            title="A useful story",
            url="https://example.com/story",
            raw_metadata={"score": 321, "num_comments": 87, "hn_id": 48888193},
        ),
    )
    update_ai_ranking(c, source_id, "48888193", score=91, summary="中文摘要", rationale="重要")

    resp = client.get(f"/channels/{channel_id}")

    assert resp.status_code == 200
    assert "HN · 热门" in resp.text
    assert "来源：HN · 热门" in resp.text
    assert "321分 87评论" in resp.text


def test_channel_drilldown_shows_unknown_hackernews_feed_after_prefix(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech", "developer news")
    source_id = create_source(c, channel_id, "hackernews_stories", {"feed": "front_page"})
    insert_new(
        c,
        source_id,
        RawItem(
            external_id="99",
            title="Unknown feed story",
            url="https://example.com/unknown",
            raw_metadata={"score": 10, "num_comments": 2, "hn_id": 99},
        ),
    )
    update_ai_ranking(c, source_id, "99", score=80, summary="摘要", rationale="重要")

    resp = client.get(f"/channels/{channel_id}")

    assert resp.status_code == 200
    assert "HN · front_page" in resp.text
    assert "hackernews_stories" not in resp.text


def test_channel_drilldown_shows_hackernews_query_label(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech", "developer news")
    source_id = create_source(
        c,
        channel_id,
        "hackernews_query",
        {"query": "local-first", "sort": "recent"},
    )
    insert_new(
        c,
        source_id,
        RawItem(
            external_id="42",
            title="Query result",
            url="https://example.com/result",
            raw_metadata={"score": 55, "num_comments": 12, "hn_id": 42},
        ),
    )
    update_ai_ranking(c, source_id, "42", score=90, summary="摘要", rationale="匹配")

    resp = client.get(f"/channels/{channel_id}")

    assert "HN 搜索 · local-first" in resp.text
    assert "55分 12评论" in resp.text


def test_folded_hackernews_item_keeps_point_and_comment_engagement(conn, client):
    _, c = conn
    channel_id = create_channel(c, "Tech", "developer news")
    source_id = create_source(c, channel_id, "hackernews_stories", {"feed": "best"})
    for index in range(10):
        insert_new(
            c,
            source_id,
            RawItem(
                external_id=str(index),
                title=f"Story {index}",
                url=f"https://example.com/{index}",
                raw_metadata={
                    "score": 55 if index == 9 else index,
                    "num_comments": 12 if index == 9 else index,
                },
            ),
        )
        update_ai_ranking(
            c,
            source_id,
            str(index),
            score=100 - index,
            summary=f"summary {index}",
            rationale="r",
        )

    resp = client.get(f"/channels/{channel_id}")

    assert "summary 9" in resp.text
    assert "55分 12评论" in resp.text


def test_channel_drilldown_shows_unread_count_and_source_summary(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="RBNZ 降息", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    assert "1 条新内容" in resp.text
    assert "来源：r/PersonalFinanceNZ" in resp.text


def test_channel_drilldown_404_for_missing_channel(client):
    resp = client.get("/channels/999")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Channel not found"}


def test_unknown_route_uses_branded_not_found_page(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert "页面不存在" in resp.text
    assert 'href="/"' in resp.text


def test_create_app_reads_session_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "env-secret")
    fresh_app = create_app(str(tmp_path / "t.db"))
    assert fresh_app.state.session_secret == "env-secret"


def test_create_app_accepts_explicit_session_secret_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "env-secret")
    fresh_app = create_app(str(tmp_path / "t2.db"), session_secret="explicit-secret")
    assert fresh_app.state.session_secret == "explicit-secret"


def test_response_includes_security_headers(client):
    resp = client.get("/")
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


def test_response_disallows_search_indexing(client):
    resp = client.get("/")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in resp.text
    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        in resp.text
    )


def test_channel_drilldown_link_has_noopener_and_validated_scheme(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(
        external_id="t1", title="Rates fall", url="javascript:alert(1)"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    assert 'rel="noopener noreferrer"' in resp.text
    assert "javascript:alert" not in resp.text


def test_static_htmx_file_is_served(client):
    resp = client.get("/static/htmx.min.js")
    assert resp.status_code == 200
    assert "htmx" in resp.text.lower()


def test_csp_allows_self_hosted_scripts(client):
    resp = client.get("/")
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert "script-src 'none'" not in resp.headers["content-security-policy"]


def test_drilldown_shows_interactive_vote_buttons_when_authenticated(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")

    resp = authed_client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    assert "hx-post=" in resp.text
    assert "csrf_token" in resp.text
    assert '/static/htmx.min.js' in resp.text


def test_drilldown_shows_static_vote_state_when_anonymous(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(c, item_id, 1)

    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    assert "hx-post=" not in resp.text
    assert 'class="vote-state up"' in resp.text


def test_vote_controls_restore_focus_and_announce_status(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = authed_client.get(f"/channels/{channel_id}")

    assert f'data-focus-key="item-{item_id}-up"' in resp.text
    assert f'data-focus-key="item-{item_id}-down"' in resp.text
    assert f'data-focus-key="item-{item_id}-reason"' not in resp.text
    assert 'aria-pressed="false"' in resp.text
    assert 'class="votes" role="group" aria-label="内容反馈"' in resp.text
    assert 'id="feedback-status"' in resp.text
    assert 'role="status" aria-live="polite"' in resp.text
    assert '<script src="/static/beehive.js?v=' in resp.text


def test_drilldown_hides_vote_reason_when_anonymous(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(c, item_id, -1, "this is a private reason nobody else should see")

    resp = client.get(f"/channels/{channel_id}")
    assert "this is a private reason nobody else should see" not in resp.text


def test_vote_route_requires_session(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="T", url="https://x"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = client.post(f"/items/{item_id}/vote", data={"value": "1", "csrf_token": "x"},
                        follow_redirects=False)
    assert resp.status_code == 303


def test_vote_route_casts_upvote_and_returns_fragment(conn, authed_client, db_path):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200
    assert 'class="up"' in resp.text
    assert 'aria-pressed="true"' in resp.text

    conn2 = connect(db_path)
    vote = conn2.execute("SELECT * FROM votes WHERE item_id=?", (item_id,)).fetchone()
    assert vote["value"] == 1


def test_vote_route_clicking_same_polarity_again_unvotes(conn, authed_client, db_path):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    authed_client.post(f"/items/{item_id}/vote", data={"value": "1", "csrf_token": "csrf1"})
    resp = authed_client.post(f"/items/{item_id}/vote", data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200

    conn2 = connect(db_path)
    assert conn2.execute("SELECT * FROM votes WHERE item_id=?", (item_id,)).fetchone() is None


def test_vote_route_reason_update_keeps_polarity(conn, authed_client, db_path):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    authed_client.post(f"/items/{item_id}/vote", data={"value": "-1", "csrf_token": "csrf1"})
    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "-1", "reason": "too niche", "csrf_token": "csrf1"})
    assert resp.status_code == 200

    conn2 = connect(db_path)
    vote = conn2.execute("SELECT * FROM votes WHERE item_id=?", (item_id,)).fetchone()
    assert vote["value"] == -1
    assert vote["reason"] == "too niche"


def test_vote_route_rejects_wrong_csrf(conn, authed_client, db_path):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="T", url="https://x"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "wrong"})
    assert resp.status_code == 403

    conn2 = connect(db_path)
    assert conn2.execute("SELECT * FROM votes WHERE item_id=?", (item_id,)).fetchone() is None


def test_viewing_drilldown_as_owner_marks_items_read_for_next_visit(conn, authed_client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")

    first = authed_client.get(f"/channels/{channel_id}")
    assert "1 条新内容" in first.text  # THIS render still shows the pre-visit unread count

    second = authed_client.get(f"/channels/{channel_id}")
    assert "0 条新内容" in second.text  # the NEXT render reflects last visit's mark-read


def test_viewing_drilldown_anonymously_does_not_mark_items_read(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=91, summary="s", rationale="r")

    client.get(f"/channels/{channel_id}")
    client.get(f"/channels/{channel_id}")  # visiting twice as an anonymous reader

    item = c.execute("SELECT is_read FROM items WHERE external_id='t1'").fetchone()
    assert item["is_read"] == 0


def test_mark_all_read_route_marks_channel_and_redirects(conn, authed_client, db_path):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="T", url="https://x"))

    resp = authed_client.post(f"/channels/{channel_id}/mark-all-read",
                               data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/channels/{channel_id}"

    conn2 = connect(db_path)
    item = conn2.execute("SELECT is_read FROM items WHERE external_id='t1'").fetchone()
    assert item["is_read"] == 1


def test_mark_all_read_route_requires_session(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    resp = client.post(f"/channels/{channel_id}/mark-all-read",
                        data={"csrf_token": "x"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_archive_shows_items_across_channels_grouped_by_day(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    c.execute("UPDATE items SET fetched_at = '2026-07-05T08:00:00' WHERE external_id='t1'")
    c.commit()

    resp = client.get("/archive")
    assert resp.status_code == 200
    assert "Rates fall" in resp.text
    assert "2026-07-05" in resp.text


def test_archive_filters_by_channel_query_param(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    other_id = create_channel(c, "Other Channel", "profile")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    other_source_id = create_source(c, other_id, "reddit_subreddit", {"subreddit": "y"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates news", url="https://x"))
    insert_new(c, other_source_id, RawItem(external_id="t2", title="Other news", url="https://y"))

    resp = client.get(f"/archive?channel={channel_id}")
    assert "Rates news" in resp.text
    assert "Other news" not in resp.text


def test_archive_never_marks_anything_read(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))

    client.get("/archive")

    item = c.execute("SELECT is_read FROM items WHERE external_id='t1'").fetchone()
    assert item["is_read"] == 0


def test_archive_paginates_with_page_query_param(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    for i in range(35):
        insert_new(c, source_id, RawItem(external_id=f"t{i}", title=f"Title {i}", url="https://x"))
        c.execute("UPDATE items SET fetched_at = ? WHERE external_id = ?",
                   (f"2026-07-{(i % 28) + 1:02d}T00:00:00", f"t{i}"))
    c.commit()

    first_page = client.get("/archive")
    second_page = client.get("/archive?page=2")
    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.text != second_page.text


def test_archive_strips_vote_reason_from_render_context(conn, client):
    """Archive is always anonymous (no session dependency), so the private down-vote reason
    must never reach the template context — not just be left CSS-hidden/unrendered. archive.html
    happens not to print vote_reason today, so a resp.text check alone can't catch a route-level
    regression; the context assertion guards the invariant the route strip actually enforces."""
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(c, item_id, -1, "this is a private reason nobody else should see")

    resp = client.get("/archive")
    assert "this is a private reason nobody else should see" not in resp.text
    ctx_items = [it for _day, day_items in resp.context["groups"] for it in day_items]
    assert ctx_items, "the down-voted item should appear in the archive context"
    assert all(it["vote_reason"] is None for it in ctx_items)


def test_dashboard_header_links_to_archive_and_admin_login(conn, client):
    resp = client.get("/")
    assert 'href="/archive"' in resp.text
    assert 'href="/admin/login"' in resp.text


def test_dashboard_teaser_links_to_the_original_post_in_a_new_tab(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall",
                                      url="https://reddit.com/r/x/comments/t1"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = client.get("/")
    assert resp.status_code == 200
    assert f'href="/items/{item_id}/open" target="_blank" rel="noopener noreferrer"' in resp.text
    assert "RBNZ 降息" in resp.text


def test_dashboard_card_link_goes_to_the_channel_not_the_post(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    resp = client.get("/")
    assert f'href="/channels/{channel_id}"' in resp.text
    # internal site navigation must never carry target="_blank" -- only links to the
    # original post do
    assert f'href="/channels/{channel_id}" target="_blank"' not in resp.text


def test_archive_title_links_to_the_original_post_in_a_new_tab(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall",
                                      url="https://reddit.com/r/x/comments/t1"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = client.get("/archive")
    assert f'href="/items/{item_id}/open" target="_blank" rel="noopener noreferrer"' in resp.text
    assert "RBNZ 降息" in resp.text


def test_archive_filters_by_search_query_param(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Interest rates rise", url="https://x"))
    insert_new(c, source_id, RawItem(external_id="t2", title="Housing market update", url="https://y"))

    resp = client.get("/archive?q=rates")
    assert "Interest rates rise" in resp.text
    assert "Housing market update" not in resp.text


def test_dashboard_logo_links_to_home(client):
    resp = client.get("/")
    assert 'class="brand"' in resp.text
    assert 'class="brand-mark"' in resp.text
    assert 'href="/" aria-current="page"' in resp.text


def test_dashboard_exposes_accessible_navigation_and_skip_link(client):
    resp = client.get("/")
    assert '<a class="skip-link" href="#main-content">' in resp.text
    assert '<nav class="site-nav" aria-label="主导航">' in resp.text
    assert '<main id="main-content"' in resp.text


def test_favicon_is_served(client):
    resp = client.get("/static/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")


def test_interaction_helper_is_served(client):
    resp = client.get("/static/beehive.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")


def test_dashboard_shows_ranked_signal_table(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall",
                                      url="https://reddit.com/r/x/comments/t1"))
    update_ai_ranking(c, source_id, "t1", score=95, summary="RBNZ 大幅降息", rationale="r")

    resp = client.get("/")
    assert resp.status_code == 200
    assert '<table class="signal-table">' in resp.text
    assert '<th scope="col">分数</th>' in resp.text
    assert '<th scope="col">Channel</th>' in resp.text
    assert '<th scope="col" class="signal-source-heading">来源</th>' in resp.text
    assert '<th scope="col">AI 摘要</th>' in resp.text
    assert 'class="signal-row is-unread"' in resp.text
    assert f'class="signal-channel" href="/channels/{channel_id}"' in resp.text
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    assert f'class="signal-summary" href="/items/{item_id}/open" target="_blank" rel="noopener noreferrer"' in resp.text
    assert "RBNZ 大幅降息" in resp.text


def test_dashboard_hides_signal_table_when_nothing_qualifies(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<table class="signal-table">' not in resp.text
    assert "当前没有待处理信号" in resp.text


def test_dashboard_requests_twenty_four_ranked_signals(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    for index in range(25):
        external_id = f"signal-{index}"
        insert_new(c, source_id, RawItem(
            external_id=external_id,
            title=f"Signal {index}",
            url=f"https://example.com/{index}",
        ))
        update_ai_ranking(
            c,
            source_id,
            external_id,
            score=float(index),
            summary=f"摘要 {index}",
            rationale="r",
        )

    resp = client.get("/")

    assert resp.status_code == 200
    assert len(resp.context["highlights"]) == 24
    assert resp.context["has_more_signals"] is True
    assert "<span>24+</span>" in resp.text
    assert "<b>24+ / 25</b>" in resp.text
    assert "摘要 24" in resp.text
    assert "摘要 0" not in resp.text


def test_dashboard_counts_all_pending_high_priority_signals(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    for index in range(25):
        external_id = f"signal-{index}"
        insert_new(c, source_id, RawItem(
            external_id=external_id,
            title=f"Signal {index}",
            url=f"https://example.com/{index}",
        ))
        update_ai_ranking(
            c,
            source_id,
            external_id,
            score=90 + index,
            summary=f"摘要 {index}",
            rationale="r",
        )

    resp = client.get("/")

    assert resp.context["high_priority_count"] == 25
    assert resp.context["pending_signal_count"] == 25
    assert "≥90 25" in resp.text
    assert "<b>24+ / 25</b>" in resp.text


def test_archive_treats_empty_channel_query_param_as_no_filter(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))

    # An HTML <select> with an empty-value "全部" (all) option submits channel="" when that
    # option is chosen, not an omitted param -- this must behave like "no channel filter",
    # not crash with a 422 (int_parsing) error.
    resp = client.get("/archive?channel=")
    assert resp.status_code == 200
    assert "Rates fall" in resp.text


def test_archive_treats_non_numeric_channel_query_param_as_no_filter(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))

    # A hand-crafted non-numeric value (never produced by the <select>, whose only non-empty
    # options are real channel ids) must not 500 -- treat it the same as "no filter".
    resp = client.get("/archive?channel=not-a-number")
    assert resp.status_code == 200
    assert "Rates fall" in resp.text


def test_archive_treats_empty_date_query_params_as_no_filter(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))

    # An unfilled <input type="date"> submits from=/to="" (empty string), not an omitted
    # param -- this must behave like "no date filter", not silently exclude every row
    # (date('') is NULL in SQLite, so a naive `>= date('')` comparison matches nothing).
    resp = client.get("/archive?from=&to=")
    assert resp.status_code == 200
    assert "Rates fall" in resp.text


def test_dashboard_teaser_carries_an_exact_time_tooltip(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x",
                                      created_at=datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    resp = client.get("/")
    assert 'title="2026-07-09 12:00"' in resp.text


def test_dashboard_shows_each_channels_own_next_fetch_countdown(
    conn,
    client,
    monkeypatch,
):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr("beehive.web.public.datetime", FixedDatetime)
    _, c = conn
    daily_id = create_channel(
        c,
        "Daily",
        "profile",
        fetch_interval_hours=24,
    )
    new_id = create_channel(
        c,
        "Never fetched",
        "profile",
        fetch_interval_hours=24,
    )
    daily_source = create_source(c, daily_id, "reddit_subreddit", {"subreddit": "daily"})
    create_source(c, new_id, "reddit_subreddit", {"subreddit": "new"})
    record_fetch_success(c, daily_source, "2026-07-13T07:00:00+00:00")

    resp = client.get("/")

    assert resp.status_code == 200
    assert resp.text.count("21小时后抓取") == 1
    assert resp.text.count("1分钟后抓取") == 1


def test_dashboard_hides_next_fetch_for_channel_without_sources(
    conn,
    client,
    monkeypatch,
):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr("beehive.web.public.datetime", FixedDatetime)
    _, c = conn
    create_channel(c, "Empty", "profile", fetch_interval_hours=24)

    resp = client.get("/")

    assert resp.status_code == 200
    assert "后抓取" not in resp.text


def test_dashboard_freshness_has_exact_time_tooltip(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(c, source_id, "2026-07-09T03:00:00")

    resp = client.get("/")
    assert 'title="2026-07-09 15:00 ·' in resp.text  # NZST = UTC+12 in July


def test_channel_drilldown_freshness_has_exact_time_tooltip(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(c, source_id, "2026-07-09T03:00:00")

    resp = client.get(f"/channels/{channel_id}")
    assert 'title="2026-07-09 15:00"' in resp.text  # NZST = UTC+12 in July


def test_open_item_route_redirects_to_the_real_url_and_marks_it_opened(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall",
                                      url="https://reddit.com/r/x/comments/t1"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = client.get(f"/items/{item_id}/open", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://reddit.com/r/x/comments/t1"

    row = c.execute("SELECT opened_at FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["opened_at"] is not None


def test_open_item_route_404s_for_a_missing_item(client):
    resp = client.get("/items/999/open", follow_redirects=False)
    assert resp.status_code == 404


def test_dashboard_teaser_link_goes_through_the_open_tracking_route(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    resp = client.get("/")
    assert f'href="/items/{item_id}/open"' in resp.text
    assert 'href="https://x" target="_blank"' not in resp.text  # no longer a direct link


def test_dashboard_shows_fetch_stats(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(c, source_id, "2026-07-09T03:00:00", raw_count=50, new_count=20)

    resp = client.get("/")
    assert "50" in resp.text and "20" in resp.text and "40%" in resp.text


def test_channel_drilldown_hides_read_items_by_default(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Unread item", url="https://x"))
    insert_new(c, source_id, RawItem(external_id="t2", title="Read item", url="https://y"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t2'").fetchone()[0]
    c.execute("UPDATE items SET is_read = 1 WHERE id = ?", (item_id,))
    c.commit()

    resp = client.get(f"/channels/{channel_id}")
    assert "Unread item" in resp.text
    assert "Read item" not in resp.text
    assert "1 条已读内容" in resp.text
    assert f'href="/channels/{channel_id}?show_read=1"' in resp.text


def test_channel_drilldown_shows_read_items_when_show_read_param_present(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Unread item", url="https://x"))
    insert_new(c, source_id, RawItem(external_id="t2", title="Read item", url="https://y"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t2'").fetchone()[0]
    c.execute("UPDATE items SET is_read = 1 WHERE id = ?", (item_id,))
    c.commit()

    resp = client.get(f"/channels/{channel_id}?show_read=1")
    assert "Unread item" in resp.text
    assert "Read item" in resp.text


def test_channel_drilldown_no_reveal_line_when_nothing_is_read(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Unread item", url="https://x"))

    resp = client.get(f"/channels/{channel_id}")
    assert "条已读内容" not in resp.text


def test_channel_drilldown_shows_best_comment_summary_when_present(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    c.execute("UPDATE items SET best_comment_summary = ? WHERE id = ?",
              ("有人指出实际数字不同", item_id))
    c.commit()

    resp = client.get(f"/channels/{channel_id}")
    assert 'class="comment-mark"' in resp.text
    assert "有人指出实际数字不同" in resp.text


def test_channel_drilldown_shows_nothing_extra_when_best_comment_summary_is_absent(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    resp = client.get(f"/channels/{channel_id}")
    assert 'class="comment-mark"' not in resp.text


def test_dashboard_signal_shows_best_comment_summary_when_present(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=95, summary="RBNZ 大幅降息", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    c.execute("UPDATE items SET best_comment_summary = ? WHERE id = ?",
              ("有人指出实际数字不同", item_id))
    c.commit()

    resp = client.get("/")
    assert '<details class="signal-comment">' in resp.text
    assert '<summary aria-label="查看最佳评论摘要">“</summary>' in resp.text
    assert "有人指出实际数字不同" in resp.text


def test_dashboard_signal_shows_nothing_extra_when_best_comment_summary_is_absent(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=95, summary="RBNZ 大幅降息", rationale="r")

    resp = client.get("/")
    assert 'class="signal-comment"' not in resp.text


def test_archive_shows_best_comment_summary_when_present(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    c.execute("UPDATE items SET best_comment_summary = ? WHERE id = ?",
              ("有人指出实际数字不同", item_id))
    c.commit()

    resp = client.get("/archive")
    assert 'class="comment-mark"' in resp.text
    assert "有人指出实际数字不同" in resp.text


def test_archive_shows_nothing_extra_when_best_comment_summary_is_absent(conn, client):
    _, c = conn
    channel_id = create_channel(c, "NZ Finance", "economic news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(c, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    resp = client.get("/archive")
    assert 'class="comment-mark"' not in resp.text


def test_official_source_label_uses_fixed_public_label():
    from beehive.web.public import _source_label

    item = {
        "source_type": "rbnz_news",
        "source_config": "{}",
        "raw_metadata": {"publisher": "RBNZ News"},
    }
    assert _source_label(item) == "RBNZ News"


def test_federal_reserve_category_is_the_secondary_label():
    from beehive.web.public import _engagement_label

    item = {
        "source_type": "federal_reserve_news",
        "source_config": "{}",
        "raw_metadata": {"publisher": "Federal Reserve", "category": "Monetary Policy"},
    }
    assert _engagement_label(item) == "Monetary Policy"


def test_official_feed_without_category_has_no_secondary_label():
    from beehive.web.public import _engagement_label

    item = {
        "source_type": "nz_government_news",
        "source_config": "{}",
        "raw_metadata": {"publisher": "NZ Government"},
    }
    assert _engagement_label(item) == ""


def test_official_source_summary_uses_public_label():
    from beehive.web.public import _source_summary

    sources = [{"type": "nz_government_news", "config": "{}"}]
    assert _source_summary(sources) == "NZ Government"
