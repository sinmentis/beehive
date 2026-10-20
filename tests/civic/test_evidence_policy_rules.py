# tests/civic/test_evidence_policy_rules.py
"""Direct tests of the Civic evidence policy rules, away from the fixture corpus.

The corpus proves the engine decides the approved cases correctly. These tests prove the things a
finite corpus cannot: that invalid input is refused rather than guessed at, that unknown
publishers, tiers and categories fail closed, that the dedup windows are inclusive at the exact
boundary and nowhere past it, and that the decision does not depend on the order evidence
happened to arrive in.

Everything here is synthetic and local to this file: no fixture is read, so a corpus change can
never quietly weaken these invariants.
"""
from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta, timezone

import pytest

from beehive.civic.batch_policy import (PUBLISHED_CAP_BY_MODE, CivicRankedCandidate,
                                        CivicRunMode, published_cap, select_batch)
from beehive.civic.dedup import (PUBLISHED_FINGERPRINT_WINDOW, SEALED_CANDIDATE_WINDOW,
                                 CivicDedupContext, PublishedFingerprintRecord,
                                 SealedCandidateRecord, evaluate_dedup)
from beehive.civic.documents import CivicDocument
from beehive.civic.evidence import (MAX_RESEARCH_ATTEMPTS, CivicCandidateClaim,
                                    CivicClaimCategory, CivicContentClass, CivicEvidenceItem,
                                    CivicOwnershipRegistry, CivicPublicationDisposition,
                                    CivicPublisher, CivicPublisherKind, CivicQualificationOutcome,
                                    CivicRecordKind, CivicRecordOperation, CivicSourceKind,
                                    CivicSourceTier, qualify_candidate, source_tier_for,
                                    qualify_with_bounded_research)
from beehive.civic.reasons import (REASON_ORDER, CivicPolicyReason, CivicReasonDisposition,
                                   order_reasons, reason_disposition, require_disposition)
from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier

NOW = datetime(2026, 8, 12, 6, 30, tzinfo=timezone(timedelta(hours=12)))
EVENT_DATE = date(2026, 8, 9)
UTC_NOW = NOW.astimezone(timezone.utc)

PUBLISHERS = (
    CivicPublisher(
        publisher_id="register", name="Register", kind=CivicPublisherKind.OFFICIAL,
        source_kind=CivicSourceKind.LEGISLATION),
    CivicPublisher(
        publisher_id="auditor", name="Auditor", kind=CivicPublisherKind.OFFICIAL,
        source_kind=CivicSourceKind.OFFICIAL_REVIEW),
    CivicPublisher(
        publisher_id="alpha-party", name="Alpha Party", kind=CivicPublisherKind.PARTY,
        source_kind=CivicSourceKind.PARTY_PRIMARY, party_slug="alpha"),
    CivicPublisher(
        publisher_id="beta-party", name="Beta Party", kind=CivicPublisherKind.PARTY,
        source_kind=CivicSourceKind.PARTY_PRIMARY, party_slug="beta"),
    CivicPublisher(
        publisher_id="north-daily", name="North Daily", kind=CivicPublisherKind.MEDIA,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="north-group",
        ownership_cluster="north-group"),
    CivicPublisher(
        publisher_id="north-weekly", name="North Weekly", kind=CivicPublisherKind.MEDIA,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="north-group",
        ownership_cluster="north-group"),
    CivicPublisher(
        publisher_id="south-times", name="South Times", kind=CivicPublisherKind.MEDIA,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="south-group",
        ownership_cluster="south-group"),
    CivicPublisher(
        publisher_id="crown-radio", name="Crown Radio", kind=CivicPublisherKind.MEDIA,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="crown-radio-group",
        ownership_cluster="crown-cluster"),
    CivicPublisher(
        publisher_id="crown-tv", name="Crown TV", kind=CivicPublisherKind.MEDIA,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="crown-tv-group",
        ownership_cluster="crown-cluster"),
    CivicPublisher(
        publisher_id="unlisted", name="Unlisted Daily", kind=CivicPublisherKind.MEDIA,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS, registered=False),
    CivicPublisher(
        publisher_id="forum", name="Forum", kind=CivicPublisherKind.DISCOVERY),
)
REGISTRY = CivicOwnershipRegistry.build(PUBLISHERS)


def candidate(**overrides) -> CivicCandidateClaim:
    kwargs = dict(
        candidate_id="cand-1", fingerprint="fp-1", record_kind=CivicRecordKind.TIMELINE,
        operation=CivicRecordOperation.ADD, subject_id="alpha", record_key="alpha/1",
        event_date=EVENT_DATE, category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
        content_class=CivicContentClass.FACTUAL, durable=True, claim_text="Alpha opened an office")
    kwargs.update(overrides)
    return CivicCandidateClaim(**kwargs)


def media(evidence_id: str, publisher_id: str, **overrides) -> CivicEvidenceItem:
    kwargs = dict(
        evidence_id=evidence_id, publisher_id=publisher_id,
        source_kind=CivicSourceKind.INDEPENDENT_NEWS,
        evidence_tier=CivicEvidenceTier.REPUTABLE_MEDIA,
        stance=CivicEvidenceStance.SUPPORTING,
        canonical_url=f"https://{publisher_id}.example/{evidence_id}",
        title="Office opening confirmed", subject_id="alpha", event_date=EVENT_DATE,
        category=CivicClaimCategory.PARTY_TIMELINE_EVENT, published_at=NOW,
        accessible_full_text=True, paywalled=False, anonymous_source_dependency=False)
    kwargs.update(overrides)
    return CivicEvidenceItem(**kwargs)


def official(evidence_id: str = "o1", **overrides) -> CivicEvidenceItem:
    kwargs = dict(
        evidence_id=evidence_id, publisher_id="register",
        source_kind=CivicSourceKind.LEGISLATION, evidence_tier=CivicEvidenceTier.OFFICIAL,
        stance=CivicEvidenceStance.SUPPORTING,
        canonical_url=f"https://register.example/{evidence_id}", title="Assent record",
        subject_id="alpha", event_date=EVENT_DATE, category=CivicClaimCategory.LEGISLATION,
        published_at=NOW, accessible_full_text=True, paywalled=False,
        anonymous_source_dependency=False)
    kwargs.update(overrides)
    return CivicEvidenceItem(**kwargs)


def party(evidence_id: str = "p1", publisher_id: str = "alpha-party", slug: str = "alpha",
          **overrides) -> CivicEvidenceItem:
    kwargs = dict(
        evidence_id=evidence_id, publisher_id=publisher_id,
        source_kind=CivicSourceKind.PARTY_PRIMARY,
        evidence_tier=CivicEvidenceTier.PARTY_PRIMARY, stance=CivicEvidenceStance.SUPPORTING,
        canonical_url=f"https://{publisher_id}.example/{evidence_id}", title="Policy release",
        subject_id="alpha", event_date=EVENT_DATE, category=CivicClaimCategory.PARTY_POLICY,
        published_at=NOW, accessible_full_text=True, paywalled=False,
        anonymous_source_dependency=False, party_slug=slug)
    kwargs.update(overrides)
    return CivicEvidenceItem(**kwargs)


def discovery(evidence_id: str = "d1") -> CivicEvidenceItem:
    return CivicEvidenceItem(
        evidence_id=evidence_id, publisher_id="forum", source_kind=None,
        evidence_tier=CivicEvidenceTier.AGGREGATOR, stance=CivicEvidenceStance.CONTEXT,
        canonical_url=f"https://forum.example/{evidence_id}", title="Forum thread",
        subject_id="alpha", event_date=EVENT_DATE,
        category=CivicClaimCategory.PARTY_TIMELINE_EVENT, published_at=NOW,
        accessible_full_text=True, paywalled=False, anonymous_source_dependency=False,
        discovery_only=True)


def qualify(claim: CivicCandidateClaim, *evidence, context=None):
    return qualify_candidate(claim, evidence, registry=REGISTRY, dedup_context=context)


# --- Reason vocabulary --------------------------------------------------------------------------


def test_reason_order_is_complete_and_alphabetical() -> None:
    assert len(REASON_ORDER) == len(set(REASON_ORDER)) == len(CivicPolicyReason)
    assert [reason.value for reason in REASON_ORDER] == sorted(
        reason.value for reason in CivicPolicyReason)


def test_every_reason_declares_a_disposition() -> None:
    for reason in CivicPolicyReason:
        assert isinstance(reason_disposition(reason), CivicReasonDisposition)


def test_order_reasons_deduplicates_and_orders() -> None:
    shuffled = [
        CivicPolicyReason.SIGNIFICANCE_DURABLE, CivicPolicyReason.EVIDENCE_INSUFFICIENT,
        CivicPolicyReason.SIGNIFICANCE_DURABLE, CivicPolicyReason.BATCH_CAP_REACHED]
    assert order_reasons(shuffled) == (
        CivicPolicyReason.BATCH_CAP_REACHED, CivicPolicyReason.EVIDENCE_INSUFFICIENT,
        CivicPolicyReason.SIGNIFICANCE_DURABLE)


def test_order_reasons_refuses_a_foreign_code() -> None:
    with pytest.raises(ValueError):
        order_reasons(["significance_durable"])


def test_require_disposition_refuses_a_contradicting_code() -> None:
    with pytest.raises(ValueError):
        require_disposition(
            [CivicPolicyReason.EVIDENCE_INSUFFICIENT], [CivicReasonDisposition.ALLOW])


# --- Source tiers -------------------------------------------------------------------------------


def test_source_tier_is_derived_from_the_source_kind() -> None:
    assert source_tier_for(CivicSourceKind.LEGISLATION) is CivicSourceTier.T0
    assert source_tier_for(CivicSourceKind.PARLIAMENT) is CivicSourceTier.T0
    assert source_tier_for(CivicSourceKind.ELECTORAL_COMMISSION) is CivicSourceTier.T0
    assert source_tier_for(CivicSourceKind.GOVERNMENT) is CivicSourceTier.T1
    assert source_tier_for(CivicSourceKind.OFFICIAL_STATISTICS) is CivicSourceTier.T1
    assert source_tier_for(CivicSourceKind.OFFICIAL_REVIEW) is CivicSourceTier.T2
    assert source_tier_for(CivicSourceKind.PARTY_PRIMARY) is CivicSourceTier.T3
    assert source_tier_for(CivicSourceKind.INDEPENDENT_NEWS) is CivicSourceTier.T4
    assert source_tier_for(None) is CivicSourceTier.T5


def test_tier_rank_orders_strongest_first() -> None:
    ranks = [tier.rank for tier in CivicSourceTier]
    assert ranks == sorted(ranks) == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("value", ["T6", "official", "", "t0"])
def test_unknown_source_tier_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        CivicSourceTier(value)


@pytest.mark.parametrize("value", ["party-timeline", "unknown", "", "Legislation"])
def test_unknown_category_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        CivicClaimCategory(value)


@pytest.mark.parametrize("value", ["blog", "wire-service", ""])
def test_unknown_source_kind_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        CivicSourceKind(value)


# --- Evidence and registry validation -----------------------------------------------------------


def test_naive_published_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        media("e1", "north-daily", published_at=datetime(2026, 8, 9, 9, 0))


def test_datetime_is_not_accepted_as_an_event_date() -> None:
    with pytest.raises(ValueError, match="event_date"):
        media("e1", "north-daily", event_date=NOW)


def test_evidence_tier_must_match_the_source_tier() -> None:
    with pytest.raises(ValueError, match="cannot be"):
        official(evidence_tier=CivicEvidenceTier.REPUTABLE_MEDIA)


def test_discovery_material_is_exactly_material_without_a_source_kind() -> None:
    with pytest.raises(ValueError, match="discovery-only"):
        media("e1", "north-daily", discovery_only=True)
    with pytest.raises(ValueError, match="discovery-only"):
        CivicEvidenceItem(
            evidence_id="e1", publisher_id="forum", source_kind=None,
            evidence_tier=CivicEvidenceTier.AGGREGATOR, stance=CivicEvidenceStance.CONTEXT,
            canonical_url="https://forum.example/e1", title="Thread", subject_id="alpha",
            event_date=EVENT_DATE, category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
            published_at=NOW, accessible_full_text=True, paywalled=False,
            anonymous_source_dependency=False, discovery_only=False)


def test_party_primary_evidence_must_name_its_party() -> None:
    with pytest.raises(ValueError, match="party_slug"):
        party(slug=None)
    with pytest.raises(ValueError, match="party_slug"):
        media("e1", "north-daily", party_slug="alpha")


def test_registered_media_publisher_needs_both_ownership_levels() -> None:
    with pytest.raises(ValueError, match="ownership"):
        CivicPublisher(
            publisher_id="x", name="X", kind=CivicPublisherKind.MEDIA,
            source_kind=CivicSourceKind.INDEPENDENT_NEWS, ownership_group="x-group")


def test_only_media_publishers_carry_ownership() -> None:
    with pytest.raises(ValueError, match="ownership"):
        CivicPublisher(
            publisher_id="x", name="X", kind=CivicPublisherKind.OFFICIAL,
            source_kind=CivicSourceKind.GOVERNMENT, ownership_group="x-group",
            ownership_cluster="x-group")


def test_registry_refuses_a_duplicate_publisher() -> None:
    with pytest.raises(ValueError, match="duplicate publisher"):
        CivicOwnershipRegistry.build([PUBLISHERS[0], PUBLISHERS[0]])


def test_duplicate_evidence_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate evidence id"):
        qualify(candidate(), media("e1", "north-daily"), media("e1", "south-times"))


# --- Ownership independence ---------------------------------------------------------------------


def test_two_outlets_under_one_owner_are_one_publisher() -> None:
    result = qualify(candidate(), media("e1", "north-daily"), media("e2", "north-weekly"))
    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP in result.reasons
    assert result.ownership_groups == ("north-group",)


def test_two_owners_in_one_cluster_are_one_publisher() -> None:
    result = qualify(candidate(), media("e1", "crown-radio"), media("e2", "crown-tv"))
    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_SAME_OWNERSHIP_CLUSTER in result.reasons
    assert result.ownership_groups == ("crown-radio-group", "crown-tv-group")
    assert result.ownership_clusters == ("crown-cluster",)


def test_two_independent_owners_publish() -> None:
    result = qualify(candidate(), media("e1", "north-daily"), media("e2", "south-times"))
    assert result.disposition is CivicPublicationDisposition.PUBLISHABLE
    assert CivicPolicyReason.EVIDENCE_MEDIA_TWO_OWNERSHIP_GROUPS in result.reasons


def test_unregistered_publisher_fails_closed() -> None:
    result = qualify(candidate(), media("e1", "north-daily"), media("e2", "unlisted"))
    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP in result.reasons
    assert result.evidence_used == ("e1",)


def test_publisher_missing_from_the_registry_fails_closed() -> None:
    result = qualify(candidate(), media("e1", "north-daily"), media("e2", "never-heard-of-it"))
    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP in result.reasons
    assert result.evidence_used == ("e1",)


def test_lesser_media_tier_is_not_reputable_media() -> None:
    result = qualify(
        candidate(), media("e1", "north-daily"),
        media("e2", "south-times", evidence_tier=CivicEvidenceTier.OTHER_MEDIA))
    assert result.evidence_used == ("e1",)
    assert CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP in result.reasons


# --- Tier and category rules --------------------------------------------------------------------


def test_legal_state_needs_a_constitutive_record() -> None:
    claim = candidate(category=CivicClaimCategory.LEGISLATION)
    media_only = qualify(
        claim, media("e1", "north-daily", category=CivicClaimCategory.LEGISLATION),
        media("e2", "south-times", category=CivicClaimCategory.LEGISLATION))
    assert media_only.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_OFFICIAL_SOURCE_REQUIRED in media_only.reasons
    assert qualify(claim, official()).disposition is CivicPublicationDisposition.PUBLISHABLE


def test_official_finding_cannot_rest_on_a_party_statement() -> None:
    claim = candidate(
        record_kind=CivicRecordKind.PROMISE_FINDING,
        category=CivicClaimCategory.OFFICIAL_FINDING)
    party_only = qualify(
        claim, party(category=CivicClaimCategory.OFFICIAL_FINDING))
    assert CivicPolicyReason.EVIDENCE_OFFICIAL_FINDING_REQUIRES_OFFICIAL_SOURCE in (
        party_only.reasons)
    review = official(
        publisher_id="auditor", source_kind=CivicSourceKind.OFFICIAL_REVIEW,
        category=CivicClaimCategory.OFFICIAL_FINDING)
    assert qualify(claim, review).disposition is CivicPublicationDisposition.PUBLISHABLE


def test_party_policy_needs_the_matching_party() -> None:
    claim = candidate(record_kind=CivicRecordKind.POLICY, category=CivicClaimCategory.PARTY_POLICY)
    assert qualify(claim, party()).disposition is CivicPublicationDisposition.PUBLISHABLE
    media_only = qualify(
        claim, media("e1", "north-daily", category=CivicClaimCategory.PARTY_POLICY),
        media("e2", "south-times", category=CivicClaimCategory.PARTY_POLICY))
    assert CivicPolicyReason.EVIDENCE_PARTY_PRIMARY_REQUIRED in media_only.reasons


def test_party_primary_without_accessible_text_is_not_publishable() -> None:
    claim = candidate(
        record_kind=CivicRecordKind.POLICY,
        category=CivicClaimCategory.PARTY_POLICY,
    )

    result = qualify(claim, party(accessible_full_text=False))

    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE in result.reasons


def test_inaccessible_party_item_does_not_veto_two_accessible_media_sources() -> None:
    claim = candidate()

    result = qualify(
        claim,
        party(accessible_full_text=False),
        media("e1", "north-daily"),
        media("e2", "south-times"),
    )

    assert result.outcome is CivicQualificationOutcome.QUALIFIED
    assert CivicPolicyReason.EVIDENCE_MEDIA_TWO_OWNERSHIP_GROUPS in result.reasons


def test_official_singleton_does_not_require_party_detail_hydration() -> None:
    claim = candidate(category=CivicClaimCategory.LEGISLATION)

    result = qualify(
        claim,
        official(
            category=CivicClaimCategory.LEGISLATION,
            accessible_full_text=False,
        ),
    )

    assert result.outcome is CivicQualificationOutcome.QUALIFIED
    assert CivicPolicyReason.EVIDENCE_OFFICIAL_SINGLETON in result.reasons


def test_one_party_cannot_establish_another_partys_policy() -> None:
    claim = candidate(record_kind=CivicRecordKind.POLICY, category=CivicClaimCategory.PARTY_POLICY)
    result = qualify(claim, party(publisher_id="beta-party", slug="beta"))
    assert result.outcome is CivicQualificationOutcome.SCREENED_OUT
    assert result.reasons == (CivicPolicyReason.EVIDENCE_PARTY_MISMATCH,)
    assert result.evidence_used == ()


def test_unsupported_record_kind_defers_unsupported() -> None:
    claim = candidate(
        record_kind=CivicRecordKind.ELECTORATE_BOUNDARY,
        category=CivicClaimCategory.ELECTORATE_BOUNDARY)
    result = qualify(claim, official(category=CivicClaimCategory.ELECTORATE_BOUNDARY))
    assert result.disposition is CivicPublicationDisposition.DEFERRED_UNSUPPORTED
    assert result.reasons == (CivicPolicyReason.RECORD_KIND_UNSUPPORTED,)


def test_out_of_scope_category_defers_even_on_a_supported_record_kind() -> None:
    claim = candidate(category=CivicClaimCategory.ELECTORATE_BOUNDARY)
    result = qualify(claim, official(category=CivicClaimCategory.ELECTORATE_BOUNDARY))
    assert result.disposition is CivicPublicationDisposition.DEFERRED_UNSUPPORTED


@pytest.mark.parametrize("category,content", [
    (CivicClaimCategory.OPINION, CivicContentClass.OPINION),
    (CivicClaimCategory.POLL, CivicContentClass.POLL),
    (CivicClaimCategory.LIVE_BLOG, CivicContentClass.LIVE_BLOG),
])
def test_hard_excluded_material_cites_nothing(category, content) -> None:
    claim = candidate(category=category, content_class=content, durable=False)
    result = qualify(
        claim, media("e1", "north-daily", category=category),
        media("e2", "south-times", category=category))
    assert result.outcome is CivicQualificationOutcome.SCREENED_OUT
    assert CivicPolicyReason.SIGNIFICANCE_EXCLUDED_CATEGORY in result.reasons
    assert result.evidence_used == ()
    assert result.evidence_excluded == ("e1", "e2")
    assert result.ownership_groups == ()


def test_non_durable_material_is_screened_out() -> None:
    result = qualify(
        candidate(durable=False), media("e1", "north-daily"), media("e2", "south-times"))
    assert result.outcome is CivicQualificationOutcome.SCREENED_OUT
    assert result.reasons == (CivicPolicyReason.SIGNIFICANCE_NON_DURABLE,)


def test_non_factual_content_class_is_screened_out() -> None:
    result = qualify(
        candidate(content_class=CivicContentClass.OPINION), media("e1", "north-daily"),
        media("e2", "south-times"))
    assert result.outcome is CivicQualificationOutcome.SCREENED_OUT
    assert CivicPolicyReason.SIGNIFICANCE_EXCLUDED_CATEGORY in result.reasons


# --- Media rule details -------------------------------------------------------------------------


def test_paywalled_evidence_needs_accessible_corroboration() -> None:
    alone = qualify(
        candidate(), media("e1", "north-daily", paywalled=True, accessible_full_text=False))
    assert CivicPolicyReason.EVIDENCE_PAYWALLED_WITHOUT_ACCESSIBLE_CORROBORATION in alone.reasons
    paired = qualify(
        candidate(), media("e1", "north-daily", paywalled=True, accessible_full_text=False),
        media("e2", "south-times"))
    assert paired.disposition is CivicPublicationDisposition.PUBLISHABLE
    assert CivicPolicyReason.EVIDENCE_PAYWALL_CORROBORATED in paired.reasons


def test_inaccessible_evidence_defers_without_a_paywall() -> None:
    result = qualify(
        candidate(), media("e1", "north-daily", accessible_full_text=False),
        media("e2", "south-times", accessible_full_text=False))
    assert CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE in result.reasons


def test_any_anonymous_source_dependency_defers() -> None:
    result = qualify(
        candidate(), media("e1", "north-daily", anonymous_source_dependency=True),
        media("e2", "south-times"))
    assert CivicPolicyReason.EVIDENCE_ANONYMOUS_SOURCE_DEPENDENCY in result.reasons


@pytest.mark.parametrize("field,value,reason", [
    ("subject_id", "beta", CivicPolicyReason.CORROBORATION_SUBJECT_MISMATCH),
    ("event_date", date(2026, 7, 1), CivicPolicyReason.CORROBORATION_EVENT_DATE_MISMATCH),
    ("category", CivicClaimCategory.PARTY_POLICY,
     CivicPolicyReason.CORROBORATION_CATEGORY_MISMATCH),
])
def test_a_corroborator_must_describe_the_same_event(field, value, reason) -> None:
    result = qualify(
        candidate(), media("e1", "north-daily"), media("e2", "south-times", **{field: value}))
    assert reason in result.reasons
    assert result.evidence_used == ("e1",)


def test_discovery_material_alone_can_never_be_evidence() -> None:
    result = qualify(candidate(), discovery())
    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert result.reasons == (
        CivicPolicyReason.EVIDENCE_DISCOVERY_ONLY_SOURCE,
        CivicPolicyReason.EVIDENCE_INSUFFICIENT)
    assert result.evidence_used == ()


def test_discovery_material_alongside_real_evidence_does_not_explain_the_decision() -> None:
    result = qualify(candidate(), discovery(), media("e1", "north-daily"))
    assert CivicPolicyReason.EVIDENCE_DISCOVERY_ONLY_SOURCE not in result.reasons
    assert CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP in result.reasons
    resolved = qualify(
        candidate(), discovery(), media("e1", "north-daily"), media("e2", "south-times"))
    assert resolved.disposition is CivicPublicationDisposition.PUBLISHABLE
    assert CivicPolicyReason.DISCOVERY_RESOLVED_TO_PUBLISHER in resolved.reasons
    assert resolved.evidence_used == ("e1", "e2")


# --- Inadmissible evidence is removed, never a veto -----------------------------------------------
#
# A media only fact is established by the publishers that survive admissibility. Material that
# cannot be evidence -- an unregistered outlet, a publisher the registry has never heard of, a
# lesser media tier, a report about a different event -- is dropped from the count and must not
# be able to hold back two admitted independent publishers that already satisfy plan 4.2 rule 4.

def _inadmissible_second_items() -> dict[str, CivicEvidenceItem]:
    return {
        "unregistered publisher": media("x1", "unlisted"),
        "publisher missing from the registry": media("x1", "never-heard-of-it"),
        "lesser media tier": media(
            "x1", "crown-radio", evidence_tier=CivicEvidenceTier.OTHER_MEDIA),
        "subject mismatch": media("x1", "crown-radio", subject_id="beta"),
        "event date mismatch": media("x1", "crown-radio", event_date=date(2026, 7, 1)),
        "category mismatch": media(
            "x1", "crown-radio", category=CivicClaimCategory.PARTY_POLICY),
    }


@pytest.mark.parametrize("label", sorted(_inadmissible_second_items()))
def test_inadmissible_evidence_does_not_veto_two_qualifying_publishers(label: str) -> None:
    attached = _inadmissible_second_items()[label]
    result = qualify(
        candidate(), media("e1", "north-daily"), media("e2", "south-times"), attached)
    assert result.disposition is CivicPublicationDisposition.PUBLISHABLE, (
        f"{label} vetoed two admitted independent publishers")
    assert result.outcome is CivicQualificationOutcome.QUALIFIED
    assert CivicPolicyReason.EVIDENCE_MEDIA_TWO_OWNERSHIP_GROUPS in result.reasons
    assert result.evidence_used == ("e1", "e2")
    assert result.evidence_excluded == ("x1",)
    assert result.ownership_groups == ("north-group", "south-group")
    assert not any(
        reason in result.reasons for reason in (
            CivicPolicyReason.EVIDENCE_UNKNOWN_OWNERSHIP_GROUP,
            CivicPolicyReason.CORROBORATION_SUBJECT_MISMATCH,
            CivicPolicyReason.CORROBORATION_EVENT_DATE_MISMATCH,
            CivicPolicyReason.CORROBORATION_CATEGORY_MISMATCH))


@pytest.mark.parametrize("label", sorted(_inadmissible_second_items()))
def test_inadmissible_evidence_explains_a_failure_it_actually_caused(label: str) -> None:
    """The same item does speak when the admitted evidence cannot establish the fact alone."""
    attached = _inadmissible_second_items()[label]
    result = qualify(candidate(), media("e1", "north-daily"), attached)
    assert result.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP not in result.reasons
    assert result.evidence_used == ("e1",)


@pytest.mark.parametrize("label", sorted(_inadmissible_second_items()))
def test_research_recovers_a_candidate_with_inadmissible_evidence_attached(label: str) -> None:
    attached = _inadmissible_second_items()[label]
    run = qualify_with_bounded_research(
        candidate(), [media("e1", "north-daily"), attached], registry=REGISTRY,
        additional_evidence=[media("e2", "south-times")])
    assert run.before.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert run.attempts_used == 1
    assert run.after.disposition is CivicPublicationDisposition.PUBLISHABLE, (
        f"one bounded research pass could not recover past an attached {label}")
    assert CivicPolicyReason.RESEARCH_RETRY_RESOLVED in run.after.reasons
    assert run.after.evidence_used == ("e1", "e2")
    assert run.after.evidence_excluded == ("x1",)


def test_ownership_arithmetic_still_explains_a_failure_it_alone_caused() -> None:
    result = qualify(candidate(), media("e1", "north-daily"), media("e2", "north-weekly"))
    assert result.reasons == (
        CivicPolicyReason.EVIDENCE_INSUFFICIENT,
        CivicPolicyReason.EVIDENCE_SINGLE_OWNERSHIP_GROUP)


def test_inaccessible_admitted_evidence_reports_its_own_failure() -> None:
    """Accessibility is a property of the admitted evidence, so an excluded item cannot mask it."""
    result = qualify(
        candidate(), media("e1", "north-daily", accessible_full_text=False),
        media("e2", "south-times", accessible_full_text=False), media("x1", "unlisted"))
    assert result.reasons == (
        CivicPolicyReason.EVIDENCE_INSUFFICIENT,
        CivicPolicyReason.EVIDENCE_NO_ACCESSIBLE_FULL_ARTICLE)


# --- Determinism ---------------------------------------------------------------------------------


def test_decision_does_not_depend_on_evidence_order() -> None:
    items = (discovery(), media("e1", "north-daily"), media("e2", "south-times"),
             media("e3", "unlisted"))
    baseline = qualify_candidate(candidate(), items, registry=REGISTRY)
    for permutation in itertools.permutations(items):
        assert qualify_candidate(candidate(), permutation, registry=REGISTRY) == baseline


def test_repeated_qualification_is_idempotent() -> None:
    items = (media("e1", "north-daily"), media("e2", "south-times"))
    first = qualify_candidate(candidate(), items, registry=REGISTRY)
    assert first == qualify_candidate(candidate(), items, registry=REGISTRY)


# --- Dedup windows -------------------------------------------------------------------------------


def sealed(age: timedelta, fingerprint: str = "fp-1") -> CivicDedupContext:
    return CivicDedupContext.build(
        NOW,
        sealed_candidates=[SealedCandidateRecord(
            candidate_id="prior", fingerprint=fingerprint,
            canonical_url="https://north-daily.example/prior", sealed_at=NOW - age)])


def published(age: timedelta, fingerprint: str = "fp-1") -> CivicDedupContext:
    return CivicDedupContext.build(
        NOW,
        published_fingerprints=[PublishedFingerprintRecord(
            fingerprint=fingerprint, batch_id="2026-08-01-01", published_at=NOW - age)])


def decide(context: CivicDedupContext, urls=("https://north-daily.example/e1",)):
    return evaluate_dedup(fingerprint="fp-1", canonical_urls=urls, context=context)


def test_sealed_window_is_inclusive_at_the_boundary() -> None:
    assert decide(sealed(SEALED_CANDIDATE_WINDOW)).is_duplicate
    assert not decide(sealed(SEALED_CANDIDATE_WINDOW + timedelta(minutes=1))).is_duplicate


def test_published_window_is_inclusive_at_the_boundary() -> None:
    assert decide(published(PUBLISHED_FINGERPRINT_WINDOW)).is_duplicate
    assert not decide(published(PUBLISHED_FINGERPRINT_WINDOW + timedelta(minutes=1))).is_duplicate


@pytest.mark.parametrize("days", range(0, 31))
def test_only_the_declared_windows_deduplicate(days: int) -> None:
    age = timedelta(days=days)
    assert decide(sealed(age)).is_duplicate is (age <= SEALED_CANDIDATE_WINDOW)
    assert decide(published(age)).is_duplicate is (age <= PUBLISHED_FINGERPRINT_WINDOW)


def test_an_aged_match_is_reported_rather_than_ignored() -> None:
    aged_sealed = decide(sealed(timedelta(days=10)))
    assert aged_sealed.reason is CivicPolicyReason.DEDUP_OUTSIDE_SEALED_WINDOW
    assert aged_sealed.matched_age == timedelta(days=10)
    aged_published = decide(published(timedelta(days=20)))
    assert aged_published.reason is CivicPolicyReason.DEDUP_OUTSIDE_PUBLISHED_WINDOW


def test_a_different_fingerprint_is_not_a_match() -> None:
    assert decide(sealed(timedelta(days=1), fingerprint="fp-other")).reason is (
        CivicPolicyReason.DEDUP_NO_PRIOR_MATCH)


def test_canonical_url_reuse_is_a_duplicate_at_any_age() -> None:
    context = CivicDedupContext.build(
        NOW,
        sealed_candidates=[SealedCandidateRecord(
            candidate_id="prior", fingerprint="fp-other",
            canonical_url="https://north-daily.example/e1", sealed_at=NOW - timedelta(days=365))])
    decision = decide(context)
    assert decision.reason is CivicPolicyReason.DUPLICATE_CANONICAL_URL
    assert decision.matched_candidate_id == "prior"


def test_dedup_compares_across_timezones() -> None:
    context = CivicDedupContext.build(
        UTC_NOW,
        sealed_candidates=[SealedCandidateRecord(
            candidate_id="prior", fingerprint="fp-1",
            canonical_url="https://north-daily.example/prior",
            sealed_at=NOW - SEALED_CANDIDATE_WINDOW)])
    assert decide(context).is_duplicate


def test_dedup_is_independent_of_record_order() -> None:
    records = [
        SealedCandidateRecord(
            candidate_id=f"prior-{index}", fingerprint="fp-1",
            canonical_url=f"https://north-daily.example/prior-{index}",
            sealed_at=NOW - timedelta(days=index))
        for index in (3, 9, 20)]
    for permutation in itertools.permutations(records):
        decision = evaluate_dedup(
            fingerprint="fp-1", canonical_urls=["https://north-daily.example/e1"],
            context=CivicDedupContext.build(NOW, sealed_candidates=permutation))
        assert decision.reason is CivicPolicyReason.DUPLICATE_SEALED_CANDIDATE_WITHIN_SEVEN_DAYS
        assert decision.matched_candidate_id == "prior-3"


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CivicDedupContext.build(datetime(2026, 8, 12, 6, 30))
    with pytest.raises(ValueError, match="timezone-aware"):
        SealedCandidateRecord(
            candidate_id="prior", fingerprint="fp-1", canonical_url="https://a.example/1",
            sealed_at=datetime(2026, 8, 12, 6, 30))
    with pytest.raises(ValueError, match="timezone-aware"):
        PublishedFingerprintRecord(
            fingerprint="fp-1", batch_id="b", published_at=datetime(2026, 8, 12, 6, 30))


def test_a_record_stamped_after_now_is_refused() -> None:
    with pytest.raises(ValueError, match="after now"):
        sealed(-timedelta(minutes=1))
    with pytest.raises(ValueError, match="after now"):
        published(-timedelta(minutes=1))


def test_duplicates_are_excluded_before_evidence_strength_is_considered() -> None:
    context = sealed(timedelta(days=1))
    result = qualify(candidate(), media("e1", "north-daily"), context=context)
    assert result.outcome is CivicQualificationOutcome.DUPLICATE
    assert result.reasons == (
        CivicPolicyReason.DUPLICATE_SEALED_CANDIDATE_WITHIN_SEVEN_DAYS,)
    assert result.evidence_used == ("e1",)


# --- Bounded research ----------------------------------------------------------------------------


def test_research_is_bounded_to_one_attempt() -> None:
    assert MAX_RESEARCH_ATTEMPTS == 1
    run = qualify_with_bounded_research(
        candidate(), [media("e1", "north-daily")], registry=REGISTRY,
        additional_evidence=[media("e2", "south-times")])
    assert run.attempts_used == 1
    assert run.after.disposition is CivicPublicationDisposition.PUBLISHABLE
    assert CivicPolicyReason.RESEARCH_RETRY_RESOLVED in run.after.reasons
    assert run.before.disposition is CivicPublicationDisposition.DEFERRED


def test_an_unresolved_attempt_defers_and_is_not_retried() -> None:
    run = qualify_with_bounded_research(
        candidate(), [media("e1", "north-daily")], registry=REGISTRY,
        additional_evidence=[media("e2", "north-weekly")])
    assert run.attempts_used == run.max_attempts == 1
    assert run.after.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert CivicPolicyReason.RESEARCH_RETRY_EXHAUSTED in run.after.reasons
    assert run.after.evidence_used == ("e1", "e2")


def test_a_publishable_candidate_never_spends_an_attempt() -> None:
    run = qualify_with_bounded_research(
        candidate(), [media("e1", "north-daily"), media("e2", "south-times")], registry=REGISTRY,
        additional_evidence=[media("e3", "crown-radio")])
    assert run.attempts_used == 0
    assert run.after is run.before


def test_a_duplicate_never_spends_an_attempt() -> None:
    run = qualify_with_bounded_research(
        candidate(), [media("e1", "north-daily")], registry=REGISTRY,
        additional_evidence=[media("e2", "south-times")],
        dedup_context=sealed(timedelta(days=1)))
    assert run.attempts_used == 0
    assert run.after.outcome is CivicQualificationOutcome.DUPLICATE


def test_an_unsupported_record_kind_never_spends_an_attempt() -> None:
    claim = candidate(
        record_kind=CivicRecordKind.PARTY_REGISTRATION,
        category=CivicClaimCategory.PARTY_TIMELINE_EVENT)
    run = qualify_with_bounded_research(
        claim, [media("e1", "north-daily")], registry=REGISTRY,
        additional_evidence=[media("e2", "south-times")])
    assert run.attempts_used == 0
    assert run.after.disposition is CivicPublicationDisposition.DEFERRED_UNSUPPORTED


def test_screened_out_material_never_spends_an_attempt() -> None:
    run = qualify_with_bounded_research(
        candidate(durable=False), [media("e1", "north-daily")], registry=REGISTRY,
        additional_evidence=[media("e2", "south-times")])
    assert run.attempts_used == 0
    assert run.after.outcome is CivicQualificationOutcome.SCREENED_OUT


# --- Batch caps ----------------------------------------------------------------------------------


def queue(count: int, start: int = 1) -> tuple[CivicRankedCandidate, ...]:
    return tuple(
        CivicRankedCandidate(
            candidate_id=f"cand-{index:02d}", rank=index, fingerprint=f"fp-{index:02d}",
            record_kind=CivicRecordKind.TIMELINE)
        for index in range(start, start + count))


def test_caps_match_the_plan() -> None:
    assert published_cap(CivicRunMode.NORMAL) == 6
    assert published_cap(CivicRunMode.ELECTION) == 10
    assert PUBLISHED_CAP_BY_MODE[CivicRunMode.NORMAL] == 6


@pytest.mark.parametrize("mode", list(CivicRunMode))
def test_an_empty_slot_stays_empty(mode: CivicRunMode) -> None:
    selection = select_batch((), mode=mode)
    assert selection.is_empty
    assert selection.published_candidate_ids == ()
    assert selection.deferred_candidate_ids == ()
    assert selection.reasons == (CivicPolicyReason.BATCH_CAP_WITHIN_LIMIT,)
    assert selection.deferral_reason_by_candidate_id == {}


@pytest.mark.parametrize("mode,size", [
    (CivicRunMode.NORMAL, size) for size in range(0, 13)
] + [(CivicRunMode.ELECTION, size) for size in range(0, 13)])
def test_the_cap_publishes_by_rank_and_defers_the_rest(mode: CivicRunMode, size: int) -> None:
    cap = published_cap(mode)
    selection = select_batch(queue(size), mode=mode)
    expected = [entry.candidate_id for entry in queue(size)]
    assert list(selection.published_candidate_ids) == expected[:cap]
    assert list(selection.deferred_candidate_ids) == expected[cap:]
    overflow = size > cap
    assert (CivicPolicyReason.BATCH_CAP_REACHED in selection.reasons) is overflow
    assert (CivicPolicyReason.BATCH_CAP_WITHIN_LIMIT in selection.reasons) is not overflow
    assert set(selection.deferral_reason_by_candidate_id) == set(expected[cap:])


def test_ties_break_on_candidate_id() -> None:
    tied = (
        CivicRankedCandidate(
            candidate_id="cand-b", rank=1, fingerprint="fp-b",
            record_kind=CivicRecordKind.TIMELINE),
        CivicRankedCandidate(
            candidate_id="cand-a", rank=1, fingerprint="fp-a",
            record_kind=CivicRecordKind.POLICY),
    )
    for permutation in itertools.permutations(tied):
        selection = select_batch(permutation, mode=CivicRunMode.NORMAL)
        assert selection.published_candidate_ids == ("cand-a", "cand-b")


def test_selection_refuses_a_queue_it_cannot_publish() -> None:
    with pytest.raises(ValueError, match="outside the automated publication scope"):
        CivicRankedCandidate(
            candidate_id="cand-1", rank=1, fingerprint="fp-1",
            record_kind=CivicRecordKind.PARTY_REGISTRATION)
    with pytest.raises(ValueError, match="rank must be"):
        CivicRankedCandidate(
            candidate_id="cand-1", rank=0, fingerprint="fp-1",
            record_kind=CivicRecordKind.TIMELINE)
    with pytest.raises(ValueError, match="duplicate candidate"):
        select_batch(queue(1) + queue(1), mode=CivicRunMode.NORMAL)


# --- Reuse of the fetched document shape ---------------------------------------------------------


def test_evidence_can_be_lifted_from_a_fetched_document() -> None:
    document = CivicDocument(
        source_key="nz_legislation", external_key="act-2026-11",
        canonical_url="https://register.example/acts/2026/11", title="Assent record",
        tier=CivicEvidenceTier.OFFICIAL, publisher="Register",
        first_seen_at=NOW, published_at=NOW - timedelta(days=1), excerpt="The register records.")
    item = CivicEvidenceItem.from_document(
        document, evidence_id="e1", publisher_id="register",
        source_kind=CivicSourceKind.LEGISLATION, stance=CivicEvidenceStance.SUPPORTING,
        subject_id="alpha", event_date=EVENT_DATE, category=CivicClaimCategory.LEGISLATION,
        accessible_full_text=True)
    assert item.source_tier is CivicSourceTier.T0
    assert item.canonical_url == document.canonical_url
    assert item.published_at == document.effective_at
    claim = candidate(category=CivicClaimCategory.LEGISLATION)
    assert qualify(claim, item).disposition is CivicPublicationDisposition.PUBLISHABLE
