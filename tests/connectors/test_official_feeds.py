import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape

import pytest

from beehive.connectors.official_feeds import (
    FEED_DEFINITIONS,
    FeedDefinition,
    OfficialFeedConnector,
)
from beehive.connectors.registry import get

_RSS_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel><title>Official</title>'
)
_RSS_FOOTER = "</channel></rss>"


def _feed(*items_xml: str) -> bytes:
    return (_RSS_HEADER + "".join(items_xml) + _RSS_FOOTER).encode("utf-8")


def _item(
    *,
    guid="https://www.rbnz.govt.nz/news/1",
    title="OCR held at 3.00 percent",
    link="https://www.rbnz.govt.nz/news/1",
    pub_date="Wed, 09 Jul 2026 14:00:00 +1200",
    description="<p>The Monetary Policy Committee <b>agreed</b> to hold.</p>",
    category=None,
) -> str:
    parts = ["<item>"]
    if title is not None:
        parts.append(f"<title>{escape(title)}</title>")
    if link is not None:
        parts.append(f"<link>{escape(link)}</link>")
    if guid is not None:
        parts.append(f"<guid>{escape(guid)}</guid>")
    if pub_date is not None:
        parts.append(f"<pubDate>{escape(pub_date)}</pubDate>")
    if description is not None:
        parts.append(f"<description>{escape(description)}</description>")
    if category is not None:
        parts.append(f"<category>{escape(category)}</category>")
    parts.append("</item>")
    return "".join(parts)


_RBNZ = FeedDefinition("rbnz_news", "https://www.rbnz.govt.nz/feeds/news", "RBNZ News")


def _connector(raw: bytes, definition: FeedDefinition = _RBNZ) -> OfficialFeedConnector:
    return OfficialFeedConnector(definition, fetch_bytes=lambda url: raw)


def test_maps_core_fields():
    item = _connector(_feed(_item())).fetch({})[0]
    assert item.external_id == "https://www.rbnz.govt.nz/news/1"
    assert item.title == "OCR held at 3.00 percent"
    assert item.url == "https://www.rbnz.govt.nz/news/1"
    assert item.raw_metadata == {"publisher": "RBNZ News"}


def test_requests_definition_url():
    captured = {}

    def fake_fetch(url):
        captured["url"] = url
        return _feed(_item())

    OfficialFeedConnector(_RBNZ, fetch_bytes=fake_fetch).fetch({})
    assert captured["url"] == "https://www.rbnz.govt.nz/feeds/news"


def test_default_fetch_uses_descriptive_user_agent_and_timeout(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return _feed(_item())

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    OfficialFeedConnector(_RBNZ).fetch({})

    assert captured["request"].full_url == _RBNZ.url
    assert captured["request"].headers["User-agent"] == "beehive/0.1 (by /u/sinmentis)"
    assert captured["timeout"] == 30


def test_html_description_is_cleaned_to_plain_text():
    item = _connector(_feed(_item())).fetch({})[0]
    assert item.body == "The Monetary Policy Committee agreed to hold."


def test_body_capped_at_1500_chars():
    long_html = "<p>" + ("x" * 2000) + "</p>"
    item = _connector(_feed(_item(description=long_html))).fetch({})[0]
    assert len(item.body) == 1500


def test_guid_is_preferred_identifier():
    item = _connector(
        _feed(_item(guid="urn:guid:42", link="https://example.govt.nz/a"))
    ).fetch({})[0]
    assert item.external_id == "urn:guid:42"


def test_link_is_used_when_guid_is_absent():
    item = _connector(
        _feed(_item(guid=None, link="https://example.govt.nz/a"))
    ).fetch({})[0]
    assert item.external_id == "https://example.govt.nz/a"


def test_parses_nz_offset_pubdate():
    item = _connector(
        _feed(_item(pub_date="Wed, 09 Jul 2026 14:00:00 +1200"))
    ).fetch({})[0]
    assert item.created_at == datetime(
        2026, 7, 9, 14, 0, tzinfo=timezone(timedelta(hours=12))
    )


def test_parses_gmt_pubdate():
    item = _connector(
        _feed(_item(pub_date="Wed, 09 Jul 2026 02:00:00 GMT"))
    ).fetch({})[0]
    assert item.created_at == datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)


def test_federal_reserve_category_is_stored():
    fed = FeedDefinition(
        "federal_reserve_news",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "Federal Reserve",
    )
    item = _connector(
        _feed(_item(category="Monetary Policy")), definition=fed
    ).fetch({})[0]
    assert item.raw_metadata == {
        "publisher": "Federal Reserve",
        "category": "Monetary Policy",
    }


def test_missing_category_leaves_metadata_without_category():
    item = _connector(_feed(_item(category=None))).fetch({})[0]
    assert "category" not in item.raw_metadata


def test_caps_at_50_items():
    items_xml = [_item(guid=f"g{i}", link=f"https://x/{i}") for i in range(60)]
    result = _connector(_feed(*items_xml)).fetch({})
    assert len(result) == 50


def test_missing_title_fails_the_fetch():
    with pytest.raises(ValueError, match="title, link, or identifier"):
        _connector(_feed(_item(title=None))).fetch({})


def test_missing_link_and_guid_fails_the_fetch():
    with pytest.raises(ValueError, match="title, link, or identifier"):
        _connector(_feed(_item(guid=None, link=None))).fetch({})


def test_validate_rejects_non_empty_config():
    with pytest.raises(ValueError, match="config must be empty"):
        OfficialFeedConnector(_RBNZ).validate_config({"url": "https://evil"})


def test_validate_accepts_empty_config():
    OfficialFeedConnector(_RBNZ).validate_config({})


def test_fetch_rejects_non_empty_config_before_request():
    connector = OfficialFeedConnector(
        _RBNZ,
        fetch_bytes=lambda url: pytest.fail("request must not run"),
    )
    with pytest.raises(ValueError, match="config must be empty"):
        connector.fetch({"url": "https://evil"})


def test_three_definitions_registered():
    assert {d.type_key for d in FEED_DEFINITIONS} == {
        "rbnz_news",
        "nz_government_news",
        "federal_reserve_news",
    }
    for definition in FEED_DEFINITIONS:
        assert get(definition.type_key).type_key == definition.type_key
