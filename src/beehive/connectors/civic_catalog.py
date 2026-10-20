"""The party and media half of the Civic source catalog.

`beehive.civic.catalog` declares the official sources; this module declares the party-primary and
media ones. Both produce the same thing -- `CivicSourceEntry` values whose connectors return
`CivicFetchResult`s of `CivicDocument`s -- so an orchestrator enumerates
`catalog.enabled_sources() + party_media.enabled_sources()` and never learns that two teams wrote
them. There is no second record type, no second health vocabulary, and no second error hierarchy
here: `PartyMediaSourceEntry` *is* a `CivicSourceEntry`, and every failure raised is a
`beehive.civic.documents.CivicSourceError`.

What this half adds, because parties and mastheads are not government departments:

* **Ownership decides corroboration.** `owner_id` groups mastheads that share an owner, so two
  reports from one company can never count as two independent sources for a Civic Candidate. RNZ,
  1News and Te Ao are all crown-funded public media; NZME publishes the Herald and Newstalk ZB;
  Stuff and The Post are one company. `independent_owner_count` is what reads it.
* **Terms decide use.** `use_policy` separates "may be cited" from "may only hint" from "the
  publisher's terms forbid this entirely". A RESTRICTED source carries no endpoint at all, so
  nothing fetches it, no body reaches an AI call, and the discovery matcher refuses to resolve to
  it -- lifting that means committing an actual licence here.
* **Unavailable is a state, not an omission.** A party that blocks us or publishes nothing
  machine-readable stays in the catalog with its reason and a connector that refuses. That is
  what makes a coverage gap visible and attributable instead of looking like a quiet week.

Classification is the application's: `tier`, `publisher` and `owner_id` come from these constants
and are copied onto every document. Nothing downstream can read a tier out of remote data.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from urllib.parse import urlsplit

from beehive.civic.catalog import CivicSourceEntry
from beehive.civic.documents import CivicDocument, CivicFetchResult, CivicSourceError
from beehive.domain.civic import CivicEvidenceTier

# Descriptive and attributable, never an impersonation of a browser or another vendor's agent.
# Distinct from `civic.fetching.CIVIC_USER_AGENT` only in naming the party/media crawl, so an
# operator reading their logs can tell which half of PartyMap is calling.
PARTY_MEDIA_USER_AGENT = (
    "PartyMap Party & Media Watch/0.1 (+https://partymap.co.nz)"
)

# A party or masthead fetch returns at most this many documents. Bounded output is what keeps one
# verbose sitemap from dominating a Civic Run's screening budget; reaching it is reported as a
# degraded crawl rather than silently truncated.
MAX_DOCUMENTS_PER_FETCH = 50
# Below `civic.fetching.DEFAULT_MAX_BYTES`: the largest of these documents is ACT's whole-site
# sitemap at ~320 KB and 1News's RSS at ~450 KB.
PARTY_MEDIA_MAX_BYTES = 4 * 1024 * 1024

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class UsePolicy(str, Enum):
    """What PartyMap may do with a source's content.

    EVIDENCE -- may be fetched, summarised, passed to an AI call, and cited in a published
    statement. DISCOVERY_ONLY -- may only suggest that something happened; its URL never becomes
    Civic Evidence and its body never corroborates a Candidate, because an aggregator carries
    someone else's reporting and citing it would double-count the original. RESTRICTED -- the
    publisher's terms forbid this use; never fetched, never sent to an AI call, never cited.
    """

    EVIDENCE = "evidence"
    DISCOVERY_ONLY = "discovery_only"
    RESTRICTED = "restricted"


class SourceAvailability(str, Enum):
    """Why a catalogued source is or is not fetchable. Kept explicit rather than omitting the
    source, so a Watch's health can name the gap: "Te Tai Tokerau blocks us" is actionable, a
    silently missing party is not."""

    ENABLED = "enabled"
    # Declares a sitemap or feed but serves 403 to identified non-browser clients.
    BLOCKED = "blocked"
    # Publishes no machine-readable publication surface at all.
    NO_PUBLIC_SURFACE = "no_public_surface"
    # A surface exists, but its licence does not cover this use.
    LICENCE_WITHHELD = "licence_withheld"


class CivicSourceUnavailableError(CivicSourceError):
    """The source is catalogued but not fetchable (blocked, or nothing machine-readable).

    A subclass of the canonical error rather than a parallel one, so a caller that already
    handles `CivicSourceError` handles this without knowing party sources exist."""


class CivicSourceRestrictedError(CivicSourceError):
    """The publisher's terms forbid this use. Raised before any request is made."""


class CivicSourceHostError(CivicSourceError):
    """A URL that would have left a connector is not on its source's host allow-list. Covers a
    feed linking off-site and a redirect chain that ended somewhere else: every URL a document
    carries is re-checked against the catalog, so off-allowlist content cannot become
    evidence."""


_TIER_BY_POLICY: dict[UsePolicy, frozenset[CivicEvidenceTier]] = {
    UsePolicy.EVIDENCE: frozenset({
        CivicEvidenceTier.PARTY_PRIMARY,
        CivicEvidenceTier.REPUTABLE_MEDIA,
        CivicEvidenceTier.OTHER_MEDIA,
    }),
    UsePolicy.RESTRICTED: frozenset({
        CivicEvidenceTier.REPUTABLE_MEDIA, CivicEvidenceTier.OTHER_MEDIA}),
    UsePolicy.DISCOVERY_ONLY: frozenset({CivicEvidenceTier.AGGREGATOR}),
}


def normalize_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


def url_host(url: str) -> str:
    """The host of `url`, or "" when it is not an absolute http(s) URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    return normalize_host(parts.hostname)


@dataclass(frozen=True)
class UnavailableSourceDefinition:
    """The definition of a source that exists but cannot be read.

    Carries the same `source_key`/`publisher`/`tier` surface every other definition does, so a
    blocked party enumerates, reports and fails exactly like a working one instead of being a
    special case in every caller."""

    source_key: str
    publisher: str
    tier: CivicEvidenceTier
    reason: str
    endpoint: None = None
    crawl_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(f"{self.source_key}: an unavailable source must record why")


class RefusingConnector:
    """The connector an unavailable source gets. Same shape as every other Civic connector, so
    the catalog needs no branch; it simply refuses, loudly and with the reason."""

    def __init__(self, definition: UnavailableSourceDefinition, transport=None):
        self._definition = definition

    @property
    def source_key(self) -> str:
        return self._definition.source_key

    @property
    def definition(self) -> UnavailableSourceDefinition:
        return self._definition

    def validate_config(self, config: dict) -> None:
        if config:
            raise ValueError(f"{self._definition.source_key} config must be empty")

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        raise CivicSourceUnavailableError(self._definition.source_key, self._definition.reason)


class RestrictedConnector(RefusingConnector):
    """The connector a licence-restricted source gets. Fetching is refused before a request is
    built, so there is no code path on which a restricted publisher's body exists in memory."""

    def fetch(self, now: datetime, *, config: dict | None = None) -> CivicFetchResult:
        self.validate_config(config or {})
        raise CivicSourceRestrictedError(self._definition.source_key, self._definition.reason)


@dataclass(frozen=True)
class PartyMediaSourceEntry(CivicSourceEntry):
    """A Civic catalog entry with the two facts official sources do not need: who owns the
    outlet, and what its terms permit.

    Subclasses `CivicSourceEntry` deliberately -- `isinstance(entry, CivicSourceEntry)` holds, so
    the orchestrator enumerates official and party/media sources through one type rather than
    learning about a second one."""

    owner_id: str = ""
    use_policy: UsePolicy = UsePolicy.EVIDENCE
    availability: SourceAvailability = SourceAvailability.ENABLED
    hosts: frozenset[str] = field(default_factory=frozenset)
    # Trailing mastheads an aggregator appends to a headline, stripped before matching.
    title_suffixes: tuple[str, ...] = ()
    note: str = ""
    # False when article pages are client-rendered app shells with no article text in their
    # HTML. The feed still suggests and matches stories, but nothing can ever be captured, so
    # its evidence is never a source-refresh target and never blocks a capturable neighbour.
    article_capture: bool = True

    def __post_init__(self) -> None:
        if not self.source_key.strip() or not self.owner_id.strip():
            raise ValueError("a party/media source needs a source_key and owner_id")
        if not self.hosts:
            raise ValueError(f"{self.source_key}: a party/media source needs allowed hosts")
        for host in self.hosts:
            if host != normalize_host(host) or "/" in host or ":" in host:
                raise ValueError(f"{self.source_key}: {host!r} is not a bare lowercase host")
        if self.tier not in _TIER_BY_POLICY[self.use_policy]:
            raise ValueError(
                f"{self.source_key}: tier {self.tier.value!r} is not valid for a "
                f"{self.use_policy.value} source")
        if self.use_policy is UsePolicy.RESTRICTED:
            # Fail closed: a restricted outlet keeps no endpoint, so there is nothing for a
            # future caller to accidentally fetch.
            if self.availability is not SourceAvailability.LICENCE_WITHHELD:
                raise ValueError(f"{self.source_key}: a restricted source must be LICENCE_WITHHELD")
            if self.connector_class is not RestrictedConnector:
                raise ValueError(
                    f"{self.source_key}: a restricted source must use RestrictedConnector")
        if self.availability is SourceAvailability.ENABLED:
            endpoint = getattr(self.definition, "endpoint", None)
            if endpoint is None:
                raise ValueError(f"{self.source_key}: an enabled source needs an endpoint")
            if url_host(endpoint.url) not in self.hosts:
                raise ValueError(
                    f"{self.source_key}: endpoint {endpoint.url!r} is not on its own host "
                    "allow-list")
        elif getattr(self.definition, "endpoint", None) is not None:
            raise ValueError(
                f"{self.source_key}: an unavailable source must not carry an endpoint")
        if not self.is_fetchable and not self.note.strip():
            raise ValueError(f"{self.source_key}: an unavailable source must record why")

    @property
    def url(self) -> str:
        endpoint = getattr(self.definition, "endpoint", None)
        return endpoint.url if endpoint is not None else ""

    @property
    def is_fetchable(self) -> bool:
        return (
            self.availability is SourceAvailability.ENABLED
            and self.use_policy is not UsePolicy.RESTRICTED
        )

    @property
    def may_be_evidence(self) -> bool:
        """True only for an enabled source whose licence permits citation. Everything else --
        discovery-only aggregators, blocked parties, restricted mastheads -- is excluded here
        rather than at each call site."""
        return self.is_fetchable and self.use_policy is UsePolicy.EVIDENCE

    @property
    def may_send_body_to_ai(self) -> bool:
        """Whether a fetched body may be put in a prompt. Only citable sources qualify: an
        aggregator's body is someone else's reporting, and a restricted outlet's body is exactly
        what its terms withhold."""
        return self.may_be_evidence

    @property
    def publisher_labels(self) -> tuple[str, ...]:
        """Every name this source is known by, deduplicated so a masthead that repeats its own
        label as a headline suffix does not look like two publishers."""
        return tuple(dict.fromkeys((self.publisher, *self.title_suffixes)))

    def owns_url(self, url: str) -> bool:
        """Exact-host membership, not a suffix match: `evil-national.org.nz` and
        `news.national.org.nz.attacker.test` both fail."""
        return url_host(url) in self.hosts

    def assert_owns_url(self, url: str) -> str:
        if not self.owns_url(url):
            raise CivicSourceHostError(
                self.source_key,
                f"{url!r} is not on the allowed hosts {sorted(self.hosts)!r}")
        return url

    def assert_fetchable(self) -> None:
        """Raises the specific reason this source may not be fetched, in the canonical error
        hierarchy, so a blocked or restricted source fails identically everywhere."""
        if self.use_policy is UsePolicy.RESTRICTED:
            raise CivicSourceRestrictedError(self.source_key, self.note)
        if self.availability is not SourceAvailability.ENABLED:
            raise CivicSourceUnavailableError(
                self.source_key, f"{self.availability.value}: {self.note}")


class PartyMediaCatalog:
    """An immutable, host-indexed set of `PartyMediaSourceEntry` values.

    Host uniqueness is enforced on construction: two entries claiming one host would make "which
    policy applies to this URL?" ambiguous, and the Google discovery matcher answers exactly that
    question before deciding whether a URL may become evidence."""

    def __init__(self, entries: Iterable[PartyMediaSourceEntry], *, name: str):
        ordered = tuple(entries)
        by_key: dict[str, PartyMediaSourceEntry] = {}
        by_host: dict[str, PartyMediaSourceEntry] = {}
        for entry in ordered:
            if entry.source_key in by_key:
                raise ValueError(f"{name}: duplicate civic source {entry.source_key!r}")
            by_key[entry.source_key] = entry
            for host in entry.hosts:
                if host in by_host:
                    raise ValueError(
                        f"{name}: host {host!r} is claimed by both "
                        f"{by_host[host].source_key!r} and {entry.source_key!r}")
                by_host[host] = entry
        self._name = name
        self._entries = ordered
        self._by_key = by_key
        self._by_host = by_host

    @property
    def name(self) -> str:
        return self._name

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[PartyMediaSourceEntry]:
        return iter(self._entries)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_key)

    def all_sources(self) -> tuple[PartyMediaSourceEntry, ...]:
        return self._entries

    def get(self, source_key: str) -> PartyMediaSourceEntry:
        try:
            return self._by_key[source_key]
        except KeyError:
            raise ValueError(f"{self._name}: unknown civic source {source_key!r}") from None

    def for_url(self, url: str) -> PartyMediaSourceEntry | None:
        """The entry that owns `url`, or None when no catalogued source does. None is the
        fail-closed answer everywhere it is used: an unknown host is never evidence."""
        return self._by_host.get(url_host(url))

    def enabled_sources(self) -> tuple[PartyMediaSourceEntry, ...]:
        return tuple(entry for entry in self._entries if entry.is_fetchable)

    def disabled_sources(self) -> tuple[PartyMediaSourceEntry, ...]:
        return tuple(entry for entry in self._entries if not entry.is_fetchable)

    def owner_ids(self) -> frozenset[str]:
        return frozenset(entry.owner_id for entry in self._entries)


def merge_catalogs(*catalogs: PartyMediaCatalog, name: str) -> PartyMediaCatalog:
    """One catalog covering every party and media surface, used where a URL must be classified
    without knowing in advance which it belongs to. Re-runs the duplicate-key and duplicate-host
    checks across the combined set."""
    return PartyMediaCatalog(
        (entry for catalog in catalogs for entry in catalog), name=name)


def independent_owner_count(
    documents: Iterable[CivicDocument], catalog: PartyMediaCatalog
) -> int:
    """How many *independent* owners back these documents.

    Two mastheads of the same company are one source of truth, not two: Stuff and The Post share
    a newsroom, and RNZ, 1News and Te Ao are all crown-funded public media. Corroboration counts
    owners so a Civic Candidate cannot be published on the strength of one organisation's story
    appearing twice. Documents from sources that may not be cited are excluded entirely.
    """
    owners: set[str] = set()
    for document in documents:
        entry = catalog.for_url(document.canonical_url)
        if entry is not None and entry.may_be_evidence:
            owners.add(entry.owner_id)
    return len(owners)


__all__ = [
    "HTML_CONTENT_TYPES",
    "MAX_DOCUMENTS_PER_FETCH",
    "PARTY_MEDIA_MAX_BYTES",
    "PARTY_MEDIA_USER_AGENT",
    "CivicSourceHostError",
    "CivicSourceRestrictedError",
    "CivicSourceUnavailableError",
    "PartyMediaCatalog",
    "PartyMediaSourceEntry",
    "RefusingConnector",
    "RestrictedConnector",
    "SourceAvailability",
    "UnavailableSourceDefinition",
    "UsePolicy",
    "independent_owner_count",
    "merge_catalogs",
    "normalize_host",
    "url_host",
]
