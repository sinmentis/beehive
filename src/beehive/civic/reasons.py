"""The closed reason-code vocabulary every Civic publication decision is explained with.

A decision that cannot be explained cannot be audited, so the evidence engine never returns a
free-form string: every qualification, deferral, exclusion and batch-cap decision names codes
from `CivicPolicyReason` and nothing else. Three properties are what make that useful:

* **Closed.** The vocabulary is an Enum, so a rule cannot invent a code, and a downstream reader
  (notification, admin page, PartyMap receipt) can switch on it exhaustively.
* **Dispositional.** Every code declares whether it *allows*, *blocks* or *defers* publication
  (`reason_disposition`). A publishable decision may only carry allow codes and a deferral may
  only carry defer codes, which is checked once here rather than re-argued at each call site.
* **Deterministically ordered.** `order_reasons` sorts by the declaration order in
  `CivicPolicyReason`, not by whatever order the rules happened to fire, so the same inputs
  always produce byte-identical output. The declaration order is alphabetical by value and
  `tests/civic/test_evidence_policy_rules.py` keeps it that way, so adding a code in the wrong
  place is a test failure rather than a silent reordering of already-published explanations.

The vocabulary mirrors `tests/fixtures/civic/evidence_policy/schema.json`; that corpus is the
approved contract, and a code existing on only one side is a bug in whichever side is newer.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable


class CivicReasonDisposition(str, Enum):
    """What a reason code does to publication.

    ALLOW codes only ever appear on a publishable decision, BLOCK codes on a candidate that is
    screened out or already published, DEFER codes on one that keeps its evidence for a later
    slot. Mixing them is what `require_disposition` refuses."""

    ALLOW = "allow"
    BLOCK = "block"
    DEFER = "defer"


class CivicPolicyReason(str, Enum):
    """Every reason a Civic Candidate can be published, held back, or excluded.

    Declared in alphabetical order by value: `order_reasons` uses the declaration order as the
    output order, so the two are deliberately the same thing."""

    BATCH_CAP_REACHED = "batch_cap_reached"
    BATCH_CAP_WITHIN_LIMIT = "batch_cap_within_limit"
    CORROBORATION_CATEGORY_MATCH = "corroboration_category_match"
    CORROBORATION_CATEGORY_MISMATCH = "corroboration_category_mismatch"
    CORROBORATION_EVENT_DATE_MATCH = "corroboration_event_date_match"
    CORROBORATION_EVENT_DATE_MISMATCH = "corroboration_event_date_mismatch"
    CORROBORATION_SUBJECT_MATCH = "corroboration_subject_match"
    CORROBORATION_SUBJECT_MISMATCH = "corroboration_subject_mismatch"
    DEDUP_NO_PRIOR_MATCH = "dedup_no_prior_match"
    DEDUP_OUTSIDE_PUBLISHED_WINDOW = "dedup_outside_published_window"
    DEDUP_OUTSIDE_SEALED_WINDOW = "dedup_outside_sealed_window"
    DESTINATION_SCOPE_V1 = "destination_scope_v1"
    DISCOVERY_RESOLVED_TO_PUBLISHER = "discovery_resolved_to_publisher"
    DUPLICATE_CANONICAL_URL = "duplicate_canonical_url"
    DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS = (
        "duplicate_published_fingerprint_within_fourteen_days")
    DUPLICATE_SEALED_CANDIDATE_WITHIN_SEVEN_DAYS = "duplicate_sealed_candidate_within_seven_days"
    EVIDENCE_ACCESSIBLE_FULL_ARTICLE = "evidence_accessible_full_article"
    EVIDENCE_ANONYMOUS_SOURCE_DEPENDENCY = "evidence_anonymous_source_dependency"
    EVIDENCE_DISCOVERY_ONLY_SOURCE = "evidence_discovery_only_source"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    EVIDENCE_MEDIA_TWO_OWNERSHIP_GROUPS = "evidence_media_two_ownership_groups"
    EVIDENCE_NAMED_SOURCES_ONLY = "evidence_named_sources_only"
    EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE = "evidence_no_accessible_full_article"
    EVIDENCE_OFFICIAL_FINDING_REQUIRES_OFFICIAL_SOURCE = (
        "evidence_official_finding_requires_official_source")
    EVIDENCE_OFFICIAL_SINGLETON = "evidence_official_singleton"
    EVIDENCE_OFFICIAL_SOURCE_REQUIRED = "evidence_official_source_required"
    EVIDENCE_PARTY_MISMATCH = "evidence_party_mismatch"
    EVIDENCE_PARTY_PRIMARY_REQUIRED = "evidence_party_primary_required"
    EVIDENCE_PARTY_PRIMARY_SINGLETON = "evidence_party_primary_singleton"
    EVIDENCE_PAYWALL_CORROBORATED = "evidence_paywall_corroborated"
    EVIDENCE_PAYWALLED_WITHOUT_ACCESSIBLE_CORROBORATION = (
        "evidence_paywalled_without_accessible_corroboration")
    EVIDENCE_SAME_OWNERSHIP_CLUSTER = "evidence_same_ownership_cluster"
    EVIDENCE_SINGLE_OWNERSHIP_GROUP = "evidence_single_ownership_group"
    EVIDENCE_UNKNOWN_OWNERSHIP_GROUP = "evidence_unknown_ownership_group"
    RECORD_KIND_SUPPORTED = "record_kind_supported"
    RECORD_KIND_UNSUPPORTED = "record_kind_unsupported"
    RESEARCH_RETRY_EXHAUSTED = "research_retry_exhausted"
    RESEARCH_RETRY_RESOLVED = "research_retry_resolved"
    SIGNIFICANCE_DURABLE = "significance_durable"
    SIGNIFICANCE_EXCLUDED_CATEGORY = "significance_excluded_category"
    SIGNIFICANCE_NON_DURABLE = "significance_non_durable"
    SUBJECT_AMBIGUOUS_V1 = "subject_ambiguous_v1"
    SUBJECT_UNSUPPORTED_V1 = "subject_unsupported_v1"


_ALLOW = CivicReasonDisposition.ALLOW
_BLOCK = CivicReasonDisposition.BLOCK
_DEFER = CivicReasonDisposition.DEFER

_DISPOSITIONS: dict[CivicPolicyReason, CivicReasonDisposition] = {
    CivicPolicyReason.BATCH_CAP_REACHED: _DEFER,
    CivicPolicyReason.BATCH_CAP_WITHIN_LIMIT: _ALLOW,
    CivicPolicyReason.CORROBORATION_CATEGORY_MATCH: _ALLOW,
    CivicPolicyReason.CORROBORATION_CATEGORY_MISMATCH: _DEFER,
    CivicPolicyReason.CORROBORATION_EVENT_DATE_MATCH: _ALLOW,
    CivicPolicyReason.CORROBORATION_EVENT_DATE_MISMATCH: _DEFER,
    CivicPolicyReason.CORROBORATION_SUBJECT_MATCH: _ALLOW,
    CivicPolicyReason.CORROBORATION_SUBJECT_MISMATCH: _DEFER,
    CivicPolicyReason.DEDUP_NO_PRIOR_MATCH: _ALLOW,
    CivicPolicyReason.DEDUP_OUTSIDE_PUBLISHED_WINDOW: _ALLOW,
    CivicPolicyReason.DEDUP_OUTSIDE_SEALED_WINDOW: _ALLOW,
    CivicPolicyReason.DESTINATION_SCOPE_V1: _DEFER,
    CivicPolicyReason.DISCOVERY_RESOLVED_TO_PUBLISHER: _ALLOW,
    CivicPolicyReason.DUPLICATE_CANONICAL_URL: _BLOCK,
    CivicPolicyReason.DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS: _BLOCK,
    CivicPolicyReason.DUPLICATE_SEALED_CANDIDATE_WITHIN_SEVEN_DAYS: _BLOCK,
    CivicPolicyReason.EVIDENCE_ACCESSIBLE_FULL_ARTICLE: _ALLOW,
    CivicPolicyReason.EVIDENCE_ANONYMOUS_SOURCE_DEPENDENCY: _DEFER,
    CivicPolicyReason.EVIDENCE_DISCOVERY_ONLY_SOURCE: _DEFER,
    CivicPolicyReason.EVIDENCE_INSUFFICIENT: _DEFER,
    CivicPolicyReason.EVIDENCE_MEDIA_TWO_OWNERSHIP_GROUPS: _ALLOW,
    CivicPolicyReason.EVIDENCE_NAMED_SOURCES_ONLY: _ALLOW,
    CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE: _DEFER,
    CivicPolicyReason.EVIDENCE_OFFICIAL_FINDING_REQUIRES_OFFICIAL_SOURCE: _DEFER,
    CivicPolicyReason.EVIDENCE_OFFICIAL_SINGLETON: _ALLOW,
    CivicPolicyReason.EVIDENCE_OFFICIAL_SOURCE_REQUIRED: _DEFER,
    CivicPolicyReason.EVIDENCE_PARTY_MISMATCH: _BLOCK,
    CivicPolicyReason.EVIDENCE_PARTY_PRIMARY_REQUIRED: _DEFER,
    CivicPolicyReason.EVIDENCE_PARTY_PRIMARY_SINGLETON: _ALLOW,
    CivicPolicyReason.EVIDENCE_PAYWALL_CORROBORATED: _ALLOW,
    CivicPolicyReason.EVIDENCE_PAYWALLED_WITHOUT_ACCESSIBLE_CORROBORATION: _DEFER,
    CivicPolicyReason.EVIDENCE_SAME_OWNERSHIP_CLUSTER: _DEFER,
    CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP: _DEFER,
    CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP: _DEFER,
    CivicPolicyReason.RECORD_KIND_SUPPORTED: _ALLOW,
    CivicPolicyReason.RECORD_KIND_UNSUPPORTED: _DEFER,
    CivicPolicyReason.RESEARCH_RETRY_EXHAUSTED: _DEFER,
    CivicPolicyReason.RESEARCH_RETRY_RESOLVED: _ALLOW,
    CivicPolicyReason.SIGNIFICANCE_DURABLE: _ALLOW,
    CivicPolicyReason.SIGNIFICANCE_EXCLUDED_CATEGORY: _BLOCK,
    CivicPolicyReason.SIGNIFICANCE_NON_DURABLE: _BLOCK,
    CivicPolicyReason.SUBJECT_AMBIGUOUS_V1: _DEFER,
    CivicPolicyReason.SUBJECT_UNSUPPORTED_V1: _DEFER,
}

# The one output order for reason codes. Declaration order rather than `sorted()` on the value,
# so the order is a decision recorded in this file instead of a property of string comparison.
REASON_ORDER: tuple[CivicPolicyReason, ...] = tuple(CivicPolicyReason)

_ORDER_INDEX: dict[CivicPolicyReason, int] = {
    reason: index for index, reason in enumerate(REASON_ORDER)}


def reason_disposition(reason: CivicPolicyReason) -> CivicReasonDisposition:
    return _DISPOSITIONS[reason]


def order_reasons(reasons: Iterable[CivicPolicyReason]) -> tuple[CivicPolicyReason, ...]:
    """De-duplicated reason codes in the single declared order.

    Rules add codes to a set as they fire; the order they fired in is an implementation detail
    that must never leak into a published explanation, so ordering happens exactly here."""
    unique = set()
    for reason in reasons:
        if not isinstance(reason, CivicPolicyReason):
            raise ValueError(f"not a Civic policy reason: {reason!r}")
        unique.add(reason)
    return tuple(sorted(unique, key=_ORDER_INDEX.__getitem__))


def require_disposition(
    reasons: Iterable[CivicPolicyReason], allowed: Iterable[CivicReasonDisposition],
) -> None:
    """Refuse a decision whose codes contradict it.

    A publishable candidate carrying a defer code, or a deferral carrying a block code, means two
    rules disagreed about what happened. That is a programming error, not a policy outcome, so it
    raises rather than being published with a misleading explanation."""
    permitted = frozenset(allowed)
    offending = sorted(
        reason.value for reason in reasons if _DISPOSITIONS[reason] not in permitted)
    if offending:
        names = ", ".join(sorted(item.value for item in permitted))
        raise ValueError(
            f"reason codes {offending} are not {names} codes for this decision")
