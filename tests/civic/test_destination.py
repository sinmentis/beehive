from datetime import date, datetime, timezone

from beehive.civic.destination import (
    constrain_qualification_to_destination_v1,
    is_destination_v1_claim,
)
from beehive.civic.evidence import (
    CivicCandidateClaim,
    CivicClaimCategory,
    CivicContentClass,
    CivicEvidenceItem,
    CivicOwnershipRegistry,
    CivicPublisher,
    CivicPublisherKind,
    CivicQualificationOutcome,
    CivicRecordKind,
    CivicRecordOperation,
    CivicSourceKind,
    qualify_candidate,
)
from beehive.civic.reasons import CivicPolicyReason
from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
REGISTRY = CivicOwnershipRegistry.build([
    CivicPublisher(
        publisher_id="green",
        name="Green Party",
        kind=CivicPublisherKind.PARTY,
        source_kind=CivicSourceKind.PARTY_PRIMARY,
        party_slug="green",
    )
])


def _claim(**overrides):
    values = {
        "candidate_id": "1",
        "fingerprint": "green:event",
        "record_kind": CivicRecordKind.TIMELINE,
        "operation": CivicRecordOperation.ADD,
        "subject_id": "green",
        "record_key": "green:event",
        "event_date": date(2026, 8, 1),
        "category": CivicClaimCategory.PARTY_TIMELINE_EVENT,
        "content_class": CivicContentClass.FACTUAL,
        "durable": True,
        "claim_text": "A durable event",
    }
    values.update(overrides)
    return CivicCandidateClaim(**values)


def _evidence(category=CivicClaimCategory.PARTY_TIMELINE_EVENT):
    return CivicEvidenceItem(
        evidence_id="green:release",
        publisher_id="green",
        source_kind=CivicSourceKind.PARTY_PRIMARY,
        evidence_tier=CivicEvidenceTier.PARTY_PRIMARY,
        stance=CivicEvidenceStance.SUPPORTING,
        canonical_url="https://www.greens.org.nz/release",
        title="Release",
        subject_id="green",
        event_date=date(2026, 8, 1),
        category=category,
        published_at=NOW,
        accessible_full_text=True,
        paywalled=False,
        anonymous_source_dependency=False,
        party_slug="green",
    )


def test_exact_v1_shape_keeps_the_broad_qualification():
    claim = _claim()
    broad = qualify_candidate(claim, [_evidence()], registry=REGISTRY)
    assert broad.is_publishable
    assert is_destination_v1_claim(claim)
    assert constrain_qualification_to_destination_v1(claim, broad) is broad


def test_broad_publishable_policy_shape_closes_at_destination_boundary():
    claim = _claim(
        record_kind=CivicRecordKind.POLICY,
        category=CivicClaimCategory.PARTY_POLICY,
    )
    broad = qualify_candidate(
        claim, [_evidence(CivicClaimCategory.PARTY_POLICY)], registry=REGISTRY)
    assert broad.is_publishable
    narrowed = constrain_qualification_to_destination_v1(claim, broad)
    assert narrowed.outcome is CivicQualificationOutcome.DEFERRED_UNSUPPORTED
    assert narrowed.reasons == (CivicPolicyReason.DESTINATION_SCOPE_V1,)
    assert not narrowed.research_retry_eligible


def test_scope_does_not_relabel_an_evidence_deferral():
    claim = _claim(record_kind=CivicRecordKind.POLICY)
    deferred = qualify_candidate(claim, [], registry=REGISTRY)
    assert constrain_qualification_to_destination_v1(claim, deferred) is deferred
