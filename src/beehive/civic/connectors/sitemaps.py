"""Sitemap-based Civic sources: OAG (gzipped), the Ombudsman, and Stats NZ (index + children).

A sitemap is the fallback for institutions that publish no feed and no API. It is also the
easiest crawl to get wrong, so three rules are enforced here rather than per source:

* **Crawl-delay is real.** Stats NZ publishes `Crawl-delay: 10` in robots.txt, and a sitemap
  index means N+1 requests to the same host in a row. The pacer in `fetching.py` holds the gap;
  `crawl_delay_seconds` on the definition is what tells it how long. Honouring a 10-second delay
  across a 5-child index is also why `max_children` is small: the point is to be a well-behaved
  guest, not to mirror the site.
* **A sitemap index is not a sitemap.** `<sitemapindex>` lists child sitemaps, `<urlset>` lists
  pages. Detected by root tag, never by filename, because `sitemap.xml` is used for both across
  these three sites. One level of nesting is followed and no more -- deeper nesting is reported
  as degraded rather than recursed into, so a misconfigured index can never turn into an
  unbounded crawl.
* **A partial crawl says so.** One failing child sitemap degrades the result and keeps the
  children that succeeded, because losing the whole of Stats NZ because one section 503'd is
  worse than reporting "9 of 10 sections".

Sitemap entries carry `<lastmod>` and nothing else, so `updated_at` is populated and
`published_at` is left None -- exactly the case `CivicDocument`'s three-timestamp split exists
for. A title is not available either, so the definition supplies a `title_from_url` rule: the
last meaningful path segment, humanized. That is application-controlled, never remote text.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urlsplit

from beehive.civic.documents import (CivicDocument, CivicFetchResult, MAX_TITLE_CHARS,
                                      bounded_text, build_result, canonical_url)
from beehive.civic.fetching import (CivicEndpoint, CivicTransport, SITEMAP_MAX_BYTES,
                                     CivicSourceError, decompress_gzip, parse_iso8601, parse_xml)
from beehive.domain.civic import CivicEvidenceTier

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_MAX_CHILD_SITEMAPS = 10
_MAX_URLS = 500


@dataclass(frozen=True)
class SitemapSourceDefinition:
    source_key: str
    endpoint: CivicEndpoint
    publisher: str
    tier: CivicEvidenceTier
    crawl_delay_seconds: float = 0.0
    is_gzipped: bool = False
    # Only paths containing one of these fragments become documents. Application-controlled, so a
    # site restructure produces fewer documents rather than a crawl of its whole marketing site.
    path_includes: tuple[str, ...] = ()
    max_children: int = _MAX_CHILD_SITEMAPS
    max_urls: int = _MAX_URLS


# Percent-decoding can yield C0 control characters from a malformed or hostile path. This is a
# display field, so they are stripped rather than passed through.
_CONTROL_CHARS = {chr(code): " " for code in range(0x20)} | {chr(0x7F): " "}


def _title_from_url(url: str) -> str:
    """A readable title from the last meaningful path segment. Application-derived, never text
    pulled from the remote page -- a sitemap gives us no title, and inventing one from remote
    content would be reading untrusted data into a display field for no benefit.

    The segment is percent-decoded BEFORE it is split, because sitemap paths are encoded: ACT's
    `/news/new-zealand%E2%80%99s-new-religion` otherwise produces the literal title
    "New zealand%e2%80%99s new religion". Decoding is lenient (`errors="replace"`) so one
    malformed escape degrades a single title instead of losing the whole sitemap."""
    path = urlsplit(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    if not segment:
        return urlsplit(url).hostname or url
    for suffix in (".html", ".htm", ".aspx", ".pdf"):
        if segment.lower().endswith(suffix):
            segment = segment[: -len(suffix)]
    decoded = unquote(segment, errors="replace").translate(str.maketrans(_CONTROL_CHARS))
    readable = " ".join(decoded.replace("_", "-").replace("-", " ").split())
    return bounded_text(readable.capitalize() or segment, limit=MAX_TITLE_CHARS)


def _entries(root: ET.Element, tag: str) -> list[ET.Element]:
    return root.findall(f"{_SITEMAP_NS}{tag}")


def _child(element: ET.Element, tag: str) -> str | None:
    found = element.find(f"{_SITEMAP_NS}{tag}")
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _is_index(root: ET.Element) -> bool:
    return root.tag == f"{_SITEMAP_NS}sitemapindex"


class SitemapConnector:
    """Reads one fixed sitemap (or sitemap index) into normalized documents."""

    def __init__(
        self, definition: SitemapSourceDefinition, transport: CivicTransport | None = None,
    ):
        self._definition = definition
        self._transport = transport or CivicTransport()

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> SitemapSourceDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def _load(self, endpoint: CivicEndpoint) -> ET.Element:
        definition = self._definition
        response = self._transport.request(
            endpoint,
            source_key=definition.source_key,
            crawl_delay_seconds=definition.crawl_delay_seconds)
        body = response.body
        if definition.is_gzipped or response.content_type in (
                "application/gzip", "application/x-gzip"):
            body = decompress_gzip(
                body, source_key=definition.source_key, url=endpoint.url,
                max_bytes=SITEMAP_MAX_BYTES)
        return parse_xml(body, source_key=definition.source_key, url=endpoint.url)

    def _documents_from(
        self, root: ET.Element, base_url: str, now: datetime, budget: int,
    ) -> tuple[list[CivicDocument], list[str]]:
        """Documents from one `<urlset>`, bounded by `budget`.

        Reaching the budget while entries remain is reported, never silently truncated: NZ
        Legislation's sitemap is ~43,000 URLs, so a cap is unavoidable, and a consumer that
        mistook the first 500 for the whole corpus would conclude that everything else had been
        withdrawn."""
        definition = self._definition
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        entries = _entries(root, "url")
        matched = 0
        for entry in entries:
            location = _child(entry, "loc")
            if not location:
                degraded.append("sitemap entry has no <loc>")
                continue
            try:
                target = canonical_url(location, base=base_url)
            except ValueError as exc:
                degraded.append(f"{location}: {exc}")
                continue
            if definition.path_includes and not any(
                    fragment in urlsplit(target).path for fragment in definition.path_includes):
                continue
            matched += 1
            if len(documents) >= budget:
                continue
            documents.append(CivicDocument(
                source_key=definition.source_key,
                external_key=target,
                canonical_url=target,
                title=_title_from_url(target),
                tier=definition.tier,
                publisher=definition.publisher,
                first_seen_at=now,
                published_at=None,
                updated_at=parse_iso8601(_child(entry, "lastmod")),
                raw_metadata={"sitemap": base_url}))
        if matched > len(documents):
            # Reported for a flat <urlset> exactly as for a child of an index: a capped crawl
            # must never surface as OK, or a consumer reading "500 URLs, healthy" concludes the
            # other 42,000 were withdrawn.
            degraded.append(
                f"{base_url}: kept {len(documents)} of {matched} matching URLs at the "
                f"{definition.max_urls}-URL cap")
        return documents, degraded

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        """Fetches the sitemap, following one level of index nesting.

        A child sitemap that fails degrades the result and the successful children are kept: one
        section returning 503 must not lose the whole institution."""
        self.validate_config(config or {})
        definition = self._definition
        root = self._load(definition.endpoint)
        pages_fetched = 1
        documents: list[CivicDocument] = []
        degraded: list[str] = []

        if not _is_index(root):
            documents, degraded = self._documents_from(
                root, definition.endpoint.url, now, definition.max_urls)
            return build_result(
                definition.source_key, documents, now, degraded_reasons=degraded,
                pages_fetched=pages_fetched)

        children = _entries(root, "sitemap")
        selected = children[: definition.max_children]
        if len(children) > definition.max_children:
            degraded.append(
                f"sitemap index lists {len(children)} children; stopped at the "
                f"{definition.max_children}-child cap")
        for child in selected:
            location = _child(child, "loc")
            if not location:
                degraded.append("sitemap index entry has no <loc>")
                continue
            try:
                child_root = self._load(definition.endpoint.at(canonical_url(location)))
                pages_fetched += 1
            except (CivicSourceError, ValueError) as exc:
                degraded.append(f"child sitemap {location}: {exc}")
                continue
            if _is_index(child_root):
                degraded.append(
                    f"child sitemap {location} is itself an index; nesting beyond one level is "
                    "never followed")
                continue
            budget = definition.max_urls - len(documents)
            if budget <= 0:
                degraded.append(
                    f"stopped at the {definition.max_urls}-URL cap before {location}")
                break
            child_documents, child_degraded = self._documents_from(
                child_root, location, now, budget)
            documents.extend(child_documents)
            degraded.extend(child_degraded)

        return build_result(
            definition.source_key, documents, now, degraded_reasons=degraded,
            pages_fetched=pages_fetched)


__all__ = ["SitemapConnector", "SitemapSourceDefinition"]
