"""Any RSS or Atom feed as an Editorial Source (`rss_feed`).

The Owner pastes a feed URL, or the address of a page that links to its feed. Saving the Source
runs `resolve_config`, which follows the page's <link rel="alternate"> to the feed itself and
suggests the feed's own title as the Source name. The stored config is always
{"feed_url": "<the feed>"}.

Fetching: a feed URL is an arbitrary address the Owner typed, and feed hosts redirect often, so it
goes through `deep_read.fetch.ArticleFetcher` rather than `connectors.http`: DNS pinning, every
redirect re-validated, response caps and an absolute time budget (see url_safety.py). Only the
content types a feed, or a page linking to one, can have are accepted.

Parsing uses feedparser, because real feeds are messy (RSS 0.9x/1.0/2.0, Atom, undeclared HTML
entities, broken XML) and it copes with all of them. Two rules keep it safe. It only ever gets the
bytes already fetched, wrapped in BytesIO: given a bare str or bytes, feedparser fetches it as a
URL, or reads it as a local file path when it names one. And it never resolves external entities
(feedparser turns them off, and expat caps entity expansion).

Tests inject a fake fetch function and never touch the network.
"""
from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import feedparser

from beehive.connectors.base import RawItem, ResolvedSource
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind
from beehive.connectors.registry import register
from beehive.connectors.text import html_to_text, single_line
from beehive.deep_read.fetch import (
    ArticleFetcher,
    FetchedArticle,
    FetchFailure,
    FetchFailureReason,
)
from beehive.domain.channels import ChannelKind
from beehive.url_safety import is_safe_external_href

TYPE_KEY = "rss_feed"

# ValueError messages the admin maps to translated text (web/admin/sources.py).
URL_REQUIRED = "rss_feed config needs 'feed_url' to be an http(s) URL"
NOT_A_FEED = "no RSS or Atom feed found at that address"
UNREACHABLE = "could not load that address"

_FETCH_LIMIT = 50
_BODY_CHAR_CAP = 1500
_TITLE_CHAR_CAP = 300
_NAME_CHAR_CAP = 120
_EXTERNAL_ID_CAP = 500
_MAX_DISCOVERED_LINKS = 3
_FEED_LINK_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
)
# What a feed, or a page linking to one, may be served as. Some servers label feeds text/plain
# or application/octet-stream; parsing decides whether it really is one.
_ACCEPTED_CONTENT_TYPES = _FEED_LINK_TYPES | frozenset({
    "application/xml",
    "text/xml",
    "application/x-rss+xml",
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/octet-stream",
})
_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, "
    "text/html;q=0.5"
)
_USER_AGENT = "beehive/0.1 (+https://github.com/sinmentis/beehive)"

_UNSAFE_REASONS = frozenset({
    FetchFailureReason.MALFORMED_URL,
    FetchFailureReason.INVALID_SCHEME,
    FetchFailureReason.CREDENTIALS_IN_URL,
    FetchFailureReason.INVALID_PORT,
    FetchFailureReason.PROHIBITED_ADDRESS,
})
_PROTOCOL_REASONS = frozenset({
    FetchFailureReason.TOO_MANY_REDIRECTS,
    FetchFailureReason.REDIRECT_MISSING_LOCATION,
    FetchFailureReason.UNSUPPORTED_CONTENT_TYPE,
    FetchFailureReason.UNSUPPORTED_CONTENT_ENCODING,
})
_TOO_LARGE_REASONS = frozenset({
    FetchFailureReason.RESPONSE_TOO_LARGE,
    FetchFailureReason.DECOMPRESSED_TOO_LARGE,
})

FeedFetch = Callable[[str], FetchedArticle | FetchFailure]


def _default_fetch(url: str) -> FetchedArticle | FetchFailure:
    with ArticleFetcher(
        allowed_content_types=_ACCEPTED_CONTENT_TYPES, accept=_ACCEPT, user_agent=_USER_AGENT
    ) as fetcher:
        return fetcher.fetch(url)


def _failure_kind(failure: FetchFailure) -> ConnectorHttpErrorKind:
    if failure.reason in _UNSAFE_REASONS:
        return ConnectorHttpErrorKind.UNSAFE_URL
    if failure.reason in _PROTOCOL_REASONS:
        return ConnectorHttpErrorKind.PROTOCOL
    if failure.reason in _TOO_LARGE_REASONS:
        return ConnectorHttpErrorKind.TOO_LARGE
    if failure.reason is FetchFailureReason.HTTP_ERROR:
        if failure.status_code in (404, 410):
            return ConnectorHttpErrorKind.NOT_FOUND
        if failure.status_code in (401, 403):
            return ConnectorHttpErrorKind.ACCESS_DENIED
        if failure.status_code in (408, 425, 429) or (failure.status_code or 0) >= 500:
            return ConnectorHttpErrorKind.TRANSIENT
        return ConnectorHttpErrorKind.PROTOCOL
    return ConnectorHttpErrorKind.TRANSIENT


def _failure_reason(failure: FetchFailure) -> str:
    if failure.status_code is not None:
        return f"HTTP {failure.status_code}"
    return failure.reason.value.replace("_", " ")


def _parse(fetched: FetchedArticle) -> feedparser.FeedParserDict:
    content_type = fetched.content_type
    if fetched.declared_charset:
        content_type += f"; charset={fetched.declared_charset}"
    return feedparser.parse(
        io.BytesIO(fetched.raw),
        response_headers={"content-type": content_type, "content-location": fetched.url},
    )


def _is_feed(parsed: feedparser.FeedParserDict) -> bool:
    return bool(parsed.get("version")) or bool(parsed.entries)


class _FeedLinkFinder(HTMLParser):
    """Every <link rel="alternate"> to an RSS or Atom feed, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        values = {name: (value or "") for name, value in attrs}
        rels = values.get("rel", "").lower().split()
        media_type = values.get("type", "").split(";")[0].strip().lower()
        if "alternate" in rels and media_type in _FEED_LINK_TYPES and values.get("href"):
            self.hrefs.append(values["href"].strip())


def _feed_links(page: FetchedArticle) -> list[str]:
    finder = _FeedLinkFinder()
    finder.feed(page.html)
    finder.close()
    links = []
    for href in finder.hrefs:
        url = urljoin(page.url, href)
        if is_safe_external_href(url) and url not in links:
            links.append(url)
    return links[:_MAX_DISCOVERED_LINKS]


def _entry_link(entry: feedparser.FeedParserDict) -> str:
    link = entry.get("link") or ""
    if is_safe_external_href(link):
        return link
    for candidate in entry.get("links") or []:
        href = candidate.get("href") or ""
        if candidate.get("rel", "alternate") == "alternate" and is_safe_external_href(href):
            return href
    return ""


def _entry_time(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_body(entry: feedparser.FeedParserDict) -> str:
    for content in entry.get("content") or []:
        text = html_to_text(content.get("value"), cap=_BODY_CHAR_CAP)
        if text:
            return text
    return html_to_text(entry.get("summary"), cap=_BODY_CHAR_CAP)


def _to_raw_item(entry: feedparser.FeedParserDict, feed_title: str) -> RawItem | None:
    """One feed entry as an item, or None when it has nothing a reader could open."""
    link = _entry_link(entry)
    if not link:
        return None
    body = _entry_body(entry)
    title = single_line(entry.get("title"), cap=_TITLE_CHAR_CAP) or single_line(
        body, cap=_TITLE_CHAR_CAP)
    if not title:
        return None
    metadata: dict[str, str] = {}
    if feed_title:
        metadata["feed_title"] = feed_title
    author = single_line(entry.get("author"), cap=_NAME_CHAR_CAP)
    if author:
        metadata["author"] = author
    return RawItem(
        external_id=str(entry.get("id") or link)[:_EXTERNAL_ID_CAP],
        title=title,
        url=link,
        body=body,
        created_at=_entry_time(entry),
        raw_metadata=metadata,
    )


class RssFeedConnector:
    type_key = TYPE_KEY
    supported_channel_kinds = frozenset({ChannelKind.EDITORIAL})

    def __init__(self, fetch: FeedFetch = _default_fetch):
        self._fetch = fetch

    def validate_config(self, config: dict) -> None:
        url = config.get("feed_url")
        if set(config) != {"feed_url"} or not isinstance(url, str):
            raise ValueError(URL_REQUIRED)
        try:
            parts = urlsplit(url)
        except ValueError:
            raise ValueError(URL_REQUIRED) from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(URL_REQUIRED)

    def resolve_config(self, config: dict) -> ResolvedSource:
        """The feed behind what the Owner typed: the address itself when it is a feed, else the
        first feed the page links to. Raises ValueError when neither works."""
        self.validate_config(config)
        url = config["feed_url"]
        fetched = self._load_for_owner(url)
        parsed = _parse(fetched)
        if _is_feed(parsed):
            return ResolvedSource({"feed_url": url}, _feed_title(parsed))
        for feed_url in _feed_links(fetched):
            linked = self._fetch(feed_url)
            if isinstance(linked, FetchFailure):
                continue
            linked_parsed = _parse(linked)
            if _is_feed(linked_parsed):
                return ResolvedSource({"feed_url": feed_url}, _feed_title(linked_parsed))
        raise ValueError(NOT_A_FEED)

    def fetch(self, config: dict) -> list[RawItem]:
        return self._items(config, limit=_FETCH_LIMIT)

    def fetch_preview(self, config: dict, *, limit: int) -> list[RawItem]:
        return self._items(config, limit=limit)

    def _load_for_owner(self, url: str) -> FetchedArticle:
        fetched = self._fetch(url)
        if isinstance(fetched, FetchFailure):
            raise ValueError(f"{UNREACHABLE}: {_failure_reason(fetched)}")
        return fetched

    def _items(self, config: dict, *, limit: int) -> list[RawItem]:
        self.validate_config(config)
        url = config["feed_url"]
        fetched = self._fetch(url)
        if isinstance(fetched, FetchFailure):
            raise ConnectorHttpError(
                _failure_kind(fetched), f"{url}: {fetched.reason.value}: {fetched.detail}")
        if fetched.truncated:
            raise ConnectorHttpError(
                ConnectorHttpErrorKind.TOO_LARGE, f"{url}: the feed is larger than the size cap")
        parsed = _parse(fetched)
        if not _is_feed(parsed):
            raise ConnectorHttpError(
                ConnectorHttpErrorKind.PROTOCOL, f"{url}: not an RSS or Atom feed")
        feed_title = _feed_title(parsed)
        items: list[RawItem] = []
        for entry in parsed.entries:
            item = _to_raw_item(entry, feed_title)
            if item is not None:
                items.append(item)
                if len(items) >= limit:
                    break
        return items


def _feed_title(parsed: feedparser.FeedParserDict) -> str:
    return single_line(parsed.feed.get("title"), cap=_NAME_CHAR_CAP)


register(RssFeedConnector())
