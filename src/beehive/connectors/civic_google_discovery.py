"""Turning a Google News item into something PartyMap may actually cite -- or refusing to.

Google News items are useful for one thing: noticing that a story exists. They cannot be used as
evidence, because a Google News item's link and guid are opaque redirect tokens rather than the
publisher's URL, and resolving them is exactly what Google's feed terms and robots rules do not
permit. This module therefore implements no token decoding, no `batchexecute` call, and no fetch
of `news.google.com/rss/articles`; the hint's own URL is never read during resolution and can
never be returned.

What it does instead is deterministic re-finding: take the hint's headline and stated publisher,
look the publisher up in the committed catalog, and check whether that publisher's *own*
`CivicDocument`s -- fetched from the fixed endpoint the catalog pins -- include the same headline
at about the same time. If exactly one document matches, that document is the result. Nothing
else is.

Every refusal is recorded rather than guessed, because the failure mode of guessing is publishing
a civic claim attributed to a story that does not say it. A hint stays discovery-only when:

* the publisher is not catalogued at all, or two catalogued sources answer to the same label;
* the publisher is catalogued but licence-restricted (NZME, RNZ), blocked, or discovery-only;
* the hint or the candidate document has no publication time, or they are further apart than
  `PUBLICATION_TOLERANCE`;
* the normalised headlines do not match exactly;
* more than one document matches, which is ambiguous;
* the matched document's URL is not on the publisher's own host allow-list, which is what a
  redirect chain that ended somewhere else looks like from here.

Matching is exact on a normalised headline, not fuzzy. A similarity threshold would be a dial
someone eventually turns up, and the cost of a wrong turn is a misattributed civic statement.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from beehive.civic.documents import CivicDocument, as_utc
from beehive.connectors.civic_catalog import (
    PartyMediaCatalog,
    PartyMediaSourceEntry,
    UsePolicy,
)
from beehive.connectors.civic_sources import PARTY_MEDIA_CATALOG

# How far apart a hint and a publisher's own document may be and still be the same publication.
# Wide enough for feed lag, republication, and a timezone-free date-only entry; narrow enough
# that a follow-up story a week later cannot be mistaken for the original.
PUBLICATION_TOLERANCE = timedelta(hours=36)

# The whole party/media surface in one lookup, so "who published this URL?" has one answer.
CIVIC_CATALOG = PARTY_MEDIA_CATALOG

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
# The separators aggregators use before appending a masthead to a headline.
_SUFFIX_SEPARATORS = (" - ", " | ", " – ", " — ", " · ")


class DiscoveryOutcome(str, Enum):
    """Why a hint did or did not become a citable publisher URL. Recorded on the resolution so a
    Civic Run can report "12 hints, 4 resolved, 5 restricted publishers, 3 unknown" instead of a
    bare count."""

    RESOLVED = "resolved"
    UNKNOWN_PUBLISHER = "unknown_publisher"
    DISCOVERY_ONLY_PUBLISHER = "discovery_only_publisher"
    RESTRICTED_PUBLISHER = "restricted_publisher"
    PUBLISHER_UNAVAILABLE = "publisher_unavailable"
    MISSING_DATE = "missing_date"
    NO_MATCH = "no_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    OFF_ALLOWLIST_URL = "off_allowlist_url"


@dataclass(frozen=True)
class DiscoveryHint:
    """One aggregator item, in the only shape this module accepts.

    `url` is kept so a caller can log what it saw and so `may_leave_discovery` can refuse it. It
    is never read during resolution: an aggregator's redirect target is not a result.
    """

    title: str
    url: str
    publisher_name: str
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a discovery hint needs a title")


@dataclass(frozen=True)
class DiscoveryResolution:
    """The result of trying to re-find a hint at its publisher.

    `document` is populated only for RESOLVED, and only ever with a document the publisher's own
    connector produced. Every other outcome leaves it None: the hint stays discovery-only.
    """

    outcome: DiscoveryOutcome
    document: CivicDocument | None = None
    source_key: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.outcome is DiscoveryOutcome.RESOLVED) != (self.document is not None):
            raise ValueError("a resolution carries a document exactly when it resolved")

    @property
    def is_resolved(self) -> bool:
        return self.outcome is DiscoveryOutcome.RESOLVED


def normalize_headline(title: str, *, suffixes: tuple[str, ...] = ()) -> str:
    """A headline reduced to what two publishers of the same story agree on.

    Strips a trailing masthead (`"... - RNZ"`, `"... | Stuff"`), then case, accents, punctuation,
    and whitespace differences. Deliberately lossy in the same way for both sides of a comparison,
    and never lossy enough to make two different headlines equal: the word sequence is preserved.
    """
    text = " ".join(title.split())
    for suffix in suffixes:
        for separator in _SUFFIX_SEPARATORS:
            marker = f"{separator}{suffix}"
            if text.endswith(marker):
                text = text[: -len(marker)]
                break
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", stripped)).strip()


def _normalize_publisher(name: str) -> str:
    return normalize_headline(name)


def find_publisher(
    publisher_name: str, catalog: PartyMediaCatalog = CIVIC_CATALOG
) -> PartyMediaSourceEntry | None:
    """The catalogued source a publisher label names, or None.

    Matching is on the labels the catalog itself commits (`publisher` plus `title_suffixes`), so
    an aggregator cannot introduce a publisher simply by claiming one. None is the fail-closed
    answer and it covers both halves of "we cannot tell": an unknown label, and a label two
    catalogued sources both answer to.
    """
    wanted = _normalize_publisher(publisher_name)
    if not wanted:
        return None
    matches = [
        entry for entry in catalog
        if any(_normalize_publisher(label) == wanted for label in entry.publisher_labels)
    ]
    return matches[0] if len(matches) == 1 else None


def may_leave_discovery(url: str, catalog: PartyMediaCatalog = CIVIC_CATALOG) -> bool:
    """Whether `url` may be cited as Civic Evidence at all.

    True only for a URL on a catalogued, enabled, citable source's own host. Every aggregator URL
    -- `news.google.com/rss/articles/...` above all -- every social post, every licence-restricted
    masthead, and every unknown host is False. Callers use this as the last gate before a URL is
    written as evidence, so a URL that reached them by some other path than `resolve_hint` still
    cannot slip through.
    """
    entry = catalog.for_url(url)
    return entry is not None and entry.may_be_evidence


def _refusal_for(entry: PartyMediaSourceEntry) -> DiscoveryOutcome | None:
    if entry.use_policy is UsePolicy.RESTRICTED:
        return DiscoveryOutcome.RESTRICTED_PUBLISHER
    if entry.use_policy is UsePolicy.DISCOVERY_ONLY:
        return DiscoveryOutcome.DISCOVERY_ONLY_PUBLISHER
    if not entry.may_be_evidence:
        return DiscoveryOutcome.PUBLISHER_UNAVAILABLE
    return None


def resolve_hint(
    hint: DiscoveryHint,
    candidates: list[CivicDocument],
    *,
    catalog: PartyMediaCatalog = CIVIC_CATALOG,
    tolerance: timedelta = PUBLICATION_TOLERANCE,
) -> DiscoveryResolution:
    """Re-find `hint` among `candidates`, which must be documents this publisher's own connector
    produced from its catalogued endpoint.

    The returned document is one of `candidates` -- never anything derived from the hint -- so the
    only thing that can leave discovery is something the publisher itself published at its own
    address.
    """
    entry = find_publisher(hint.publisher_name, catalog)
    if entry is None:
        return DiscoveryResolution(
            DiscoveryOutcome.UNKNOWN_PUBLISHER,
            detail=f"publisher {hint.publisher_name!r} is not catalogued")
    refusal = _refusal_for(entry)
    if refusal is not None:
        return DiscoveryResolution(refusal, source_key=entry.source_key, detail=entry.note)
    if hint.published_at is None:
        return DiscoveryResolution(
            DiscoveryOutcome.MISSING_DATE,
            source_key=entry.source_key,
            detail="a publication time is required on both sides to match a hint")

    wanted = normalize_headline(hint.title, suffixes=entry.publisher_labels)
    if not wanted:
        return DiscoveryResolution(
            DiscoveryOutcome.NO_MATCH,
            source_key=entry.source_key,
            detail="headline normalised to nothing")

    matches = [
        document for document in candidates
        if document.source_key == entry.source_key
        and normalize_headline(document.title, suffixes=entry.publisher_labels) == wanted
        and _within(document.published_at, hint.published_at, tolerance)
    ]
    if not matches:
        return DiscoveryResolution(
            DiscoveryOutcome.NO_MATCH,
            source_key=entry.source_key,
            detail="the publisher's own feed does not carry this headline in the time window")
    if len(matches) > 1:
        return DiscoveryResolution(
            DiscoveryOutcome.AMBIGUOUS_MATCH,
            source_key=entry.source_key,
            detail=f"{len(matches)} documents match; refusing to choose")

    match = matches[0]
    if not entry.owns_url(match.canonical_url) or not may_leave_discovery(
            match.canonical_url, catalog):
        return DiscoveryResolution(
            DiscoveryOutcome.OFF_ALLOWLIST_URL,
            source_key=entry.source_key,
            detail=f"{match.canonical_url!r} is not a citable URL on {sorted(entry.hosts)!r}")
    return DiscoveryResolution(
        DiscoveryOutcome.RESOLVED, document=match, source_key=entry.source_key)


def resolve_hints(
    hints: list[DiscoveryHint],
    candidates: list[CivicDocument],
    *,
    catalog: PartyMediaCatalog = CIVIC_CATALOG,
    tolerance: timedelta = PUBLICATION_TOLERANCE,
) -> list[tuple[DiscoveryHint, DiscoveryResolution]]:
    """`resolve_hint` over a batch, preserving order so a run's report lines up with its hints."""
    return [
        (hint, resolve_hint(hint, candidates, catalog=catalog, tolerance=tolerance))
        for hint in hints
    ]


def _within(left: datetime | None, right: datetime | None, tolerance: timedelta) -> bool:
    """Whether two publication instants are close enough to describe the same publication. A
    missing instant is never close enough -- there is nothing to compare, and a source that
    publishes no times (a sitemap with no lastmod) therefore never matches a hint."""
    if left is None or right is None:
        return False
    return abs(as_utc(left) - as_utc(right)) <= tolerance


__all__ = [
    "CIVIC_CATALOG",
    "PUBLICATION_TOLERANCE",
    "DiscoveryHint",
    "DiscoveryOutcome",
    "DiscoveryResolution",
    "find_publisher",
    "may_leave_discovery",
    "normalize_headline",
    "resolve_hint",
    "resolve_hints",
]
