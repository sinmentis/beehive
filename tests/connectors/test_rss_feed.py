import pytest

from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind
from beehive.connectors.rss_feed import NOT_A_FEED, UNREACHABLE, URL_REQUIRED, RssFeedConnector
from beehive.deep_read.fetch import FetchedArticle, FetchFailure, FetchFailureReason

_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Trail &amp; Tent Notes</title>
  <link>https://trail.example/</link>
  <item>
    <title>Winter <b>boots</b> reviewed</title>
    <link>https://trail.example/posts/boots</link>
    <guid isPermaLink="false">post-41</guid>
    <author>ana@trail.example (Ana)</author>
    <pubDate>Tue, 29 Sep 2026 20:15:00 +1300</pubDate>
    <description>&lt;p&gt;Four pairs, one winner.&lt;/p&gt;&lt;p&gt;Spending on R&amp;amp;D</description>
  </item>
  <item>
    <title>No link here</title>
  </item>
  <item>
    <title>Script link</title>
    <link>javascript:alert(1)</link>
  </item>
  <item>
    <link>https://trail.example/posts/untitled</link>
    <description>A post with no title still gets one from its text.</description>
  </item>
</channel></rss>"""

_ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Hut Log</title>
  <entry>
    <title>Hut reopened</title>
    <link rel="alternate" href="/2026/hut-reopened"/>
    <id>tag:hut.example,2026:1</id>
    <updated>2026-09-28T09:00:00Z</updated>
    <content type="html">&lt;p&gt;The hut is open again.&lt;/p&gt;</content>
  </entry>
</feed>"""

_PAGE = b"""<!doctype html><html><head><title>Trail blog</title>
<link rel="stylesheet" href="/site.css">
<link rel="alternate" type="application/rss+xml" title="Posts" href="/feed.xml">
</head><body><p>Hello</p></body></html>"""


def _fetched(url, raw, content_type="application/rss+xml", charset=None, truncated=False):
    return FetchedArticle(
        url=url, status_code=200, content_type=content_type, html=raw.decode("utf-8", "replace"),
        truncated=truncated, raw=raw, declared_charset=charset,
    )


def _connector(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        response = responses[url]
        return response(url) if callable(response) else response

    connector = RssFeedConnector(fetch=fetch)
    connector.calls = calls
    return connector


def test_rss_items_carry_title_link_text_time_and_feed_title():
    url = "https://trail.example/feed"
    items = _connector({url: _fetched(url, _RSS)}).fetch({"feed_url": url})

    assert [item.url for item in items] == [
        "https://trail.example/posts/boots",
        "https://trail.example/posts/untitled",
    ]
    boots = items[0]
    assert boots.title == "Winter boots reviewed"
    assert boots.external_id == "post-41"
    assert boots.body == "Four pairs, one winner.\nSpending on R&D"
    assert boots.created_at.isoformat() == "2026-09-29T07:15:00+00:00"
    assert boots.raw_metadata == {"feed_title": "Trail & Tent Notes", "author": "ana@trail.example (Ana)"}
    assert items[1].title == "A post with no title still gets one from its text."
    assert items[1].external_id == "https://trail.example/posts/untitled"


def test_atom_links_resolve_against_the_feed_address():
    url = "https://hut.example/atom.xml"
    items = _connector({url: _fetched(url, _ATOM, "application/atom+xml")}).fetch({"feed_url": url})

    assert len(items) == 1
    assert items[0].url == "https://hut.example/2026/hut-reopened"
    assert items[0].body == "The hut is open again."
    assert items[0].raw_metadata == {"feed_title": "Hut Log"}


def test_preview_honours_its_limit():
    url = "https://trail.example/feed"
    items = _connector({url: _fetched(url, _RSS)}).fetch_preview({"feed_url": url}, limit=1)
    assert [item.title for item in items] == ["Winter boots reviewed"]


def test_encoding_named_only_in_the_xml_prolog_is_respected():
    url = "https://cn.example/feed"
    raw = (
        '<?xml version="1.0" encoding="GBK"?><rss version="2.0"><channel><title>户外装备</title>'
        "<item><title>清仓</title><link>https://cn.example/1</link></item></channel></rss>"
    ).encode("gbk")
    items = _connector({url: _fetched(url, raw, "text/xml")}).fetch({"feed_url": url})
    assert items[0].title == "清仓"
    assert items[0].raw_metadata["feed_title"] == "户外装备"


def test_a_body_that_names_a_local_file_is_never_read_as_one(tmp_path):
    secret = tmp_path / "feed.xml"
    secret.write_bytes(_RSS)
    url = "https://evil.example/feed"
    connector = _connector({url: _fetched(url, str(secret).encode(), "text/plain")})

    with pytest.raises(ConnectorHttpError) as excinfo:
        connector.fetch({"feed_url": url})
    assert excinfo.value.kind is ConnectorHttpErrorKind.PROTOCOL


def test_a_page_that_is_not_a_feed_fails_the_fetch():
    url = "https://trail.example/"
    connector = _connector({url: _fetched(url, b"<html><body>hi</body></html>", "text/html")})
    with pytest.raises(ConnectorHttpError) as excinfo:
        connector.fetch({"feed_url": url})
    assert excinfo.value.kind is ConnectorHttpErrorKind.PROTOCOL


def test_a_truncated_feed_fails_instead_of_storing_half_of_it():
    url = "https://trail.example/feed"
    connector = _connector({url: _fetched(url, _RSS, truncated=True)})
    with pytest.raises(ConnectorHttpError) as excinfo:
        connector.fetch({"feed_url": url})
    assert excinfo.value.kind is ConnectorHttpErrorKind.TOO_LARGE


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (FetchFailure(FetchFailureReason.HTTP_ERROR, "gone", status_code=404), ConnectorHttpErrorKind.NOT_FOUND),
        (FetchFailure(FetchFailureReason.HTTP_ERROR, "no", status_code=403), ConnectorHttpErrorKind.ACCESS_DENIED),
        (FetchFailure(FetchFailureReason.HTTP_ERROR, "busy", status_code=503), ConnectorHttpErrorKind.TRANSIENT),
        (FetchFailure(FetchFailureReason.PROHIBITED_ADDRESS, "10.0.0.1"), ConnectorHttpErrorKind.UNSAFE_URL),
        (FetchFailure(FetchFailureReason.TIMEOUT, "slow"), ConnectorHttpErrorKind.TRANSIENT),
        (FetchFailure(FetchFailureReason.TOO_MANY_REDIRECTS, "loop"), ConnectorHttpErrorKind.PROTOCOL),
    ],
)
def test_fetch_failures_keep_their_kind(failure, kind):
    url = "https://trail.example/feed"
    with pytest.raises(ConnectorHttpError) as excinfo:
        _connector({url: failure}).fetch({"feed_url": url})
    assert excinfo.value.kind is kind


def test_resolve_keeps_a_feed_address_and_suggests_its_title():
    url = "https://trail.example/feed"
    resolved = _connector({url: _fetched(url, _RSS)}).resolve_config({"feed_url": url})
    assert resolved.config == {"feed_url": url}
    assert resolved.suggested_name == "Trail & Tent Notes"


def test_resolve_follows_a_page_to_the_feed_it_links_to():
    page = "https://trail.example/blog"
    feed = "https://trail.example/feed.xml"
    connector = _connector({page: _fetched(page, _PAGE, "text/html"), feed: _fetched(feed, _RSS)})

    resolved = connector.resolve_config({"feed_url": page})

    assert resolved.config == {"feed_url": feed}
    assert resolved.suggested_name == "Trail & Tent Notes"
    assert connector.calls == [page, feed]


def test_resolve_says_when_there_is_no_feed():
    page = "https://trail.example/about"
    connector = _connector({page: _fetched(page, b"<html><head></head></html>", "text/html")})
    with pytest.raises(ValueError, match=NOT_A_FEED):
        connector.resolve_config({"feed_url": page})


def test_resolve_reports_an_unreachable_address_in_owner_terms():
    url = "https://trail.example/feed"
    connector = _connector({url: FetchFailure(FetchFailureReason.HTTP_ERROR, "x", status_code=404)})
    with pytest.raises(ValueError) as excinfo:
        connector.resolve_config({"feed_url": url})
    assert str(excinfo.value) == f"{UNREACHABLE}: HTTP 404"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"feed_url": ""},
        {"feed_url": "https://"},
        {"feed_url": "ftp://trail.example/feed"},
        {"feed_url": 42},
        {"feed_url": "https://trail.example/feed", "extra": 1},
    ],
)
def test_validate_config_needs_exactly_one_http_address(config):
    with pytest.raises(ValueError, match="feed_url"):
        RssFeedConnector(fetch=lambda url: None).validate_config(config)
    assert URL_REQUIRED.startswith("rss_feed config")
