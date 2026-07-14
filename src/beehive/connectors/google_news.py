"""Phase 2's second SourceConnector. Fetches via Google News's public, unauthenticated RSS
search endpoint (https://news.google.com/rss/search) -- Google News has no official public API
at all; this RSS endpoint is the sanctioned "personal feed reader" surface its own <copyright>
tag describes.
Unlike Reddit's Atom feed this is RSS 2.0 with no XML namespace. The <link>/<guid> are opaque
Google redirect tokens, not the publisher's real URL -- passed through unchanged rather than
resolved server-side (a real
browser resolves them fine, but curl/base64 approaches do not, and reverse-engineering Google's
resolution endpoint is exactly the kind of scraping fragility this project avoids elsewhere).
Tests inject a fake fetch_rss and never touch the network."""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from beehive.connectors.base import RawItem
from beehive.connectors.registry import register

_FETCH_LIMIT = 50
_USER_AGENT = "beehive/0.1 (by /u/sinmentis)"


def _default_fetch_rss(query: str, limit: int) -> bytes:
    # Google's search RSS endpoint has no documented count/limit parameter -- `limit` is kept as
    # a parameter (matching RedditSubredditConnector's fetch_rss shape) but not used in the URL;
    # the cap is enforced client-side in fetch() after parsing instead.
    params = urlencode({"q": query, "hl": "en-NZ", "gl": "NZ", "ceid": "NZ:en"})
    url = f"https://news.google.com/rss/search?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
        return response.read()


def _to_raw_item(item) -> RawItem:
    pub_date_el = item.find("pubDate")
    created_at = parsedate_to_datetime(pub_date_el.text) if pub_date_el is not None else None
    source_el = item.find("source")
    raw_metadata = {"source_name": source_el.text} if source_el is not None and source_el.text else {}
    return RawItem(
        external_id=item.find("guid").text,
        title=item.find("title").text,
        url=item.find("link").text,
        body="",
        created_at=created_at,
        raw_metadata=raw_metadata,
    )


class GoogleNewsQueryConnector:
    type_key = "google_news_query"

    def __init__(self, fetch_rss=_default_fetch_rss):
        self._fetch_rss = fetch_rss

    def validate_config(self, config: dict) -> None:
        if not config.get("query"):
            raise ValueError("google_news_query config needs a non-empty 'query' key")

    def fetch(self, config: dict) -> list[RawItem]:
        query = config["query"]
        raw_xml = self._fetch_rss(query, _FETCH_LIMIT)
        root = ET.fromstring(raw_xml)  # noqa: S314 (Google's own feed, not user input)
        items = root.findall(".//item")[:_FETCH_LIMIT]
        return [_to_raw_item(item) for item in items]


register(GoogleNewsQueryConnector())
