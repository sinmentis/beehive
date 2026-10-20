"""NZ Legislation: sitemap discovery, bounded per-document hydration, and the retired endpoints.

**There is no aggregate keyless XML feed.** An earlier version of this module treated
`https://www.legislation.govt.nz/subscribe/en/latest.xml` as a "recently changed legislation"
document. It is not one -- that path does not serve an aggregate listing, and building a source
on it produced a permanently empty feed that looked healthy. The keyless surface is per-document
only, so the access model here is two explicit steps:

1. **Discover** canonical work URLs from `https://www.legislation.govt.nz/sitemap.xml`
   (`sitemaps.SitemapConnector`, catalogued as `nz_legislation_sitemap`). That sitemap is roughly
   5.2 MiB and ~43,000 URLs, which is why it is given `SITEMAP_MAX_BYTES` rather than the default
   cap and why its output is bounded with an explicit degraded reason -- a consumer that mistook
   the first N for the whole corpus would conclude everything else had been withdrawn.
2. **Hydrate**, optionally and boundedly, a small number of changed documents by deriving each
   one's XML URL from its own canonical path (`document_xml_url`). Derivation only happens for
   path shapes that have been verified; anything else returns None rather than guessing a URL
   into existence.

`/v0/works` remains available and remains optional: `LegislationApiConnector.is_available` is
False without a key and `catalog.enabled_sources` omits the source entirely, so Civic runs on the
keyless paths with narrower coverage instead of failing.

Retired endpoints are refused twice over. `TOMBSTONE_PATHS` blocks them at construction, so a
catalog edit cannot reintroduce one; and `fetching.assert_not_tombstone_document` catches the
harder runtime case where a live path starts answering with a structurally valid Atom feed whose
id is `url_changed`.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from beehive.civic.documents import (CivicDocument, CivicFetchResult, CivicTombstoneError,
                                      MAX_TITLE_CHARS, bounded_text, build_result, canonical_url,
                                      stable_external_key)
from beehive.civic.fetching import (CivicEndpoint, CivicTransport, DEFAULT_MAX_ITEMS,
                                     assert_not_tombstone_document, parse_iso8601, parse_json,
                                     parse_xml, require_mapping, require_sequence)
from beehive.civic.source_extraction import PublicationWitness, publication_witness
from beehive.domain.civic import CivicEvidenceTier

LEGISLATION_HOSTS = frozenset({"legislation.govt.nz", "www.legislation.govt.nz"})
LEGISLATION_API_HOSTS = frozenset({"api.legislation.govt.nz"})

# The environment variable the optional API key is read from. Named here so there is exactly one
# spelling, and absent by default in every environment including tests.
LEGISLATION_API_KEY_ENV = "CIVIC_LEGISLATION_API_KEY"

# Paths that answer HTTP 200 but are no longer live sources. Refused at construction so a future
# catalog edit cannot silently reintroduce a feed that reports permanent emptiness as success.
#
# `/subscribe/en/latest.xml` is here for a different reason from the RSS path: it is not a
# retirement notice, it simply never was the aggregate "latest legislation" document it was once
# configured as. Blocking it in the same place keeps both mistakes un-repeatable.
TOMBSTONE_PATHS = frozenset({
    "/subscribe/nzpco-rss.xml",
    "/subscribe/en/latest.xml",
})

# The document path kinds the live sitemap actually publishes, counted over all 43,149 entries:
#
#   secondary-legislation  20,980   /secondary-legislation/pco-drafted/1887/1/en/latest/
#   act                    17,470   /act/imperial/1267/23/en/latest/
#   amendment-paper         2,806   /amendment-paper/government/2006/50/en/latest/
#   bill                    1,893   /bill/government/1998/238/en/latest/
#
# `regulation` and `sop` are deliberately NOT here: they were assumed by an earlier version and
# appear nowhere in the sitemap. (Confusingly, they ARE the XML ROOT TAGS -- secondary
# legislation roots as <regulation> and an amendment paper roots as <sop> -- which is exactly how
# the wrong assumption survived. Path kind and root tag are different vocabularies.)
#
# The document root is `/{kind}/{branch}/{year}/{number}` and the language/version tail is
# `/en/latest/`; the XML form is that root plus `/en/latest.xml`. The older
# `/latest/DLM4326000.html` link shape is still matched because it appears across the site.
_DOCUMENT_PATH_RE = re.compile(
    r"^/(?P<kind>act|bill|secondary-legislation|amendment-paper)/(?P<branch>[^/]+)/"
    r"(?P<year>\d{4})/(?P<number>[^/]+)"
    r"(?:/(?P<lang>en|mi))?"
    r"(?:/(?P<version>latest|[0-9.]+))?"
    r"(?:/(?P<file>[^/]*\.html?))?/?$",
    re.IGNORECASE)
_HYDRATION_SUFFIX = "en/latest.xml"

# 9,673 of the secondary-legislation entries are agency-drafted items whose number is a
# tilde-prefixed internal key (`/secondary-legislation/agency-drafted/2000/~274499/en/latest/`).
# Those answer HTTP 500 for the `.xml` form -- verified against the live site -- so no XML URL is
# derived for them at all. Deriving one anyway would spend a request per document to collect a
# guaranteed server error and would mark the whole pass degraded for a condition that is simply
# how the site works.
_UNHYDRATABLE_NUMBER_PREFIX = "~"

DEFAULT_HYDRATION_LIMIT = 10


def assert_not_tombstone(url: str, *, source_key: str) -> None:
    """Refuses a known dead endpoint before any request is made.

    Raised as `CivicTombstoneError` rather than a plain ValueError so the operational meaning --
    permanent, retrying is pointless, the catalog entry itself must change -- survives all the
    way to whatever records the failure."""
    path = urlsplit(url).path.rstrip("/") or "/"
    if path.lower() in TOMBSTONE_PATHS:
        raise CivicTombstoneError(
            source_key,
            f"{url} is not a live aggregate source (it either answers HTTP 200 with a retirement "
            "notice or never served a listing at all); it must never be configured as a Civic "
            "source")


def document_xml_url(page_url: str) -> str | None:
    """The XML form of one legislation document, or None when the path is not a verified shape.

    Returning None rather than guessing is the point: NZ Legislation soft-404s a surprising
    number of near-miss paths, so a derived URL that "looks right" can quietly hydrate a
    not-found page into what a reader sees as the text of an Act."""
    parts = urlsplit(page_url)
    if parts.hostname is None:
        return None
    host = parts.hostname.lower()
    if not any(host == allowed or host.endswith(f".{allowed}")
               for allowed in LEGISLATION_HOSTS):
        return None
    match = _DOCUMENT_PATH_RE.match(parts.path)
    if match is None:
        return None
    groups = match.groupdict()
    if groups["lang"] is None and groups["version"] is None:
        # `/{kind}/{branch}/{year}/{number}` alone is the instrument index, not a document.
        return None
    if groups["number"].startswith(_UNHYDRATABLE_NUMBER_PREFIX):
        return None
    root = f"/{groups['kind'].lower()}/{groups['branch']}/{groups['year']}/{groups['number']}"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       f"{root}/{_HYDRATION_SUFFIX}", "", ""))


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _find_first(root: ET.Element, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _text(root.find(f".//{name}"))
        if value:
            return value
    return None


@dataclass(frozen=True)
class LegislationHydrationDefinition:
    """The per-document XML hydration path. Not a listing source: it is only ever pointed at a
    URL the sitemap already discovered."""

    source_key: str
    publisher: str
    tier: CivicEvidenceTier
    allowed_hosts: frozenset[str] = LEGISLATION_HOSTS
    crawl_delay_seconds: float = 2.0
    max_documents: int = DEFAULT_HYDRATION_LIMIT
    # Per-document XML is small; this is well under the shared default and keeps one oversized
    # instrument from consuming a run's whole byte budget.
    max_bytes: int = 2 * 1024 * 1024


# Every legislation XML document carries its identity and its dates as ROOT ATTRIBUTES, not as
# child elements, and the attribute names differ by instrument kind. Captured from the live site:
#
#   <act      id="DLM10900"  date.assent="1988-07-28" date.as.at="2008-01-01"
#             date.first.valid="2007-09-18" date.terminated="2008-01-01" .../>
#   <regulation id="LMS963891" date.gazetted="2024-06-06" date.signed="2024-06-04"
#             date.as.at="2024-06-04" date.first.valid="2024-06-06"
#             date.terminated="nulldate" .../>          <- secondary legislation
#   <sop      id="DLM1106000" stage="published" .../>   <- amendment paper, no dates at all
#   <bill     id="DLM947600"  date.terminated="2008-12-15" .../>
#
# Assent, signing and commencement are legal events, not publication timestamps.
_PUBLISHED_ATTRS = ("date.gazetted",)
_EVENT_ATTRS = ("date.assent", "date.gazetted", "date.signed", "date.first.valid")
_UPDATED_ATTRS = ("date.as.at", "date.first.valid")
_ID_ATTRS = ("id",)

# `date.terminated="nulldate"` is a real value the site emits for instruments that have not been
# terminated. Parsed naively it is simply an unparseable string, but it is listed explicitly so
# nobody later "fixes" the parser by treating it as a date.
_NULL_DATE_SENTINELS = frozenset({"nulldate", "null", ""})


def _attr(root: ET.Element, names: tuple[str, ...]) -> str | None:
    """The first present, non-sentinel root attribute. Root attributes are authoritative; the
    element fallbacks below only run when none is present."""
    for name in names:
        value = root.get(name)
        if value is None:
            continue
        cleaned = value.strip()
        if cleaned and cleaned.lower() not in _NULL_DATE_SENTINELS:
            return cleaned
    return None


def _xml_date_witness(
    root: ET.Element, attributes: tuple[str, ...], elements: tuple[str, ...],
) -> PublicationWitness | None:
    for name in attributes:
        witness = publication_witness(
            _attr(root, (name,)), source=f"legislation-xml-attribute-{name}",
        )
        if witness is not None:
            return witness
    for name in elements:
        witness = publication_witness(
            _find_first(root, (name,)), source=f"legislation-xml-element-{name}",
        )
        if witness is not None:
            return witness
    return None


class LegislationHydrator:
    """Bounded, opt-in enrichment of documents the sitemap already discovered.

    Never a discovery source of its own: it takes canonical page URLs, derives each one's XML
    form via `document_xml_url`, and fetches at most `max_documents` of them. A URL whose path is
    not a verified document shape -- or is one of the tilde-numbered agency-drafted items whose
    XML answers HTTP 500 -- is skipped and reported, never guessed at.

    **Identity replacement is deterministic.** A sitemap document is identified by its canonical
    URL, because that is all a sitemap provides. A hydrated document is identified by the
    instrument's own DLM/LMS id from the XML root, which is the genuinely stable key. The
    replacement is one-directional and reproducible -- the same page URL always yields the same
    id -- and the URL it replaces is recorded as `raw_metadata["sitemap_external_key"]`, so a
    caller can migrate an existing Candidate deterministically instead of the same instrument
    appearing under two dedupe keys."""

    # `billref` is last because it is the amendment-paper case: those documents carry no
    # <title> at all, and the bill they amend is the only human-meaningful label available.
    _TITLE_TAGS = ("title", "shortTitle", "name", "billref")
    _ID_TAGS = ("id", "identifier", "workId", "pitId")
    _DATE_TAGS = ("publishedDate",)
    _EVENT_TAGS = ("assentDate", "commencementDate", "date")
    _UPDATED_TAGS = ("lastModified", "modified", "updated", "amendedDate")

    def __init__(
        self,
        definition: LegislationHydrationDefinition,
        transport: CivicTransport | None = None,
    ):
        self._definition = definition
        self._transport = transport or CivicTransport()

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> LegislationHydrationDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def _endpoint_for(self, xml_url: str) -> CivicEndpoint:
        return CivicEndpoint(
            url=xml_url,
            allowed_hosts=self._definition.allowed_hosts,
            accept="application/xml",
            content_types=frozenset({"application/xml", "text/xml"}),
            max_bytes=self._definition.max_bytes)

    def _identity(self, root: ET.Element, page_url: str) -> str:
        """The instrument's DLM/LMS id, root attribute first, element fallback second, and the
        page URL only if the document carries no id at all."""
        return (_attr(root, _ID_ATTRS)
                or _find_first(root, self._ID_TAGS)
                or page_url)

    def hydrate(
        self, page_urls: list[str], now: datetime, *, config: dict | None = None,
    ) -> CivicFetchResult:
        """Fetches the XML form of up to `max_documents` discovered documents."""
        self.validate_config(config or {})
        definition = self._definition
        documents: list[CivicDocument] = []
        degraded: list[str] = []
        fetched = 0

        for page_url in page_urls:
            if fetched >= definition.max_documents:
                degraded.append(
                    f"stopped at the {definition.max_documents}-document hydration cap with "
                    f"{len(page_urls)} candidates")
                break
            xml_url = document_xml_url(page_url)
            if xml_url is None:
                degraded.append(
                    f"{page_url}: no XML URL was derived (not a verified legislation document "
                    "path, or an agency-drafted item whose XML is not served)")
                continue
            endpoint = self._endpoint_for(xml_url)
            response = self._transport.request(
                endpoint, source_key=definition.source_key,
                crawl_delay_seconds=definition.crawl_delay_seconds)
            fetched += 1
            root = parse_xml(response.body, source_key=definition.source_key, url=xml_url)
            assert_not_tombstone_document(
                root, source_key=definition.source_key, url=xml_url)
            title = _find_first(root, self._TITLE_TAGS)
            if not title:
                degraded.append(f"{xml_url}: document XML has no title")
                continue
            try:
                publication = _xml_date_witness(root, _PUBLISHED_ATTRS, self._DATE_TAGS)
                event = _xml_date_witness(root, _EVENT_ATTRS, self._EVENT_TAGS)
                documents.append(CivicDocument(
                    source_key=definition.source_key,
                    external_key=stable_external_key(
                        self._identity(root, page_url), source_key=definition.source_key,
                        field_name="legislation id"),
                    canonical_url=canonical_url(page_url),
                    title=bounded_text(title, limit=MAX_TITLE_CHARS),
                    tier=definition.tier,
                    publisher=definition.publisher,
                    first_seen_at=now,
                    published_at=publication.value if publication else None,
                    updated_at=(parse_iso8601(_attr(root, _UPDATED_ATTRS))
                                or parse_iso8601(_find_first(root, self._UPDATED_TAGS))),
                    raw_metadata={
                        "legislation_xml_url": xml_url,
                        "legislation_root": root.tag.split("}")[-1],
                        "publication": publication.as_metadata() if publication else None,
                        "event_date": event.as_metadata()["calendar_date"] if event else None,
                        "event_date_provenance": event.as_metadata() if event else None,
                        # The identity this document replaces, so a caller can migrate an
                        # existing sitemap-keyed Candidate without minting a second one.
                        "sitemap_external_key": canonical_url(page_url),
                    }))
            except ValueError as exc:
                # Covers a malformed id or an unusable canonical URL: one bad instrument
                # degrades the pass rather than aborting a whole hydration batch.
                degraded.append(f"{xml_url}: {exc}")
        return build_result(definition.source_key, documents, now, degraded_reasons=degraded,
                            pages_fetched=fetched)


@dataclass(frozen=True)
class LegislationApiDefinition:
    """The optional keyed `api.legislation.govt.nz/v0/works` source."""

    source_key: str
    endpoint: CivicEndpoint
    publisher: str
    tier: CivicEvidenceTier
    crawl_delay_seconds: float = 1.0
    max_items: int = DEFAULT_MAX_ITEMS
    api_key_env: str = LEGISLATION_API_KEY_ENV
    header_name: str = "Ocp-Apim-Subscription-Key"

    def __post_init__(self) -> None:
        assert_not_tombstone(self.endpoint.url, source_key=self.source_key)


class LegislationApiConnector:
    """The keyed Works API, which is entirely optional.

    `is_available` is False when no key is configured, and `catalog.enabled_sources` omits the
    source in that case. This is the point of the design: Civic must run fully on keyless paths,
    so a missing key narrows coverage rather than failing a Civic Run. The key is read from the
    environment at fetch time and never stored, logged, or accepted from config."""

    def __init__(
        self,
        definition: LegislationApiDefinition,
        transport: CivicTransport | None = None,
        *,
        environ: dict[str, str] | None = None,
    ):
        self._definition = definition
        self._transport = transport or CivicTransport()
        self._environ = environ if environ is not None else os.environ

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> LegislationApiDefinition:
        return self._definition

    @property
    def is_available(self) -> bool:
        return bool(self._api_key())

    def _api_key(self) -> str:
        return (self._environ.get(self._definition.api_key_env) or "").strip()

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        """Raises ValueError when no key is configured. Callers reach this only by explicitly
        asking for a source `enabled_sources` already excluded, so the failure is a programming
        error rather than an operational one."""
        self.validate_config(config or {})
        definition = self._definition
        key = self._api_key()
        if not key:
            raise ValueError(
                f"{definition.source_key} needs {definition.api_key_env}; it is optional, so "
                "the catalog omits this source entirely when the key is absent")
        response = self._transport.request(
            definition.endpoint,
            source_key=definition.source_key,
            crawl_delay_seconds=definition.crawl_delay_seconds,
            extra_headers={definition.header_name: key})
        payload = parse_json(
            response.body, source_key=definition.source_key, url=definition.endpoint.url)
        envelope = require_mapping(
            payload, source_key=definition.source_key, field_name="works response")
        works = require_sequence(
            envelope.get("works", envelope.get("results", [])),
            source_key=definition.source_key, field_name="works")

        documents: list[CivicDocument] = []
        degraded: list[str] = []
        for index, record in enumerate(works[: definition.max_items]):
            try:
                documents.append(_work_document(record, definition, now))
            except ValueError as exc:
                degraded.append(f"work {index}: {exc}")
        return build_result(definition.source_key, documents, now, degraded_reasons=degraded)


def _work_document(
    record: Any, definition: LegislationApiDefinition, first_seen_at: datetime,
) -> CivicDocument:
    work = require_mapping(record, source_key=definition.source_key, field_name="work")
    identity = stable_external_key(
        work.get("id") or work.get("workId"), source_key=definition.source_key,
        field_name="work id")
    title = work.get("title") or work.get("name")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"work {identity} has no title")
    link = work.get("url") or work.get("link")
    if not isinstance(link, str) or not link.strip():
        raise ValueError(f"work {identity} has no url")
    publication = publication_witness(
        work.get("publishedDate"), source="legislation-works-publishedDate",
    )
    assent = publication_witness(work.get("assentDate"), source="legislation-works-assentDate")
    return CivicDocument(
        source_key=definition.source_key,
        external_key=identity,
        canonical_url=canonical_url(link, base=definition.endpoint.url),
        title=bounded_text(title, limit=MAX_TITLE_CHARS),
        tier=definition.tier,
        publisher=definition.publisher,
        first_seen_at=first_seen_at,
        published_at=publication.value if publication else None,
        updated_at=parse_iso8601(work.get("lastModified") or work.get("modified")),
        excerpt=bounded_text(work.get("summary") or work.get("description")),
        raw_metadata={
            "legislation_source": "works_api",
            "publication": publication.as_metadata() if publication else None,
            "event_date": assent.as_metadata()["calendar_date"] if assent else None,
            "event_date_provenance": assent.as_metadata() if assent else None,
        })


__all__ = [
    "DEFAULT_HYDRATION_LIMIT",
    "LEGISLATION_API_HOSTS",
    "LEGISLATION_API_KEY_ENV",
    "LEGISLATION_HOSTS",
    "LegislationApiConnector",
    "LegislationApiDefinition",
    "LegislationHydrationDefinition",
    "LegislationHydrator",
    "TOMBSTONE_PATHS",
    "assert_not_tombstone",
    "document_xml_url",
]
