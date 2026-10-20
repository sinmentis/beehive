"""The application's own catalog of official Civic sources.

This module is the single place a Civic source may be declared, and it is deliberately a set of
module-level constants. Three properties follow from that, and all three are load-bearing:

* **No arbitrary URLs.** Every endpoint is a literal here. Nothing reads a URL from stored
  config, a request parameter, a feed, or a model response, so there is no code path that can be
  talked into fetching somewhere else. Each endpoint additionally carries its own redirect
  allow-list, so even a compromised origin cannot bounce Beehive at a third party.
* **Tier is assigned here, by us.** `CivicEvidenceTier.OFFICIAL` is applied because *we* decided
  the Electoral Commission is an official source, not because any response said so. Nothing
  downstream may promote a document's tier, which is what stops `evidence_supports_publication`
  from being satisfied by a source describing itself as authoritative.
* **Optional credentials never become required.** The only key anywhere in Civic is NZ
  Legislation's optional Works API key. `enabled_sources` omits that source when the key is
  absent, so coverage narrows and nothing fails.

Endpoints are exactly the current ones the operators publish. The retired NZ Legislation RSS path
is recorded in `connectors/legislation.TOMBSTONE_PATHS` and refused at construction rather than
being listed here with a comment hoping nobody re-enables it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from beehive.civic.connectors.ckan import CkanConnector, CkanSourceDefinition
from beehive.civic.connectors.feeds import (CivicFeedConnector, FeedSourceDefinition,
                                             adapt_official_feed)
from beehive.civic.connectors.legislation import (LEGISLATION_API_HOSTS, LEGISLATION_API_KEY_ENV,
                                                   LEGISLATION_HOSTS, LegislationApiConnector,
                                                   LegislationApiDefinition,
                                                   LegislationHydrationDefinition,
                                                   LegislationHydrator)
from beehive.civic.connectors.parliament import (BILLS_SEARCH_FILTERS,
                                                  HANSARD_DAILY_URL_TEMPLATE,
                                                  HANSARD_SEARCH_FILTERS,
                                                  SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE,
                                                  SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE,
                                                  SELECT_COMMITTEES_SEARCH_FILTERS,
                                                  ParliamentApiConnector,
                                                  ParliamentApiDefinition)
from beehive.civic.connectors.sitemaps import SitemapConnector, SitemapSourceDefinition
from beehive.civic.fetching import CivicEndpoint, CivicTransport, SITEMAP_MAX_BYTES
from beehive.connectors.official_feeds import FEED_DEFINITIONS
from beehive.domain.civic import CivicEvidenceTier

_JSON = frozenset({"application/json", "text/json"})
_XML = frozenset({"application/xml", "text/xml", "application/rss+xml", "application/atom+xml"})
_GZIP = frozenset({"application/gzip", "application/x-gzip", "application/octet-stream"})

_ELECTIONS_HOSTS = frozenset({"elections.nz", "vote.nz"})
_BEEHIVE_HOSTS = frozenset({"beehive.govt.nz"})
_PARLIAMENT_HOSTS = frozenset({"parliament.nz"})
_OAG_HOSTS = frozenset({"oag.parliament.nz", "ao.parliament.nz"})
_OMBUDSMAN_HOSTS = frozenset({"ombudsman.parliament.nz"})
_STATS_HOSTS = frozenset({"stats.govt.nz"})
_DATA_GOVT_HOSTS = frozenset({"data.govt.nz"})
_RBNZ_HOSTS = frozenset({"rbnz.govt.nz"})

# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

# The Electoral Commission publishes the ten most recently UPDATED pages, so an entry
# reappearing with a newer updated_at is an edit, not a new publication -- see documents.py on
# why first_seen_at is stored separately. The site asks for a 2-second crawl delay.
ELECTORAL_COMMISSION = FeedSourceDefinition(
    source_key="nz_electoral_commission",
    endpoint=CivicEndpoint(
        url="https://elections.nz/media-and-news/rss",
        allowed_hosts=_ELECTIONS_HOSTS,
        accept="application/rss+xml, application/xml;q=0.9",
        content_types=_XML),
    publisher="New Zealand Electoral Commission",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=2.0,
    max_items=10)

# Beehive's guid is a Drupal node URL and its link is the canonical release/speech path. The node
# URL survives a slug rename, so it is the identity; the release path is what a reader is sent to.
BEEHIVE_RELEASES = FeedSourceDefinition(
    source_key="nz_beehive_releases",
    endpoint=CivicEndpoint(
        url="https://www.beehive.govt.nz/rss.xml",
        allowed_hosts=_BEEHIVE_HOSTS,
        accept="application/rss+xml, application/xml;q=0.9",
        content_types=_XML),
    publisher="New Zealand Government (Beehive)",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=1.0,
    guid_is_identity=True)

# Atom, so identity is <id> and the timestamp is <updated>.
ORDER_PAPER = FeedSourceDefinition(
    source_key="nz_parliament_order_paper",
    endpoint=CivicEndpoint(
        url=(
            "https://www3.parliament.nz/en/channellisting/rss/1691/"
            "92035c6e-1be3-40fb-aafa-37d4346d78cc"),
        allowed_hosts=_PARLIAMENT_HOSTS,
        accept="application/atom+xml, application/xml;q=0.9",
        content_types=_XML),
    publisher="New Zealand Parliament (Order Paper)",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=2.0,
    is_atom=True)

# Reuse, not re-implementation: these two endpoints are already declared and parsed for Channels.
_OFFICIAL_FEEDS_BY_KEY = {feed.type_key: feed for feed in FEED_DEFINITIONS}

RBNZ_NEWS = adapt_official_feed(
    _OFFICIAL_FEEDS_BY_KEY["rbnz_news"],
    source_key="nz_rbnz_news",
    tier=CivicEvidenceTier.OFFICIAL,
    allowed_hosts=_RBNZ_HOSTS,
    crawl_delay_seconds=1.0)

# There is deliberately NO second entry adapting official_feeds' `nz_government_news`: it points
# at the very same https://www.beehive.govt.nz/rss.xml that BEEHIVE_RELEASES already reads.
# Two catalog entries over one endpoint fetch the same bytes twice AND namespace their dedupe
# keys differently ("nz_beehive_releases:..." vs "nz_government_news:..."), so the same release
# would enter two Civic Candidates and defeat the Watch-wide deduplication entirely.
# BEEHIVE_RELEASES is kept because it is the richer definition -- it treats the Drupal node URL
# as the stable identity, which the adapted Channel feed does not.

# ---------------------------------------------------------------------------
# Parliament APIs
# ---------------------------------------------------------------------------


# The captured live request body, not an approximation: bills.parliament.nz returns a
# differently-scoped or differently-ordered page for a partial filter object rather than an error.
PARLIAMENT_BILLS = ParliamentApiDefinition(
    source_key="nz_parliament_bills",
    endpoint=CivicEndpoint(
        url="https://bills.parliament.nz/api/data/search",
        allowed_hosts=_PARLIAMENT_HOSTS,
        method="POST",
        accept="application/json",
        content_types=_JSON),
    publisher="New Zealand Parliament (Bills)",
    tier=CivicEvidenceTier.OFFICIAL,
    result_keys=("results",),
    total_keys=("totalResults",),
    published_keys=("publicationDate",),
    event_keys=("introductionDate", "lastStageDate"),
    updated_keys=("lastStageDate", "lastModified"),
    base_payload=BILLS_SEARCH_FILTERS,
    # The live record carries `url: null`, so the public page must be derived. `/v/6/{id}` is the
    # route the publisher's own links use. It cannot be confirmed by fetching: this is a Blazor
    # app that answers 200 with an identical shell for every path, including a well-formed but
    # nonexistent UUID -- which is how an earlier version of this file talked itself into `/v/0`.
    url_base="https://bills.parliament.nz/v/6",
    allow_id_url_fallback=True,
    crawl_delay_seconds=2.0)

PARLIAMENT_SELECT_COMMITTEES = ParliamentApiDefinition(
    source_key="nz_parliament_select_committees",
    endpoint=CivicEndpoint(
        url="https://selectcommittees.parliament.nz/api/data/search",
        allowed_hosts=_PARLIAMENT_HOSTS,
        method="POST",
        accept="application/json",
        content_types=_JSON),
    publisher="New Zealand Parliament (Select Committees)",
    tier=CivicEvidenceTier.OFFICIAL,
    result_keys=("results",),
    total_keys=("totalResults",),
    id_keys=("id",),
    title_keys=("title",),
    published_keys=("publicationDate",),
    updated_keys=("lastModified",),
    url_keys=(),
    excerpt_keys=("subtitle",),
    # The Bills body shape with this endpoint's own valid sort column; Bills' column 17 answers
    # HTTP 500 here.
    base_payload=SELECT_COMMITTEES_SEARCH_FILTERS,
    # Unlike Bills, this site's `/v/{n}/` segment is a DOCUMENT-TYPE code rather than one
    # site-wide view index, and the codes are neither ordered nor contiguous -- which is how an
    # earlier version of this entry, having found 0 and 4 and searched only 0..8, ended up
    # omitting two thirds of every fetch as "unroutable". Each code in
    # SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE is the one the publisher's own listing links use, and
    # `Bill` is not in it because the publisher links bills off-site to bills.parliament.nz.
    # A documentType in neither table still gets no URL and is reported once for the whole type,
    # because an unverified route is never fabricated.
    url_base="https://selectcommittees.parliament.nz/v",
    url_view_index_by_type=SELECT_COMMITTEE_VIEW_INDEX_BY_TYPE,
    url_external_by_type=SELECT_COMMITTEE_EXTERNAL_URL_BY_TYPE,
    # Emitting a bills.parliament.nz URL is deliberate, so it is declared. Anything outside
    # parliament.nz is a hard error rather than a link Beehive would publish.
    document_url_hosts=_PARLIAMENT_HOSTS,
    allow_id_url_fallback=False,
    crawl_delay_seconds=2.0)

# Hansard's listing is OData-shaped (`value[]` + `@odata.count`). EVERY path under
# hansard.parliament.nz answers HTTP 200 with the same client-rendered shell, so a `/<uuid>` link
# would pass every automated check and resolve to nothing. Id-based fallback is therefore
# disabled outright, and the page is derived from the sitting date -- the key used by both the
# API (`/api/data/daily/{YYYY-MM-DD}`) and the publisher's transcript route.
HANSARD_DEBATES = ParliamentApiDefinition(
    source_key="nz_parliament_hansard",
    endpoint=CivicEndpoint(
        url="https://hansard.parliament.nz/api/data/search",
        allowed_hosts=_PARLIAMENT_HOSTS,
        method="POST",
        accept="application/json",
        content_types=_JSON),
    publisher="New Zealand Parliament (Hansard)",
    tier=CivicEvidenceTier.OFFICIAL,
    result_keys=("value",),
    total_keys=("@odata.count",),
    # Field names captured from the live listing: lowercase camelCase, no URL field of any kind.
    id_keys=("id", "documentId"),
    title_keys=("title", "heading"),
    published_keys=("publicationDate",),
    event_keys=("sittingDate",),
    updated_keys=("lastModified", "modifiedDate"),
    url_keys=(),
    excerpt_keys=("subtitle", "summary"),
    base_payload=HANSARD_SEARCH_FILTERS,
    url_template=HANSARD_DAILY_URL_TEMPLATE,
    url_token_strategy="hansard_daily_date",
    allow_id_url_fallback=False,
    crawl_delay_seconds=2.0)

# The bounded, opt-in per-day detail endpoint. Never fetched by `fetch()`: a sitting day's
# transcript metadata is hundreds of KB, and the listing already carries what a Civic Run needs.
HANSARD_DAILY_DETAIL_ENDPOINT = CivicEndpoint(
    url="https://hansard.parliament.nz/api/data/daily",
    allowed_hosts=_PARLIAMENT_HOSTS,
    accept="application/json",
    content_types=_JSON,
    max_bytes=2 * 1024 * 1024)

# ---------------------------------------------------------------------------
# Legislation
# ---------------------------------------------------------------------------

# There is deliberately NO aggregate keyless XML source. `/subscribe/en/latest.xml` was once
# configured as one and never served a listing, which produced a permanently empty feed that
# looked healthy; it is now in `TOMBSTONE_PATHS` so it cannot be reintroduced. The keyless
# surface is sitemap discovery plus optional per-document hydration.
#
# This sitemap is ~5.2 MiB and ~43,000 URLs, so it gets SITEMAP_MAX_BYTES rather than the default
# cap, and its bounded output reports the shortfall instead of silently truncating.
LEGISLATION_SITEMAP = SitemapSourceDefinition(
    source_key="nz_legislation_sitemap",
    endpoint=CivicEndpoint(
        url="https://www.legislation.govt.nz/sitemap.xml",
        allowed_hosts=LEGISLATION_HOSTS,
        accept="application/xml",
        content_types=_XML,
        max_bytes=SITEMAP_MAX_BYTES),
    publisher="New Zealand Legislation",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=2.0)

# Enrichment for documents the sitemap already discovered, never a discovery source of its own.
LEGISLATION_HYDRATION = LegislationHydrationDefinition(
    source_key="nz_legislation_document",
    publisher="New Zealand Legislation",
    tier=CivicEvidenceTier.OFFICIAL)

# Optional. `enabled_sources` omits this entirely when no key is configured, so Civic runs on the
# keyless paths above rather than failing.
LEGISLATION_WORKS_API = LegislationApiDefinition(
    source_key="nz_legislation_works",
    endpoint=CivicEndpoint(
        url="https://api.legislation.govt.nz/v0/works",
        allowed_hosts=LEGISLATION_API_HOSTS,
        accept="application/json",
        content_types=_JSON),
    publisher="New Zealand Legislation",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=1.0)

# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------

OAG_SITEMAP = SitemapSourceDefinition(
    source_key="nz_oag_reports",
    endpoint=CivicEndpoint(
        url="https://ao.parliament.nz/sitemap.xml.gz",
        allowed_hosts=_OAG_HOSTS,
        accept="application/gzip, application/xml;q=0.9",
        content_types=_GZIP,
        max_bytes=SITEMAP_MAX_BYTES),
    publisher="Office of the Auditor-General",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=2.0,
    is_gzipped=True)

OMBUDSMAN_SITEMAP = SitemapSourceDefinition(
    source_key="nz_ombudsman",
    endpoint=CivicEndpoint(
        url="https://www.ombudsman.parliament.nz/sitemap.xml",
        allowed_hosts=_OMBUDSMAN_HOSTS,
        accept="application/xml",
        content_types=_XML,
        max_bytes=SITEMAP_MAX_BYTES),
    publisher="Office of the Ombudsman",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=2.0)

# Stats NZ publishes Crawl-delay: 10, and a sitemap index means N+1 sequential requests to the
# same host -- which is exactly why the pacer holds the gap rather than each connector.
STATS_NZ_SITEMAP = SitemapSourceDefinition(
    source_key="nz_stats",
    endpoint=CivicEndpoint(
        url="https://www.stats.govt.nz/sitemap.xml",
        allowed_hosts=_STATS_HOSTS,
        accept="application/xml",
        content_types=_XML,
        max_bytes=SITEMAP_MAX_BYTES),
    publisher="Stats NZ",
    tier=CivicEvidenceTier.OFFICIAL,
    crawl_delay_seconds=10.0,
    max_children=5)

# ---------------------------------------------------------------------------
# Open data
# ---------------------------------------------------------------------------

DATA_GOVT_CKAN = CkanSourceDefinition(
    source_key="nz_data_govt",
    endpoint=CivicEndpoint(
        url="https://catalogue.data.govt.nz/api/3/action/package_search",
        allowed_hosts=_DATA_GOVT_HOSTS,
        accept="application/json",
        content_types=_JSON),
    publisher="data.govt.nz",
    tier=CivicEvidenceTier.OFFICIAL,
    dataset_url_base="https://catalogue.data.govt.nz/dataset",
    crawl_delay_seconds=1.0)


@dataclass(frozen=True)
class CivicSourceEntry:
    """One catalog entry: the definition, the connector class that reads it, and whether it needs
    an optional credential. `requires_api_key_env` is the only credential concept in Civic, and
    it is always optional."""

    source_key: str
    definition: Any
    connector_class: type
    requires_api_key_env: str | None = None

    @property
    def tier(self) -> CivicEvidenceTier:
        return self.definition.tier

    @property
    def publisher(self) -> str:
        return self.definition.publisher

    @property
    def url(self) -> str:
        return self.definition.endpoint.url

    @property
    def crawl_delay_seconds(self) -> float:
        return getattr(self.definition, "crawl_delay_seconds", 0.0)

    def build(self, transport: CivicTransport | None = None, **kwargs):
        return self.connector_class(self.definition, transport, **kwargs)


CIVIC_SOURCES: tuple[CivicSourceEntry, ...] = (
    CivicSourceEntry("nz_electoral_commission", ELECTORAL_COMMISSION, CivicFeedConnector),
    CivicSourceEntry("nz_beehive_releases", BEEHIVE_RELEASES, CivicFeedConnector),
    CivicSourceEntry("nz_parliament_order_paper", ORDER_PAPER, CivicFeedConnector),
    CivicSourceEntry("nz_rbnz_news", RBNZ_NEWS, CivicFeedConnector),
    CivicSourceEntry("nz_parliament_bills", PARLIAMENT_BILLS, ParliamentApiConnector),
    CivicSourceEntry(
        "nz_parliament_select_committees", PARLIAMENT_SELECT_COMMITTEES, ParliamentApiConnector),
    CivicSourceEntry("nz_parliament_hansard", HANSARD_DEBATES, ParliamentApiConnector),
    CivicSourceEntry("nz_legislation_sitemap", LEGISLATION_SITEMAP, SitemapConnector),
    CivicSourceEntry(
        "nz_legislation_works", LEGISLATION_WORKS_API, LegislationApiConnector,
        requires_api_key_env=LEGISLATION_API_KEY_ENV),
    CivicSourceEntry("nz_oag_reports", OAG_SITEMAP, SitemapConnector),
    CivicSourceEntry("nz_ombudsman", OMBUDSMAN_SITEMAP, SitemapConnector),
    CivicSourceEntry("nz_stats", STATS_NZ_SITEMAP, SitemapConnector),
    CivicSourceEntry("nz_data_govt", DATA_GOVT_CKAN, CkanConnector),
)

_BY_KEY = {entry.source_key: entry for entry in CIVIC_SOURCES}


def get_source(source_key: str) -> CivicSourceEntry:
    try:
        return _BY_KEY[source_key]
    except KeyError:
        raise ValueError(f"unknown Civic source: {source_key!r}") from None


def all_sources() -> tuple[CivicSourceEntry, ...]:
    return CIVIC_SOURCES


def enabled_sources(environ: dict[str, str] | None = None) -> tuple[CivicSourceEntry, ...]:
    """Every source that can actually run in this environment.

    A source whose optional API key is absent is omitted rather than being offered and failing.
    That is what makes the Legislation Works key genuinely optional: Civic keeps working on the
    keyless `latest.xml` and sitemap paths, with narrower coverage and no error."""
    env = environ if environ is not None else os.environ
    return tuple(
        entry for entry in CIVIC_SOURCES
        if entry.requires_api_key_env is None
        or (env.get(entry.requires_api_key_env) or "").strip())


def source_keys() -> tuple[str, ...]:
    return tuple(entry.source_key for entry in CIVIC_SOURCES)


def legislation_hydrator(transport: CivicTransport | None = None) -> LegislationHydrator:
    """The bounded per-document legislation hydrator.

    Deliberately NOT a `CivicSourceEntry`: it discovers nothing on its own and must be handed
    URLs that `nz_legislation_sitemap` already found, so listing it beside the listing sources
    would invite it being scheduled like one."""
    return LegislationHydrator(LEGISLATION_HYDRATION, transport)


def hansard_daily_detail_endpoint() -> CivicEndpoint:
    """The opt-in Hansard per-day detail endpoint. Not part of any listing fetch; a caller passes
    it to `ParliamentApiConnector.fetch_daily_detail` for one specific sitting date."""
    return HANSARD_DAILY_DETAIL_ENDPOINT


__all__ = [
    "CIVIC_SOURCES",
    "HANSARD_DAILY_DETAIL_ENDPOINT",
    "HANSARD_DAILY_URL_TEMPLATE",
    "LEGISLATION_HYDRATION",
    "CivicSourceEntry",
    "all_sources",
    "enabled_sources",
    "get_source",
    "hansard_daily_detail_endpoint",
    "legislation_hydrator",
    "source_keys",
]
