"""The Civic evidence policy engine: may this Candidate be published, and on what evidence?

One pure function decides it -- `qualify_candidate` -- and everything it needs is passed in.
No network, no database, no AI, no clock: the same Candidate, the same evidence, the same
publisher registry and the same dedup context always produce the same decision, which is what
lets a published statement be re-justified months later from stored inputs alone.

The rules are plan sections 3.1, 3.2, 4.1, 4.2, 4.3 and 7.6, applied in a fixed order:

1. **Admissibility.** Each evidence item is classified against the publisher registry first.
   Inadmissible material is *removed*, never a veto: it does not count towards the rules, and it
   cannot hold back a Candidate the admitted evidence already establishes. Discovery material
   (aggregators, forums, social syndication) can never be evidence, so it must not be able to
   explain why a Candidate that still holds real evidence failed. A media publisher with no
   registered ownership group is removed for the same reason the registry exists: two outlets
   under one owner are one publisher, and an unknown owner cannot be shown to be independent of
   anything. Removed material speaks only when what remains is not enough.
2. **Scope.** A record kind or category outside the initial automated scope defers as
   `deferred_unsupported`. It is never coerced into a record type PartyMap happens to have.
3. **Significance.** Opinion, polls and live blogs are hard exclusions, as is anything that is
   not durable. These are screened out, and a screened-out Candidate cites nothing: its evidence
   is all excluded because the subject matter itself can never be published.
4. **Party attribution.** A party primary source speaks for its own party only. When one party's
   characterization of another party's policy is all there is, the Candidate is screened out.
5. **Duplication.** Already sealed or already published means excluded, before any evidence
   strength is considered, because a duplicate is not something more research can fix.
6. **Evidence strength.** Category decides which tiers can establish the claim: T0 for legal or
   parliamentary state, T0-T2 for an official finding, the matching party's T3 for a policy, and
   reputable media alone only for a durable party timeline event -- and then only under the full
   plan 4.2 rule 4 test (two ownership groups, two ownership clusters, one accessible full
   article, no anonymous-source dependency, agreement on subject, event date and category).

Everything unknown fails closed. An unregistered publisher, an owner the registry does not
carry, a tier that cannot establish the category, a category with no rule: each defers or
excludes rather than being given the benefit of the doubt.

**Reason codes are the output, not a log line.** Every decision carries codes from
`beehive.civic.reasons`, ordered deterministically, and a decision may only carry codes whose
disposition matches it: a publishable Candidate carries allow codes, a deferral carries defer
codes, an exclusion carries block codes. The engine emits *every* applicable code for the
decision it reached rather than only the one that happened to decide it, so an explanation never
depends on which rule fired first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Mapping, Sequence

from beehive.civic.dedup import CivicDedupContext, CivicDedupDecision, evaluate_dedup
from beehive.civic.documents import CivicDocument
from beehive.civic.reasons import (CivicPolicyReason, CivicReasonDisposition, order_reasons,
                                   require_disposition)
from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier

# Plan 7.7: one bounded extra research pass per Candidate, ever. Not a retry budget that grows
# with attempts -- the second pass exists to close a specific evidence gap, and a Candidate that
# still cannot be evidenced after it defers with its evidence intact.
MAX_RESEARCH_ATTEMPTS = 1


class CivicSourceKind(str, Enum):
    """The destination dataset's `SourceKind`. Reused verbatim rather than re-spelled, because a
    Civic Evidence row becomes a PartyMap citation with this exact value."""

    ELECTORAL_COMMISSION = "electoral-commission"
    GOVERNMENT = "government"
    INDEPENDENT_NEWS = "independent-news"
    LEGISLATION = "legislation"
    OFFICIAL_REVIEW = "official-review"
    OFFICIAL_STATISTICS = "official-statistics"
    PARLIAMENT = "parliament"
    PARTY_PRIMARY = "party-primary"


class CivicSourceTier(str, Enum):
    """Plan 4.1's source hierarchy, strongest first.

    This is the plan's own vocabulary, not a second one: `CivicEvidenceTier` in
    `beehive.domain.civic` records how strong a source is (official, party primary, reputable
    media...), while a tier records *which* official source it is, and the evidence rules need
    that distinction -- a legal state needs a constitutive record (T0) and an audit finding does
    not (T0-T2), yet both are `CivicEvidenceTier.OFFICIAL`. A tier is therefore always derived
    from the source kind by `source_tier_for`, never supplied, so nothing can promote itself."""

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    T5 = "T5"

    @property
    def rank(self) -> int:
        """0 is strongest. T5 is discovery-only material and is never evidence."""
        return int(self.value[1:])


_TIER_BY_SOURCE_KIND: dict[CivicSourceKind, CivicSourceTier] = {
    CivicSourceKind.ELECTORAL_COMMISSION: CivicSourceTier.T0,
    CivicSourceKind.PARLIAMENT: CivicSourceTier.T0,
    CivicSourceKind.LEGISLATION: CivicSourceTier.T0,
    CivicSourceKind.GOVERNMENT: CivicSourceTier.T1,
    CivicSourceKind.OFFICIAL_STATISTICS: CivicSourceTier.T1,
    CivicSourceKind.OFFICIAL_REVIEW: CivicSourceTier.T2,
    CivicSourceKind.PARTY_PRIMARY: CivicSourceTier.T3,
    CivicSourceKind.INDEPENDENT_NEWS: CivicSourceTier.T4,
}

# Which `CivicEvidenceTier` a source of each tier may claim. A T0 register describing itself as
# reputable media, or a party release describing itself as official, is a contradiction between
# two application-controlled values and fails closed at construction.
_EVIDENCE_TIERS_BY_TIER: dict[CivicSourceTier, frozenset[CivicEvidenceTier]] = {
    CivicSourceTier.T0: frozenset({CivicEvidenceTier.OFFICIAL}),
    CivicSourceTier.T1: frozenset({CivicEvidenceTier.OFFICIAL}),
    CivicSourceTier.T2: frozenset({CivicEvidenceTier.OFFICIAL}),
    CivicSourceTier.T3: frozenset({CivicEvidenceTier.PARTY_PRIMARY}),
    CivicSourceTier.T4: frozenset({CivicEvidenceTier.REPUTABLE_MEDIA,
                                   CivicEvidenceTier.OTHER_MEDIA}),
    CivicSourceTier.T5: frozenset({CivicEvidenceTier.AGGREGATOR}),
}


def source_tier_for(source_kind: CivicSourceKind | None) -> CivicSourceTier:
    """The plan 4.1 tier of a source kind. No PartyMap source kind means discovery-only (T5)."""
    if source_kind is None:
        return CivicSourceTier.T5
    return _TIER_BY_SOURCE_KIND[source_kind]


class CivicPublisherKind(str, Enum):
    OFFICIAL = "official"
    PARTY = "party"
    MEDIA = "media"
    DISCOVERY = "discovery"


class CivicRecordKind(str, Enum):
    """The PartyMap record a Candidate would create or change."""

    CORRECTION = "correction"
    ELECTORATE_BOUNDARY = "electorate-boundary"
    PARTY_REGISTRATION = "party-registration"
    POLICY = "policy"
    PROMISE_FINDING = "promise-finding"
    TIMELINE = "timeline"


# Plan 3.1. Everything else is plan 3.2 deferred scope and defers as `deferred_unsupported`.
SUPPORTED_RECORD_KINDS: frozenset[CivicRecordKind] = frozenset({
    CivicRecordKind.CORRECTION, CivicRecordKind.POLICY, CivicRecordKind.PROMISE_FINDING,
    CivicRecordKind.TIMELINE})


class CivicRecordOperation(str, Enum):
    ADD = "add"
    CORRECT = "correct"
    RETRACT = "retract"


class CivicContentClass(str, Enum):
    """What the material *is*, independently of what it is about. Only FACTUAL may publish."""

    FACTUAL = "factual"
    LIVE_BLOG = "live-blog"
    OPINION = "opinion"
    POLL = "poll"


class CivicClaimCategory(str, Enum):
    CORRECTION = "correction"
    ELECTORAL_ADMINISTRATION = "electoral-administration"
    ELECTORATE_BOUNDARY = "electorate-boundary"
    LEGISLATION = "legislation"
    LIVE_BLOG = "live-blog"
    OFFICIAL_FINDING = "official-finding"
    OPINION = "opinion"
    PARTY_POLICY = "party-policy"
    PARTY_TIMELINE_EVENT = "party-timeline-event"
    POLL = "poll"


@dataclass(frozen=True)
class _CategoryRule:
    """Which tiers establish a category, and what it means when none of them are present."""

    publishable: bool
    sufficient_tiers: frozenset[CivicSourceTier]
    media_only_publishable: bool
    requires_matching_party: bool
    insufficient_reason: CivicPolicyReason | None


_T0 = CivicSourceTier.T0
_T1 = CivicSourceTier.T1
_T2 = CivicSourceTier.T2
_T3 = CivicSourceTier.T3
_T4 = CivicSourceTier.T4

_CATEGORY_RULES: dict[CivicClaimCategory, _CategoryRule] = {
    # Plan 3.1: a correction carries the same evidence bar as the record it corrects.
    CivicClaimCategory.CORRECTION: _CategoryRule(
        publishable=True, sufficient_tiers=frozenset({_T0, _T1, _T2, _T3}),
        media_only_publishable=False, requires_matching_party=False, insufficient_reason=None),
    # Plan 4.1: electoral administration state comes from a T0 or T1 official source.
    CivicClaimCategory.ELECTORAL_ADMINISTRATION: _CategoryRule(
        publishable=True, sufficient_tiers=frozenset({_T0, _T1}), media_only_publishable=False,
        requires_matching_party=False,
        insufficient_reason=CivicPolicyReason.EVIDENCE_OFFICIAL_SOURCE_REQUIRED),
    # Plan 3.2: electorate facts are outside the initial automated scope.
    CivicClaimCategory.ELECTORATE_BOUNDARY: _CategoryRule(
        publishable=False, sufficient_tiers=frozenset(), media_only_publishable=False,
        requires_matching_party=False, insufficient_reason=None),
    # Plan 4.2 rule 2: a legal or parliamentary state requires a T0 source.
    CivicClaimCategory.LEGISLATION: _CategoryRule(
        publishable=True, sufficient_tiers=frozenset({_T0}), media_only_publishable=False,
        requires_matching_party=False,
        insufficient_reason=CivicPolicyReason.EVIDENCE_OFFICIAL_SOURCE_REQUIRED),
    CivicClaimCategory.LIVE_BLOG: _CategoryRule(
        publishable=False, sufficient_tiers=frozenset(), media_only_publishable=False,
        requires_matching_party=False, insufficient_reason=None),
    # Plan 4.2 rule 3: an outcome or official finding requires T0 to T2 evidence.
    CivicClaimCategory.OFFICIAL_FINDING: _CategoryRule(
        publishable=True, sufficient_tiers=frozenset({_T0, _T1, _T2}),
        media_only_publishable=False, requires_matching_party=False,
        insufficient_reason=(
            CivicPolicyReason.EVIDENCE_OFFICIAL_FINDING_REQUIRES_OFFICIAL_SOURCE)),
    CivicClaimCategory.OPINION: _CategoryRule(
        publishable=False, sufficient_tiers=frozenset(), media_only_publishable=False,
        requires_matching_party=False, insufficient_reason=None),
    # Plan 4.2 rule 1: a party policy, promise or position requires that party's T3 source.
    CivicClaimCategory.PARTY_POLICY: _CategoryRule(
        publishable=True, sufficient_tiers=frozenset({_T3}), media_only_publishable=False,
        requires_matching_party=True,
        insufficient_reason=CivicPolicyReason.EVIDENCE_PARTY_PRIMARY_REQUIRED),
    # Plan 3.1 durable party timeline event, established through plan 4.2 rule 4.
    CivicClaimCategory.PARTY_TIMELINE_EVENT: _CategoryRule(
        publishable=True, sufficient_tiers=frozenset({_T0, _T1, _T2, _T3, _T4}),
        media_only_publishable=True, requires_matching_party=False, insufficient_reason=None),
    CivicClaimCategory.POLL: _CategoryRule(
        publishable=False, sufficient_tiers=frozenset(), media_only_publishable=False,
        requires_matching_party=False, insufficient_reason=None),
}

# Plan 4.3 hard exclusions: material that can never be stated as a neutral factual state change.
# Distinguished from a merely out-of-scope category, which defers instead of being screened out.
HARD_EXCLUDED_CATEGORIES: frozenset[CivicClaimCategory] = frozenset({
    CivicClaimCategory.LIVE_BLOG, CivicClaimCategory.OPINION, CivicClaimCategory.POLL})


class CivicEvidenceExclusion(str, Enum):
    """Why one evidence item was not cited.

    SCREENED_OUT is the whole-Candidate case: material that can never be published cites nothing,
    so its evidence is excluded even though each item might be individually admissible."""

    DISCOVERY_ONLY = "discovery_only"
    UNKNOWN_OWNERSHIP = "unknown_ownership"
    PARTY_MISMATCH = "party_mismatch"
    SUBJECT_MISMATCH = "subject_mismatch"
    EVENT_DATE_MISMATCH = "event_date_mismatch"
    CATEGORY_MISMATCH = "category_mismatch"
    SCREENED_OUT = "screened_out"


# Exclusions that took admissible evidence away from the corroboration count. They never veto:
# the media rule is decided on the admitted evidence first, and these codes only speak when that
# evidence fails the independence test -- and then they speak in place of the ownership
# arithmetic, because "the second report described a different event" is the real finding and
# "only one ownership group" is merely its consequence.
_REDUCING_EXCLUSIONS: dict[CivicEvidenceExclusion, CivicPolicyReason] = {
    CivicEvidenceExclusion.UNKNOWN_OWNERSHIP: (
        CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP),
    CivicEvidenceExclusion.SUBJECT_MISMATCH: CivicPolicyReason.CORROBORATION_SUBJECT_MISMATCH,
    CivicEvidenceExclusion.EVENT_DATE_MISMATCH: (
        CivicPolicyReason.CORROBORATION_EVENT_DATE_MISMATCH),
    CivicEvidenceExclusion.CATEGORY_MISMATCH: CivicPolicyReason.CORROBORATION_CATEGORY_MISMATCH,
}


class CivicQualificationOutcome(str, Enum):
    """The Candidate-level outcome recorded against a Civic Run."""

    QUALIFIED = "qualified"
    SCREENED_OUT = "screened_out"
    DUPLICATE = "duplicate"
    DEFERRED_EVIDENCE = "deferred_evidence"
    DEFERRED_UNSUPPORTED = "deferred_unsupported"
    DEFERRED_CAPACITY = "deferred_capacity"


class CivicPublicationDisposition(str, Enum):
    """The coarse decision: publish, hold, hold as out of scope, or never publish."""

    PUBLISHABLE = "publishable"
    DEFERRED = "deferred"
    DEFERRED_UNSUPPORTED = "deferred_unsupported"
    EXCLUDED = "excluded"


_DISPOSITION_BY_OUTCOME: dict[CivicQualificationOutcome, CivicPublicationDisposition] = {
    CivicQualificationOutcome.QUALIFIED: CivicPublicationDisposition.PUBLISHABLE,
    CivicQualificationOutcome.SCREENED_OUT: CivicPublicationDisposition.EXCLUDED,
    CivicQualificationOutcome.DUPLICATE: CivicPublicationDisposition.EXCLUDED,
    CivicQualificationOutcome.DEFERRED_EVIDENCE: CivicPublicationDisposition.DEFERRED,
    CivicQualificationOutcome.DEFERRED_UNSUPPORTED: (
        CivicPublicationDisposition.DEFERRED_UNSUPPORTED),
    CivicQualificationOutcome.DEFERRED_CAPACITY: CivicPublicationDisposition.DEFERRED,
}

_ALLOWED_DISPOSITIONS: dict[CivicPublicationDisposition, frozenset[CivicReasonDisposition]] = {
    CivicPublicationDisposition.PUBLISHABLE: frozenset({CivicReasonDisposition.ALLOW}),
    CivicPublicationDisposition.DEFERRED: frozenset({CivicReasonDisposition.DEFER}),
    CivicPublicationDisposition.DEFERRED_UNSUPPORTED: frozenset({CivicReasonDisposition.DEFER}),
    CivicPublicationDisposition.EXCLUDED: frozenset({CivicReasonDisposition.BLOCK}),
}

# Defer codes a second research pass could plausibly close. `record_kind_unsupported` is not one
# of them: no amount of research makes an out-of-scope record type publishable, and spending the
# single bounded attempt on it would burn the budget that a real evidence gap needs.
RESEARCH_RETRYABLE_REASONS: frozenset[CivicPolicyReason] = frozenset({
    CivicPolicyReason.CORROBORATION_CATEGORY_MISMATCH,
    CivicPolicyReason.CORROBORATION_EVENT_DATE_MISMATCH,
    CivicPolicyReason.CORROBORATION_SUBJECT_MISMATCH,
    CivicPolicyReason.EVIDENCE_ANONYMOUS_SOURCE_DEPENDENCY,
    CivicPolicyReason.EVIDENCE_DISCOVERY_ONLY_SOURCE,
    CivicPolicyReason.EVIDENCE_INSUFFICIENT,
    CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE,
    CivicPolicyReason.EVIDENCE_OFFICIAL_FINDING_REQUIRES_OFFICIAL_SOURCE,
    CivicPolicyReason.EVIDENCE_OFFICIAL_SOURCE_REQUIRED,
    CivicPolicyReason.EVIDENCE_PARTY_PRIMARY_REQUIRED,
    CivicPolicyReason.EVIDENCE_PAYWALLED_WITHOUT_ACCESSIBLE_CORROBORATION,
    CivicPolicyReason.EVIDENCE_SAME_OWNERSHIP_CLUSTER,
    CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP,
    CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP,
})


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value


@dataclass(frozen=True)
class CivicPublisher:
    """One entry in the publisher registry.

    Ownership is the whole point of the registry. `ownership_group` is the masthead's owner and
    `ownership_cluster` is the level above it: two public broadcasters with separate ownership
    groups but one ultimate owner are one publisher for corroboration, which is exactly what a
    group-only check would miss."""

    publisher_id: str
    name: str
    kind: CivicPublisherKind
    source_kind: CivicSourceKind | None = None
    ownership_group: str | None = None
    ownership_cluster: str | None = None
    registered: bool = True
    party_slug: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.publisher_id, "publisher id")
        _require_text(self.name, "publisher name")
        if self.kind is not CivicPublisherKind.MEDIA:
            if self.ownership_group is not None or self.ownership_cluster is not None:
                raise ValueError(
                    f"{self.publisher_id}: only a media publisher carries ownership")
        elif self.registered and (self.ownership_group is None or self.ownership_cluster is None):
            raise ValueError(
                f"{self.publisher_id}: a registered media publisher needs an ownership group "
                f"and an ownership cluster")
        if self.kind is CivicPublisherKind.PARTY:
            _require_text(self.party_slug or "", f"{self.publisher_id} party_slug")
        elif self.party_slug is not None:
            raise ValueError(f"{self.publisher_id}: only a party publisher carries a party_slug")
        if self.kind is CivicPublisherKind.DISCOVERY and self.source_kind is not None:
            raise ValueError(
                f"{self.publisher_id}: a discovery publisher has no PartyMap source kind")

    @property
    def is_evidence_capable(self) -> bool:
        """Whether this publisher can ever appear in a citation. Discovery never can, and an
        unregistered outlet fails closed because independence cannot be shown for it."""
        if self.kind is CivicPublisherKind.DISCOVERY or not self.registered:
            return False
        if self.kind is CivicPublisherKind.MEDIA:
            return self.ownership_group is not None and self.ownership_cluster is not None
        return True


@dataclass(frozen=True)
class CivicOwnershipRegistry:
    """The application's own list of who owns which masthead.

    A publisher that is not in here is unknown, and unknown fails closed: it is excluded from
    evidence rather than counted as one more independent voice."""

    publishers: tuple[CivicPublisher, ...] = ()

    def __post_init__(self) -> None:
        by_id: dict[str, CivicPublisher] = {}
        for publisher in self.publishers:
            if publisher.publisher_id in by_id:
                raise ValueError(f"duplicate publisher id: {publisher.publisher_id}")
            by_id[publisher.publisher_id] = publisher
        object.__setattr__(self, "_by_id", by_id)

    @classmethod
    def build(cls, publishers: Iterable[CivicPublisher]) -> CivicOwnershipRegistry:
        return cls(tuple(publishers))

    def get(self, publisher_id: str) -> CivicPublisher | None:
        mapping: Mapping[str, CivicPublisher] = getattr(self, "_by_id")
        return mapping.get(publisher_id)


@dataclass(frozen=True)
class CivicEvidenceItem:
    """One piece of evidence offered for a Candidate, before any rule has looked at it.

    The accessibility flags are facts the fetcher established, not judgments: `paywalled` and
    `accessible_full_text` are separate because an archive copy of a paywalled article is
    accessible, and a plain fetch failure is inaccessible without being paywalled."""

    evidence_id: str
    publisher_id: str
    source_kind: CivicSourceKind | None
    evidence_tier: CivicEvidenceTier
    stance: CivicEvidenceStance
    canonical_url: str
    title: str
    subject_id: str
    event_date: date
    category: CivicClaimCategory
    published_at: datetime
    accessible_full_text: bool
    paywalled: bool
    anonymous_source_dependency: bool
    discovery_only: bool = False
    party_slug: str | None = None
    url: str = ""
    excerpt: str = ""

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence id")
        _require_text(self.publisher_id, f"{self.evidence_id} publisher id")
        _require_text(self.canonical_url, f"{self.evidence_id} canonical_url")
        _require_text(self.title, f"{self.evidence_id} title")
        _require_text(self.subject_id, f"{self.evidence_id} subject id")
        if not isinstance(self.event_date, date) or isinstance(self.event_date, datetime):
            raise ValueError(f"{self.evidence_id}: event_date must be a date")
        if not isinstance(self.published_at, datetime):
            raise ValueError(f"{self.evidence_id}: published_at must be a datetime")
        if self.published_at.tzinfo is None or (
                self.published_at.tzinfo.utcoffset(self.published_at) is None):
            raise ValueError(f"{self.evidence_id}: published_at must be timezone-aware")
        tier = self.source_tier
        if self.evidence_tier not in _EVIDENCE_TIERS_BY_TIER[tier]:
            raise ValueError(
                f"{self.evidence_id}: {tier.value} evidence cannot be "
                f"{self.evidence_tier.value}")
        if (self.source_kind is None) is not self.discovery_only:
            raise ValueError(
                f"{self.evidence_id}: discovery-only material is exactly the material with no "
                f"PartyMap source kind")
        if self.source_kind is CivicSourceKind.PARTY_PRIMARY:
            _require_text(self.party_slug or "", f"{self.evidence_id} party_slug")
        elif self.party_slug is not None:
            raise ValueError(
                f"{self.evidence_id}: only a party primary source carries a party_slug")

    @property
    def source_tier(self) -> CivicSourceTier:
        return source_tier_for(self.source_kind)

    @classmethod
    def from_document(
        cls,
        document: CivicDocument,
        *,
        evidence_id: str,
        publisher_id: str,
        source_kind: CivicSourceKind | None,
        stance: CivicEvidenceStance,
        subject_id: str,
        event_date: date,
        category: CivicClaimCategory,
        accessible_full_text: bool,
        paywalled: bool = False,
        anonymous_source_dependency: bool = False,
        party_slug: str | None = None,
    ) -> CivicEvidenceItem:
        """Lift a fetched `CivicDocument` into evidence.

        The document supplies what it actually observed -- canonical URL, title, excerpt, tier,
        and `effective_at` as the publication instant -- and the caller supplies what only the
        screening stage knows: which Candidate this is evidence for and how accessible it was."""
        return cls(
            evidence_id=evidence_id,
            publisher_id=publisher_id,
            source_kind=source_kind,
            evidence_tier=document.tier,
            stance=stance,
            canonical_url=document.canonical_url,
            title=document.title,
            subject_id=subject_id,
            event_date=event_date,
            category=category,
            published_at=document.effective_at,
            accessible_full_text=accessible_full_text,
            paywalled=paywalled,
            anonymous_source_dependency=anonymous_source_dependency,
            discovery_only=source_kind is None,
            party_slug=party_slug,
            url=document.canonical_url,
            excerpt=document.excerpt)


@dataclass(frozen=True)
class CivicCandidateClaim:
    """The statement a Candidate proposes, in the terms the policy decides on.

    `fingerprint` is the cross-day identity of the underlying development (the same role
    `CivicCandidate.dedupe_key` plays in the persisted domain), which is what the dedup windows
    compare."""

    candidate_id: str
    fingerprint: str
    record_kind: CivicRecordKind
    operation: CivicRecordOperation
    subject_id: str
    record_key: str
    event_date: date
    category: CivicClaimCategory
    content_class: CivicContentClass
    durable: bool
    claim_text: str

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate id")
        _require_text(self.fingerprint, f"{self.candidate_id} fingerprint")
        _require_text(self.subject_id, f"{self.candidate_id} subject id")
        _require_text(self.record_key, f"{self.candidate_id} record key")
        _require_text(self.claim_text, f"{self.candidate_id} claim text")
        if not isinstance(self.event_date, date) or isinstance(self.event_date, datetime):
            raise ValueError(f"{self.candidate_id}: event_date must be a date")


@dataclass(frozen=True)
class CivicEvidenceAssessment:
    """What the admissibility pass decided about one evidence item."""

    evidence_id: str
    publisher_id: str
    publisher_kind: CivicPublisherKind | None
    source_tier: CivicSourceTier
    admitted: bool
    exclusion: CivicEvidenceExclusion | None = None
    ownership_group: str | None = None
    ownership_cluster: str | None = None


@dataclass(frozen=True)
class CivicQualification:
    """One Candidate's publication decision, with everything needed to justify it later."""

    candidate_id: str
    disposition: CivicPublicationDisposition
    outcome: CivicQualificationOutcome
    reasons: tuple[CivicPolicyReason, ...]
    evidence_used: tuple[str, ...]
    evidence_excluded: tuple[str, ...]
    ownership_groups: tuple[str, ...]
    ownership_clusters: tuple[str, ...]
    assessments: tuple[CivicEvidenceAssessment, ...]
    dedup: CivicDedupDecision | None = None

    def __post_init__(self) -> None:
        require_disposition(self.reasons, _ALLOWED_DISPOSITIONS[self.disposition])
        if _DISPOSITION_BY_OUTCOME[self.outcome] is not self.disposition:
            raise ValueError(
                f"{self.candidate_id}: outcome {self.outcome.value} does not belong to "
                f"disposition {self.disposition.value}")
        if not self.reasons:
            raise ValueError(f"{self.candidate_id}: a decision needs at least one reason")

    @property
    def is_publishable(self) -> bool:
        return self.disposition is CivicPublicationDisposition.PUBLISHABLE

    @property
    def research_retry_eligible(self) -> bool:
        """Whether the single bounded research pass could plausibly change this decision.

        Only an evidence deferral qualifies. An exclusion, a duplicate and an out-of-scope record
        kind are all decided by something more evidence cannot alter."""
        return (self.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
                and bool(set(self.reasons) & RESEARCH_RETRYABLE_REASONS))


@dataclass(frozen=True)
class CivicResearchQualification:
    """The Candidate's decision across its one bounded research attempt.

    `before` is the decision that justified spending the attempt and `after` is the decision that
    stands. When no attempt was warranted the two are the same object."""

    before: CivicQualification
    after: CivicQualification
    attempts_used: int
    additional_evidence_ids: tuple[str, ...] = ()
    max_attempts: int = MAX_RESEARCH_ATTEMPTS

    def __post_init__(self) -> None:
        if not 0 <= self.attempts_used <= self.max_attempts:
            raise ValueError(
                f"{self.before.candidate_id}: research attempts {self.attempts_used} exceed the "
                f"bound of {self.max_attempts}")


@dataclass(frozen=True)
class _EvidenceVerdict:
    satisfied: bool
    allow: frozenset[CivicPolicyReason]
    failure: frozenset[CivicPolicyReason]


def _assess(
    item: CivicEvidenceItem, candidate: CivicCandidateClaim, registry: CivicOwnershipRegistry,
) -> CivicEvidenceAssessment:
    """Classify one item. The first failing check wins, strongest disqualifier first: material
    that can never be evidence, then material whose independence cannot be established, then
    material that is about something else."""
    publisher = registry.get(item.publisher_id)
    tier = item.source_tier
    group = publisher.ownership_group if publisher is not None else None
    cluster = publisher.ownership_cluster if publisher is not None else None

    def excluded(reason: CivicEvidenceExclusion) -> CivicEvidenceAssessment:
        return CivicEvidenceAssessment(
            evidence_id=item.evidence_id, publisher_id=item.publisher_id,
            publisher_kind=publisher.kind if publisher is not None else None,
            source_tier=tier, admitted=False, exclusion=reason,
            ownership_group=group, ownership_cluster=cluster)

    if (item.discovery_only or tier is CivicSourceTier.T5
            or (publisher is not None and publisher.kind is CivicPublisherKind.DISCOVERY)):
        return excluded(CivicEvidenceExclusion.DISCOVERY_ONLY)
    if publisher is None or not publisher.is_evidence_capable:
        return excluded(CivicEvidenceExclusion.UNKNOWN_OWNERSHIP)
    if publisher.kind is CivicPublisherKind.MEDIA and (
            item.evidence_tier is not CivicEvidenceTier.REPUTABLE_MEDIA):
        return excluded(CivicEvidenceExclusion.UNKNOWN_OWNERSHIP)
    if publisher.source_kind is not None and publisher.source_kind is not item.source_kind:
        return excluded(CivicEvidenceExclusion.UNKNOWN_OWNERSHIP)
    if tier is CivicSourceTier.T3 and (
            item.party_slug != candidate.subject_id
            or (publisher.party_slug is not None and publisher.party_slug != item.party_slug)):
        return excluded(CivicEvidenceExclusion.PARTY_MISMATCH)
    if item.subject_id != candidate.subject_id:
        return excluded(CivicEvidenceExclusion.SUBJECT_MISMATCH)
    if item.event_date != candidate.event_date:
        return excluded(CivicEvidenceExclusion.EVENT_DATE_MISMATCH)
    if item.category is not candidate.category:
        return excluded(CivicEvidenceExclusion.CATEGORY_MISMATCH)
    return CivicEvidenceAssessment(
        evidence_id=item.evidence_id, publisher_id=item.publisher_id, publisher_kind=publisher.kind,
        source_tier=tier, admitted=True, ownership_group=group, ownership_cluster=cluster)


def _media_verdict(
    admitted: Sequence[tuple[CivicEvidenceItem, CivicEvidenceAssessment]],
    exclusions: frozenset[CivicEvidenceExclusion],
) -> _EvidenceVerdict:
    """Plan 4.2 rule 4, decided on the admitted evidence alone.

    Inadmissible material is *removed*, never a veto. An unregistered outlet, a publisher the
    registry has never heard of, a lesser media tier or a report about a different event cannot
    stop two admitted independent publishers from establishing the fact -- it simply does not
    count towards them. That is also what lets one bounded research pass rescue a Candidate: the
    excluded item stays attached and excluded while the newly found publisher supplies the second
    ownership group.

    Those exclusions only speak when the admitted evidence fails the independence test, and then
    they speak instead of the arithmetic: "the second report described a different event" is the
    finding, and "only one ownership group" is merely its consequence. Accessibility, paywall and
    anonymous-source failures are properties of the admitted evidence itself, so they always
    report their own code."""
    media = [
        (item, assessment) for item, assessment in admitted
        if assessment.publisher_kind is CivicPublisherKind.MEDIA]
    reducing = frozenset(
        reason for exclusion, reason in _REDUCING_EXCLUSIONS.items() if exclusion in exclusions)
    if not media:
        if reducing:
            return _EvidenceVerdict(False, frozenset(), reducing)
        if CivicEvidenceExclusion.DISCOVERY_ONLY in exclusions:
            return _EvidenceVerdict(
                False, frozenset(),
                frozenset({CivicPolicyReason.EVIDENCE_DISCOVERY_ONLY_SOURCE}))
        return _EvidenceVerdict(False, frozenset(), frozenset())

    paywalled = [item for item, _ in media if item.paywalled]
    accessible = [item for item, _ in media if item.accessible_full_text]
    if not accessible:
        reason = (CivicPolicyReason.EVIDENCE_PAYWALLED_WITHOUT_ACCESSIBLE_CORROBORATION
                  if paywalled else CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE)
        return _EvidenceVerdict(False, frozenset(), frozenset({reason}))
    if any(item.anonymous_source_dependency for item, _ in media):
        return _EvidenceVerdict(
            False, frozenset(),
            frozenset({CivicPolicyReason.EVIDENCE_ANONYMOUS_SOURCE_DEPENDENCY}))

    groups = {assessment.ownership_group for _, assessment in media}
    clusters = {assessment.ownership_cluster for _, assessment in media}
    if len(groups) < 2 or len(clusters) < 2:
        if reducing:
            return _EvidenceVerdict(False, frozenset(), reducing)
        reason = (CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP if len(groups) < 2
                  else CivicPolicyReason.EVIDENCE_SAME_OWNERSHIP_CLUSTER)
        return _EvidenceVerdict(False, frozenset(), frozenset({reason}))

    allow = {
        CivicPolicyReason.EVIDENCE_MEDIA_TWO_OWNERSHIP_GROUPS,
        CivicPolicyReason.EVIDENCE_ACCESSIBLE_FULL_ARTICLE,
        CivicPolicyReason.EVIDENCE_NAMED_SOURCES_ONLY,
        CivicPolicyReason.CORROBORATION_SUBJECT_MATCH,
        CivicPolicyReason.CORROBORATION_EVENT_DATE_MATCH,
        CivicPolicyReason.CORROBORATION_CATEGORY_MATCH,
    }
    if paywalled:
        allow.add(CivicPolicyReason.EVIDENCE_PAYWALL_CORROBORATED)
    return _EvidenceVerdict(True, frozenset(allow), frozenset())


def _evidence_verdict(
    candidate: CivicCandidateClaim,
    admitted: Sequence[tuple[CivicEvidenceItem, CivicEvidenceAssessment]],
    exclusions: frozenset[CivicEvidenceExclusion],
) -> _EvidenceVerdict:
    rule = _CATEGORY_RULES[candidate.category]
    if rule.requires_matching_party:
        # Belt and braces with the admissibility pass: a category that names one party may only
        # ever be established by that party's own primary source.
        qualifying = [
            (item, assessment) for item, assessment in admitted
            if assessment.source_tier is _T3 and item.party_slug == candidate.subject_id]
    else:
        qualifying = [
            (item, assessment) for item, assessment in admitted
            if assessment.source_tier is not CivicSourceTier.T4
            and assessment.source_tier in rule.sufficient_tiers]
    usable = [
        (item, assessment)
        for item, assessment in qualifying
        if (
            assessment.source_tier is not _T3
            or (item.accessible_full_text and not item.paywalled)
        )
    ]
    allow: set[CivicPolicyReason] = set()
    if any(assessment.source_tier.rank <= _T2.rank for _, assessment in usable):
        allow.add(CivicPolicyReason.EVIDENCE_OFFICIAL_SINGLETON)
    if any(assessment.source_tier is _T3 for _, assessment in usable):
        # An admitted T3 item already proved it speaks for this Candidate's own party: a party
        # primary source for anyone else is excluded during admissibility.
        allow.add(CivicPolicyReason.EVIDENCE_PARTY_PRIMARY_SINGLETON)
    if allow:
        return _EvidenceVerdict(True, frozenset(allow), frozenset())
    if rule.media_only_publishable:
        media = _media_verdict(admitted, exclusions)
        if media.satisfied or media.failure:
            return media
    inaccessible_party = [
        item for item, assessment in qualifying
        if assessment.source_tier is _T3
        and (not item.accessible_full_text or item.paywalled)
    ]
    if inaccessible_party:
        reason = (
            CivicPolicyReason.EVIDENCE_PAYWALLED_WITHOUT_ACCESSIBLE_CORROBORATION
            if any(item.paywalled for item in inaccessible_party)
            else CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE
        )
        return _EvidenceVerdict(False, frozenset(), frozenset({reason}))
    failure = frozenset() if rule.insufficient_reason is None else frozenset(
        {rule.insufficient_reason})
    return _EvidenceVerdict(False, frozenset(), failure)


def _result(
    candidate: CivicCandidateClaim,
    outcome: CivicQualificationOutcome,
    reasons: Iterable[CivicPolicyReason],
    assessments: Sequence[CivicEvidenceAssessment],
    used_ids: Iterable[str],
    dedup: CivicDedupDecision | None = None,
) -> CivicQualification:
    used = frozenset(used_ids)
    ordered = sorted(assessments, key=lambda assessment: assessment.evidence_id)
    cited = [assessment for assessment in ordered if assessment.evidence_id in used]
    groups = sorted({
        assessment.ownership_group for assessment in cited
        if assessment.publisher_kind is CivicPublisherKind.MEDIA and assessment.ownership_group})
    clusters = sorted({
        assessment.ownership_cluster for assessment in cited
        if assessment.publisher_kind is CivicPublisherKind.MEDIA
        and assessment.ownership_cluster})
    return CivicQualification(
        candidate_id=candidate.candidate_id,
        disposition=_DISPOSITION_BY_OUTCOME[outcome],
        outcome=outcome,
        reasons=order_reasons(reasons),
        evidence_used=tuple(sorted(used)),
        evidence_excluded=tuple(sorted(
            assessment.evidence_id for assessment in assessments
            if assessment.evidence_id not in used)),
        ownership_groups=tuple(groups),
        ownership_clusters=tuple(clusters),
        assessments=tuple(ordered),
        dedup=dedup)


def qualify_candidate(
    candidate: CivicCandidateClaim,
    evidence: Iterable[CivicEvidenceItem],
    *,
    registry: CivicOwnershipRegistry,
    dedup_context: CivicDedupContext | None = None,
) -> CivicQualification:
    """Decide whether one Candidate may be published, and on which of its evidence.

    Pure: no clock, no network, no I/O. `dedup_context` carries the instant to compare against,
    so the same call decides the same way whenever it is made. Order of `evidence` never affects
    the decision."""
    items = tuple(evidence)
    seen: set[str] = set()
    for item in items:
        if item.evidence_id in seen:
            raise ValueError(f"duplicate evidence id: {item.evidence_id}")
        seen.add(item.evidence_id)

    assessments = tuple(_assess(item, candidate, registry) for item in items)
    admitted = tuple(
        (item, assessment) for item, assessment in zip(items, assessments) if assessment.admitted)
    admitted_ids = tuple(assessment.evidence_id for _, assessment in admitted)
    exclusions = frozenset(
        assessment.exclusion for assessment in assessments
        if assessment.exclusion is not None)

    rule = _CATEGORY_RULES[candidate.category]
    out_of_scope = (
        candidate.record_kind not in SUPPORTED_RECORD_KINDS
        or (not rule.publishable and candidate.category not in HARD_EXCLUDED_CATEGORIES))
    if out_of_scope:
        return _result(
            candidate, CivicQualificationOutcome.DEFERRED_UNSUPPORTED,
            {CivicPolicyReason.RECORD_KIND_UNSUPPORTED}, assessments, admitted_ids)

    screened: set[CivicPolicyReason] = set()
    if (candidate.category in HARD_EXCLUDED_CATEGORIES
            or candidate.content_class is not CivicContentClass.FACTUAL):
        screened.add(CivicPolicyReason.SIGNIFICANCE_EXCLUDED_CATEGORY)
    if not candidate.durable:
        screened.add(CivicPolicyReason.SIGNIFICANCE_NON_DURABLE)
    if screened:
        return _result(
            candidate, CivicQualificationOutcome.SCREENED_OUT, screened, assessments, ())

    if not admitted and CivicEvidenceExclusion.PARTY_MISMATCH in exclusions:
        return _result(
            candidate, CivicQualificationOutcome.SCREENED_OUT,
            {CivicPolicyReason.EVIDENCE_PARTY_MISMATCH}, assessments, ())

    allow: set[CivicPolicyReason] = {
        CivicPolicyReason.RECORD_KIND_SUPPORTED, CivicPolicyReason.SIGNIFICANCE_DURABLE}
    decision: CivicDedupDecision | None = None
    if dedup_context is not None:
        decision = evaluate_dedup(
            fingerprint=candidate.fingerprint,
            canonical_urls=[item.canonical_url for item in items],
            context=dedup_context)
        if decision.is_duplicate:
            return _result(
                candidate, CivicQualificationOutcome.DUPLICATE, {decision.reason}, assessments,
                admitted_ids, dedup=decision)
        allow.add(decision.reason)

    verdict = _evidence_verdict(candidate, admitted, exclusions)
    if verdict.satisfied:
        allow |= verdict.allow
        if CivicEvidenceExclusion.DISCOVERY_ONLY in exclusions:
            allow.add(CivicPolicyReason.DISCOVERY_RESOLVED_TO_PUBLISHER)
        return _result(
            candidate, CivicQualificationOutcome.QUALIFIED, allow, assessments, admitted_ids,
            dedup=decision)

    failure = set(verdict.failure)
    failure.add(CivicPolicyReason.EVIDENCE_INSUFFICIENT)
    return _result(
        candidate, CivicQualificationOutcome.DEFERRED_EVIDENCE, failure, assessments,
        admitted_ids, dedup=decision)


def qualify_with_bounded_research(
    candidate: CivicCandidateClaim,
    evidence: Iterable[CivicEvidenceItem],
    *,
    registry: CivicOwnershipRegistry,
    additional_evidence: Iterable[CivicEvidenceItem] = (),
    dedup_context: CivicDedupContext | None = None,
) -> CivicResearchQualification:
    """Qualify, and spend at most one research attempt when more evidence could close the gap.

    The engine never fetches anything: `additional_evidence` is what a research pass already
    found, and this function decides whether that pass was allowed to happen at all. A hard
    exclusion, a duplicate and an out-of-scope record kind never spend the attempt, so the extra
    evidence is ignored rather than being used to argue around a decision research cannot
    change."""
    items = tuple(evidence)
    extra = tuple(additional_evidence)
    before = qualify_candidate(
        candidate, items, registry=registry, dedup_context=dedup_context)
    if not extra or not before.research_retry_eligible:
        return CivicResearchQualification(before=before, after=before, attempts_used=0)

    after = qualify_candidate(
        candidate, items + extra, registry=registry, dedup_context=dedup_context)
    marker = (CivicPolicyReason.RESEARCH_RETRY_RESOLVED if after.is_publishable
              else CivicPolicyReason.RESEARCH_RETRY_EXHAUSTED)
    if after.outcome in (CivicQualificationOutcome.QUALIFIED,
                         CivicQualificationOutcome.DEFERRED_EVIDENCE):
        after = _result(
            candidate, after.outcome, set(after.reasons) | {marker}, after.assessments,
            after.evidence_used, dedup=after.dedup)
    return CivicResearchQualification(
        before=before, after=after, attempts_used=MAX_RESEARCH_ATTEMPTS,
        additional_evidence_ids=tuple(sorted(item.evidence_id for item in extra)))
