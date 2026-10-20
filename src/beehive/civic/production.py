"""Production construction of Civic policy inputs from application-owned catalogs.

Remote documents contribute text, dates, and their publisher's external identity. Trust,
ownership, source kind, party identity, and destination shape come only from the committed
source catalogs in ``beehive.civic.catalog`` and ``beehive.connectors.civic_*``.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from beehive.civic.clustering import CivicDocumentCluster
from beehive.civic.documents import (
    CivicDocument,
    CivicSourceError,
    bounded_text,
)
from beehive.civic.evidence import (
    CivicCandidateClaim,
    CivicClaimCategory,
    CivicContentClass,
    CivicOwnershipRegistry,
    CivicPublisher,
    CivicPublisherKind,
    CivicRecordKind,
    CivicRecordOperation,
    CivicSourceKind,
)
from beehive.civic.fetching import CivicEndpoint, CivicTransport
from beehive.civic.source_extraction import (
    SourceExtractionError,
    article_title,
    capture_metadata,
    document_publication,
    extract_source_article,
    has_current_article_capture,
    has_full_article_extraction,
    publication_from_html,
)
from beehive.deep_read.extract import (
    ExtractionQuality,
    PartialReason,
)
from beehive.connectors.civic_catalog import (
    HTML_CONTENT_TYPES,
    PARTY_MEDIA_MAX_BYTES,
    PartyMediaSourceEntry,
    UsePolicy,
)
from beehive.connectors.civic_media import MEDIA_CATALOG
from beehive.connectors.civic_party import PARTY_CATALOG
from beehive.connectors.civic_sources import PARTY_MEDIA_CATALOG, all_sources
from beehive.connectors.civic_strategies import party_media_transport
from beehive.connectors.http import ConnectorHttpError
from beehive.db.civic_candidates import (
    add_candidate_evidence_if_claimed,
    list_candidate_evidence,
)
from beehive.db.civic_qualifications import get_latest_qualification
from beehive.domain.civic import (
    CivicCandidate,
    CivicEvidence,
    CivicEvidenceStance,
    CivicTerm,
    CivicTermRole,
    CivicWatch,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_OFFICIAL_SOURCE_KINDS: dict[str, CivicSourceKind] = {
    "nz_electoral_commission": CivicSourceKind.ELECTORAL_COMMISSION,
    "nz_beehive_releases": CivicSourceKind.GOVERNMENT,
    "nz_parliament_order_paper": CivicSourceKind.PARLIAMENT,
    "nz_rbnz_news": CivicSourceKind.OFFICIAL_STATISTICS,
    "nz_parliament_bills": CivicSourceKind.PARLIAMENT,
    "nz_parliament_select_committees": CivicSourceKind.PARLIAMENT,
    "nz_parliament_hansard": CivicSourceKind.PARLIAMENT,
    "nz_legislation_sitemap": CivicSourceKind.LEGISLATION,
    "nz_legislation_document": CivicSourceKind.LEGISLATION,
    "nz_legislation_works": CivicSourceKind.LEGISLATION,
    "nz_oag_reports": CivicSourceKind.OFFICIAL_REVIEW,
    "nz_ombudsman": CivicSourceKind.OFFICIAL_REVIEW,
    "nz_stats": CivicSourceKind.OFFICIAL_STATISTICS,
    "nz_data_govt": CivicSourceKind.OFFICIAL_STATISTICS,
}

_ELECTORAL_SOURCES = frozenset({"nz_electoral_commission"})
_LEGISLATION_SOURCES = frozenset({
    "nz_legislation_sitemap",
    "nz_legislation_document",
    "nz_legislation_works",
    "nz_parliament_bills",
    "nz_parliament_order_paper",
    "nz_parliament_hansard",
})
_FINDING_SOURCES = frozenset({
    "nz_oag_reports",
    "nz_ombudsman",
    "nz_stats",
    "nz_data_govt",
    "nz_rbnz_news",
})

# Closed, application-owned names for the 17 PartyMap parties. Remote text may match one of
# these phrases; it can never supply the resulting slug. All aliases are at least two tokens
# except the established initialism ALCP, avoiding generic one-word matches such as "act",
# "green", "national", or "vision".
PARTY_ALIASES: dict[str, tuple[str, ...]] = {
    "national": (
        "New Zealand National Party",
        "National Party",
        "NZ National Party",
    ),
    "labour": (
        "New Zealand Labour Party",
        "Labour Party",
    ),
    "green": (
        "Green Party of Aotearoa New Zealand",
        "Green Party",
    ),
    "act": (
        "ACT New Zealand",
        "ACT Party",
    ),
    "nz-first": (
        "New Zealand First",
        "NZ First",
    ),
    "te-pati-maori": (
        "Te Pāti Māori",
        "Te Pati Maori",
        "Māori Party",
        "Maori Party",
    ),
    "opportunity": (
        "The Opportunities Party",
        "Opportunities Party",
        "Opportunity Party",
    ),
    "alliance": (
        "Alliance Party of Aotearoa New Zealand",
        "Alliance Party",
    ),
    "te-tai-tokerau": ("Te Tai Tokerau Party",),
    "free-palestine": ("Free Palestine Party",),
    "nz-loyal": (
        "New Zealand Loyal",
        "NZ Loyal",
    ),
    "outdoors-freedom": (
        "NZ Outdoors & Freedom Party",
        "NZ Outdoors and Freedom Party",
        "New Zealand Outdoors & Freedom Party",
        "New Zealand Outdoors and Freedom Party",
        "Outdoors & Freedom Party",
        "Outdoors and Freedom Party",
    ),
    "conservative": (
        "Conservative Party NZ",
        "New Zealand Conservative Party",
        "Conservative Party",
    ),
    "vision-nz": ("Vision New Zealand",),
    "womens-rights": (
        "Women's Rights Party",
        "Womens Rights Party",
    ),
    "animal-justice": (
        "Animal Justice Party Aotearoa New Zealand",
        "Animal Justice Party",
    ),
    "cannabis": (
        "Aotearoa Legalise Cannabis Party",
        "Legalise Cannabis Party",
        "ALCP",
    ),
}

if set(PARTY_ALIASES) != set(PARTY_CATALOG.keys):
    raise RuntimeError(
        "the Civic party alias registry must cover exactly the 17 party catalog entries")


class _PartyResolvable(Protocol):
    title: str
    excerpt: str


class PartySubjectResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class PartySubjectResolution:
    status: PartySubjectResolutionStatus
    subject_id: str | None
    matched_party_slugs: tuple[str, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.status is PartySubjectResolutionStatus.RESOLVED


@dataclass(frozen=True)
class CivicSourceProfile:
    source_key: str
    publisher_id: str
    publisher: str
    publisher_kind: CivicPublisherKind
    source_kind: CivicSourceKind | None
    evidence_capable: bool
    ownership_group: str | None = None
    ownership_cluster: str | None = None
    party_slug: str | None = None
    registered: bool = True


def build_source_profiles(
    entries: Iterable[object] | None = None,
) -> dict[str, CivicSourceProfile]:
    """Build the closed source classification used by evidence persistence."""
    profiles: dict[str, CivicSourceProfile] = {}
    for entry in entries if entries is not None else all_sources():
        source_key = entry.source_key
        if source_key in profiles:
            raise ValueError(f"duplicate Civic source profile: {source_key}")
        if isinstance(entry, PartyMediaSourceEntry):
            if source_key in PARTY_CATALOG.keys:
                profile = CivicSourceProfile(
                    source_key=source_key,
                    publisher_id=source_key,
                    publisher=entry.publisher,
                    publisher_kind=CivicPublisherKind.PARTY,
                    source_kind=CivicSourceKind.PARTY_PRIMARY,
                    evidence_capable=entry.may_be_evidence,
                    party_slug=source_key,
                    registered=entry.may_be_evidence,
                )
            elif source_key in MEDIA_CATALOG.keys:
                discovery = entry.use_policy is UsePolicy.DISCOVERY_ONLY
                profile = CivicSourceProfile(
                    source_key=source_key,
                    publisher_id=source_key,
                    publisher=entry.publisher,
                    publisher_kind=(
                        CivicPublisherKind.DISCOVERY
                        if discovery else CivicPublisherKind.MEDIA
                    ),
                    source_kind=(
                        None if discovery else CivicSourceKind.INDEPENDENT_NEWS
                    ),
                    evidence_capable=entry.may_be_evidence,
                    ownership_group=entry.owner_id if not discovery else None,
                    ownership_cluster=entry.owner_id if not discovery else None,
                    registered=entry.may_be_evidence if not discovery else True,
                )
            else:
                raise ValueError(
                    f"{source_key}: party/media source is not in an application catalog")
        else:
            try:
                source_kind = _OFFICIAL_SOURCE_KINDS[source_key]
            except KeyError:
                raise ValueError(
                    f"{source_key}: official Civic source has no source-kind classification"
                ) from None
            profile = CivicSourceProfile(
                source_key=source_key,
                publisher_id=source_key,
                publisher=entry.publisher,
                publisher_kind=CivicPublisherKind.OFFICIAL,
                source_kind=source_kind,
                evidence_capable=True,
            )
        profiles[source_key] = profile
    return profiles


def build_ownership_registry(
    entries: Iterable[object] | None = None,
) -> CivicOwnershipRegistry:
    """Construct the policy registry from production catalogs, never fixtures."""
    publishers = []
    for profile in build_source_profiles(entries).values():
        publishers.append(CivicPublisher(
            publisher_id=profile.publisher_id,
            name=profile.publisher,
            kind=profile.publisher_kind,
            source_kind=profile.source_kind,
            ownership_group=profile.ownership_group,
            ownership_cluster=profile.ownership_cluster,
            registered=profile.registered,
            party_slug=profile.party_slug,
        ))
    return CivicOwnershipRegistry.build(publishers)


def filter_documents_for_vocabulary(
    documents: Iterable[CivicDocument],
    terms: Sequence[CivicTerm],
) -> tuple[CivicDocument, ...]:
    """Apply required/excluded Civic Terms before any Candidate is created."""
    required = tuple(
        term.normalized_term for term in terms if term.role is CivicTermRole.REQUIRED)
    excluded = tuple(
        term.normalized_term for term in terms if term.role is CivicTermRole.EXCLUDED)
    if not required:
        raise ValueError("a production Civic Run needs at least one required Civic Term")
    accepted = []
    for document in documents:
        haystack = " ".join(
            f"{document.title} {document.excerpt} {document.publisher}".casefold().split())
        if any(term in haystack for term in excluded):
            continue
        if any(term in haystack for term in required):
            accepted.append(document)
    return tuple(accepted)


def category_for_documents(
    documents: Sequence[CivicDocument],
) -> CivicClaimCategory:
    keys = {document.source_key for document in documents}
    if keys & _ELECTORAL_SOURCES:
        return CivicClaimCategory.ELECTORAL_ADMINISTRATION
    if keys & _LEGISLATION_SOURCES:
        return CivicClaimCategory.LEGISLATION
    if keys & _FINDING_SOURCES:
        return CivicClaimCategory.OFFICIAL_FINDING
    return CivicClaimCategory.PARTY_TIMELINE_EVENT


def explicit_event_date(document: CivicDocument) -> date | None:
    if document.source_key not in _OFFICIAL_SOURCE_KINDS:
        return None
    value = document.raw_metadata.get("event_date") or document.raw_metadata.get("sitting_date")
    if value is None:
        return None
    if not isinstance(value, str):
        raise CivicSourceError(document.source_key, "event date must be an ISO calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CivicSourceError(document.source_key, "invalid explicit event date") from exc


def event_date_for_document(document: CivicDocument, watch: CivicWatch) -> date | None:
    """Use only this document's event or publication witness, never Candidate context."""
    event = explicit_event_date(document)
    if event is not None:
        return event
    witness = document_publication(document)
    if witness is None:
        return None
    zone = ZoneInfo(
        witness.timezone if witness.precision == "date" else watch.schedule_timezone)
    return witness.value.astimezone(zone).date()


def event_date_for_cluster(
    cluster: CivicDocumentCluster, watch: CivicWatch,
) -> date | None:
    events = [
        day for document in cluster.documents
        if (day := explicit_event_date(document)) is not None
    ]
    if events:
        return min(events)
    published = [
        day for document in cluster.documents
        if (day := event_date_for_document(document, watch)) is not None]
    if not published:
        return None
    return min(published)


def _tokens(value: str) -> tuple[str, ...]:
    folded = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character for character in folded if not unicodedata.combining(character))
    return tuple(_TOKEN_RE.findall(without_marks))


def _contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(
        tokens[index:index + width] == phrase
        for index in range(len(tokens) - width + 1)
    )


_PARTY_ALIAS_TOKENS: dict[str, tuple[tuple[str, ...], ...]] = {
    slug: tuple(dict.fromkeys(_tokens(alias) for alias in aliases))
    for slug, aliases in PARTY_ALIASES.items()
}

_alias_owner: dict[tuple[str, ...], str] = {}
for _slug, _aliases in _PARTY_ALIAS_TOKENS.items():
    for _alias in _aliases:
        previous = _alias_owner.setdefault(_alias, _slug)
        if previous != _slug:
            raise RuntimeError(
                f"Civic party alias {_alias!r} belongs to both {previous!r} and {_slug!r}")


def resolve_party_subject(
    records: Sequence[_PartyResolvable],
    *,
    source_keys: Sequence[str],
    profiles: Mapping[str, CivicSourceProfile],
) -> PartySubjectResolution:
    """Resolve exactly one application-owned party slug for a document/evidence group.

    Party-primary catalog ownership wins without consulting text. Official/media material can
    only match a closed alias phrase on token boundaries in its title or excerpt.
    """
    if len(records) != len(source_keys):
        raise ValueError("party subject resolution needs one source key per record")
    primary_slugs = {
        profiles[source_key].party_slug
        for source_key in source_keys
        if profiles[source_key].party_slug is not None
    }
    if len(primary_slugs) == 1:
        slug = next(iter(primary_slugs))
        return PartySubjectResolution(
            PartySubjectResolutionStatus.RESOLVED, slug, (slug,))
    if len(primary_slugs) > 1:
        matches = tuple(sorted(primary_slugs))
        return PartySubjectResolution(
            PartySubjectResolutionStatus.AMBIGUOUS, None, matches)

    matched: set[str] = set()
    for record in records:
        tokens = _tokens(f"{record.title} {record.excerpt}")
        for slug, aliases in _PARTY_ALIAS_TOKENS.items():
            if any(_contains_phrase(tokens, alias) for alias in aliases):
                matched.add(slug)
    if len(matched) == 1:
        slug = next(iter(matched))
        return PartySubjectResolution(
            PartySubjectResolutionStatus.RESOLVED, slug, (slug,))
    if not matched:
        return PartySubjectResolution(
            PartySubjectResolutionStatus.UNSUPPORTED, None)
    matches = tuple(sorted(matched))
    return PartySubjectResolution(
        PartySubjectResolutionStatus.AMBIGUOUS, None, matches)


def resolve_cluster_party_subject(
    cluster: CivicDocumentCluster,
    profiles: Mapping[str, CivicSourceProfile],
) -> PartySubjectResolution:
    return resolve_party_subject(
        cluster.documents,
        source_keys=tuple(document.source_key for document in cluster.documents),
        profiles=profiles,
    )


def resolve_candidate_party_subject(
    evidence: Sequence[CivicEvidence],
    profiles: Mapping[str, CivicSourceProfile],
) -> PartySubjectResolution:
    return resolve_party_subject(
        evidence,
        source_keys=tuple(item.source_ref for item in evidence),
        profiles=profiles,
    )


def construct_candidate_claim(
    conn: sqlite3.Connection,
    candidate: CivicCandidate,
) -> CivicCandidateClaim:
    """Rebuild the deterministic claim from persisted evidence, including after a resume."""
    evidence = list_candidate_evidence(conn, candidate.id)
    if not evidence:
        raise ValueError(f"Civic Candidate {candidate.id} has no persisted evidence")
    subjects = {item.subject_id for item in evidence}
    if len(subjects) != 1 or not next(iter(subjects), ""):
        raise ValueError(
            f"Civic Candidate {candidate.id} evidence does not carry one consistent subject_id")
    subject_id = next(iter(subjects))
    if subject_id not in PARTY_CATALOG.keys:
        raise ValueError(
            f"Civic Candidate {candidate.id} subject_id {subject_id!r} is not catalogued")
    dated_evidence = [
        item for item in evidence
        if item.event_date is not None and item.claim_category is not None
    ]
    if not dated_evidence:
        raise ValueError(
            f"Civic Candidate {candidate.id} evidence has no event date/category")
    basis = min(
        dated_evidence,
        key=lambda item: (
            item.tier.rank,
            0 if item.stance is CivicEvidenceStance.SUPPORTING else 1,
            item.id or 0,
        ),
    )
    qualification = get_latest_qualification(conn, candidate.id)
    record_kind = (
        qualification.record_kind
        if qualification is not None and qualification.record_kind is not None
        else CivicRecordKind.TIMELINE
    )
    return CivicCandidateClaim(
        candidate_id=str(candidate.id),
        fingerprint=candidate.dedupe_key,
        record_kind=record_kind,
        operation=(
            CivicRecordOperation.CORRECT
            if record_kind is CivicRecordKind.CORRECTION
            else CivicRecordOperation.ADD
        ),
        subject_id=subject_id,
        record_key=candidate.dedupe_key,
        event_date=basis.event_date,
        category=CivicClaimCategory(basis.claim_category),
        content_class=CivicContentClass.FACTUAL,
        durable=True,
        claim_text=candidate.statement,
    )


def _accessible_full_text(document: CivicDocument, profile: CivicSourceProfile) -> bool:
    if not profile.evidence_capable:
        return False
    if profile.publisher_kind in (CivicPublisherKind.PARTY, CivicPublisherKind.MEDIA):
        return has_full_article_extraction(document)
    return bool(document.excerpt.strip())


class CivicHydrationStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    HYDRATED = "hydrated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CivicHydrationResult:
    document: CivicDocument
    status: CivicHydrationStatus
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is CivicHydrationStatus.HYDRATED and not has_full_article_extraction(
            self.document,
        ):
            raise ValueError("hydrated Civic evidence must carry full-article provenance")
        if self.status is CivicHydrationStatus.UNAVAILABLE and not self.error_code:
            raise ValueError("unavailable Civic hydration must carry an error code")
        if self.status is not CivicHydrationStatus.UNAVAILABLE and self.error_code is not None:
            raise ValueError("only unavailable Civic hydration carries an error code")


def _article_title_from_html(html: str) -> str | None:
    return article_title(html)


def _published_at_from_html(
    html: str, *, url: str = "", source_key: str = "",
) -> datetime | None:
    witness = publication_from_html(html, url=url, source_key=source_key)
    return witness.value if witness is not None else None


# Template furniture every party CMS wraps its articles in. Both ends are stripped only when
# they are recognisable with certainty, because the alternative -- storing them -- is worse than
# it looks: this text is the public record's own proof, and it is also the closed set that
# `generation.numeric_tokens` builds, so a street number in a promoter statement quietly
# licenses that number in a generated summary.
_NAVIGATION_WORDS = frozenset({
    "back", "home", "menu", "news", "press", "media", "release", "releases", "speech",
    "speeches", "publications", "article", "articles",
})
_MAX_NAVIGATION_CHARS = 64
# The promoter statement the Electoral Act requires on party material. It is recognised by its
# shape, not merely its opening words: a short trailing sentence naming a promoter and address.
# Prose *about* authorisation statements runs longer than one short clause, so it is left alone.
_AUTHORISATION_STATEMENT = re.compile(
    r"\s*Authorised by\b[^.!?]{0,90}[.!?]?\s*\Z", re.IGNORECASE)
_MAX_AUTHORISATION_STATEMENTS = 4


def _strip_template_chrome(text: str, heading: str | None) -> str:
    """Drop leading navigation and a trailing promoter statement, or return `text` unchanged.

    Fails closed in both directions. The prefix is removed only when the article's own heading
    is what follows it and everything before that heading is navigation vocabulary; the suffix
    only when the statement begins in the final stretch of the text."""
    flat = " ".join(text.split())
    if heading and heading.strip():
        target = " ".join(heading.split()).casefold()
        index = flat.casefold().find(target)
        if 0 < index <= _MAX_NAVIGATION_CHARS:
            words = flat[:index].replace("|", " ").replace(">", " ").split()
            if words and all(word.strip(".,:·-").casefold() in _NAVIGATION_WORDS
                             for word in words):
                flat = flat[index:]

    for _ in range(_MAX_AUTHORISATION_STATEMENTS):
        stripped = _AUTHORISATION_STATEMENT.sub("", flat, count=1).rstrip()
        if stripped == flat or not stripped:
            break
        flat = stripped
    return flat


class PartyMediaDocumentHydrator:
    """Fetch bounded on-host HTML for selected evidence without full-article provenance.

    Listing connectors remain cheap and deterministic. The worker invokes this after clustering,
    so no more than the run's bounded Candidate set reaches article detail pages.
    An unknown publication date stays unknown; date sufficiency belongs to the whole cluster.
    """

    def __init__(self, transport: CivicTransport | None = None) -> None:
        self._transport = transport or party_media_transport()

    def __call__(
        self,
        document: CivicDocument,
        now: datetime,
    ) -> CivicHydrationResult:
        if (
            has_current_article_capture(document)
            or document.source_key not in PARTY_MEDIA_CATALOG.keys
        ):
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.NOT_NEEDED,
            )
        entry = PARTY_MEDIA_CATALOG.get(document.source_key)
        if not entry.may_be_evidence:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.NOT_NEEDED,
            )
        try:
            entry.assert_owns_url(document.canonical_url)
        except CivicSourceError:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_off_host",
            )
        paths = getattr(entry.definition, "path_includes", ())
        if paths and not any(path in urlsplit(document.canonical_url).path for path in paths):
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_path_disallowed",
            )
        try:
            endpoint = CivicEndpoint(
                url=document.canonical_url,
                allowed_hosts=entry.hosts,
                accept="text/html, application/xhtml+xml",
                content_types=HTML_CONTENT_TYPES,
                max_bytes=PARTY_MEDIA_MAX_BYTES,
            )
        except ValueError:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_url_invalid",
            )
        try:
            response = self._transport.request(
                endpoint,
                source_key=document.source_key,
                crawl_delay_seconds=entry.crawl_delay_seconds,
            )
        except CivicSourceError as exc:
            cause = exc.__cause__
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code=(
                    f"detail_fetch_{cause.kind.value}"
                    if isinstance(cause, ConnectorHttpError) else "detail_fetch_failed"
                ),
            )
        if not entry.owns_url(response.final_url):
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_off_host",
            )
        if paths and not any(path in urlsplit(response.final_url).path for path in paths):
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_path_disallowed",
            )
        html = response.body.decode("utf-8", errors="replace")
        try:
            article = extract_source_article(
                html, url=response.final_url, source_key=document.source_key,
            )
        except SourceExtractionError as exc:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code=exc.code,
            )
        extraction = article.extraction
        if extraction.quality is ExtractionQuality.UNUSABLE:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_extraction_unusable",
            )
        if PartialReason.PAYWALL_LIKE in extraction.reasons:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_paywall_detected",
            )
        if extraction.quality is not ExtractionQuality.COMPLETE:
            return CivicHydrationResult(
                document=document,
                status=CivicHydrationStatus.UNAVAILABLE,
                error_code="detail_extraction_partial",
            )
        if article.publication is None and document.published_at is not None:
            article = replace(article, publication=document_publication(document))
        published_at = article.publication.value if article.publication else None
        heading = article.title
        body = _strip_template_chrome(extraction.text, heading)
        publisher_labels = {
            label.casefold()
            for label in entry.publisher_labels
        }
        hydrated_title = (
            heading
            if heading and heading.casefold() not in publisher_labels
            else document.title
        )
        return CivicHydrationResult(
            document=replace(
                document,
                title=hydrated_title,
                published_at=published_at,
                excerpt=bounded_text(body),
                raw_metadata={
                    **document.raw_metadata,
                    "detail_hydrated": True,
                    "detail_title_hydrated": hydrated_title != document.title,
                    "detail_publication_date_hydrated": (
                        document.published_at != published_at
                    ),
                    "detail_publication_date_missing": published_at is None,
                    "detail_extraction_quality": extraction.quality.value,
                    "source_capture": capture_metadata(
                        document, article=article, body=body,
                        captured_at=now, fetched_url=response.final_url,
                    ),
                },
            ),
            status=CivicHydrationStatus.HYDRATED,
        )


def _anonymous_dependency(profile: CivicSourceProfile) -> bool:
    # First-party records speak for themselves. A generic media listing cannot establish that
    # its report rests only on named sources, so it fails closed until enrichment records that.
    return profile.publisher_kind is CivicPublisherKind.MEDIA


def persist_cluster_evidence(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate: CivicCandidate,
    cluster: CivicDocumentCluster,
    watch: CivicWatch,
    subject_id: str,
    profiles: Mapping[str, CivicSourceProfile],
    now: datetime,
    now_fn=None,
) -> tuple[CivicEvidence, ...] | None:
    """Persist every source document behind a Candidate through the Run claim fence."""
    category = category_for_documents(cluster.documents)
    if subject_id and subject_id not in PARTY_CATALOG.keys:
        raise ValueError(f"unknown resolved party slug: {subject_id!r}")
    written: list[CivicEvidence] = []
    for index, document in enumerate(cluster.documents):
        try:
            profile = profiles[document.source_key]
        except KeyError:
            raise ValueError(
                f"{document.source_key}: fetched Civic document has no source profile") from None
        evidence = add_candidate_evidence_if_claimed(
            conn,
            run_id=run_id,
            claim_token=claim_token,
            candidate_id=candidate.id,
            source_ref=document.source_key,
            external_key=document.dedupe_key,
            url=document.canonical_url,
            title=document.title,
            tier=document.tier,
            stance=(
                CivicEvidenceStance.SUPPORTING
                if index == 0 else CivicEvidenceStance.CORROBORATING
            ),
            now=now,
            now_fn=now_fn,
            publisher=profile.publisher,
            excerpt=document.excerpt,
            published_at=document.published_at,
            publisher_id=profile.publisher_id,
            source_kind=(
                profile.source_kind.value if profile.source_kind is not None else None
            ),
            party_slug=profile.party_slug,
            subject_id=subject_id,
            claim_category=category.value,
            event_date=event_date_for_document(document, watch),
            canonical_url=document.canonical_url,
            accessible_full_text=_accessible_full_text(document, profile),
            paywalled=False,
            anonymous_source_dependency=_anonymous_dependency(profile),
            source_metadata=document.raw_metadata,
        )
        if evidence is None:
            return None
        written.append(evidence)
    return tuple(written)


def persist_research_evidence(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate: CivicCandidate,
    claim: CivicCandidateClaim,
    documents: Sequence[CivicDocument],
    profiles: Mapping[str, CivicSourceProfile],
    now: datetime,
    now_fn=None,
) -> tuple[CivicEvidence, ...] | None:
    """Attach research under the same claim/category while preserving each source's own date."""
    from beehive.db.civic_watches import get_civic_watch

    watch = get_civic_watch(conn, candidate.watch_id)
    if watch is None:
        raise ValueError(f"no Civic Watch with id={candidate.watch_id}")
    written: list[CivicEvidence] = []
    for document in documents:
        try:
            profile = profiles[document.source_key]
        except KeyError:
            raise ValueError(
                f"{document.source_key}: research result has no source profile") from None
        evidence = add_candidate_evidence_if_claimed(
            conn,
            run_id=run_id,
            claim_token=claim_token,
            candidate_id=candidate.id,
            source_ref=document.source_key,
            external_key=document.dedupe_key,
            url=document.canonical_url,
            title=document.title,
            tier=document.tier,
            stance=CivicEvidenceStance.CORROBORATING,
            now=now,
            now_fn=now_fn,
            publisher=profile.publisher,
            excerpt=document.excerpt,
            published_at=document.published_at,
            publisher_id=profile.publisher_id,
            source_kind=(
                profile.source_kind.value if profile.source_kind is not None else None
            ),
            party_slug=profile.party_slug,
            subject_id=claim.subject_id,
            claim_category=claim.category.value,
            event_date=event_date_for_document(document, watch),
            canonical_url=document.canonical_url,
            accessible_full_text=_accessible_full_text(document, profile),
            paywalled=False,
            anonymous_source_dependency=_anonymous_dependency(profile),
            source_metadata=document.raw_metadata,
        )
        if evidence is None:
            return None
        written.append(evidence)
    return tuple(written)


__all__ = [
    "CivicHydrationResult",
    "CivicHydrationStatus",
    "PARTY_ALIASES",
    "PartyMediaDocumentHydrator",
    "PartySubjectResolution",
    "PartySubjectResolutionStatus",
    "CivicSourceProfile",
    "build_ownership_registry",
    "build_source_profiles",
    "category_for_documents",
    "construct_candidate_claim",
    "event_date_for_cluster",
    "filter_documents_for_vocabulary",
    "persist_cluster_evidence",
    "persist_research_evidence",
    "resolve_candidate_party_subject",
    "resolve_cluster_party_subject",
    "resolve_party_subject",
]
