# tests/db/test_civic_qualifications.py
"""Durable receipts for the pure evidence engine: replay, drift, and the one bounded re-decision.

The engine is deterministic, so the interesting behavior is not "does it decide the same way
twice" but what the *database* does when a worker comes back to a Candidate it has already
decided. Three answers, and only three: replay it (identical inputs), refuse it (changed inputs),
or record a second decision because the single bounded research pass has been spent.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.evidence import (CivicCandidateClaim, CivicClaimCategory, CivicContentClass,
                                    CivicEvidenceItem, CivicOwnershipRegistry, CivicPublisher,
                                    CivicPublisherKind, CivicQualificationOutcome,
                                    CivicRecordKind, CivicRecordOperation, CivicSourceKind,
                                    qualify_candidate)
from beehive.civic.pipeline import CIVIC_ENGINE_VERSION
from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_candidates import create_candidate
from beehive.db.civic_qualifications import (CivicQualificationDriftError, get_latest_qualification,
                                             get_qualification, list_qualifications,
                                             list_run_qualifications, record_qualification)
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)

_REGISTRY = CivicOwnershipRegistry.build([
    CivicPublisher(publisher_id="beehive", name="Beehive", kind=CivicPublisherKind.OFFICIAL,
                   source_kind=CivicSourceKind.GOVERNMENT),
])


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def context(conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    return run.id, candidate


def _claim(candidate_id, *, category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
           record_kind=CivicRecordKind.TIMELINE, durable=True):
    return CivicCandidateClaim(
        candidate_id=str(candidate_id), fingerprint="dk-1", record_kind=record_kind,
        operation=CivicRecordOperation.ADD, subject_id="green-party", record_key="dk-1",
        event_date=T0.date(), category=category, content_class=CivicContentClass.FACTUAL,
        durable=durable, claim_text="A durable event")


def _item(key="e1"):
    return CivicEvidenceItem(
        evidence_id=key, publisher_id="beehive", source_kind=CivicSourceKind.GOVERNMENT,
        evidence_tier=CivicEvidenceTier.OFFICIAL, stance=CivicEvidenceStance.SUPPORTING,
        canonical_url=f"https://example.test/{key}", title="Release", subject_id="green-party",
        event_date=T0.date(), category=CivicClaimCategory.PARTY_TIMELINE_EVENT, published_at=T0,
        accessible_full_text=True, paywalled=False, anonymous_source_dependency=False)


def _record(conn, run_id, candidate_id, qualification, *, input_hash="a" * 64, attempts=0,
            engine_version=CIVIC_ENGINE_VERSION, now=T0):
    conn.execute("BEGIN IMMEDIATE")
    try:
        record = record_qualification(
            conn, candidate_id=candidate_id, run_id=run_id, qualification=qualification,
            engine_version=engine_version, input_hash=input_hash,
            output_hash="b" * 64, research_attempts_used=attempts, now=now)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return record


def _qualified(candidate_id):
    return qualify_candidate(_claim(candidate_id), [_item()], registry=_REGISTRY)


def _deferred(candidate_id):
    """One masthead with no ownership registered: the engine cannot show independence, so the
    Candidate defers on evidence -- the only outcome the research pass may re-open."""
    item = CivicEvidenceItem(
        evidence_id="m1", publisher_id="unknown-masthead",
        source_kind=CivicSourceKind.INDEPENDENT_NEWS,
        evidence_tier=CivicEvidenceTier.REPUTABLE_MEDIA, stance=CivicEvidenceStance.SUPPORTING,
        canonical_url="https://example.test/m1", title="Report", subject_id="green-party",
        event_date=T0.date(), category=CivicClaimCategory.PARTY_TIMELINE_EVENT, published_at=T0,
        accessible_full_text=True, paywalled=False, anonymous_source_dependency=False)
    return qualify_candidate(_claim(candidate_id), [item], registry=_REGISTRY)


# ============================================================================
# Recording
# ============================================================================


def test_a_decision_is_stored_with_its_reasons_and_engine_version(conn, context):
    run_id, candidate = context
    record = _record(conn, run_id, candidate.id, _qualified(candidate.id))
    assert record.outcome is CivicQualificationOutcome.QUALIFIED
    assert record.is_publishable
    assert record.engine_version == CIVIC_ENGINE_VERSION
    assert record.reasons
    assert record.evidence_used == ("e1",)
    assert record.decided_at == T0


def test_the_deferred_case_records_its_defer_reasons(conn, context):
    run_id, candidate = context
    record = _record(conn, run_id, candidate.id, _deferred(candidate.id))
    assert record.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
    assert record.is_research_retryable
    assert not record.is_publishable


def test_a_candidate_outside_the_run_is_rejected(conn, context):
    run_id, candidate = context
    with pytest.raises(ValueError, match="no Civic Candidate"):
        _record(conn, run_id + 999, candidate.id, _qualified(candidate.id))


# ============================================================================
# Replay
# ============================================================================


def test_identical_inputs_replay_the_stored_decision(conn, context):
    run_id, candidate = context
    first = _record(conn, run_id, candidate.id, _qualified(candidate.id))
    second = _record(
        conn, run_id, candidate.id, _qualified(candidate.id), now=T0 + timedelta(hours=3))
    assert first.id == second.id
    assert second.decided_at == T0, "a replay must not restamp the decision"
    assert len(list_qualifications(conn, candidate.id)) == 1


def test_changed_inputs_raise_rather_than_overturning_the_decision(conn, context):
    run_id, candidate = context
    _record(conn, run_id, candidate.id, _qualified(candidate.id), input_hash="a" * 64)
    with pytest.raises(CivicQualificationDriftError) as excinfo:
        _record(conn, run_id, candidate.id, _qualified(candidate.id), input_hash="c" * 64)
    assert excinfo.value.candidate_id == candidate.id
    assert excinfo.value.stored_hash == "a" * 64
    assert excinfo.value.offered_hash == "c" * 64


def test_a_different_engine_build_is_drift_too(conn, context):
    """The same inputs decided by a different build of the engine is not a replay: the code that
    reached the stored conclusion is part of what has to match."""
    run_id, candidate = context
    _record(conn, run_id, candidate.id, _qualified(candidate.id))
    with pytest.raises(CivicQualificationDriftError):
        _record(
            conn, run_id, candidate.id, _qualified(candidate.id),
            engine_version="civic-evidence/999")


# ============================================================================
# The one bounded re-decision
# ============================================================================


def test_the_research_pass_records_a_second_decision_beside_the_first(conn, context):
    run_id, candidate = context
    first = _record(conn, run_id, candidate.id, _deferred(candidate.id))
    second = _record(
        conn, run_id, candidate.id, _qualified(candidate.id), input_hash="d" * 64, attempts=1)
    assert first.id != second.id
    records = list_qualifications(conn, candidate.id)
    assert [record.research_attempts_used for record in records] == [0, 1]
    assert get_latest_qualification(conn, candidate.id).id == second.id


def test_a_final_decision_cannot_be_re_opened(conn, context):
    """Only an evidence deferral is re-openable. A qualified, screened-out, duplicate or
    unsupported Candidate is decided by something the research pass cannot change."""
    run_id, candidate = context
    _record(conn, run_id, candidate.id, _qualified(candidate.id))
    with pytest.raises(ValueError, match="is final and cannot be re-decided"):
        _record(
            conn, run_id, candidate.id, _deferred(candidate.id), input_hash="d" * 64,
            attempts=1)


def test_a_research_decision_without_a_first_decision_is_rejected(conn, context):
    run_id, candidate = context
    with pytest.raises(ValueError, match="needs the first qualification on record"):
        _record(conn, run_id, candidate.id, _qualified(candidate.id), attempts=1)


def test_a_third_pass_is_structurally_impossible(conn, context):
    run_id, candidate = context
    with pytest.raises(ValueError, match="must be 0 or 1"):
        _record(conn, run_id, candidate.id, _qualified(candidate.id), attempts=2)


def test_going_backwards_to_an_earlier_pass_is_rejected(conn, context):
    run_id, candidate = context
    _record(conn, run_id, candidate.id, _deferred(candidate.id))
    _record(conn, run_id, candidate.id, _qualified(candidate.id), input_hash="d" * 64,
            attempts=1)
    with pytest.raises(CivicQualificationDriftError):
        _record(conn, run_id, candidate.id, _deferred(candidate.id), input_hash="e" * 64)


# ============================================================================
# Reading
# ============================================================================


def test_run_qualifications_come_back_in_a_stable_order(conn, context):
    from beehive.db.civic_batches import get_batch_for_run

    run_id, candidate = context
    batch = get_batch_for_run(conn, run_id)
    other = create_candidate(conn, batch.id, "dk-2", "T", "S", T0)
    _record(conn, run_id, candidate.id, _qualified(candidate.id))
    _record(conn, run_id, other.id, _qualified(other.id))
    records = list_run_qualifications(conn, run_id)
    assert [record.candidate_id for record in records] == sorted(
        [candidate.id, other.id])


def test_an_undecided_candidate_has_no_record(conn, context):
    _run_id, candidate = context
    assert get_qualification(conn, candidate.id, 0) is None
    assert get_latest_qualification(conn, candidate.id) is None
    assert list_qualifications(conn, candidate.id) == []
