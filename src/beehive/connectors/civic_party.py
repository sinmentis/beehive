"""The 17 registered New Zealand parties PartyMap follows, as Civic catalog entries.

The slugs and home pages are the PartyMap party set, transcribed from the site's own
`src/data/base-parties.ts`. That file stays the single source of truth for who the parties are;
this module adds the one thing it does not carry -- how, and whether, each party's own
publications can be read.

Every party is one `PartyMediaSourceEntry`: one fixed endpoint, one exact host allow-list, one
connector class, and the ownership and licence facts corroboration depends on. There is no
per-Source URL, no endpoint discovery at run time, and no fallback, so a party site that changes
shape fails visibly against the strategy recorded here instead of quietly matching something else
on the same domain.

Six parties publish a plain feed and are read by the canonical `CivicFeedConnector`. The rest
publish only a sitemap, an embedded publication list, or a listing page:

* National, ACT, Vision -- whole-site sitemap through the canonical `SitemapConnector`, filtered
  to `/news/`. ACT's lists ~110 static pages (home, forms, people, policies) before its first
  release and ~3,100 releases after them, which is why the filter is not optional: unfiltered,
  the first page of a fetch was navigation and no publications at all.
* Greens -- `sitemap_news.xml`, which carries real headlines and publication dates.
* Labour -- the `/news` page's embedded publication list.
* Conservative -- the Wix sitemap index; its news child is named around an opaque id, so the
  canonical index walk plus a `/news/` path filter is what reaches it stably.
* NZ Outdoors & Freedom -- the `/media` listing page, at the 10 s crawl delay its robots.txt asks
  for.

Four are catalogued but not fetchable, and stay that way until the party itself changes: Te Tai
Tokerau and NZ Loyal declare sitemaps in robots.txt but serve 403 to identified non-browser
clients; Free Palestine disallows everything in robots.txt; the Aotearoa Legalise Cannabis Party
publishes no dated publication surface. Each raises `CivicSourceUnavailableError` with its reason
rather than being silently skipped or replaced with scraping, so a Civic Watch's coverage gap is
visible and attributable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from beehive.civic.connectors.feeds import CivicFeedConnector, FeedSourceDefinition
from beehive.civic.connectors.sitemaps import SitemapConnector, SitemapSourceDefinition
from beehive.civic.documents import CivicFetchResult, CivicSourceHealth
from beehive.civic.fetching import CivicEndpoint, CivicTransport
from beehive.connectors.base import RawItem
from beehive.connectors.civic_catalog import (
    HTML_CONTENT_TYPES,
    MAX_DOCUMENTS_PER_FETCH,
    PARTY_MEDIA_MAX_BYTES,
    PartyMediaCatalog,
    PartyMediaSourceEntry,
    RefusingConnector,
    SourceAvailability,
    UnavailableSourceDefinition,
    UsePolicy,
)
from beehive.connectors.civic_strategies import (
    EmbeddedListConnector,
    EmbeddedListDefinition,
    LinkIndexConnector,
    LinkIndexDefinition,
    NewsSitemapConnector,
    NewsSitemapDefinition,
    fetch_entry,
)
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind
from beehive.domain.civic import CivicEvidenceTier

_TIER = CivicEvidenceTier.PARTY_PRIMARY
_NEWS_PATHS = ("/news/",)


def _endpoint(url: str, host: str, *, html: bool = False) -> CivicEndpoint:
    """One party endpoint, pinned to its own host. XML sources keep `CivicEndpoint`'s own
    content-type default so the two halves of the catalog agree on what XML is."""
    options = {"url": url, "allowed_hosts": frozenset({host}), "max_bytes": PARTY_MEDIA_MAX_BYTES}
    if html:
        options["content_types"] = HTML_CONTENT_TYPES
    return CivicEndpoint(**options)


def _party(
    *,
    slug: str,
    publisher: str,
    host: str,
    definition=None,
    connector_class=None,
    availability: SourceAvailability = SourceAvailability.ENABLED,
    title_suffixes: tuple[str, ...] = (),
    note: str = "",
) -> PartyMediaSourceEntry:
    """One party entry. `owner_id` is the party itself: a party never corroborates another
    party, and two releases from one party are one voice, not two."""
    if definition is None:
        definition = UnavailableSourceDefinition(
            source_key=slug, publisher=publisher, tier=_TIER, reason=note)
        connector_class = RefusingConnector
    return PartyMediaSourceEntry(
        source_key=slug,
        definition=definition,
        connector_class=connector_class,
        owner_id=f"party:{slug}",
        use_policy=UsePolicy.EVIDENCE,
        availability=availability,
        hosts=frozenset({host}),
        title_suffixes=title_suffixes,
        note=note,
    )


PARTY_SOURCES: tuple[PartyMediaSourceEntry, ...] = (
    _party(
        slug="national",
        publisher="New Zealand National Party",
        host="www.national.org.nz",
        definition=SitemapSourceDefinition(
            source_key="national",
            endpoint=_endpoint("https://www.national.org.nz/sitemap.xml", "www.national.org.nz"),
            publisher="New Zealand National Party",
            tier=_TIER,
            path_includes=_NEWS_PATHS,
            max_urls=MAX_DOCUMENTS_PER_FETCH),
        connector_class=SitemapConnector,
        title_suffixes=("National Party",),
    ),
    _party(
        slug="labour",
        publisher="New Zealand Labour Party",
        host="www.labour.org.nz",
        definition=EmbeddedListDefinition(
            source_key="labour",
            endpoint=_endpoint("https://www.labour.org.nz/news", "www.labour.org.nz", html=True),
            publisher="New Zealand Labour Party",
            tier=_TIER,
            path_includes=_NEWS_PATHS),
        connector_class=EmbeddedListConnector,
        title_suffixes=("Labour Party",),
    ),
    _party(
        slug="green",
        publisher="Green Party of Aotearoa New Zealand",
        host="www.greens.org.nz",
        definition=NewsSitemapDefinition(
            source_key="green",
            endpoint=_endpoint(
                "https://www.greens.org.nz/sitemap_news.xml", "www.greens.org.nz"),
            publisher="Green Party of Aotearoa New Zealand",
            tier=_TIER),
        connector_class=NewsSitemapConnector,
        title_suffixes=("Green Party",),
        note="Article HTML refuses identified clients (403); the news sitemap is the only "
             "surface, and it carries real headlines and publication dates.",
    ),
    _party(
        slug="act",
        publisher="ACT New Zealand",
        host="www.act.org.nz",
        definition=SitemapSourceDefinition(
            source_key="act",
            endpoint=_endpoint("https://www.act.org.nz/sitemap_en-NZ.xml", "www.act.org.nz"),
            publisher="ACT New Zealand",
            tier=_TIER,
            path_includes=_NEWS_PATHS,
            max_urls=MAX_DOCUMENTS_PER_FETCH),
        connector_class=SitemapConnector,
        title_suffixes=("ACT Party", "ACT"),
        note="Whole-site locale sitemap, newest releases first. It carries no <lastmod>, so its "
             "documents are undated by construction and can never be matched on time.",
    ),
    _party(
        slug="nz-first",
        publisher="New Zealand First",
        host="www.nzfirst.nz",
        definition=FeedSourceDefinition(
            source_key="nz-first",
            endpoint=_endpoint("https://www.nzfirst.nz/news.rss", "www.nzfirst.nz"),
            publisher="New Zealand First",
            tier=_TIER,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=CivicFeedConnector,
        title_suffixes=("NZ First",),
    ),
    _party(
        slug="te-pati-maori",
        publisher="Te Pāti Māori",
        host="www.maoriparty.org.nz",
        definition=FeedSourceDefinition(
            source_key="te-pati-maori",
            endpoint=_endpoint(
                "https://www.maoriparty.org.nz/panui.rss", "www.maoriparty.org.nz"),
            publisher="Te Pāti Māori",
            tier=_TIER,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=CivicFeedConnector,
        title_suffixes=("Maori Party",),
    ),
    _party(
        slug="opportunity",
        publisher="The Opportunities Party",
        host="www.opportunity.org.nz",
        definition=FeedSourceDefinition(
            source_key="opportunity",
            endpoint=_endpoint(
                "https://www.opportunity.org.nz/news.rss", "www.opportunity.org.nz"),
            publisher="The Opportunities Party",
            tier=_TIER,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=CivicFeedConnector,
        title_suffixes=("TOP",),
    ),
    _party(
        slug="alliance",
        publisher="Alliance Party of Aotearoa New Zealand",
        host="allianceparty.nz",
        definition=FeedSourceDefinition(
            source_key="alliance",
            endpoint=_endpoint("https://allianceparty.nz/feed/", "allianceparty.nz"),
            publisher="Alliance Party of Aotearoa New Zealand",
            tier=_TIER,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=CivicFeedConnector,
        title_suffixes=("Alliance Party",),
        note="WordPress; the REST API exposes the same posts and is not used, one surface per "
             "source.",
    ),
    _party(
        slug="te-tai-tokerau",
        publisher="Te Tai Tokerau Party",
        host="tetaitokerauparty.org.nz",
        availability=SourceAvailability.BLOCKED,
        note="robots.txt declares a sitemap index, but every request returns HTTP 403.",
    ),
    _party(
        slug="free-palestine",
        publisher="Free Palestine",
        host="palfree.nz",
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="robots.txt disallows crawling and no feed, sitemap, or dated listing is published.",
    ),
    _party(
        slug="nz-loyal",
        publisher="New Zealand Loyal",
        host="nzloyal.com",
        availability=SourceAvailability.BLOCKED,
        note="robots.txt declares a sitemap index, but every request returns HTTP 403.",
    ),
    _party(
        slug="outdoors-freedom",
        publisher="NZ Outdoors & Freedom Party",
        host="nzofp.co.nz",
        definition=LinkIndexDefinition(
            source_key="outdoors-freedom",
            endpoint=_endpoint("https://nzofp.co.nz/media", "nzofp.co.nz", html=True),
            publisher="NZ Outdoors & Freedom Party",
            tier=_TIER,
            crawl_delay_seconds=10.0,
            link_pattern="media-release"),
        connector_class=LinkIndexConnector,
        title_suffixes=("Outdoors & Freedom Party",),
        note="No feed or sitemap on the party host; the /media listing carries no dates, and "
             "robots.txt asks for a 10 s crawl delay.",
    ),
    _party(
        slug="conservative",
        publisher="Conservative Party NZ",
        host="www.conservatives.nz",
        definition=SitemapSourceDefinition(
            source_key="conservative",
            endpoint=_endpoint(
                "https://www.conservatives.nz/sitemap.xml", "www.conservatives.nz"),
            publisher="Conservative Party NZ",
            tier=_TIER,
            crawl_delay_seconds=10.0,
            path_includes=_NEWS_PATHS,
            max_children=5,
            max_urls=MAX_DOCUMENTS_PER_FETCH),
        connector_class=SitemapConnector,
        title_suffixes=("Conservative Party",),
        note="Wix rebuilds the news sitemap's filename around an opaque id, so the index is "
             "walked and the /news/ path filter is what selects releases.",
    ),
    _party(
        slug="vision-nz",
        publisher="Vision New Zealand",
        host="www.vision.org.nz",
        definition=SitemapSourceDefinition(
            source_key="vision-nz",
            endpoint=_endpoint("https://www.vision.org.nz/sitemap.xml", "www.vision.org.nz"),
            publisher="Vision New Zealand",
            tier=_TIER,
            path_includes=_NEWS_PATHS,
            max_urls=MAX_DOCUMENTS_PER_FETCH),
        connector_class=SitemapConnector,
        note="Sitemap carries no <lastmod>, so its documents are undated by construction.",
    ),
    _party(
        slug="womens-rights",
        publisher="Women's Rights Party",
        host="womensrightsparty.nz",
        definition=FeedSourceDefinition(
            source_key="womens-rights",
            endpoint=_endpoint("https://womensrightsparty.nz/feed/", "womensrightsparty.nz"),
            publisher="Women's Rights Party",
            tier=_TIER,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=CivicFeedConnector,
    ),
    _party(
        slug="animal-justice",
        publisher="Animal Justice Party Aotearoa New Zealand",
        host="animaljustice.org.nz",
        definition=FeedSourceDefinition(
            source_key="animal-justice",
            endpoint=_endpoint("https://animaljustice.org.nz/feed/", "animaljustice.org.nz"),
            publisher="Animal Justice Party Aotearoa New Zealand",
            tier=_TIER,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=CivicFeedConnector,
        title_suffixes=("Animal Justice Party",),
        note="WordPress feed; a bot filter answers 403 to some clients and the feed itself can "
             "carry no items for long stretches. The first is a fetch error, the second is an "
             "EMPTY result -- they are never conflated.",
    ),
    _party(
        slug="cannabis",
        publisher="Aotearoa Legalise Cannabis Party",
        host="www.alcp.org.nz",
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="The sitemap lists only static pages; no dated publication surface exists.",
    ),
)

PARTY_CATALOG = PartyMediaCatalog(PARTY_SOURCES, name="party")

# The PartyMap party set is fixed at 17. Asserted at import so a party added or dropped here
# without updating the site's own party data (or vice versa) fails immediately.
EXPECTED_PARTY_COUNT = 17
if len(PARTY_CATALOG) != EXPECTED_PARTY_COUNT:
    raise RuntimeError(
        f"party catalog holds {len(PARTY_CATALOG)} parties, expected {EXPECTED_PARTY_COUNT}")


def all_sources() -> tuple[PartyMediaSourceEntry, ...]:
    """Every party entry, including the ones that cannot currently be read. Matches
    `beehive.civic.catalog.all_sources`, so an orchestrator concatenates the two."""
    return PARTY_CATALOG.all_sources()


def enabled_sources() -> tuple[PartyMediaSourceEntry, ...]:
    return PARTY_CATALOG.enabled_sources()


def get_source(source_key: str) -> PartyMediaSourceEntry:
    return PARTY_CATALOG.get(source_key)


def source_keys() -> tuple[str, ...]:
    return PARTY_CATALOG.keys


def fetch_party(
    slug: str,
    now: datetime,
    *,
    transport: CivicTransport | None = None,
    limit: int = MAX_DOCUMENTS_PER_FETCH,
) -> CivicFetchResult:
    """One party's own releases, as a canonical Civic fetch result."""
    return fetch_entry(PARTY_CATALOG.get(slug), now, transport=transport, limit=limit)


class CivicPartyConnector:
    """The Channel-facing adapter for a party's releases.

    Civic itself uses `fetch_party` and reads `CivicDocument`s; this exists so the same catalogued
    source can also feed an Editorial Channel without a second implementation. The Source config
    names a party slug and nothing else -- no URL, host, or credential field -- so a stored Source
    can never redirect this connector at another address.
    """

    type_key = "civic_party_release"
    supported_channel_kinds = frozenset({ChannelKind.EDITORIAL})

    def __init__(
        self,
        catalog: PartyMediaCatalog = PARTY_CATALOG,
        transport: CivicTransport | None = None,
    ):
        self._catalog = catalog
        self._transport = transport

    def validate_config(self, config: dict) -> None:
        if set(config) != {"party"}:
            raise ValueError("civic_party_release config must be exactly {'party': <slug>}")
        slug = config["party"]
        if not isinstance(slug, str):
            raise ValueError("civic_party_release 'party' must be a string slug")
        self._catalog.get(slug)

    def entry_for(self, config: dict) -> PartyMediaSourceEntry:
        self.validate_config(config)
        return self._catalog.get(config["party"])

    def fetch_result(
        self, config: dict, *, limit: int = MAX_DOCUMENTS_PER_FETCH, now: datetime | None = None
    ) -> CivicFetchResult:
        entry = self.entry_for(config)
        return fetch_entry(
            entry,
            now or datetime.now(timezone.utc),
            transport=self._transport,
            limit=limit)

    def fetch(self, config: dict) -> list[RawItem]:
        return civic_documents_to_raw_items(
            self.fetch_result(config), self._catalog)

    def fetch_preview(self, config: dict, *, limit: int) -> list[RawItem]:
        if limit < 1:
            raise ValueError("preview limit must be positive")
        return civic_documents_to_raw_items(
            self.fetch_result(config, limit=limit), self._catalog)


def civic_documents_to_raw_items(
    result: CivicFetchResult, catalog: PartyMediaCatalog
) -> list[RawItem]:
    """Channel items from a Civic fetch result, carrying the catalog's own classification.

    A TOMBSTONE result raises: an endpoint that answers 200 with a retirement page is not a quiet
    week, and a Channel that silently showed nothing would hide it. EMPTY and DEGRADED both
    return what was collected, because an Editorial Channel is a stream rather than a complete
    snapshot -- the reasons stay on the `CivicFetchResult` for Civic's own health reporting.
    """
    if result.health is CivicSourceHealth.TOMBSTONE:
        raise RuntimeError(
            f"{result.source_key}: source is a tombstone, not an empty week")
    items: list[RawItem] = []
    for document in result.documents:
        entry = catalog.get(document.source_key)
        metadata = {
            "civic_source": entry.source_key,
            "civic_owner": entry.owner_id,
            "civic_tier": document.tier.value,
            "civic_use_policy": entry.use_policy.value,
            "civic_may_be_evidence": entry.may_be_evidence,
            "civic_dedupe_key": document.dedupe_key,
            "civic_health": result.health.value,
            "publisher": document.publisher,
        }
        items.append(RawItem(
            external_id=document.external_key,
            title=document.title,
            url=document.canonical_url,
            body=document.excerpt,
            created_at=document.published_at or document.updated_at,
            raw_metadata=metadata,
        ))
    return items


register(CivicPartyConnector())
