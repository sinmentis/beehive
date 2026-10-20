"""The New Zealand outlets PartyMap may read, who owns them, and what their terms allow.

Same shape as the party half and as `beehive.civic.catalog`: every outlet is a
`PartyMediaSourceEntry` whose connector returns a `CivicFetchResult`. Two decisions are specific
to media, and conflating them is what this module exists to prevent.

**Ownership decides corroboration.** `owner_id` groups mastheads that share an owner, so two
reports from one company can never count as two independent sources for a Civic Candidate. RNZ,
1News and Te Ao are all crown-funded public media; NZME publishes the Herald and Newstalk ZB;
Stuff and The Post are one company; Sky owns ThreeNews; Allied Press owns the Otago Daily Times.
The Spinoff, Newsroom, interest.co.nz and Waatea each stand alone and carry their own owner id.

**Terms decide use.** Two owners' terms rule out this use entirely, and both fail closed here
rather than at the point of publication:

* NZME (nzherald.co.nz, newstalkzb.co.nz) expressly forbids automated and AI use of its content.
* RNZ publishes its feeds for personal, non-commercial use only.

Both are catalogued with `UsePolicy.RESTRICTED`, no endpoint, and `RestrictedConnector`, so
nothing fetches them, no body of theirs reaches an AI call, and the discovery matcher refuses to
resolve a headline to them. They stay catalogued -- rather than deleted -- precisely so a matcher
or a future contributor gets an explicit "restricted by licence" instead of the much weaker
"unknown publisher". Lifting either restriction means committing an actual licence here.

Aggregators, wires and social platforms are catalogued as discovery-only. They may suggest that
something happened; they can never be the evidence that it did, because what they carry is
someone else's reporting and citing them would double-count the original.

The evidence tier is the application's own classification and is never read from a document:
`REPUTABLE_MEDIA` is a general-news desk with editorial standards and corrections, `OTHER_MEDIA`
is a commentary-led or narrowly scoped outlet, and `AGGREGATOR` covers everything that
redistributes rather than reports.
"""
from __future__ import annotations

from datetime import datetime, timezone

from beehive.civic.connectors.feeds import CivicFeedConnector, FeedSourceDefinition
from beehive.civic.documents import CivicFetchResult
from beehive.civic.fetching import CivicEndpoint, CivicTransport
from beehive.connectors.base import RawItem
from beehive.connectors.civic_catalog import (
    MAX_DOCUMENTS_PER_FETCH,
    PARTY_MEDIA_MAX_BYTES,
    PartyMediaCatalog,
    PartyMediaSourceEntry,
    RefusingConnector,
    RestrictedConnector,
    SourceAvailability,
    UnavailableSourceDefinition,
    UsePolicy,
)
from beehive.connectors.civic_party import civic_documents_to_raw_items
from beehive.connectors.civic_strategies import (
    NewsSitemapConnector,
    NewsSitemapDefinition,
    WallClockFeedConnector,
    WallClockFeedDefinition,
    fetch_entry,
)
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind
from beehive.domain.civic import CivicEvidenceTier

# Ownership groups. One constant per owner so a new masthead joins an existing group by reusing
# the constant instead of inventing a near-duplicate string.
OWNER_CROWN_PUBLIC = "crown-public"
OWNER_NZME = "nzme"
OWNER_STUFF_GROUP = "stuff-group"
OWNER_SKY = "sky"
OWNER_ALLIED_PRESS = "allied-press"
OWNER_SPINOFF = "spinoff"
OWNER_NEWSROOM = "newsroom"
OWNER_INTEREST = "interest"
OWNER_WAATEA = "waatea"
OWNER_SCOOP = "scoop"
OWNER_GOOGLE = "google"
OWNER_MICROSOFT = "microsoft"
OWNER_YAHOO = "yahoo"
OWNER_META = "meta"
OWNER_X = "x"
OWNER_REDDIT = "reddit"

_REPUTABLE = CivicEvidenceTier.REPUTABLE_MEDIA
_OTHER = CivicEvidenceTier.OTHER_MEDIA
_AGGREGATOR = CivicEvidenceTier.AGGREGATOR


def _feed(
    key: str, url: str, host: str, publisher: str, tier: CivicEvidenceTier, *, is_atom: bool
) -> FeedSourceDefinition:
    return FeedSourceDefinition(
        source_key=key,
        endpoint=CivicEndpoint(
            url=url, allowed_hosts=frozenset({host}), max_bytes=PARTY_MEDIA_MAX_BYTES),
        publisher=publisher,
        tier=tier,
        is_atom=is_atom,
        # A masthead's guid is often a CMS id rather than a permalink; the link is the URL a
        # reader is sent to, and identity follows it so evidence and citation agree.
        guid_is_identity=False,
        max_items=MAX_DOCUMENTS_PER_FETCH)


def _outlet(
    *,
    key: str,
    publisher: str,
    owner_id: str,
    hosts: tuple[str, ...],
    tier: CivicEvidenceTier = _REPUTABLE,
    use_policy: UsePolicy = UsePolicy.EVIDENCE,
    availability: SourceAvailability = SourceAvailability.ENABLED,
    definition=None,
    connector_class=None,
    title_suffixes: tuple[str, ...] = (),
    note: str = "",
    article_capture: bool = True,
) -> PartyMediaSourceEntry:
    if definition is None:
        definition = UnavailableSourceDefinition(
            source_key=key, publisher=publisher, tier=tier, reason=note)
        connector_class = (
            RestrictedConnector if use_policy is UsePolicy.RESTRICTED else RefusingConnector)
    return PartyMediaSourceEntry(
        source_key=key,
        definition=definition,
        connector_class=connector_class,
        owner_id=owner_id,
        use_policy=use_policy,
        availability=availability,
        hosts=frozenset(hosts),
        title_suffixes=title_suffixes,
        note=note,
        article_capture=article_capture,
    )


MEDIA_SOURCES: tuple[PartyMediaSourceEntry, ...] = (
    _outlet(
        key="rnz",
        publisher="RNZ",
        owner_id=OWNER_CROWN_PUBLIC,
        hosts=("www.rnz.co.nz", "rnz.co.nz"),
        use_policy=UsePolicy.RESTRICTED,
        availability=SourceAvailability.LICENCE_WITHHELD,
        title_suffixes=("RNZ News",),
        note="RNZ publishes its feeds for personal, non-commercial use only; no endpoint is "
             "catalogued and no body may reach an AI call without an explicit licence.",
    ),
    _outlet(
        key="1news",
        publisher="1News",
        owner_id=OWNER_CROWN_PUBLIC,
        hosts=("www.1news.co.nz",),
        definition=_feed(
            "1news", "https://www.1news.co.nz/arc/outboundfeeds/rss/?outputType=xml",
            "www.1news.co.nz", "1News", _REPUTABLE, is_atom=False),
        connector_class=CivicFeedConnector,
        title_suffixes=("1 News", "TVNZ"),
    ),
    _outlet(
        key="te-ao",
        publisher="Te Ao Māori News",
        owner_id=OWNER_CROWN_PUBLIC,
        hosts=("www.teaonews.co.nz",),
        definition=_feed(
            "te-ao", "https://www.teaonews.co.nz/arc/outboundfeeds/rss/?outputType=xml",
            "www.teaonews.co.nz", "Te Ao Māori News", _REPUTABLE, is_atom=False),
        connector_class=CivicFeedConnector,
        title_suffixes=("Te Ao Maori News",),
        note="teaomaori.news redirects here; only the destination host is allow-listed, so a "
             "future redirect elsewhere fails instead of following.",
    ),
    _outlet(
        key="nz-herald",
        publisher="The New Zealand Herald",
        owner_id=OWNER_NZME,
        hosts=("www.nzherald.co.nz", "nzherald.co.nz"),
        use_policy=UsePolicy.RESTRICTED,
        availability=SourceAvailability.LICENCE_WITHHELD,
        title_suffixes=("NZ Herald", "New Zealand Herald"),
        note="NZME's terms expressly forbid automated and AI use of its content; nothing here "
             "may be fetched, summarised, or cited.",
    ),
    _outlet(
        key="newstalk-zb",
        publisher="Newstalk ZB",
        owner_id=OWNER_NZME,
        hosts=("www.newstalkzb.co.nz", "newstalkzb.co.nz"),
        tier=_OTHER,
        use_policy=UsePolicy.RESTRICTED,
        availability=SourceAvailability.LICENCE_WITHHELD,
        note="Covered by the same NZME terms as the Herald.",
    ),
    _outlet(
        key="stuff",
        publisher="Stuff",
        owner_id=OWNER_STUFF_GROUP,
        hosts=("www.stuff.co.nz",),
        definition=_feed(
            "stuff", "https://www.stuff.co.nz/rss", "www.stuff.co.nz", "Stuff", _REPUTABLE,
            is_atom=True),
        connector_class=CivicFeedConnector,
        article_capture=False,
        note="Article pages are client-rendered app shells (checked 2026-09-29): no article "
             "text in the HTML, so its feed items can suggest stories but never be captured.",
    ),
    _outlet(
        key="the-post",
        publisher="The Post",
        owner_id=OWNER_STUFF_GROUP,
        hosts=("www.thepost.co.nz",),
        definition=_feed(
            "the-post", "https://www.thepost.co.nz/rss", "www.thepost.co.nz", "The Post",
            _REPUTABLE, is_atom=True),
        connector_class=CivicFeedConnector,
        article_capture=False,
        note="Same owner as Stuff, so the two never corroborate each other. Article pages are "
             "client-rendered app shells (checked 2026-09-29), so nothing can be captured.",
    ),
    _outlet(
        key="three-news",
        publisher="ThreeNews",
        owner_id=OWNER_SKY,
        hosts=("www.threenews.co.nz",),
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        title_suffixes=("Three News", "Newshub"),
        note="Sky-owned; the site redirects to a video app shell and publishes no feed or news "
             "sitemap, so it is catalogued for ownership and matching only.",
    ),
    _outlet(
        key="odt",
        publisher="Otago Daily Times",
        owner_id=OWNER_ALLIED_PRESS,
        hosts=("www.odt.co.nz",),
        definition=NewsSitemapDefinition(
            source_key="odt",
            endpoint=CivicEndpoint(
                url="https://www.odt.co.nz/sitemaps/odt/sitemap_news1.xml",
                allowed_hosts=frozenset({"www.odt.co.nz"}),
                max_bytes=PARTY_MEDIA_MAX_BYTES),
            publisher="Otago Daily Times",
            tier=_REPUTABLE,
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=NewsSitemapConnector,
        title_suffixes=("ODT",),
    ),
    _outlet(
        key="spinoff",
        publisher="The Spinoff",
        owner_id=OWNER_SPINOFF,
        hosts=("thespinoff.co.nz",),
        tier=_OTHER,
        definition=_feed(
            "spinoff", "https://thespinoff.co.nz/feed", "thespinoff.co.nz", "The Spinoff",
            _OTHER, is_atom=True),
        connector_class=CivicFeedConnector,
        note="Commentary-led, editorially independent.",
    ),
    _outlet(
        key="newsroom",
        publisher="Newsroom",
        owner_id=OWNER_NEWSROOM,
        hosts=("newsroom.co.nz",),
        definition=_feed(
            "newsroom", "https://newsroom.co.nz/feed/", "newsroom.co.nz", "Newsroom", _REPUTABLE,
            is_atom=False),
        connector_class=CivicFeedConnector,
    ),
    _outlet(
        key="interest",
        publisher="interest.co.nz",
        owner_id=OWNER_INTEREST,
        hosts=("www.interest.co.nz",),
        definition=WallClockFeedDefinition(
            source_key="interest",
            endpoint=CivicEndpoint(
                url="https://www.interest.co.nz/rss",
                allowed_hosts=frozenset({"www.interest.co.nz"}),
                max_bytes=PARTY_MEDIA_MAX_BYTES),
            publisher="interest.co.nz",
            tier=_REPUTABLE,
            publication_timezone="Pacific/Auckland",
            max_items=MAX_DOCUMENTS_PER_FETCH),
        connector_class=WallClockFeedConnector,
        note="Publishes wall-clock New Zealand times ('12th Aug 26, 5:01pm') and a site-relative "
             "guid, so identity follows the article link and the time is read in its own zone.",
    ),
    _outlet(
        key="waatea",
        publisher="Waatea News",
        owner_id=OWNER_WAATEA,
        hosts=("waateanews.com",),
        tier=_OTHER,
        definition=_feed(
            "waatea", "https://waateanews.com/feed/", "waateanews.com", "Waatea News", _OTHER,
            is_atom=False),
        connector_class=CivicFeedConnector,
        title_suffixes=("Waatea",),
    ),
    _outlet(
        key="scoop",
        publisher="Scoop",
        owner_id=OWNER_SCOOP,
        hosts=("www.scoop.co.nz", "img.scoop.co.nz"),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        title_suffixes=("Scoop News",),
        note="Republishes releases verbatim; a release is evidence at its own party's site, not "
             "here, so no endpoint is catalogued.",
    ),
    _outlet(
        key="google-news",
        publisher="Google News",
        owner_id=OWNER_GOOGLE,
        hosts=("news.google.com",),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="Its article URLs are opaque redirect tokens whose resolution its terms and robots "
             "rules do not permit; Google items are hints only and never leave discovery.",
    ),
    _outlet(
        key="msn",
        publisher="MSN",
        owner_id=OWNER_MICROSOFT,
        hosts=("www.msn.com",),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="Syndicates other outlets' reporting.",
    ),
    _outlet(
        key="yahoo-news",
        publisher="Yahoo News",
        owner_id=OWNER_YAHOO,
        hosts=("nz.news.yahoo.com",),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        title_suffixes=("Yahoo",),
        note="Syndicates other outlets' reporting.",
    ),
    _outlet(
        key="facebook",
        publisher="Facebook",
        owner_id=OWNER_META,
        hosts=("www.facebook.com", "facebook.com"),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="Community posts are unattributable and unverifiable at collection time.",
    ),
    _outlet(
        key="x",
        publisher="X",
        owner_id=OWNER_X,
        hosts=("x.com", "twitter.com"),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="Community posts are unattributable and unverifiable at collection time.",
    ),
    _outlet(
        key="reddit",
        publisher="Reddit",
        owner_id=OWNER_REDDIT,
        hosts=("www.reddit.com", "reddit.com"),
        tier=_AGGREGATOR,
        use_policy=UsePolicy.DISCOVERY_ONLY,
        availability=SourceAvailability.NO_PUBLIC_SURFACE,
        note="Community posts are unattributable and unverifiable at collection time.",
    ),
)

MEDIA_CATALOG = PartyMediaCatalog(MEDIA_SOURCES, name="media")


def all_sources() -> tuple[PartyMediaSourceEntry, ...]:
    return MEDIA_CATALOG.all_sources()


def enabled_sources() -> tuple[PartyMediaSourceEntry, ...]:
    return MEDIA_CATALOG.enabled_sources()


def get_source(source_key: str) -> PartyMediaSourceEntry:
    return MEDIA_CATALOG.get(source_key)


def source_keys() -> tuple[str, ...]:
    return MEDIA_CATALOG.keys


def fetch_outlet(
    key: str,
    now: datetime,
    *,
    transport: CivicTransport | None = None,
    limit: int = MAX_DOCUMENTS_PER_FETCH,
) -> CivicFetchResult:
    """One outlet's own reporting, as a canonical Civic fetch result."""
    return fetch_entry(MEDIA_CATALOG.get(key), now, transport=transport, limit=limit)


class CivicMediaConnector:
    """The Channel-facing adapter for an outlet's reporting. Like the party connector, the Source
    config names a catalog key and nothing else, and a restricted or surface-less outlet fails
    with its recorded reason rather than being fetched some other way."""

    type_key = "civic_media_report"
    supported_channel_kinds = frozenset({ChannelKind.EDITORIAL})

    def __init__(
        self,
        catalog: PartyMediaCatalog = MEDIA_CATALOG,
        transport: CivicTransport | None = None,
    ):
        self._catalog = catalog
        self._transport = transport

    def validate_config(self, config: dict) -> None:
        if set(config) != {"outlet"}:
            raise ValueError("civic_media_report config must be exactly {'outlet': <key>}")
        outlet = config["outlet"]
        if not isinstance(outlet, str):
            raise ValueError("civic_media_report 'outlet' must be a string key")
        self._catalog.get(outlet)

    def entry_for(self, config: dict) -> PartyMediaSourceEntry:
        self.validate_config(config)
        return self._catalog.get(config["outlet"])

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
        return civic_documents_to_raw_items(self.fetch_result(config), self._catalog)

    def fetch_preview(self, config: dict, *, limit: int) -> list[RawItem]:
        if limit < 1:
            raise ValueError("preview limit must be positive")
        return civic_documents_to_raw_items(
            self.fetch_result(config, limit=limit), self._catalog)


register(CivicMediaConnector())
