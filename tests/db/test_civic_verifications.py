# tests/db/test_civic_verifications.py
"""Civic Research Attempt and Civic Verification persistence: a durable research budget, and
append-only verifier records that cannot claim more than their evidence allows."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_candidates import add_candidate_evidence, create_candidate
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import (MAX_RESEARCH_ATTEMPTS, get_latest_verification,
                                             has_supporting_verification,
                                             list_research_attempts, list_verifications,
                                             record_research_attempt, record_verification,
                                             reserve_research_attempt)
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicEvidenceStance, CivicEvidenceTier, CivicResearchOutcome,
                                   CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def candidate(conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    return create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)


def _evidence(conn, candidate_id, key, stance=CivicEvidenceStance.SUPPORTING,
              tier=CivicEvidenceTier.OFFICIAL):
    return add_candidate_evidence(
        conn, candidate_id, "official_feed", key, f"https://example.test/{key}", "Release",
        tier, stance, T0, publisher="Beehive")


# ============================================================================
# Research budget
# ============================================================================


def test_reserving_returns_sequential_attempt_numbers(conn, candidate):
    assert reserve_research_attempt(conn, candidate.id) == 1
    assert reserve_research_attempt(conn, candidate.id) == 2


def test_the_research_budget_is_bounded(conn, candidate):
    for _ in range(MAX_RESEARCH_ATTEMPTS):
        assert reserve_research_attempt(conn, candidate.id) is not None
    assert reserve_research_attempt(conn, candidate.id) is None


def test_the_budget_is_spent_durably_even_if_no_outcome_is_ever_recorded(conn, candidate):
    """A worker that crashes mid-pass leaks exactly one reservation -- it can never quietly buy
    itself another by simply not writing the outcome."""
    reserve_research_attempt(conn, candidate.id)
    row = conn.execute(
        "SELECT research_attempt_count FROM civic_candidates WHERE id = ?",
        (candidate.id,)).fetchone()
    assert row["research_attempt_count"] == 1
    assert list_research_attempts(conn, candidate.id) == []


def test_reserving_for_a_missing_candidate_returns_none(conn):
    assert reserve_research_attempt(conn, 999) is None


def test_recording_an_outcome_against_a_reserved_number(conn, candidate):
    number = reserve_research_attempt(conn, candidate.id)
    attempt = record_research_attempt(
        conn, candidate.id, number, CivicResearchOutcome.RESOLVED, T0, T1, evidence_added=2,
        notes="found the official release")
    assert attempt.attempt_number == 1
    assert attempt.outcome is CivicResearchOutcome.RESOLVED
    assert attempt.evidence_added == 2
    assert list_research_attempts(conn, candidate.id) == [attempt]


def test_research_attempts_are_append_only(conn, candidate):
    number = reserve_research_attempt(conn, candidate.id)
    record_research_attempt(
        conn, candidate.id, number, CivicResearchOutcome.UNRESOLVED, T0, T1)
    with pytest.raises(ValueError, match="already recorded"):
        record_research_attempt(
            conn, candidate.id, number, CivicResearchOutcome.RESOLVED, T0, T1, evidence_added=1)
    assert len(list_research_attempts(conn, candidate.id)) == 1


def test_a_resolved_attempt_must_have_added_evidence(conn, candidate):
    number = reserve_research_attempt(conn, candidate.id)
    with pytest.raises(ValueError, match="added evidence"):
        record_research_attempt(
            conn, candidate.id, number, CivicResearchOutcome.RESOLVED, T0, T1, evidence_added=0)


def test_an_errored_attempt_needs_an_error_code(conn, candidate):
    number = reserve_research_attempt(conn, candidate.id)
    with pytest.raises(ValueError, match="error_code"):
        record_research_attempt(conn, candidate.id, number, CivicResearchOutcome.ERROR, T0, T1)
    attempt = record_research_attempt(
        conn, candidate.id, number, CivicResearchOutcome.ERROR, T0, T1,
        error_code="connector_timeout")
    assert attempt.error_code == "connector_timeout"


def test_recording_for_a_missing_candidate_raises(conn):
    with pytest.raises(ValueError, match="no Civic Candidate"):
        record_research_attempt(conn, 999, 1, CivicResearchOutcome.UNRESOLVED, T0, T1)


# ============================================================================
# Verification records
# ============================================================================


def test_verifications_are_numbered_per_candidate(conn, candidate):
    _evidence(conn, candidate.id, "e1")
    first = record_verification(
        conn, candidate.id, CivicVerificationVerdict.INSUFFICIENT, "gpt-5", T0)
    second = record_verification(
        conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T1)
    assert (first.attempt_number, second.attempt_number) == (1, 2)
    assert [v.id for v in list_verifications(conn, candidate.id)] == [first.id, second.id]
    assert get_latest_verification(conn, candidate.id).id == second.id


def test_evidence_counts_are_read_from_the_database_not_the_caller(conn, candidate):
    _evidence(conn, candidate.id, "e1")
    _evidence(conn, candidate.id, "e2", stance=CivicEvidenceStance.CORROBORATING)
    _evidence(conn, candidate.id, "e3", stance=CivicEvidenceStance.CONTEXT)
    verification = record_verification(
        conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    assert verification.supporting_evidence_count == 2
    assert verification.contradicting_evidence_count == 0


def test_a_supported_verdict_without_supporting_evidence_is_refused(conn, candidate):
    with pytest.raises(ValueError, match="supporting evidence"):
        record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    assert list_verifications(conn, candidate.id) == []


def test_a_supported_verdict_beside_contradicting_evidence_is_refused(conn, candidate):
    _evidence(conn, candidate.id, "e1")
    _evidence(conn, candidate.id, "e2", stance=CivicEvidenceStance.CONTRADICTING)
    with pytest.raises(ValueError, match="cannot be SUPPORTED"):
        record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)


def test_a_contradicted_verdict_needs_contradicting_evidence(conn, candidate):
    _evidence(conn, candidate.id, "e1")
    with pytest.raises(ValueError, match="contradicting evidence"):
        record_verification(
            conn, candidate.id, CivicVerificationVerdict.CONTRADICTED, "gpt-5", T0)
    _evidence(conn, candidate.id, "e2", stance=CivicEvidenceStance.CONTRADICTING)
    verification = record_verification(
        conn, candidate.id, CivicVerificationVerdict.CONTRADICTED, "gpt-5", T0)
    assert verification.contradicting_evidence_count == 1


def test_an_errored_verdict_records_no_counts_and_needs_an_error_code(conn, candidate):
    _evidence(conn, candidate.id, "e1")
    with pytest.raises(ValueError, match="error_code"):
        record_verification(conn, candidate.id, CivicVerificationVerdict.ERROR, "", T0)
    errored = record_verification(
        conn, candidate.id, CivicVerificationVerdict.ERROR, "", T0,
        error_code="model_unavailable")
    assert errored.supporting_evidence_count == 0
    assert not errored.permits_publication


def test_a_non_errored_verdict_needs_the_model_that_produced_it(conn, candidate):
    _evidence(conn, candidate.id, "e1")
    with pytest.raises(ValueError, match="model"):
        record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "  ", T0)


def test_recording_for_a_missing_candidate_raises_and_writes_nothing(conn):
    with pytest.raises(ValueError, match="no Civic Candidate"):
        record_verification(conn, 999, CivicVerificationVerdict.INSUFFICIENT, "gpt-5", T0)


def test_a_supporting_verdict_is_never_retracted_by_a_later_error(conn, candidate):
    """A SUPPORTED verdict is a fact that was established; a transient model failure afterwards
    must not silently un-publish what it justified."""
    _evidence(conn, candidate.id, "e1")
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    record_verification(
        conn, candidate.id, CivicVerificationVerdict.ERROR, "", T1, error_code="timeout")
    assert has_supporting_verification(conn, candidate.id)
    assert get_latest_verification(conn, candidate.id).verdict is CivicVerificationVerdict.ERROR


def test_no_verification_means_no_support(conn, candidate):
    assert not has_supporting_verification(conn, candidate.id)
    assert get_latest_verification(conn, candidate.id) is None


def test_the_schema_itself_refuses_an_unsupported_supported_row(conn, candidate):
    """Even a caller bypassing this module cannot write a SUPPORTED verdict with no supporting
    evidence -- the CHECK constraint mirrors the domain invariant."""
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_verifications (candidate_id, attempt_number, verdict, "
            "supporting_evidence_count, contradicting_evidence_count, model, recorded_at) "
            "VALUES (?, 1, 'supported', 0, 0, 'gpt-5', ?)",
            (candidate.id, T0.isoformat()))


def test_the_schema_itself_refuses_duplicate_attempt_numbers(conn, candidate):
    import sqlite3
    _evidence(conn, candidate.id, "e1")
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_verifications (candidate_id, attempt_number, verdict, "
            "supporting_evidence_count, contradicting_evidence_count, model, recorded_at) "
            "VALUES (?, 1, 'insufficient', 0, 0, 'gpt-5', ?)",
            (candidate.id, T0.isoformat()))
