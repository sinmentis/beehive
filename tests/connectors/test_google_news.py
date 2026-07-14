from datetime import datetime, timezone

import pytest

from beehive.connectors.google_news import GoogleNewsQueryConnector

_RSS_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
    '<title>"OpenAI" - Google News</title>'
)
_RSS_FOOTER = "</channel></rss>"


def _feed(*items_xml: str) -> bytes:
    return (_RSS_HEADER + "".join(items_xml) + _RSS_FOOTER).encode("utf-8")


def _news_item(
    guid="CBMiWkFVX3lxTE5PX3B6STFOQTMxcXRNdHlSMFRMRWthS0wyeDl2bVV1VnZhdVBIUzl0eGkxYjMyV1ZGTFhoUll5cTl0dmJHaE9KNFFzWjZVZ1pIUUhOMklUQnM3Zw",
    title="Apple sues OpenAI, its employees claiming theft of trade secrets - BBC",
    link=None,
    pub_date="Fri, 10 Jul 2026 22:54:16 GMT",
    source_name="BBC",
    source_url="https://www.bbc.com",
):
    # Mirrors the real shape observed from https://news.google.com/rss/search?q=... :
    # <link>/<guid> are opaque Google redirect tokens, not the publisher's real URL.
    link = link if link is not None else f"https://news.google.com/rss/articles/{guid}?oc=5"
    source_tag = f'<source url="{source_url}">{source_name}</source>' if source_name else ""
    return (
        "<item>"
        f"<title>{title}</title>"
        f"<link>{link}</link>"
        f'<guid isPermaLink="false">{guid}</guid>'
        f"<pubDate>{pub_date}</pubDate>"
        f'<description>&lt;a href="{link}" target="_blank"&gt;{title}&lt;/a&gt;</description>'
        f"{source_tag}"
        "</item>"
    )


def test_fetch_maps_entries_to_raw_items():
    fake_fetch = lambda query, limit: _feed(_news_item())  # noqa: E731
    connector = GoogleNewsQueryConnector(fetch_rss=fake_fetch)
    items = connector.fetch({"query": "OpenAI"})

    assert len(items) == 1
    item = items[0]
    guid = "CBMiWkFVX3lxTE5PX3B6STFOQTMxcXRNdHlSMFRMRWthS0wyeDl2bVV1VnZhdVBIUzl0eGkxYjMyV1ZGTFhoUll5cTl0dmJHaE9KNFFzWjZVZ1pIUUhOMklUQnM3Zw"
    assert item.external_id == guid
    assert item.title == "Apple sues OpenAI, its employees claiming theft of trade secrets - BBC"
    assert item.url == f"https://news.google.com/rss/articles/{guid}?oc=5"
    assert item.body == ""
    assert item.created_at == datetime(2026, 7, 10, 22, 54, 16, tzinfo=timezone.utc)
    assert item.raw_metadata == {"source_name": "BBC"}


def test_fetch_handles_missing_source_tag():
    connector = GoogleNewsQueryConnector(
        fetch_rss=lambda query, limit: _feed(_news_item(source_name=None)))
    item = connector.fetch({"query": "x"})[0]
    assert item.raw_metadata == {}


def test_fetch_maps_every_entry_in_the_feed():
    connector = GoogleNewsQueryConnector(
        fetch_rss=lambda query, limit: _feed(
            _news_item(guid="g1", title="First"),
            _news_item(guid="g2", title="Second"),
        )
    )
    items = connector.fetch({"query": "x"})
    assert [i.external_id for i in items] == ["g1", "g2"]
    assert [i.title for i in items] == ["First", "Second"]


def test_fetch_caps_at_fifty_items():
    sixty_items = [_news_item(guid=f"g{i}", title=f"Item {i}") for i in range(60)]
    connector = GoogleNewsQueryConnector(fetch_rss=lambda query, limit: _feed(*sixty_items))
    items = connector.fetch({"query": "x"})
    assert len(items) == 50
    assert items[0].external_id == "g0"
    assert items[49].external_id == "g49"


def test_fetch_requests_the_search_rss_endpoint_with_the_configured_query():
    captured = {}

    def fake_fetch(query, limit):
        captured["query"] = query
        captured["limit"] = limit
        return _feed()

    connector = GoogleNewsQueryConnector(fetch_rss=fake_fetch)
    connector.fetch({"query": "New Zealand economy"})
    assert captured["query"] == "New Zealand economy"
    assert captured["limit"] == 50


def test_validate_config_requires_query():
    connector = GoogleNewsQueryConnector(fetch_rss=lambda query, limit: _feed())
    with pytest.raises(ValueError, match="query"):
        connector.validate_config({})
    connector.validate_config({"query": "OpenAI"})  # does not raise


def test_type_key():
    assert GoogleNewsQueryConnector(fetch_rss=lambda query, limit: _feed()).type_key == "google_news_query"
