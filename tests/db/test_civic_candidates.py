# tests/db/test_civic_candidates.py
"""Civic Candidate and Civic Evidence persistence: cross-day deduplication, idempotent evidence
capture, and the two gates that stand between a draft statement and publication."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (get_or_create_batch_if_claimed, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence,
                                          candidate_has_publishable_evidence, create_candidate,
                                          get_candidate, get_candidate_by_dedupe_key,
                                          list_batch_candidates, list_candidate_evidence,
                                          list_run_candidates, mark_candidate_verified,
                                          reject_candidate,
                                          reject_stale_verified_capacity_carryover_if_claimed,
                                          set_candidate_status,
                                          supersede_candidate)
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier,
                                   CivicRunStatus, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


def _open_batch(conn, watch_id, slot_at):
    run = enqueue_civic_run(conn, watch_id, slot_at, slot_at)
    lease = claim_civic_run(conn, run.id, slot_at, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, slot_at)
    return lease.run, batch


def _finish_run(conn, run, now):
    """Seals the Run's envelope and terminalizes it. Any Candidate still unsettled is rejected
    first, because a seal refuses to freeze work that is still in flight."""
    batch_id = get_batch_id(conn, run.id)
    for candidate in list_batch_candidates(conn, batch_id):
        if not candidate.is_settled:
            reject_candidate(conn, candidate.id, "run_ended", now)
    seal_batch_if_claimed(conn, batch_id, run.id, run.claim_token, now)
    complete_civic_run(conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, now)


def get_batch_id(conn, run_id):
    return conn.execute(
        "SELECT id FROM civic_batches WHERE run_id = ?", (run_id,)).fetchone()["id"]


@pytest.fixture
def batch(conn, watch_id):
    _run, batch = _open_batch(conn, watch_id, T0)
    return batch


def _official(conn, candidate_id, key="e1", **overrides):
    kwargs = dict(
        source_ref="official_feed", external_key=key, url=f"https://example.test/{key}",
        title="Official release", tier=CivicEvidenceTier.OFFICIAL,
        stance=CivicEvidenceStance.SUPPORTING, publisher="Beehive")
    kwargs.update(overrides)
    return add_candidate_evidence(conn, candidate_id, now=T0, **kwargs)


def _publishable(conn, batch, dedupe_key="dk-1"):
    candidate = create_candidate(conn, batch.id, dedupe_key, "Title", "Statement", T0)
    _official(conn, candidate.id)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    return candidate


# ============================================================================
# Creation and deduplication
# ============================================================================


def test_creates_a_drafted_candidate(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    assert candidate.status is CivicCandidateStatus.DRAFTED
    assert candidate.batch_id == batch.id
    assert candidate.settled_at is None
    assert candidate.research_attempt_count == 0


def test_the_same_key_inside_one_batch_returns_the_same_candidate(conn, batch):
    first = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    second = create_candidate(conn, batch.id, "dk-1", "Different title", "Different", T0)
    assert first.id == second.id
    assert second.title == "Title"
    assert len(list_batch_candidates(conn, batch.id)) == 1


def test_deduplication_spans_days_not_just_one_batch(conn, watch_id):
    """The whole point of a Watch-scoped dedupe key: yesterday's story is never republished."""
    run_one, batch_one = _open_batch(conn, watch_id, T0)
    first = create_candidate(conn, batch_one.id, "dk-1", "Rates decision", "Statement", T0)
    _finish_run(conn, run_one, T0)

    _run_two, batch_two = _open_batch(conn, watch_id, T0 + DAY)
    again = create_candidate(conn, batch_two.id, "dk-1", "Rates decision", "Statement", T0 + DAY)

    assert again.id == first.id
    assert again.batch_id == batch_one.id
    assert list_batch_candidates(conn, batch_two.id) == []


def test_a_rejected_candidate_still_blocks_the_same_key_tomorrow(conn, watch_id):
    run_one, batch_one = _open_batch(conn, watch_id, T0)
    candidate = create_candidate(conn, batch_one.id, "dk-1", "T", "S", T0)
    reject_candidate(conn, candidate.id, "insufficient_evidence", T0)
    _finish_run(conn, run_one, T0)

    _run_two, batch_two = _open_batch(conn, watch_id, T0 + DAY)
    again = create_candidate(conn, batch_two.id, "dk-1", "T", "S", T0 + DAY)
    assert again.status is CivicCandidateStatus.REJECTED


def test_two_watches_may_use_the_same_dedupe_key_independently(conn):
    first_watch = create_civic_watch(conn, "A", "Q", T0).id
    second_watch = create_civic_watch(conn, "B", "Q", T0).id
    _run_a, batch_a = _open_batch(conn, first_watch, T0)
    _run_b, batch_b = _open_batch(conn, second_watch, T0)
    a = create_candidate(conn, batch_a.id, "shared-key", "T", "S", T0)
    b = create_candidate(conn, batch_b.id, "shared-key", "T", "S", T0)
    assert a.id != b.id


def test_an_empty_dedupe_key_is_rejected(conn, batch):
    with pytest.raises(ValueError, match="dedupe_key"):
        create_candidate(conn, batch.id, "  ", "T", "S", T0)


def test_creating_against_a_missing_batch_raises(conn):
    with pytest.raises(ValueError, match="no Civic Batch"):
        create_candidate(conn, 999, "dk", "T", "S", T0)


def test_lookup_helpers(conn, watch_id, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    assert get_candidate(conn, candidate.id) == candidate
    assert get_candidate_by_dedupe_key(conn, watch_id, "dk-1") == candidate
    assert get_candidate_by_dedupe_key(conn, watch_id, "nope") is None
    assert [c.id for c in list_run_candidates(conn, candidate.run_id)] == [candidate.id]
    assert list_run_candidates(
        conn, candidate.run_id, CivicCandidateStatus.VERIFIED) == []


# ============================================================================
# Evidence
# ============================================================================


def test_evidence_is_recorded_with_its_tier_and_stance(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    evidence = _official(conn, candidate.id)
    assert evidence.tier is CivicEvidenceTier.OFFICIAL
    assert evidence.stance is CivicEvidenceStance.SUPPORTING
    assert evidence.captured_at == T0
    assert list_candidate_evidence(conn, candidate.id) == [evidence]


def test_recapturing_the_same_source_is_idempotent(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    first = _official(conn, candidate.id, key="e1")
    second = _official(conn, candidate.id, key="e1", stance=CivicEvidenceStance.CONTRADICTING)
    assert first.id == second.id
    assert second.stance is CivicEvidenceStance.SUPPORTING
    assert len(list_candidate_evidence(conn, candidate.id)) == 1


def test_the_same_source_may_support_two_different_candidates(conn, batch):
    first = create_candidate(conn, batch.id, "dk-1", "T1", "S1", T0)
    second = create_candidate(conn, batch.id, "dk-2", "T2", "S2", T0)
    _official(conn, first.id, key="shared")
    _official(conn, second.id, key="shared")
    assert len(list_candidate_evidence(conn, first.id)) == 1
    assert len(list_candidate_evidence(conn, second.id)) == 1


def test_evidence_cannot_be_added_to_a_settled_candidate(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    reject_candidate(conn, candidate.id, "off_topic", T0)
    with pytest.raises(ValueError, match="cannot add Civic Evidence"):
        _official(conn, candidate.id, key="late")


def test_evidence_for_a_missing_candidate_raises(conn):
    with pytest.raises(ValueError, match="no Civic Candidate"):
        _official(conn, 999)


def test_publishable_evidence_check_reflects_the_domain_rule(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    assert not candidate_has_publishable_evidence(conn, candidate.id)
    _official(conn, candidate.id, key="e1")
    assert candidate_has_publishable_evidence(conn, candidate.id)
    _official(conn, candidate.id, key="e2", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
              stance=CivicEvidenceStance.CONTRADICTING, publisher="Herald")
    assert not candidate_has_publishable_evidence(conn, candidate.id)


# ============================================================================
# Status transitions
# ============================================================================


def test_status_moves_through_the_legal_path(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    researching = set_candidate_status(conn, candidate.id, CivicCandidateStatus.RESEARCHING, T0)
    assert researching.status is CivicCandidateStatus.RESEARCHING
    verifying = set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    assert verifying.status is CivicCandidateStatus.VERIFYING
    assert verifying.settled_at is None


def test_an_illegal_status_move_is_refused(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    with pytest.raises(ValueError, match="invalid Civic Candidate transition"):
        set_candidate_status(conn, candidate.id, CivicCandidateStatus.DRAFTED, T0)


def test_settling_statuses_need_their_dedicated_functions(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    for target in (CivicCandidateStatus.VERIFIED, CivicCandidateStatus.REJECTED,
                   CivicCandidateStatus.SUPERSEDED):
        with pytest.raises(ValueError, match="dedicated function"):
            set_candidate_status(conn, candidate.id, target, T0)


def test_status_change_on_a_missing_candidate_raises(conn):
    with pytest.raises(ValueError, match="no Civic Candidate"):
        set_candidate_status(conn, 999, CivicCandidateStatus.VERIFYING, T0)


# ============================================================================
# Publication gates
# ============================================================================


def test_publication_requires_both_evidence_and_a_supporting_verdict(conn, batch):
    candidate = _publishable(conn, batch)
    verified = mark_candidate_verified(conn, candidate.id, T0)
    assert verified.status is CivicCandidateStatus.VERIFIED
    assert verified.settled_at == T0
    assert verified.is_published


def test_stale_carryover_rejection_refuses_an_ordinary_verified_candidate(
    conn, watch_id,
):
    run, batch = _open_batch(conn, watch_id, T0)
    candidate = _publishable(conn, batch)
    mark_candidate_verified(conn, candidate.id, T0)
    with pytest.raises(ValueError, match="not an adopted capacity carryover"):
        reject_stale_verified_capacity_carryover_if_claimed(
            conn,
            run_id=run.id,
            claim_token=run.claim_token,
            candidate_id=candidate.id,
            now=T0,
        )
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFIED


def test_stale_carryover_rejection_refuses_a_sealed_verified_candidate(
    conn, watch_id,
):
    run, batch = _open_batch(conn, watch_id, T0)
    candidate = _publishable(conn, batch)
    mark_candidate_verified(conn, candidate.id, T0)
    seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    with pytest.raises(ValueError, match="unsealed VERIFIED"):
        reject_stale_verified_capacity_carryover_if_claimed(
            conn,
            run_id=run.id,
            claim_token=run.claim_token,
            candidate_id=candidate.id,
            now=T0,
        )
    sealed = get_candidate(conn, candidate.id)
    assert sealed.status is CivicCandidateStatus.VERIFIED
    assert sealed.sealed_into_batch_at is not None


def test_publication_without_evidence_is_refused(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    with pytest.raises(ValueError, match="publishable Civic Evidence"):
        mark_candidate_verified(conn, candidate.id, T0)
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFYING


def test_publication_with_only_one_low_tier_source_is_refused(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    _official(conn, candidate.id, key="e1", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
              publisher="Herald")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    with pytest.raises(ValueError, match="publishable Civic Evidence"):
        mark_candidate_verified(conn, candidate.id, T0)


def test_publication_without_a_supporting_verification_is_refused(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    _official(conn, candidate.id)
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, candidate.id, CivicVerificationVerdict.INSUFFICIENT, "gpt-5", T0)
    with pytest.raises(ValueError, match="no supporting Civic Verification"):
        mark_candidate_verified(conn, candidate.id, T0)


def test_a_contradicting_source_landing_late_blocks_publication(conn, batch):
    """The evidence gate is re-read at publication time, so a contradiction recorded after the
    verdict still vetoes."""
    candidate = _publishable(conn, batch)
    _official(conn, candidate.id, key="e2", tier=CivicEvidenceTier.REPUTABLE_MEDIA,
              stance=CivicEvidenceStance.CONTRADICTING, publisher="Herald")
    with pytest.raises(ValueError, match="publishable Civic Evidence"):
        mark_candidate_verified(conn, candidate.id, T0)


def test_a_drafted_candidate_cannot_jump_straight_to_verified(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    _official(conn, candidate.id)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    with pytest.raises(ValueError, match="invalid Civic Candidate transition"):
        mark_candidate_verified(conn, candidate.id, T0)


def test_rejection_requires_a_code(conn, batch):
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    with pytest.raises(ValueError, match="rejection_code"):
        reject_candidate(conn, candidate.id, "   ", T0)
    rejected = reject_candidate(conn, candidate.id, "off_topic", T0)
    assert rejected.status is CivicCandidateStatus.REJECTED
    assert rejected.rejection_code == "off_topic"
    assert rejected.settled_at == T0


def test_a_rejected_candidate_can_never_be_published_later(conn, batch):
    candidate = _publishable(conn, batch)
    reject_candidate(conn, candidate.id, "owner_veto", T0)
    with pytest.raises(ValueError, match="invalid Civic Candidate transition"):
        mark_candidate_verified(conn, candidate.id, T0)


# ============================================================================
# Corrections
# ============================================================================


def test_a_correction_supersedes_rather_than_edits(conn, watch_id, batch):
    original = _publishable(conn, batch, dedupe_key="dk-1")
    mark_candidate_verified(conn, original.id, T0)
    replacement = create_candidate(conn, batch.id, "dk-2", "Correction", "Corrected", T0)

    superseded = supersede_candidate(conn, original.id, replacement.id, T0 + DAY)
    assert superseded.status is CivicCandidateStatus.SUPERSEDED
    assert superseded.superseded_by_candidate_id == replacement.id
    assert superseded.settled_at == T0 + DAY
    assert superseded.statement == "Statement"


def test_only_a_verified_candidate_can_be_superseded(conn, batch):
    original = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    replacement = create_candidate(conn, batch.id, "dk-2", "T2", "S2", T0)
    with pytest.raises(ValueError, match="invalid Civic Candidate transition"):
        supersede_candidate(conn, original.id, replacement.id, T0)


def test_a_candidate_cannot_supersede_itself(conn, batch):
    candidate = _publishable(conn, batch)
    mark_candidate_verified(conn, candidate.id, T0)
    with pytest.raises(ValueError, match="cannot supersede itself"):
        supersede_candidate(conn, candidate.id, candidate.id, T0)


def test_superseding_by_an_unknown_candidate_is_refused(conn, batch):
    candidate = _publishable(conn, batch)
    mark_candidate_verified(conn, candidate.id, T0)
    with pytest.raises(ValueError, match="no Civic Candidate with id=999"):
        supersede_candidate(conn, candidate.id, 999, T0)


def test_a_correction_cannot_be_deleted_out_from_under_the_statement_it_corrected(conn, batch):
    import sqlite3
    original = _publishable(conn, batch, dedupe_key="dk-1")
    mark_candidate_verified(conn, original.id, T0)
    replacement = create_candidate(conn, batch.id, "dk-2", "Correction", "Corrected", T0)
    supersede_candidate(conn, original.id, replacement.id, T0)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM civic_candidates WHERE id = ?", (replacement.id,))
    conn.rollback()
    survivor = get_candidate(conn, original.id)
    assert survivor.status is CivicCandidateStatus.SUPERSEDED
    assert survivor.superseded_by_candidate_id == replacement.id
