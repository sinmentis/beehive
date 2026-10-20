"""The normalized shape every Civic source connector returns, and the health vocabulary that
keeps "nothing to report" distinguishable from "this source is broken".

**Three timestamps, not one.** `beehive.connectors.base.RawItem` carries a single `created_at`,
which is right for a feed item but loses the distinction Civic depends on:

* `published_at` -- when the source says the document was published / released. May be absent;
  some sitemap entries only carry a modification date. Official sitting, assent and stage
  dates are independent event witnesses in source metadata.
* `updated_at` -- when the source last changed it. A release edited three days later is not a new
  release, and cross-day deduplication has to be able to tell those apart.
* `first_seen_at` -- when *Beehive* first observed it. Supplied by the caller from its own clock,
  never parsed from the response, so a source cannot backdate itself into (or out of) a slot.

The Electoral Commission feed is the clearest case: it publishes "the 10 most recently updated
pages", so an entry reappearing with a newer `updated_at` and an unchanged `published_at` is an
edit, and `first_seen_at` is the only field that answers "did we already have this".

**Tier is assigned, never read.** `CivicDocument.tier` is copied from the application's own
`CivicSourceDefinition` (catalog.py). Nothing here ever reads a tier, trust level, or authority
hint out of remote data or a model response -- a source that could describe its own tier could
promote itself past the corroboration rules in `beehive.domain.civic`.

**Health is explicit.** `CivicFetchResult.health` forces a caller to distinguish four outcomes
that all otherwise look like "a list of documents":

* OK -- the source answered normally and the documents are complete for this fetch.
* EMPTY -- the source answered normally and genuinely had nothing. Valid, common, and not an
  error, but recorded so a source that goes quiet forever is visible.
* TOMBSTONE -- the endpoint still answers HTTP 200 but is no longer a live source (NZ
  Legislation's retired `/subscribe/nzpco-rss.xml` is the motivating case: it returns 200 with a
  page saying the feed is gone, which every naive parser reads as "zero items, success"). Never
  silently treated as EMPTY.
* DEGRADED -- part of the crawl succeeded and part did not (one sub-sitemap failed, a page cap
  was reached, some entries were unparseable). The documents that were collected are returned AND
  the shortfall is reported, so a caller never mistakes a partial crawl for a complete one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from beehive.domain.civic import CivicEvidenceTier

# Query parameters that identify a campaign or a referrer rather than a document. Stripped during
# canonicalization so the same release arriving via a newsletter link and via a sitemap produces
# one stable URL -- and therefore one Civic Candidate -- instead of two.
_TRACKING_QUERY_PREFIXES = ("utm_", "pk_", "mc_", "_hs")
_TRACKING_QUERY_KEYS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mkt_tok", "ref", "referrer", "source",
    "cid", "s_cid", "spm",
})

# Hard cap on any text this package extracts from a source. Bounded here rather than at each call
# site so no connector can hand an unbounded body to the rest of the pipeline.
MAX_EXCERPT_CHARS = 4000
MAX_TITLE_CHARS = 500


class CivicSourceHealth(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    TOMBSTONE = "tombstone"
    DEGRADED = "degraded"


class CivicSourceError(ValueError):
    """A Civic source, or one record inside it, did not match the contract this connector was
    written against.

    Subclasses ValueError deliberately. Every per-record failure in this package is caught and
    turned into a degraded reason by `except CivicSourceError`, and a caller that only knows the
    repository-wide `except ValueError` convention still catches it -- so a schema mismatch can
    never escape as a bare traceback. Carries the source key so a caller can record which source
    failed without parsing the message."""

    def __init__(self, source_key: str, message: str):
        super().__init__(f"{source_key}: {message}")
        self.source_key = source_key


class CivicTombstoneError(CivicSourceError):
    """A fixed endpoint answered successfully but is no longer a live source.

    Its own class because the operational response is different from every other failure: a
    tombstone is permanent, so retrying on the normal schedule is pointless and the catalog entry
    itself has to be replaced."""


@dataclass(frozen=True)
class CivicDocument:
    """One normalized record from an official source, before any screening or AI involvement.

    `external_key` is the source's own stable identifier -- a returned UUID, an OrderPaper id, a
    feed guid -- and never a hash of mutable content, so an edited page keeps its identity.
    `canonical_url` is where a reader should be sent, which for several of these sources is
    deliberately NOT the same string as `external_key`: the Beehive feed's guid is a Drupal node
    URL while the canonical link is the release/speech path, and the Electoral Commission's guid
    is the page URL while the entry may be re-published under a tidied link."""

    source_key: str
    external_key: str
    canonical_url: str
    title: str
    tier: CivicEvidenceTier
    publisher: str
    first_seen_at: datetime
    published_at: datetime | None = None
    updated_at: datetime | None = None
    excerpt: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_key.strip():
            raise ValueError("Civic document needs a source_key")
        if not self.external_key.strip():
            raise ValueError(f"{self.source_key}: Civic document needs an external_key")
        if not self.canonical_url.strip():
            raise ValueError(f"{self.source_key}: Civic document needs a canonical_url")
        if not self.title.strip():
            raise ValueError(f"{self.source_key}: Civic document needs a title")
        if not self.publisher.strip():
            raise ValueError(f"{self.source_key}: Civic document needs a publisher")
        for label, value in (
            ("first_seen_at", self.first_seen_at),
            ("published_at", self.published_at),
            ("updated_at", self.updated_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{self.source_key}: {label} must be timezone-aware")
        self._event_at()

    @property
    def dedupe_key(self) -> str:
        """The Watch-wide identity of the underlying development. Namespaced by source so two
        sources that happen to use the same upstream id can never collide, and stable across
        days so `beehive.db.civic_candidates` can refuse to publish the same story twice."""
        return f"{self.source_key}:{self.external_key}"

    @property
    def effective_at(self) -> datetime:
        """A collection-ordering signal: an explicit event, publication, update, then first seen.

        Modification and observation times are never publication or event-date witnesses."""
        return self._event_at() or self.published_at or self.updated_at or self.first_seen_at

    def _event_at(self) -> datetime | None:
        provenance = self.raw_metadata.get("event_date_provenance")
        if provenance is None:
            return None
        if not isinstance(provenance, dict) or not isinstance(provenance.get("value"), str):
            raise CivicSourceError(self.source_key, "event provenance needs an ISO timestamp")
        try:
            value = datetime.fromisoformat(provenance["value"])
        except ValueError as exc:
            raise CivicSourceError(self.source_key, "invalid event provenance timestamp") from exc
        if value.tzinfo is None:
            raise CivicSourceError(self.source_key, "event provenance must be timezone-aware")
        return value


@dataclass(frozen=True)
class CivicFetchResult:
    """Documents plus the health of the fetch that produced them.

    A caller must branch on `health` rather than on `len(documents)`: zero documents is a normal
    quiet day (EMPTY) for some sources and a serious signal (TOMBSTONE) for others, and a
    non-empty list can still be an incomplete crawl (DEGRADED)."""

    source_key: str
    documents: tuple[CivicDocument, ...]
    health: CivicSourceHealth
    fetched_at: datetime
    degraded_reasons: tuple[str, ...] = ()
    pages_fetched: int = 1

    def __post_init__(self) -> None:
        if self.health is CivicSourceHealth.DEGRADED and not self.degraded_reasons:
            raise ValueError(
                f"{self.source_key}: a degraded Civic fetch must say what was degraded")
        if self.health is not CivicSourceHealth.DEGRADED and self.degraded_reasons:
            raise ValueError(
                f"{self.source_key}: only a degraded Civic fetch may carry degraded_reasons")
        if self.health is CivicSourceHealth.EMPTY and self.documents:
            raise ValueError(f"{self.source_key}: an empty Civic fetch cannot carry documents")
        if self.health is CivicSourceHealth.OK and not self.documents:
            raise ValueError(
                f"{self.source_key}: a Civic fetch with no documents is EMPTY, not OK -- "
                "silently reporting success for nothing is exactly what health exists to prevent")
        if self.pages_fetched < 0:
            raise ValueError(f"{self.source_key}: pages_fetched must not be negative")

    @property
    def is_complete(self) -> bool:
        return self.health in (CivicSourceHealth.OK, CivicSourceHealth.EMPTY)


def build_result(
    source_key: str,
    documents: list[CivicDocument],
    fetched_at: datetime,
    *,
    degraded_reasons: list[str] | tuple[str, ...] = (),
    pages_fetched: int = 1,
) -> CivicFetchResult:
    """The one place a `CivicFetchResult`'s health is decided, so no connector can accidentally
    report OK for an empty crawl or forget to escalate a partial one to DEGRADED."""
    reasons = tuple(degraded_reasons)
    if reasons:
        health = CivicSourceHealth.DEGRADED
    elif documents:
        health = CivicSourceHealth.OK
    else:
        health = CivicSourceHealth.EMPTY
    return CivicFetchResult(
        source_key=source_key,
        documents=tuple(documents),
        health=health,
        fetched_at=fetched_at,
        degraded_reasons=reasons,
        pages_fetched=pages_fetched)


def canonical_url(url: str, *, base: str | None = None) -> str:
    """The stable, comparable form of a document URL.

    Lower-cases the scheme and host, drops a default port, strips the fragment, and removes
    campaign/referrer query parameters -- so the same release reached from a newsletter, a
    sitemap and a feed collapses to one URL and therefore one Civic Candidate. Everything else
    (path case, trailing slash, meaningful query parameters) is preserved: several of these
    sources serve case-sensitive paths and use query parameters as real document identity.

    `base` resolves a relative URL against its source document, which sitemaps and Atom feeds
    both need. Raises ValueError for anything that is not an absolute http(s) URL afterwards, so
    a caller can never hand a `javascript:` or scheme-relative string downstream.

    When `base` is an https URL on the *same host*, an `http` link is upgraded to https. Every
    Civic endpoint is required to be https, so the source itself is served securely on exactly
    that host: a feed that links its own release over http (NZ First does) is publishing one
    stale scheme for a document already fetched over https, not a second resource. The upgrade
    is deliberately host-scoped -- a third party's http link is left alone, because nothing here
    verified that host serves https."""
    candidate = url.strip()
    if not candidate:
        raise ValueError("empty URL")
    if base is not None and "://" not in candidate:
        from urllib.parse import urljoin

        candidate = urljoin(base, candidate)
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url!r}")
    if not parts.hostname:
        raise ValueError(f"URL has no host: {url!r}")
    host = parts.hostname.lower().rstrip(".")
    if scheme == "http" and base is not None:
        base_parts = urlsplit(base)
        base_host = (base_parts.hostname or "").lower().rstrip(".")
        if base_parts.scheme.lower() == "https" and base_host == host:
            scheme = "https"
    if parts.port is not None and parts.port not in (80, 443):
        raise ValueError(f"URL port {parts.port} is not 80 or 443: {url!r}")
    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    kept = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    return urlunsplit((scheme, netloc, parts.path, urlencode(kept), ""))


def stable_external_key(raw: Any, *, source_key: str, field_name: str = "id") -> str:
    """The source's own identifier, validated rather than derived.

    Accepts the string and integer forms these APIs actually return (Parliament hands back UUIDs
    as strings and OrderPaper ids as integers) and rejects anything else, because a missing id
    silently replaced by a content hash would make every edit look like a brand-new document."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{source_key}: {field_name} is missing")
    if isinstance(raw, int):
        return str(raw)
    if not isinstance(raw, str):
        raise ValueError(f"{source_key}: {field_name} must be a string or integer")
    value = raw.strip()
    if not value:
        raise ValueError(f"{source_key}: {field_name} is empty")
    return value


def bounded_text(value: Any, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Whitespace-collapsed, length-capped text. Non-string input becomes an empty string rather
    than raising: an absent optional description is normal, and only required fields are strict."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def as_utc(value: datetime | None) -> datetime | None:
    """Every timestamp this package emits is aware UTC, matching
    `beehive.connectors.base.as_utc`'s reasoning: a naive value persisted with `.isoformat()` is
    read back as if it were UTC, so two sources reporting the same instant in different forms
    would otherwise produce different stored strings."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
