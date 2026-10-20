"""The four reading strategies party and media sites need that official sources do not.

`beehive.civic.connectors` already covers RSS, Atom, sitemaps, CKAN and the Parliament APIs, and
every party or masthead that publishes one of those is read by those connectors -- this module
adds only what is genuinely missing and nothing else:

* `NewsSitemapConnector` -- a Google news sitemap. The canonical `SitemapConnector` reads
  `<urlset>` entries and derives a title from the URL, which is right when a sitemap carries only
  `<loc>` and `<lastmod>`. A news sitemap additionally carries `<news:title>` and
  `<news:publication_date>`, and throwing away a real headline and a real publication time in
  favour of a humanized slug would lose exactly the fields corroboration needs.
* `EmbeddedListConnector` -- a server-rendered page that hands its publication list to a client
  component as JSON. Labour publishes no feed and no news sitemap.
* `LinkIndexConnector` -- a listing page with neither. NZ Outdoors & Freedom publishes only
  `/media`.
* `WallClockFeedConnector` -- RSS whose `pubDate` is a local wall-clock time in a non-RFC form
  (`12th Aug 26, 5:01pm`) and whose `guid` is site-relative. interest.co.nz is the only such
  source; every other feed goes through the canonical `CivicFeedConnector`.

All four emit `CivicDocument`s through `build_result`, parse XML through the canonical guarded
`parse_xml` (so a DOCTYPE or ENTITY declaration is refused before expansion), canonicalize every
URL through `canonical_url`, and report caps and unparseable entries as degraded rather than
silently truncating. They are strategy parsers, not a second model.

`fetch_entry` is the one seam every party/media fetch goes through. It runs whichever connector
the catalog entry names -- canonical or from this module -- and then applies the two rules that
are specific to this half of the catalog: a document must be on its source's own host, and the
result is bounded. Both are enforced once, here, so no strategy can forget either.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from beehive.civic.documents import (
    MAX_TITLE_CHARS,
    CivicDocument,
    CivicFetchResult,
    CivicSourceError,
    bounded_text,
    build_result,
    canonical_url,
)
from beehive.civic.fetching import (
    CivicEndpoint,
    CivicResponse,
    CivicTransport,
    assert_not_tombstone_document,
    parse_iso8601,
    parse_rfc2822,
    parse_xml,
)
from beehive.civic.source_extraction import PublicationWitness, publication_witness
from beehive.connectors.civic_catalog import (
    MAX_DOCUMENTS_PER_FETCH,
    PARTY_MEDIA_USER_AGENT,
    PartyMediaSourceEntry,
)
from beehive.domain.civic import CivicEvidenceTier

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_NEWS_NS = "{http://www.google.com/schemas/sitemap-news/0.9}"

# The one embedded-JSON shape supported: a server-rendered page handing its list to a client
# component. Anything else is a parse failure, not an invitation to scrape.
_EMBEDDED_JSON_MARKER = "componentProps: "
_MAX_EMBEDDED_JSON_BLOCKS = 8
_MAX_JSON_DEPTH = 12

# interest.co.nz's Drupal feed: "12th Aug 26, 5:01pm", a wall-clock New Zealand time with no
# offset. Declared per source rather than assumed globally.
_ORDINAL_DATE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)\s+(.+)$", re.IGNORECASE)
_WALL_CLOCK_FORMATS = ("%d %b %y, %I:%M%p", "%d %b %Y, %I:%M%p")


def party_media_transport(**kwargs) -> CivicTransport:
    """The canonical transport, carrying the party/media User-Agent. Identical machinery to the
    official half -- allow-list, crawl pacing, bounded body, content-type assertion, tombstone
    detection -- with only the agent string naming which crawl an operator is looking at."""
    kwargs.setdefault("user_agent", PARTY_MEDIA_USER_AGENT)
    return CivicTransport(**kwargs)


def _zone(name: str):
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on system tzdata
        raise ValueError(f"unknown publication timezone {name!r}") from exc


def parse_wall_clock(value: Any, *, timezone_name: str) -> datetime | None:
    """A local, offset-free publication time in the one non-RFC form these feeds use.

    Deliberately a short explicit list rather than a permissive guesser: an unrecognised value
    returns None and the document is emitted undated, which is safer than a plausible-looking
    wrong timestamp deciding whether two reports describe the same event."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = _ORDINAL_DATE.match(value.strip())
    if match is None:
        return None
    candidate = f"{match.group(1)} {match.group(2)}"
    for fmt in _WALL_CLOCK_FORMATS:
        try:
            naive = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=_zone(timezone_name)).astimezone(timezone.utc)
    return None


def title_from_url(url: str) -> str:
    """A readable title from the last path segment, for surfaces that publish none.

    Application-derived from the URL the source itself chose, never text scraped from the remote
    page. Percent escapes are decoded so a release does not read as `new-zealand%E2%80%99s`."""
    path = urlsplit(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    if not segment:
        return urlsplit(url).hostname or url
    words = [word for word in re.split(r"[-_]+", unquote(segment)) if word]
    return bounded_text(" ".join(words) or segment, limit=MAX_TITLE_CHARS)


class _PlainText(HTMLParser):
    _BLOCK_TAGS = frozenset({"p", "li", "blockquote", "pre", "br", "div", "h1", "h2", "h3"})
    _SKIP_CONTENT_TAGS = frozenset({"script", "style"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_CONTENT_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    extractor = _PlainText()
    extractor.feed(value)
    return bounded_text(extractor.text())


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


@dataclass(frozen=True)
class _StrategyDefinition:
    source_key: str
    endpoint: CivicEndpoint
    publisher: str
    tier: CivicEvidenceTier
    crawl_delay_seconds: float = 0.0
    max_items: int = MAX_DOCUMENTS_PER_FETCH
    # Only paths containing one of these fragments become documents, so a whole-site surface
    # yields publications rather than navigation. Application-controlled.
    path_includes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewsSitemapDefinition(_StrategyDefinition):
    """A Google news sitemap: `<urlset>` entries carrying a `<news:news>` block."""


@dataclass(frozen=True)
class EmbeddedListDefinition(_StrategyDefinition):
    """A page whose publication list is JSON handed to a client component."""

    marker: str = _EMBEDDED_JSON_MARKER


@dataclass(frozen=True)
class LinkIndexDefinition(_StrategyDefinition):
    """A listing page read through one fixed link pattern."""

    link_pattern: str = ""

    def __post_init__(self) -> None:
        if not self.link_pattern:
            raise ValueError(f"{self.source_key}: a link index needs a link_pattern")


@dataclass(frozen=True)
class WallClockFeedDefinition(_StrategyDefinition):
    """RSS whose `pubDate` is an offset-free local time in this source's timezone."""

    publication_timezone: str = "UTC"


class _StrategyConnector:
    """Shared plumbing: one bounded request, config refused, definition exposed. Matches the
    canonical connectors' shape exactly so the catalog builds all of them the same way."""

    def __init__(self, definition: _StrategyDefinition, transport: CivicTransport | None = None):
        self._definition = definition
        self._transport = transport or party_media_transport()

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> _StrategyDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def _response(self) -> CivicResponse:
        """One bounded request through the shared transport.

        The response carries `final_url`, the URL the redirect chain actually ended on. Every hop
        was already re-validated against this endpoint's own allow-list by
        `beehive.connectors.http`, so an off-allow-list landing raises there rather than arriving
        here -- what this is used for is resolving a document's relative links against the
        document actually received rather than the one requested.
        """
        definition = self._definition
        return self._transport.request(
            definition.endpoint,
            source_key=definition.source_key,
            crawl_delay_seconds=definition.crawl_delay_seconds)

    def _keeps(self, url: str) -> bool:
        includes = self._definition.path_includes
        if not includes:
            return True
        path = urlsplit(url).path
        return any(fragment in path for fragment in includes)

    def _document(self, **kwargs) -> CivicDocument:
        definition = self._definition
        return CivicDocument(
            source_key=definition.source_key,
            tier=definition.tier,
            publisher=definition.publisher,
            **kwargs)


class NewsSitemapConnector(_StrategyConnector):
    """Reads a Google news sitemap, keeping the headline and publication time it declares."""

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        definition = self._definition
        response = self._response()
        base = response.final_url
        root = parse_xml(
            response.body, source_key=definition.source_key, url=definition.endpoint.url)
        if root.tag != f"{_SITEMAP_NS}urlset":
            raise CivicSourceError(
                definition.source_key,
                f"{definition.endpoint.url}: expected a sitemap <urlset>, got {root.tag!r}")
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        matched = 0
        for entry in root.findall(f"{_SITEMAP_NS}url"):
            location = _text(entry.find(f"{_SITEMAP_NS}loc"))
            if not location:
                degraded.append("sitemap entry has no <loc>")
                continue
            try:
                target = canonical_url(location, base=base)
            except ValueError as exc:
                degraded.append(f"{location}: {exc}")
                continue
            if not self._keeps(target):
                continue
            news = entry.find(f"{_NEWS_NS}news")
            if news is None:
                degraded.append(f"{target}: no <news:news> block, so it carries no publication")
                continue
            matched += 1
            if len(documents) >= definition.max_items:
                continue
            publication = publication_witness(
                _text(news.find(f"{_NEWS_NS}publication_date")),
                source="news-sitemap-publication-date",
            )
            documents.append(self._document(
                external_key=target,
                canonical_url=target,
                title=bounded_text(
                    _text(news.find(f"{_NEWS_NS}title")) or title_from_url(target),
                    limit=MAX_TITLE_CHARS),
                first_seen_at=now,
                published_at=publication.value if publication else None,
                updated_at=parse_iso8601(_text(entry.find(f"{_SITEMAP_NS}lastmod"))),
                raw_metadata={
                    "sitemap": base,
                    "publication": publication.as_metadata() if publication else None,
                }))
        if matched > len(documents):
            degraded.append(
                f"kept {len(documents)} of {matched} entries at the "
                f"{definition.max_items}-document cap")
        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded)


class EmbeddedListConnector(_StrategyConnector):
    """Reads the publication list a server-rendered page hands to its client component.

    Identity is the canonical on-host URL, never the list's own item id: the id is minted per
    render, so keying off it republished every release on every fetch. The id is kept in
    `raw_metadata` for tracing.
    """

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        definition = self._definition
        response = self._response()
        base = response.final_url
        html = response.body.decode("utf-8", errors="replace")
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        seen: set[str] = set()
        matched = 0
        for block in self._blocks(html):
            for candidate in _walk_json(block):
                url = candidate.get("url")
                name = candidate.get("name")
                published = candidate.get("publicationDate")
                if not isinstance(url, str) or not isinstance(name, str):
                    continue
                if not isinstance(published, str):
                    continue
                try:
                    target = _identity_url(url, base=base)
                except ValueError as exc:
                    degraded.append(f"{url}: {exc}")
                    continue
                if not self._keeps(target) or target in seen:
                    continue
                seen.add(target)
                matched += 1
                if len(documents) >= definition.max_items:
                    continue
                item_id = candidate.get("id")
                publication = publication_witness(
                    published, source="embedded-list-publicationDate",
                )
                documents.append(self._document(
                    external_key=target,
                    canonical_url=target,
                    title=bounded_text(name, limit=MAX_TITLE_CHARS),
                    first_seen_at=now,
                    published_at=publication.value if publication else None,
                    updated_at=parse_iso8601(published),
                    raw_metadata={
                        "list_item_id": item_id if isinstance(item_id, str) else None,
                        "publication": publication.as_metadata() if publication else None,
                    }))
        if not seen:
            raise CivicSourceError(
                definition.source_key,
                f"{definition.endpoint.url}: no embedded publication list found")
        if matched > len(documents):
            degraded.append(
                f"kept {len(documents)} of {matched} entries at the "
                f"{definition.max_items}-document cap")
        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded)

    def _blocks(self, html: str) -> list[dict]:
        definition = self._definition
        blocks: list[dict] = []
        position = 0
        while len(blocks) < _MAX_EMBEDDED_JSON_BLOCKS:
            start = html.find(definition.marker, position)
            if start < 0:
                break
            opening = start + len(definition.marker)
            position = opening
            if opening >= len(html) or html[opening] != "{":
                continue
            end = _matching_brace(html, opening)
            if end is None:
                raise CivicSourceError(
                    definition.source_key,
                    f"{definition.endpoint.url}: embedded JSON block is unterminated")
            position = end + 1
            try:
                parsed = json.loads(html[opening:end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                blocks.append(parsed)
        return blocks


class LinkIndexConnector(_StrategyConnector):
    """Reads a listing page through one fixed link pattern.

    The narrowest possible reading of one committed page shape, not a crawl. Such a page carries
    no publication time, so documents are emitted undated and `first_seen_at` is the only
    timestamp -- which is exactly what `CivicDocument.effective_at` falls back to.
    """

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        definition = self._definition
        response = self._response()
        base = response.final_url
        collector = _LinkCollector()
        collector.feed(response.body.decode("utf-8", errors="replace"))
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        seen: set[str] = set()
        matched = 0
        for href, text in collector.links:
            try:
                target = _identity_url(href, base=base)
            except ValueError:
                continue  # mailto:, javascript:, and anchors are ordinary page furniture
            if definition.link_pattern not in target or target in seen:
                continue
            if not self._keeps(target):
                continue
            seen.add(target)
            matched += 1
            if len(documents) >= definition.max_items:
                continue
            documents.append(self._document(
                external_key=target,
                canonical_url=target,
                title=bounded_text(text, limit=MAX_TITLE_CHARS) or title_from_url(target),
                first_seen_at=now,
                raw_metadata={"listing_page": base}))
        if matched > len(documents):
            degraded.append(
                f"kept {len(documents)} of {matched} links at the "
                f"{definition.max_items}-document cap")
        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded)


class WallClockFeedConnector(_StrategyConnector):
    """RSS whose publication times are offset-free local times and whose guid is site-relative.

    Everything else about it is ordinary RSS, so the canonical guarded parse, tombstone check and
    result building are reused; only the two source-specific rules live here.
    """

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        definition = self._definition
        response = self._response()
        base = response.final_url
        root = parse_xml(
            response.body, source_key=definition.source_key, url=definition.endpoint.url)
        assert_not_tombstone_document(
            root, source_key=definition.source_key, url=definition.endpoint.url)
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        for index, item in enumerate(root.findall(".//item")[:definition.max_items]):
            title = _text(item.find("title"))
            link = _text(item.find("link"))
            if not title or not link:
                degraded.append(f"entry {index}: feed item is missing a title or link")
                continue
            try:
                target = canonical_url(link, base=base)
            except ValueError as exc:
                degraded.append(f"entry {index}: {exc}")
                continue
            raw_date = _text(item.find("pubDate"))
            published = parse_wall_clock(
                raw_date, timezone_name=definition.publication_timezone
            ) or parse_rfc2822(raw_date)
            documents.append(self._document(
                external_key=target,
                canonical_url=target,
                title=bounded_text(title, limit=MAX_TITLE_CHARS),
                first_seen_at=now,
                published_at=published,
                updated_at=published,
                excerpt=html_to_text(_text(item.find("description"))),
                raw_metadata={
                    "feed_guid": _text(item.find("guid")),
                    "excerpt_source": "rss-description",
                    "publication": (
                        PublicationWitness(
                            published, "rss-wall-clock-pubDate", "datetime",
                            bounded_text(raw_date, limit=100), definition.publication_timezone,
                        ).as_metadata()
                        if published is not None else None
                    ),
                }))
        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded)


def fetch_entry(
    entry: PartyMediaSourceEntry,
    now: datetime,
    *,
    transport: CivicTransport | None = None,
    limit: int = MAX_DOCUMENTS_PER_FETCH,
    config: dict | None = None,
) -> CivicFetchResult:
    """The one seam a party or media fetch goes through.

    Refuses before any request when the source is blocked or licence-restricted, runs whichever
    connector the catalog names, and then applies the two rules this half of the catalog cannot
    delegate:

    * **Every document must be on its own source's host.** A feed that links off-site, and a
      redirect chain that ended somewhere else, look identical from here -- and neither may
      become evidence. Dropped documents degrade the result so the shortfall is visible rather
      than silent.
    * **The result is bounded.** Reaching the cap is reported, never quietly truncated.

    Health, degraded reasons and page counts all come back in the canonical `CivicFetchResult`.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    entry.assert_fetchable()
    connector = entry.build(transport or party_media_transport())
    result = connector.fetch(now, config=config or {})

    kept: list[CivicDocument] = []
    degraded = list(result.degraded_reasons)
    on_host = 0
    for document in result.documents:
        if not entry.owns_url(document.canonical_url):
            degraded.append(
                f"{document.canonical_url}: not on {sorted(entry.hosts)!r}, so it cannot be "
                "this source's own publication")
            continue
        on_host += 1
        if len(kept) < limit:
            kept.append(document)
    if on_host > len(kept):
        degraded.append(
            f"kept {len(kept)} of {on_host} documents at the {limit}-document cap")
    return build_result(
        entry.source_key, kept, result.fetched_at,
        degraded_reasons=degraded, pages_fetched=result.pages_fetched)


def _identity_url(url: str, *, base: str) -> str:
    """A canonical URL whose trailing slash is normalized away.

    `canonical_url` deliberately preserves the path exactly, because several official sources
    serve case- and slash-sensitive paths. A party's own listing page is the opposite case: it
    renders `/news/x` and `/news/x/` for one release, and treating those as two publications
    republishes the back catalogue the first time the site changes its mind.
    """
    canonical = canonical_url(url, base=base)
    parts = urlsplit(canonical)
    if parts.path.endswith("/") and parts.path != "/":
        canonical = canonical.replace(parts.path, parts.path.rstrip("/"), 1)
    return canonical


def _matching_brace(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _walk_json(node: object, depth: int = 0):
    if depth > _MAX_JSON_DEPTH:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value, depth + 1)


def _text(element) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


__all__ = [
    "EmbeddedListConnector",
    "EmbeddedListDefinition",
    "LinkIndexConnector",
    "LinkIndexDefinition",
    "NewsSitemapConnector",
    "NewsSitemapDefinition",
    "WallClockFeedConnector",
    "WallClockFeedDefinition",
    "fetch_entry",
    "html_to_text",
    "parse_wall_clock",
    "party_media_transport",
    "title_from_url",
]
