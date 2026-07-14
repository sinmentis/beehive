from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from beehive.connectors.base import CommentFetchTarget
from beehive.connectors.hackernews import (
    HackerNewsQueryConnector,
    HackerNewsStoriesConnector,
    _default_fetch_json,
)

_OFFICIAL_BASE = "https://hacker-news.firebaseio.com/v0"


def _story(
    item_id=48888193,
    *,
    title="A useful story",
    url="https://example.com/story",
    text="<p>First paragraph.</p><p>Second paragraph.</p>",
    score=321,
    descendants=87,
    created_at=1783900000,
    author="alice",
    item_type="story",
    deleted=False,
    dead=False,
):
    return {
        "id": item_id,
        "type": item_type,
        "by": author,
        "time": created_at,
        "title": title,
        "url": url,
        "text": text,
        "score": score,
        "descendants": descendants,
        "deleted": deleted,
        "dead": dead,
    }


@pytest.mark.parametrize(
    ("feed", "endpoint"),
    [
        ("top", "topstories"),
        ("best", "beststories"),
        ("new", "newstories"),
        ("ask", "askstories"),
        ("show", "showstories"),
        ("job", "jobstories"),
    ],
)
def test_stories_uses_the_matching_official_feed(feed, endpoint):
    calls = []

    def fetch_json(url):
        calls.append(url)
        if url.endswith(f"/{endpoint}.json"):
            return [1]
        return _story(item_id=1)

    items = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": feed})

    assert calls[0] == f"{_OFFICIAL_BASE}/{endpoint}.json"
    assert items[0].external_id == "1"


def test_stories_maps_outbound_story_fields_and_metadata():
    def fetch_json(url):
        if url.endswith("/topstories.json"):
            return [48888193]
        return _story()

    item = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})[0]

    assert item.external_id == "48888193"
    assert item.title == "A useful story"
    assert item.url == "https://example.com/story"
    assert item.body == "First paragraph.\nSecond paragraph."
    assert item.created_at == datetime.fromtimestamp(1783900000, tz=timezone.utc)
    assert item.raw_metadata == {
        "provider": "hackernews",
        "hn_id": 48888193,
        "hn_url": "https://news.ycombinator.com/item?id=48888193",
        "item_type": "story",
        "author": "alice",
        "score": 321,
        "num_comments": 87,
    }


def test_default_json_fetch_uses_user_agent_and_timeout():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'

    with patch(
        "beehive.connectors.hackernews.urllib.request.urlopen",
        return_value=response,
    ) as urlopen:
        payload = _default_fetch_json(f"{_OFFICIAL_BASE}/topstories.json")

    request = urlopen.call_args.args[0]
    assert request.get_header("User-agent") == "beehive/0.1 (personal information hub)"
    assert urlopen.call_args.kwargs["timeout"] == 15
    assert payload == {"ok": True}


def test_stories_caps_plain_text_body_at_fifteen_hundred_characters():
    def fetch_json(url):
        if url.endswith("/topstories.json"):
            return [1]
        return _story(item_id=1, text=f"<p>{'x' * 2000}</p>")

    item = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})[0]
    assert len(item.body) == 1500


def test_stories_accepts_job_items_and_uses_discussion_fallback():
    def fetch_json(url):
        if url.endswith("/jobstories.json"):
            return [7]
        return _story(item_id=7, item_type="job", url=None)

    item = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "job"})[0]
    assert item.url == "https://news.ycombinator.com/item?id=7"
    assert item.raw_metadata["item_type"] == "job"


@pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "/relative/path"])
def test_stories_falls_back_to_the_discussion_url_without_a_usable_outbound_url(url):
    def fetch_json(request_url):
        if request_url.endswith("/askstories.json"):
            return [42]
        return _story(item_id=42, url=url)

    item = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "ask"})[0]
    assert item.url == "https://news.ycombinator.com/item?id=42"
    assert item.raw_metadata["hn_url"] == item.url


def test_stories_caps_detail_requests_at_thirty_and_preserves_feed_order():
    calls = []

    def fetch_json(url):
        calls.append(url)
        if url.endswith("/topstories.json"):
            return list(range(1, 41))
        item_id = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        return _story(item_id=item_id, title=f"Story {item_id}")

    items = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})

    assert len(items) == 30
    assert [item.external_id for item in items] == [str(item_id) for item_id in range(1, 31)]
    assert not any("/item/31.json" in url for url in calls)


def test_stories_uses_bounded_detail_workers():
    def fetch_json(url):
        if url.endswith("/topstories.json"):
            return [1, 2]
        item_id = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        return _story(item_id=item_id)

    with patch(
        "beehive.connectors.hackernews.ThreadPoolExecutor",
        wraps=ThreadPoolExecutor,
    ) as executor:
        HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})

    assert executor.call_args.kwargs["max_workers"] == 8


def test_stories_enforces_the_detail_batch_deadline():
    def fetch_json(url):
        if url.endswith("/topstories.json"):
            return [1, 2]
        return _story(item_id=1)

    with patch(
        "beehive.connectors.hackernews.as_completed",
        side_effect=FutureTimeoutError,
    ) as completed:
        with pytest.raises(RuntimeError, match="no usable Items"):
            HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})

    assert completed.call_args.kwargs["timeout"] == 35


def test_stories_keeps_partial_success_and_logs_bad_details(capsys):
    def fetch_json(url):
        if url.endswith("/topstories.json"):
            return [1, 2, 3, 4, 5, 6]
        if url.endswith("/item/1.json"):
            return _story(item_id=1, title="Good")
        if url.endswith("/item/2.json"):
            raise TimeoutError("detail timeout")
        if url.endswith("/item/3.json"):
            return None
        if url.endswith("/item/4.json"):
            return _story(item_id=4, deleted=True)
        if url.endswith("/item/5.json"):
            return _story(item_id=5, dead=True)
        return {}

    items = HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})

    assert [item.external_id for item in items] == ["1"]
    output = capsys.readouterr().out
    assert "item=2" in output
    assert "item=3" in output
    assert "item=4" in output
    assert "item=5" in output
    assert "item=6" in output


def test_stories_raises_when_no_detail_is_usable():
    def fetch_json(url):
        if url.endswith("/topstories.json"):
            return [1, 2]
        return None

    with pytest.raises(RuntimeError, match="no usable Items"):
        HackerNewsStoriesConnector(fetch_json=fetch_json).fetch({"feed": "top"})


@pytest.mark.parametrize("payload", [None, {}, [], ["not-an-id"], [True]])
def test_stories_rejects_invalid_feed_payloads(payload):
    connector = HackerNewsStoriesConnector(fetch_json=lambda url: payload)
    with pytest.raises(ValueError, match="feed"):
        connector.fetch({"feed": "top"})


@pytest.mark.parametrize("config", [{}, {"feed": ""}, {"feed": "front_page"}])
def test_stories_validates_feed_config(config):
    connector = HackerNewsStoriesConnector(fetch_json=lambda url: [])
    with pytest.raises(ValueError, match="feed"):
        connector.validate_config(config)


def test_stories_type_key():
    assert HackerNewsStoriesConnector().type_key == "hackernews_stories"


def _hit(
    item_id="48888193",
    *,
    title="Search result",
    url="https://example.com/search-result",
    story_text="<p>Search body</p>",
    points=144,
    num_comments=39,
    created_at_i=1783900000,
    author="bob",
):
    return {
        "objectID": item_id,
        "title": title,
        "url": url,
        "story_text": story_text,
        "points": points,
        "num_comments": num_comments,
        "created_at_i": created_at_i,
        "author": author,
    }


@pytest.mark.parametrize(
    ("sort", "path"),
    [
        ("relevance", "/api/v1/search"),
        ("recent", "/api/v1/search_by_date"),
    ],
)
def test_query_selects_endpoint_and_encodes_story_search(sort, path):
    calls = []

    def fetch_json(url):
        calls.append(url)
        return {"hits": []}

    items = HackerNewsQueryConnector(fetch_json=fetch_json).fetch(
        {"query": 'local first "sqlite"', "sort": sort}
    )

    assert items == []
    parsed = urlparse(calls[0])
    assert parsed.path == path
    assert parse_qs(parsed.query) == {
        "query": ['local first "sqlite"'],
        "tags": ["story"],
        "hitsPerPage": ["50"],
    }


def test_query_maps_algolia_hit_and_preserves_outbound_url():
    connector = HackerNewsQueryConnector(fetch_json=lambda url: {"hits": [_hit()]})
    item = connector.fetch({"query": "sqlite", "sort": "relevance"})[0]

    assert item.external_id == "48888193"
    assert item.title == "Search result"
    assert item.url == "https://example.com/search-result"
    assert item.body == "Search body"
    assert item.created_at == datetime.fromtimestamp(1783900000, tz=timezone.utc)
    assert item.raw_metadata == {
        "provider": "hackernews",
        "hn_id": 48888193,
        "hn_url": "https://news.ycombinator.com/item?id=48888193",
        "item_type": "story",
        "author": "bob",
        "score": 144,
        "num_comments": 39,
    }


def test_query_falls_back_to_discussion_url():
    connector = HackerNewsQueryConnector(
        fetch_json=lambda url: {"hits": [_hit(item_id="42", url=None)]}
    )
    item = connector.fetch({"query": "ask", "sort": "recent"})[0]
    assert item.url == "https://news.ycombinator.com/item?id=42"


@pytest.mark.parametrize("payload", [None, [], {}, {"hits": None}])
def test_query_rejects_malformed_response_envelope(payload):
    connector = HackerNewsQueryConnector(fetch_json=lambda url: payload)
    with pytest.raises(ValueError, match="hits"):
        connector.fetch({"query": "python", "sort": "relevance"})


def test_query_skips_one_malformed_hit_and_keeps_valid_hits(capsys):
    connector = HackerNewsQueryConnector(
        fetch_json=lambda url: {
            "hits": [
                {"objectID": "not-numeric", "title": "Bad"},
                _hit(item_id="7", title="Good"),
            ]
        }
    )
    items = connector.fetch({"query": "python", "sort": "relevance"})
    assert [item.external_id for item in items] == ["7"]
    assert "skipping Algolia hit" in capsys.readouterr().out


def test_query_raises_when_nonempty_response_has_no_usable_hits():
    connector = HackerNewsQueryConnector(
        fetch_json=lambda url: {"hits": [{"objectID": "bad"}]}
    )
    with pytest.raises(RuntimeError, match="no usable Items"):
        connector.fetch({"query": "python", "sort": "relevance"})


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"query": "", "sort": "relevance"},
        {"query": "   ", "sort": "relevance"},
        {"query": "python"},
        {"query": "python", "sort": "popular"},
    ],
)
def test_query_validates_config(config):
    connector = HackerNewsQueryConnector()
    with pytest.raises(ValueError):
        connector.validate_config(config)


def test_query_type_key():
    assert HackerNewsQueryConnector().type_key == "hackernews_query"


@pytest.mark.parametrize(
    "connector_class",
    [HackerNewsStoriesConnector, HackerNewsQueryConnector],
)
def test_hackernews_comments_use_hn_id_and_return_first_visible_top_level_comment(
    connector_class,
):
    calls = []

    def fetch_json(url):
        calls.append(url)
        return {
            "children": [
                {"id": 1, "text": None, "deleted": True},
                {"id": 2, "text": "<p>Useful <b>context</b>.</p>", "author": "commenter"},
                {"id": 3, "text": "<p>Later comment</p>", "author": "other"},
            ]
        }

    connector = connector_class(fetch_json=fetch_json)
    comments = connector.fetch_comments(
        CommentFetchTarget(
            external_id="48888193",
            url="https://example.com/article",
            raw_metadata={"hn_id": 48888193},
        )
    )

    assert calls == ["https://hn.algolia.com/api/v1/items/48888193"]
    assert comments == ["Useful context."]


@pytest.mark.parametrize(
    "payload",
    [
        {"children": []},
        {"children": [{"text": None}]},
        {"children": [{"text": ""}]},
    ],
)
def test_hackernews_comments_return_empty_when_no_visible_comment(payload):
    connector = HackerNewsStoriesConnector(fetch_json=lambda url: payload)
    comments = connector.fetch_comments(
        CommentFetchTarget(
            external_id="42",
            url="https://example.com/article",
            raw_metadata={"hn_id": 42},
        )
    )
    assert comments == []


def test_hackernews_comments_require_hn_id_metadata():
    connector = HackerNewsStoriesConnector(fetch_json=lambda url: {"children": []})
    with pytest.raises(ValueError, match="hn_id"):
        connector.fetch_comments(
            CommentFetchTarget(
                external_id="42",
                url="https://example.com/article",
                raw_metadata={},
            )
        )


@pytest.mark.parametrize("payload", [None, [], {}, {"children": None}])
def test_hackernews_comments_reject_malformed_thread(payload):
    connector = HackerNewsStoriesConnector(fetch_json=lambda url: payload)
    with pytest.raises(ValueError, match="children"):
        connector.fetch_comments(
            CommentFetchTarget(
                external_id="42",
                url="https://example.com/article",
                raw_metadata={"hn_id": 42},
            )
        )
