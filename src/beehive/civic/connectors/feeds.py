"""RSS 2.0 and Atom Civic sources, plus the adapters that reuse the feeds Channels already fetch.

Three fixed feeds and one reuse path:

* **Electoral Commission** (`https://elections.nz/media-and-news/rss`) publishes the ten most
  recently *updated* pages, not the ten most recent publications. That is the reason
  `CivicDocument` separates `updated_at` from `first_seen_at`: an entry reappearing with a newer
  `updated_at` is an edit to something already seen, and only `first_seen_at` answers "is this
  new to us". Its guid is the page URL. Crawl delay 2s, as the site asks.
* **Beehive** (`https://www.beehive.govt.nz/rss.xml`) is the case where guid and link genuinely
  differ: the guid is a Drupal node URL (`/node/12345`) while the canonical link is the
  release/speech path. The node URL is the stable identity -- it survives a slug rename -- so it
  becomes `external_key`, and the release path becomes `canonical_url`. The description carries
  full HTML, kept as bounded plain text.
* **Order Paper** (`https://www3.parliament.nz/en/channellisting/rss/1691/...`) is Atom, so its
  identity is `<id>` and its timestamp is `<updated>`.

The fourth path is `adapt_official_feed`, which wraps an existing
`beehive.connectors.official_feeds.FeedDefinition` (RBNZ, NZ Government) into this package's
normalized shape. Reuse rather than re-implementation: those endpoints are already fetched and
parsed for Channels, and pointing a second, subtly different parser at the same URL is how two
views of one source drift apart.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from beehive.civic.documents import (CivicDocument, CivicFetchResult, MAX_TITLE_CHARS,
                                      bounded_text, build_result, canonical_url,
                                      stable_external_key)
from beehive.civic.fetching import (CivicEndpoint, CivicTransport, DEFAULT_MAX_ITEMS,
                                     assert_not_tombstone_document, parse_iso8601,
                                     parse_rfc2822, parse_xml)
from beehive.civic.source_extraction import PublicationWitness, publication_witness
from beehive.connectors.official_feeds import FeedDefinition
from beehive.domain.civic import CivicEvidenceTier

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class _PlainText(HTMLParser):
    """Block-aware extraction, matching `official_feeds`'s treatment so a release rendered for a
    Channel and the same release rendered for Civic read alike rather than differing by
    whitespace.

    One deliberate difference: `<script>` and `<style>` *content* is dropped entirely. An excerpt
    is display text that ends up beside a published civic statement, and inline script source is
    never that -- `official_feeds` keeps it only because changing a Channel's stored summaries is
    not this package's business."""

    _BLOCK_TAGS = {"p", "li", "blockquote", "pre", "br", "div", "h1", "h2", "h3"}
    _SKIP_CONTENT_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_CONTENT_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    extractor = _PlainText()
    extractor.feed(value)
    return bounded_text(extractor.text())


@dataclass(frozen=True)
class FeedSourceDefinition:
    """One fixed feed, with the identity rules that differ between them stated as data rather
    than as branching inside the parser."""

    source_key: str
    endpoint: CivicEndpoint
    publisher: str
    tier: CivicEvidenceTier
    crawl_delay_seconds: float = 0.0
    is_atom: bool = False
    # Beehive's guid is a node URL and its link is the canonical release path; the Electoral
    # Commission's guid IS the page URL. When True the guid is kept as the identity and the link
    # is kept as the canonical URL even where they differ.
    guid_is_identity: bool = True
    max_items: int = DEFAULT_MAX_ITEMS


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _atom_link(entry: ET.Element) -> str | None:
    """The alternate (human) link, falling back to the first link with an href. Atom allows many
    links per entry and the self/enclosure ones must never become the canonical URL."""
    fallback = None
    for link in entry.findall(f"{_ATOM_NS}link"):
        href = link.get("href")
        if not href:
            continue
        rel = (link.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _rss_document(
    item: ET.Element, definition: FeedSourceDefinition, first_seen_at: datetime,
) -> CivicDocument:
    title = _text(item.find("title"))
    link = _text(item.find("link"))
    guid = _text(item.find("guid"))
    if title is None:
        raise ValueError(f"{definition.source_key}: feed item is missing a title")
    identity_raw = guid if (definition.guid_is_identity and guid) else link
    if identity_raw is None:
        raise ValueError(f"{definition.source_key}: feed item has no guid or link")
    target = link or guid
    raw_published = _text(item.find("pubDate"))
    published_at = parse_rfc2822(raw_published)
    # Dublin Core `dc:date` is what several govt feeds use for the modification time.
    updated_at = parse_iso8601(_text(item.find("{http://purl.org/dc/elements/1.1/}date")))
    metadata: dict[str, Any] = {
        "feed_guid": guid, "feed_link": link, "excerpt_source": "rss-description",
        "publication": (
            PublicationWitness(
                published_at, "rss-pubDate", "datetime",
                bounded_text(raw_published, limit=100),
            ).as_metadata()
            if published_at is not None else None
        ),
    }
    category = _text(item.find("category"))
    if category:
        metadata["category"] = category
    return CivicDocument(
        source_key=definition.source_key,
        external_key=stable_external_key(
            identity_raw, source_key=definition.source_key, field_name="guid"),
        canonical_url=canonical_url(target, base=definition.endpoint.url),
        title=bounded_text(title, limit=MAX_TITLE_CHARS),
        tier=definition.tier,
        publisher=definition.publisher,
        first_seen_at=first_seen_at,
        published_at=published_at,
        updated_at=updated_at or published_at,
        excerpt=html_to_text(_text(item.find("description"))),
        raw_metadata=metadata)


def _atom_document(
    entry: ET.Element, definition: FeedSourceDefinition, first_seen_at: datetime,
) -> CivicDocument:
    title = _text(entry.find(f"{_ATOM_NS}title"))
    entry_id = _text(entry.find(f"{_ATOM_NS}id"))
    link = _atom_link(entry)
    if title is None:
        raise ValueError(f"{definition.source_key}: Atom entry is missing a title")
    if entry_id is None and link is None:
        raise ValueError(f"{definition.source_key}: Atom entry has no id or link")
    target = link or entry_id
    updated_at = parse_iso8601(_text(entry.find(f"{_ATOM_NS}updated")))
    publication = publication_witness(
        _text(entry.find(f"{_ATOM_NS}published")), source="atom-published",
    )
    published_at = publication.value if publication else None
    raw_summary = _text(entry.find(f"{_ATOM_NS}summary"))
    summary = raw_summary or _text(entry.find(f"{_ATOM_NS}content"))
    return CivicDocument(
        source_key=definition.source_key,
        external_key=stable_external_key(
            entry_id or link, source_key=definition.source_key, field_name="atom id"),
        canonical_url=canonical_url(target, base=definition.endpoint.url),
        title=bounded_text(title, limit=MAX_TITLE_CHARS),
        tier=definition.tier,
        publisher=definition.publisher,
        first_seen_at=first_seen_at,
        published_at=published_at,
        updated_at=updated_at or published_at,
        excerpt=html_to_text(summary),
        raw_metadata={
            "atom_id": entry_id, "atom_link": link,
            "excerpt_source": "atom-summary" if raw_summary else "atom-content",
            "publication": publication.as_metadata() if publication else None,
        })


class CivicFeedConnector:
    """Fetches one fixed feed and normalizes it. `config` must be empty, exactly like
    `official_feeds.OfficialFeedConnector`: a stored URL would let the trusted endpoint be
    redirected, which is the whole reason these definitions are constants."""

    def __init__(self, definition: FeedSourceDefinition, transport: CivicTransport | None = None):
        self._definition = definition
        self._transport = transport or CivicTransport()

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> FeedSourceDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        """One bounded fetch. Individual unparseable entries degrade the result rather than
        losing the whole feed -- an official feed with one malformed item is still worth
        reading, but the caller must be told the crawl was incomplete."""
        self.validate_config(config or {})
        definition = self._definition
        response = self._transport.request(
            definition.endpoint,
            source_key=definition.source_key,
            crawl_delay_seconds=definition.crawl_delay_seconds)
        root = parse_xml(
            response.body, source_key=definition.source_key, url=definition.endpoint.url)
        # A structurally valid Atom document can still be a retirement notice; see
        # fetching.assert_not_tombstone_document for why the content-type check is not enough.
        assert_not_tombstone_document(
            root, source_key=definition.source_key, url=definition.endpoint.url)
        if definition.is_atom:
            entries = root.findall(f"{_ATOM_NS}entry")[:definition.max_items]
            builder = _atom_document
        else:
            entries = root.findall(".//item")[:definition.max_items]
            builder = _rss_document

        documents: list[CivicDocument] = []
        degraded: list[str] = []
        for index, entry in enumerate(entries):
            try:
                documents.append(builder(entry, definition, now))
            except ValueError as exc:
                degraded.append(f"entry {index}: {exc}")
        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded)


def adapt_official_feed(
    feed: FeedDefinition,
    *,
    source_key: str,
    tier: CivicEvidenceTier,
    allowed_hosts: frozenset[str],
    crawl_delay_seconds: float = 0.0,
) -> FeedSourceDefinition:
    """Wraps an existing Channel feed definition as a Civic source.

    Reuse, not duplication: RBNZ and the NZ Government feed are already declared once in
    `beehive.connectors.official_feeds`, and pointing a second parser at the same URL from a
    separate constant is how the two views drift apart when an endpoint moves."""
    return FeedSourceDefinition(
        source_key=source_key,
        endpoint=CivicEndpoint(url=feed.url, allowed_hosts=allowed_hosts),
        publisher=feed.publisher,
        tier=tier,
        crawl_delay_seconds=crawl_delay_seconds)


__all__ = [
    "CivicFeedConnector",
    "FeedSourceDefinition",
    "adapt_official_feed",
    "html_to_text",
]
