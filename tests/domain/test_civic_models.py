# tests/domain/test_civic_models.py
"""Invariants of the pure Civic domain types. No database: every assertion here is about what a
frozen dataclass will and will not allow to exist, which is what makes an invalid row
unrepresentable once db/civic_*.py loads it back.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.domain.civic import (CivicBatch, CivicBatchStatus, CivicCandidate,
                                   CivicCandidateStatus, CivicDeliveryChannel,
                                   CivicDeliveryReceipt, CivicEvidence, CivicEvidenceStance,
                                   CivicEvidenceTier, CivicNotification,
                                   CivicNotificationStatus, CivicResearchAttempt,
                                   CivicResearchOutcome, CivicRun, CivicRunPhase, CivicRunStatus,
                                   CivicTerm, CivicTermCategory, CivicTermRole,
                                   CivicVerification, CivicVerificationVerdict,
                                   CivicVocabularyStatus, CivicVocabularyVersion, CivicWatch,
                                   CivicWatchStatus, evidence_supports_publication,
                                   normalize_term)

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _watch(**overrides) -> CivicWatch:
    kwargs = dict(
        id=1, subject="NZ election", question="What changed?", status=CivicWatchStatus.ACTIVE,
        schedule_timezone="Pacific/Auckland", schedule_time="06:00", created_at=T0)
    kwargs.update(overrides)
    return CivicWatch(**kwargs)


def _run(**overrides) -> CivicRun:
    kwargs = dict(
        id=1, watch_id=1, slot_at=T0, status=CivicRunStatus.PENDING, phase=None, requested_at=T0)
    kwargs.update(overrides)
    return CivicRun(**kwargs)


def _batch(**overrides) -> CivicBatch:
    kwargs = dict(
        id=1, run_id=1, watch_id=1, status=CivicBatchStatus.BUILDING, idempotency_key="k",
        attempt_count=0, max_attempts=5, created_at=T0)
    kwargs.update(overrides)
    return CivicBatch(**kwargs)


def _candidate(**overrides) -> CivicCandidate:
    kwargs = dict(
        id=1, watch_id=1, run_id=1, batch_id=1, dedupe_key="dk", title="T", statement="S",
        status=CivicCandidateStatus.DRAFTED, created_at=T0)
    kwargs.update(overrides)
    return CivicCandidate(**kwargs)


def _evidence(**overrides) -> CivicEvidence:
    kwargs = dict(
        id=1, candidate_id=1, source_ref="official_feed", external_key="e1",
        url="https://example.test/1", title="Release", tier=CivicEvidenceTier.OFFICIAL,
        stance=CivicEvidenceStance.SUPPORTING, captured_at=T0)
    kwargs.update(overrides)
    return CivicEvidence(**kwargs)


def _notification(**overrides) -> CivicNotification:
    kwargs = dict(
        id=1, watch_id=1, run_id=1, kind="run_failed", dedupe_key="run-1",
        subject="Run failed", body="", status=CivicNotificationStatus.PENDING, attempt_count=0,
        max_attempts=5, queued_at=T0)
    kwargs.update(overrides)
    return CivicNotification(**kwargs)


# ============================================================================
# CivicWatch
# ============================================================================


def test_watch_requires_a_subject_and_question():
    with pytest.raises(ValueError, match="subject"):
        _watch(subject="   ")
    with pytest.raises(ValueError, match="question"):
        _watch(question="")


def test_watch_requires_a_valid_hh_mm_schedule_time():
    for bad in ("6", "25:00", "06:61", "abc", "06-00"):
        with pytest.raises(ValueError, match="HH:MM"):
            _watch(schedule_time=bad)


def test_watch_accepts_boundary_schedule_times():
    assert _watch(schedule_time="00:00").schedule_time == "00:00"
    assert _watch(schedule_time="23:59").schedule_time == "23:59"


def test_paused_watch_needs_paused_at_and_active_one_must_not_have_it():
    with pytest.raises(ValueError, match="paused_at"):
        _watch(status=CivicWatchStatus.PAUSED)
    with pytest.raises(ValueError, match="paused_at"):
        _watch(status=CivicWatchStatus.ACTIVE, paused_at=T0)
    assert _watch(status=CivicWatchStatus.PAUSED, paused_at=T0).paused_at == T0


def test_retired_watch_needs_retired_at_and_is_not_schedulable():
    with pytest.raises(ValueError, match="retired_at"):
        _watch(status=CivicWatchStatus.RETIRED)
    retired = _watch(status=CivicWatchStatus.RETIRED, retired_at=T0)
    assert not retired.is_schedulable
    assert _watch().is_schedulable
    assert not _watch(status=CivicWatchStatus.PAUSED, paused_at=T0).is_schedulable


# ============================================================================
# CivicVocabularyVersion / CivicTerm
# ============================================================================


def test_draft_vocabulary_carries_no_hash_and_no_published_at():
    draft = CivicVocabularyVersion(
        id=1, watch_id=1, version=1, status=CivicVocabularyStatus.DRAFT, content_hash="",
        created_at=T0)
    assert draft.published_at is None
    with pytest.raises(ValueError, match="content_hash"):
        CivicVocabularyVersion(
            id=1, watch_id=1, version=1, status=CivicVocabularyStatus.DRAFT, content_hash="abc",
            created_at=T0)


def test_published_vocabulary_needs_a_hash_and_published_at():
    with pytest.raises(ValueError, match="content_hash"):
        CivicVocabularyVersion(
            id=1, watch_id=1, version=1, status=CivicVocabularyStatus.PUBLISHED, content_hash="",
            created_at=T0, published_at=T0)
    with pytest.raises(ValueError, match="published_at"):
        CivicVocabularyVersion(
            id=1, watch_id=1, version=1, status=CivicVocabularyStatus.PUBLISHED,
            content_hash="abc", created_at=T0)


def test_superseded_vocabulary_needs_superseded_at():
    with pytest.raises(ValueError, match="superseded_at"):
        CivicVocabularyVersion(
            id=1, watch_id=1, version=1, status=CivicVocabularyStatus.SUPERSEDED,
            content_hash="abc", created_at=T0, published_at=T0)


def test_vocabulary_identifiers_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        CivicVocabularyVersion(
            id=1, watch_id=0, version=1, status=CivicVocabularyStatus.DRAFT, content_hash="",
            created_at=T0)


def test_normalize_term_casefolds_and_collapses_whitespace():
    assert normalize_term("  Green   PARTY ") == "green party"
    assert normalize_term("Green Party") == normalize_term("green  party")


def test_term_must_match_its_own_normalized_form():
    with pytest.raises(ValueError, match="normalized"):
        CivicTerm(id=1, vocabulary_version_id=1, term="Green Party",
                  normalized_term="something else", category=CivicTermCategory.PARTY,
                  role=CivicTermRole.REQUIRED)


def test_term_rejects_empty_text():
    with pytest.raises(ValueError, match="must not be empty"):
        CivicTerm(id=1, vocabulary_version_id=1, term="  ", normalized_term="",
                  category=CivicTermCategory.TOPIC, role=CivicTermRole.OPTIONAL)


# ============================================================================
# CivicRun
# ============================================================================


def test_processing_run_needs_phase_claim_started_and_deadline():
    with pytest.raises(ValueError, match="phase and claim_token"):
        _run(status=CivicRunStatus.PROCESSING)
    with pytest.raises(ValueError, match="started_at and deadline_at"):
        _run(status=CivicRunStatus.PROCESSING, phase=CivicRunPhase.GATHERING, claim_token="t")
    ok = _run(status=CivicRunStatus.PROCESSING, phase=CivicRunPhase.GATHERING, claim_token="t",
              started_at=T0, deadline_at=T0 + timedelta(hours=1))
    assert ok.phase is CivicRunPhase.GATHERING


def test_only_a_processing_run_may_carry_a_phase():
    with pytest.raises(ValueError, match="only a processing"):
        _run(phase=CivicRunPhase.GATHERING)


def test_terminal_run_needs_completed_at_and_non_terminal_must_not_have_it():
    for status in (CivicRunStatus.COMPLETED, CivicRunStatus.CANCELLED, CivicRunStatus.FAILED):
        with pytest.raises(ValueError, match="completed_at"):
            _run(status=status)
    with pytest.raises(ValueError, match="completed_at"):
        _run(status=CivicRunStatus.PENDING, completed_at=T0)


def test_only_a_failed_run_may_carry_an_error_code():
    with pytest.raises(ValueError, match="error_code"):
        _run(status=CivicRunStatus.COMPLETED, completed_at=T0, error_code="boom")
    assert _run(status=CivicRunStatus.FAILED, completed_at=T0,
                error_code="deadline_exceeded").error_code == "deadline_exceeded"


# ============================================================================
# CivicBatch
# ============================================================================


def test_building_batch_cannot_be_sealed_and_sealed_batch_needs_hash_and_stamp():
    with pytest.raises(ValueError, match="cannot be sealed"):
        _batch(content_hash="abc")
    with pytest.raises(ValueError, match="content_hash and sealed_at"):
        _batch(status=CivicBatchStatus.SEALED)
    sealed = _batch(status=CivicBatchStatus.SEALED, content_hash="abc", sealed_at=T0)
    assert sealed.is_sealed
    assert not _batch().is_sealed


def test_only_a_delivering_batch_may_hold_a_claim_token():
    with pytest.raises(ValueError, match="claim_token"):
        _batch(status=CivicBatchStatus.SEALED, content_hash="abc", sealed_at=T0,
               claim_token="tok")
    with pytest.raises(ValueError, match="claim_token"):
        _batch(status=CivicBatchStatus.DELIVERING, content_hash="abc", sealed_at=T0)


def test_terminal_batch_needs_settled_at():
    for status in (CivicBatchStatus.DELIVERED, CivicBatchStatus.QUARANTINED,
                   CivicBatchStatus.FAILED):
        with pytest.raises(ValueError, match="settled_at"):
            _batch(status=status, content_hash="abc", sealed_at=T0)
    with pytest.raises(ValueError, match="settled_at"):
        _batch(status=CivicBatchStatus.SEALED, content_hash="abc", sealed_at=T0, settled_at=T0)


def test_batch_requires_an_idempotency_key_and_positive_attempt_budget():
    with pytest.raises(ValueError, match="idempotency_key"):
        _batch(idempotency_key="  ")
    with pytest.raises(ValueError, match="max_attempts"):
        _batch(max_attempts=0)
    with pytest.raises(ValueError, match="attempt_count"):
        _batch(attempt_count=-1)


def test_retries_remaining_never_goes_negative():
    assert _batch(attempt_count=2, max_attempts=5).retries_remaining == 3
    assert _batch(attempt_count=9, max_attempts=5).retries_remaining == 0


# ============================================================================
# CivicCandidate
# ============================================================================


def test_candidate_requires_dedupe_key_title_and_statement():
    with pytest.raises(ValueError, match="dedupe_key"):
        _candidate(dedupe_key="  ")
    with pytest.raises(ValueError, match="title"):
        _candidate(title=" ")
    with pytest.raises(ValueError, match="statement"):
        _candidate(statement="")


def test_settled_candidate_needs_settled_at_and_unsettled_must_not_have_it():
    with pytest.raises(ValueError, match="settled_at"):
        _candidate(status=CivicCandidateStatus.VERIFIED)
    with pytest.raises(ValueError, match="settled_at"):
        _candidate(status=CivicCandidateStatus.DRAFTED, settled_at=T0)


def test_rejected_candidate_needs_a_rejection_code_and_others_may_not_have_one():
    with pytest.raises(ValueError, match="rejection_code"):
        _candidate(status=CivicCandidateStatus.REJECTED, settled_at=T0)
    with pytest.raises(ValueError, match="rejection_code"):
        _candidate(status=CivicCandidateStatus.VERIFIED, settled_at=T0, rejection_code="x")


def test_superseded_candidate_needs_a_replacement_that_is_not_itself():
    with pytest.raises(ValueError, match="superseded_by_candidate_id"):
        _candidate(status=CivicCandidateStatus.SUPERSEDED, settled_at=T0)
    with pytest.raises(ValueError, match="cannot supersede itself"):
        _candidate(id=7, status=CivicCandidateStatus.SUPERSEDED, settled_at=T0,
                   superseded_by_candidate_id=7)
    with pytest.raises(ValueError, match="only a superseded"):
        _candidate(status=CivicCandidateStatus.VERIFIED, settled_at=T0,
                   superseded_by_candidate_id=2)
    ok = _candidate(id=1, status=CivicCandidateStatus.SUPERSEDED, settled_at=T0,
                    superseded_by_candidate_id=2)
    assert ok.superseded_by_candidate_id == 2


def test_only_a_verified_candidate_counts_as_published():
    assert _candidate(status=CivicCandidateStatus.VERIFIED, settled_at=T0).is_published
    assert not _candidate().is_published
    assert not _candidate(status=CivicCandidateStatus.REJECTED, settled_at=T0,
                          rejection_code="thin").is_published


@pytest.mark.parametrize(
    "status,extra",
    [(CivicCandidateStatus.DRAFTED, {}),
     (CivicCandidateStatus.RESEARCHING, {}),
     (CivicCandidateStatus.VERIFYING, {}),
     (CivicCandidateStatus.REJECTED, {"settled_at": T0, "rejection_code": "thin"})],
    ids=lambda value: getattr(value, "value", "extra"))
def test_only_a_candidate_that_reached_verified_can_be_sealed_into_a_batch(status, extra):
    with pytest.raises(ValueError, match="sealed into a Civic Batch"):
        _candidate(status=status, sealed_into_batch_at=T0, **extra)


def test_a_verified_candidate_may_carry_its_seal_stamp():
    sealed = _candidate(status=CivicCandidateStatus.VERIFIED, settled_at=T0,
                        sealed_into_batch_at=T0)
    assert sealed.is_sealed_into_batch
    assert not _candidate(status=CivicCandidateStatus.VERIFIED, settled_at=T0).is_sealed_into_batch


def test_the_seal_stamp_survives_a_later_correction():
    """Envelope membership is immutable: a Candidate superseded tomorrow was still what the
    sealed Batch published."""
    superseded = _candidate(
        id=1, status=CivicCandidateStatus.SUPERSEDED, settled_at=T0,
        superseded_by_candidate_id=2, sealed_into_batch_at=T0)
    assert superseded.is_sealed_into_batch
    assert not superseded.is_published


def test_is_settled_reports_terminal_status():
    assert not _candidate().is_settled
    assert not _candidate(status=CivicCandidateStatus.VERIFYING).is_settled
    assert _candidate(status=CivicCandidateStatus.VERIFIED, settled_at=T0).is_settled
    assert _candidate(status=CivicCandidateStatus.REJECTED, settled_at=T0,
                      rejection_code="thin").is_settled


# ============================================================================
# CivicEvidence + evidence_supports_publication
# ============================================================================


@pytest.mark.parametrize("field", ["source_ref", "external_key", "url", "title"])
def test_evidence_requires_its_identifying_fields(field):
    with pytest.raises(ValueError):
        _evidence(**{field: "  "})


def test_evidence_tiers_are_ranked_strongest_first():
    assert CivicEvidenceTier.OFFICIAL.rank < CivicEvidenceTier.PARTY_PRIMARY.rank
    assert CivicEvidenceTier.PARTY_PRIMARY.rank < CivicEvidenceTier.REPUTABLE_MEDIA.rank
    assert CivicEvidenceTier.REPUTABLE_MEDIA.rank < CivicEvidenceTier.AGGREGATOR.rank


def test_only_official_and_party_primary_are_self_sufficient():
    assert CivicEvidenceTier.OFFICIAL.is_self_sufficient
    assert CivicEvidenceTier.PARTY_PRIMARY.is_self_sufficient
    assert not CivicEvidenceTier.REPUTABLE_MEDIA.is_self_sufficient
    assert not CivicEvidenceTier.AGGREGATOR.is_self_sufficient


def test_no_evidence_never_supports_publication():
    assert not evidence_supports_publication([])


def test_one_official_source_supports_publication():
    assert evidence_supports_publication([_evidence(tier=CivicEvidenceTier.OFFICIAL)])


def test_one_lower_tier_source_alone_does_not_support_publication():
    assert not evidence_supports_publication(
        [_evidence(tier=CivicEvidenceTier.REPUTABLE_MEDIA, publisher="Herald")])


def test_two_independent_publishers_support_publication():
    assert evidence_supports_publication([
        _evidence(id=1, external_key="e1", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
                  publisher="Herald"),
        _evidence(id=2, external_key="e2", tier=CivicEvidenceTier.OTHER_MEDIA,
                  stance=CivicEvidenceStance.CORROBORATING, publisher="Stuff"),
    ])


def test_two_reports_from_the_same_publisher_are_not_independent():
    assert not evidence_supports_publication([
        _evidence(id=1, external_key="e1", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
                  publisher="Herald"),
        _evidence(id=2, external_key="e2", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
                  publisher="herald"),
    ])


def test_any_contradicting_source_vetoes_publication_however_strong_the_support():
    assert not evidence_supports_publication([
        _evidence(id=1, external_key="e1", tier=CivicEvidenceTier.OFFICIAL),
        _evidence(id=2, external_key="e2", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
                  stance=CivicEvidenceStance.CONTRADICTING, publisher="Herald"),
    ])


def test_context_only_evidence_does_not_support_publication():
    assert not evidence_supports_publication([
        _evidence(id=1, external_key="e1", tier=CivicEvidenceTier.OFFICIAL,
                  stance=CivicEvidenceStance.CONTEXT),
        _evidence(id=2, external_key="e2", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
                  stance=CivicEvidenceStance.CONTEXT, publisher="Herald"),
    ])


def test_publisher_falls_back_to_source_ref_when_unset():
    """Two sources with no publisher recorded but different connectors still count as two."""
    assert evidence_supports_publication([
        _evidence(id=1, external_key="e1", source_ref="feed_a",
                  tier=CivicEvidenceTier.REPUTABLE_MEDIA),
        _evidence(id=2, external_key="e2", source_ref="feed_b",
                  tier=CivicEvidenceTier.OTHER_MEDIA),
    ])


# ============================================================================
# CivicResearchAttempt
# ============================================================================


def test_research_attempt_cannot_complete_before_it_starts():
    with pytest.raises(ValueError, match="before it starts"):
        CivicResearchAttempt(
            id=1, candidate_id=1, attempt_number=1, outcome=CivicResearchOutcome.UNRESOLVED,
            evidence_added=0, started_at=T0, completed_at=T0 - timedelta(seconds=1))


def test_errored_research_attempt_needs_an_error_code_and_others_may_not_have_one():
    with pytest.raises(ValueError, match="error_code"):
        CivicResearchAttempt(
            id=1, candidate_id=1, attempt_number=1, outcome=CivicResearchOutcome.ERROR,
            evidence_added=0, started_at=T0, completed_at=T0)
    with pytest.raises(ValueError, match="error_code"):
        CivicResearchAttempt(
            id=1, candidate_id=1, attempt_number=1, outcome=CivicResearchOutcome.UNRESOLVED,
            evidence_added=0, started_at=T0, completed_at=T0, error_code="boom")


def test_resolved_research_attempt_must_have_added_evidence():
    with pytest.raises(ValueError, match="added evidence"):
        CivicResearchAttempt(
            id=1, candidate_id=1, attempt_number=1, outcome=CivicResearchOutcome.RESOLVED,
            evidence_added=0, started_at=T0, completed_at=T0)


# ============================================================================
# CivicVerification
# ============================================================================


def _verification(**overrides) -> CivicVerification:
    kwargs = dict(
        id=1, candidate_id=1, attempt_number=1, verdict=CivicVerificationVerdict.SUPPORTED,
        supporting_evidence_count=1, contradicting_evidence_count=0, model="gpt-5",
        recorded_at=T0)
    kwargs.update(overrides)
    return CivicVerification(**kwargs)


def test_supported_verdict_requires_supporting_evidence():
    with pytest.raises(ValueError, match="supporting evidence"):
        _verification(supporting_evidence_count=0)


def test_supported_verdict_is_impossible_beside_contradicting_evidence():
    with pytest.raises(ValueError, match="cannot be SUPPORTED"):
        _verification(supporting_evidence_count=3, contradicting_evidence_count=1)


def test_contradicted_verdict_requires_contradicting_evidence():
    with pytest.raises(ValueError, match="contradicting evidence"):
        _verification(verdict=CivicVerificationVerdict.CONTRADICTED,
                      contradicting_evidence_count=0)


def test_errored_verification_needs_an_error_code_and_skips_the_model_requirement():
    with pytest.raises(ValueError, match="error_code"):
        _verification(verdict=CivicVerificationVerdict.ERROR, supporting_evidence_count=0)
    errored = _verification(verdict=CivicVerificationVerdict.ERROR, supporting_evidence_count=0,
                            model="", error_code="model_unavailable")
    assert not errored.permits_publication


def test_non_errored_verification_needs_the_model_that_produced_it():
    with pytest.raises(ValueError, match="model"):
        _verification(model="  ")


def test_only_supported_permits_publication():
    assert _verification().permits_publication
    assert not _verification(verdict=CivicVerificationVerdict.INSUFFICIENT,
                             supporting_evidence_count=0).permits_publication


def test_verification_counts_must_not_be_negative():
    with pytest.raises(ValueError, match="must not be negative"):
        _verification(contradicting_evidence_count=-1)


# ============================================================================
# CivicNotification + CivicDeliveryReceipt
# ============================================================================


def test_only_a_sending_notification_may_hold_a_claim_token():
    with pytest.raises(ValueError, match="claim_token"):
        _notification(claim_token="tok")
    with pytest.raises(ValueError, match="claim_token"):
        _notification(status=CivicNotificationStatus.SENDING)


def test_terminal_notification_needs_settled_at():
    for status in (CivicNotificationStatus.SENT, CivicNotificationStatus.FAILED,
                   CivicNotificationStatus.SUPPRESSED):
        with pytest.raises(ValueError, match="settled_at"):
            _notification(status=status)
    with pytest.raises(ValueError, match="settled_at"):
        _notification(status=CivicNotificationStatus.PENDING, settled_at=T0)


def test_notification_requires_kind_subject_dedupe_key_and_a_positive_attempt_budget():
    with pytest.raises(ValueError, match="kind"):
        _notification(kind=" ")
    with pytest.raises(ValueError, match="dedupe_key"):
        _notification(dedupe_key="")
    with pytest.raises(ValueError, match="subject"):
        _notification(subject="")
    with pytest.raises(ValueError, match="max_attempts"):
        _notification(max_attempts=0)


def test_receipt_requires_key_hash_and_target():
    def receipt(**overrides):
        kwargs = dict(
            id=1, batch_id=1, idempotency_key="k", content_hash="h",
            channel=CivicDeliveryChannel.SPOOL, target="/spool", delivered_at=T0)
        kwargs.update(overrides)
        return CivicDeliveryReceipt(**kwargs)

    with pytest.raises(ValueError, match="idempotency_key"):
        receipt(idempotency_key=" ")
    with pytest.raises(ValueError, match="content_hash"):
        receipt(content_hash="")
    with pytest.raises(ValueError, match="target"):
        receipt(target="  ")
    with pytest.raises(ValueError, match="batch_id"):
        receipt(batch_id=0)


def test_every_civic_dataclass_is_frozen():
    with pytest.raises(Exception):
        _watch().status = CivicWatchStatus.PAUSED
    with pytest.raises(Exception):
        _run().status = CivicRunStatus.PROCESSING
    with pytest.raises(Exception):
        _batch().status = CivicBatchStatus.SEALED
    with pytest.raises(Exception):
        _candidate().status = CivicCandidateStatus.VERIFIED
