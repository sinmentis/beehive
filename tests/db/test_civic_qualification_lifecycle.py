# tests/db/test_civic_qualification_lifecycle.py
"""Deleting things a qualification receipt points at.

A duplicate receipt is the only kind that names other rows: `matched_candidate_id` is the earlier
Candidate that already published the story and `matched_batch_id` is the Batch that carried it.
Those pointers are evidence for a decision, not structure the decision depends on, and the
difference decides what deletion must do.

`ON DELETE RESTRICT` would be wrong here, and that is the finding this suite pins. It would make
removing a Civic Watch fail outright the moment any Candidate inside it had ever been recorded as
a duplicate of another -- an ordinary, expected state -- because SQLite evaluates the restrict
before the cascade that would have removed the referencing row anyway. `ON DELETE SET NULL` keeps
the receipt readable (the outcome, the reasons and the dedup reason code all survive) while the
pointer to a row that no longer exists simply goes away.

Contrast `civic_candidates.superseded_by_candidate_id`, which really is RESTRICT: a published
correction that vanished would leave the statement it corrected in a "superseded by nothing"
state, and there the pointer *is* the meaning.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.evidence import (CivicCandidateClaim, CivicClaimCategory, CivicContentClass,
                                    CivicEvidenceItem, CivicOwnershipRegistry, CivicPublisher,
                                    CivicPublisherKind, CivicQualificationOutcome,
                                    CivicRecordKind, CivicRecordOperation, CivicSourceKind,
                                    qualify_candidate)
from beehive.civic.dedup import (CivicDedupContext, PublishedFingerprintRecord,
                                 SealedCandidateRecord)
from beehive.civic.pipeline import CIVIC_ENGINE_VERSION
from beehive.civic.reasons import CivicPolicyReason
from beehive.db.civic_batches import get_or_create_batch_if_claimed, seal_batch_if_claimed
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate,
                                         mark_candidate_verified, set_candidate_status)
from beehive.db.civic_qualifications import (get_latest_qualification, list_qualifications,
                                             record_qualification)
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch, hard_delete_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier,
                                  CivicRunStatus, CivicVerificationVerdict)

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


def _claim(candidate_id, fingerprint):
    return CivicCandidateClaim(
        candidate_id=str(candidate_id), fingerprint=fingerprint,
        record_kind=CivicRecordKind.TIMELINE, operation=CivicRecordOperation.ADD,
        subject_id="green-party", record_key=fingerprint, event_date=T0.date(),
        category=CivicClaimCategory.PARTY_TIMELINE_EVENT,
        content_class=CivicContentClass.FACTUAL, durable=True, claim_text="A durable event")


def _item(key, url):
    return CivicEvidenceItem(
        evidence_id=key, publisher_id="beehive", source_kind=CivicSourceKind.GOVERNMENT,
        evidence_tier=CivicEvidenceTier.OFFICIAL, stance=CivicEvidenceStance.SUPPORTING,
        canonical_url=url, title="Release", subject_id="green-party", event_date=T0.date(),
        category=CivicClaimCategory.PARTY_TIMELINE_EVENT, published_at=T0,
        accessible_full_text=True, paywalled=False, anonymous_source_dependency=False)


@pytest.fixture
def duplicate(conn):
    """A published Candidate, and a second one the engine excluded as its duplicate.

    Built through the real path: the first is verified and sealed into a delivered-shape Batch,
    the second is qualified against a dedup context naming it, so the stored receipt carries live
    `matched_candidate_id` and `matched_batch_id` pointers rather than hand-written ones."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id

    first_run = enqueue_civic_run(conn, watch_id, T0, T0)
    first_lease = claim_civic_run(conn, first_run.id, T0, 600, 3600)
    first_batch = get_or_create_batch_if_claimed(
        conn, first_run.id, first_lease.run.claim_token, T0)
    published = create_candidate(conn, first_batch.id, "dk-1", "Title", "Statement", T0)
    add_candidate_evidence(
        conn, published.id, "official_feed", "e1", "https://example.test/story", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0, publisher="Beehive",
        canonical_url="https://example.test/story")
    set_candidate_status(conn, published.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, published.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, published.id, T0)
    sealed = seal_batch_if_claimed(
        conn, first_batch.id, first_run.id, first_lease.run.claim_token, T0)
    complete_civic_run(conn, first_run.id, first_lease.run.claim_token,
                       CivicRunStatus.COMPLETED, T0)

    later = T0 + timedelta(days=1)
    second_run = enqueue_civic_run(conn, watch_id, later, later)
    second_lease = claim_civic_run(conn, second_run.id, later, 600, 3600)
    second_batch = get_or_create_batch_if_claimed(
        conn, second_run.id, second_lease.run.claim_token, later)
    # Two duplicates, because the two pointers never appear on the same receipt: a canonical-URL
    # repeat names the earlier Candidate, and a fingerprint already published names the Batch
    # that carried it.
    repeat = create_candidate(conn, second_batch.id, "dk-2", "Again", "Statement", later)
    by_url = qualify_candidate(
        _claim(repeat.id, "dk-2"), [_item("e2", "https://example.test/story")],
        registry=_REGISTRY,
        dedup_context=CivicDedupContext.build(later, sealed_candidates=[SealedCandidateRecord(
            candidate_id=str(published.id), fingerprint="dk-1",
            canonical_url="https://example.test/story", sealed_at=T0)]))
    assert by_url.outcome is CivicQualificationOutcome.DUPLICATE

    republished = create_candidate(conn, second_batch.id, "dk-3", "Once more", "Statement", later)
    by_fingerprint = qualify_candidate(
        _claim(republished.id, "dk-3"), [_item("e3", "https://example.test/other")],
        registry=_REGISTRY,
        dedup_context=CivicDedupContext.build(
            later, published_fingerprints=[PublishedFingerprintRecord(
                fingerprint="dk-3", batch_id=str(sealed.id), published_at=T0)]))
    assert by_fingerprint.outcome is CivicQualificationOutcome.DUPLICATE

    conn.execute("BEGIN IMMEDIATE")
    record = record_qualification(
        conn, candidate_id=repeat.id, run_id=second_run.id, qualification=by_url,
        engine_version=CIVIC_ENGINE_VERSION, input_hash="a" * 64, output_hash="b" * 64,
        research_attempts_used=0, now=later, record_kind=CivicRecordKind.TIMELINE)
    batch_record = record_qualification(
        conn, candidate_id=republished.id, run_id=second_run.id, qualification=by_fingerprint,
        engine_version=CIVIC_ENGINE_VERSION, input_hash="e" * 64, output_hash="f" * 64,
        research_attempts_used=0, now=later, record_kind=CivicRecordKind.TIMELINE)
    conn.commit()

    assert record.matched_candidate_id == published.id
    assert batch_record.matched_batch_id == sealed.id
    return type("Ctx", (), {
        "watch_id": watch_id, "published": published, "published_run_id": first_run.id,
        "published_batch_id": sealed.id, "repeat": repeat, "republished": republished,
        "repeat_run_id": second_run.id, "repeat_batch_id": second_batch.id,
        "record": record, "batch_record": batch_record})()


def _receipt(conn, candidate_id):
    return get_latest_qualification(conn, candidate_id)


# ============================================================================
# The pointers are real
# ============================================================================


def test_a_duplicate_receipt_names_the_candidate_and_batch_it_repeats(conn, duplicate):
    by_url = _receipt(conn, duplicate.repeat.id)
    assert by_url.outcome is CivicQualificationOutcome.DUPLICATE
    assert by_url.matched_candidate_id == duplicate.published.id
    assert by_url.dedup_reason is CivicPolicyReason.DUPLICATE_CANONICAL_URL

    by_fingerprint = _receipt(conn, duplicate.republished.id)
    assert by_fingerprint.matched_batch_id == duplicate.published_batch_id
    assert by_fingerprint.dedup_reason is (
        CivicPolicyReason.DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS)


def test_the_pointers_are_declared_set_null_not_restrict(conn):
    """Pinned explicitly, because a RESTRICT here is invisible until a delete is attempted."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'civic_qualifications'"
    ).fetchone()[0]
    assert "matched_candidate_id INTEGER REFERENCES civic_candidates(id) ON DELETE SET NULL" in (
        " ".join(sql.split()))
    assert "matched_batch_id INTEGER REFERENCES civic_batches(id) ON DELETE SET NULL" in (
        " ".join(sql.split()))
    assert "RESTRICT" not in sql


# ============================================================================
# Deleting what a receipt points at
# ============================================================================


def test_deleting_the_matched_candidate_leaves_the_receipt_readable(conn, duplicate):
    """The decision stays explainable: the outcome, the reasons and the dedup code all survive.
    Only "which row was it" is gone, along with the row."""
    conn.execute("DELETE FROM civic_candidates WHERE id = ?", (duplicate.published.id,))
    conn.commit()

    record = _receipt(conn, duplicate.repeat.id)
    assert record is not None
    assert record.matched_candidate_id is None
    assert record.outcome is CivicQualificationOutcome.DUPLICATE
    assert record.dedup_reason is CivicPolicyReason.DUPLICATE_CANONICAL_URL
    assert record.reasons


def test_deleting_the_matched_batch_leaves_the_receipt_readable(conn, duplicate):
    conn.execute("DELETE FROM civic_batches WHERE id = ?", (duplicate.published_batch_id,))
    conn.commit()

    record = _receipt(conn, duplicate.republished.id)
    assert record is not None
    assert record.matched_batch_id is None
    assert record.outcome is CivicQualificationOutcome.DUPLICATE
    assert record.dedup_reason is (
        CivicPolicyReason.DUPLICATE_PUBLISHED_FINGERPRINT_WITHIN_FOURTEEN_DAYS)


def test_deleting_the_matched_run_cascades_to_its_batch_and_nulls_the_pointers(conn, duplicate):
    """A Run owns its Batch and its Candidates by CASCADE, so deleting it takes both pointers
    with it. Under RESTRICT this delete would fail instead."""
    conn.execute("DELETE FROM civic_runs WHERE id = ?", (duplicate.published_run_id,))
    conn.commit()

    assert _receipt(conn, duplicate.repeat.id).matched_candidate_id is None
    assert _receipt(conn, duplicate.republished.id).matched_batch_id is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_hard_deleting_the_watch_succeeds_and_removes_the_receipt_with_its_candidate(
        conn, duplicate):
    """The whole subtree goes, including the receipt -- which belongs to the duplicate Candidate,
    not to the row it pointed at. This is the delete RESTRICT used to make impossible."""
    assert hard_delete_civic_watch(conn, duplicate.watch_id) is True
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_qualifications").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM civic_candidates").fetchone()["n"] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_duplicates_own_candidate_takes_its_receipt(conn, duplicate):
    """The receipt is owned by the Candidate it decided, so it cascades with it rather than
    surviving as a decision about nothing."""
    conn.execute("DELETE FROM civic_candidates WHERE id = ?", (duplicate.repeat.id,))
    conn.commit()
    assert list_qualifications(conn, duplicate.repeat.id) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_matched_rows_does_not_disturb_other_receipts(conn, duplicate):
    """A non-duplicate receipt carries no pointers at all, so nothing about it can move when the
    rows a different decision named are removed."""
    qualification = qualify_candidate(
        _claim(duplicate.published.id, "dk-1"), [_item("e1", "https://example.test/story")],
        registry=_REGISTRY)
    conn.execute("BEGIN IMMEDIATE")
    record_qualification(
        conn, candidate_id=duplicate.published.id, run_id=duplicate.published_run_id,
        qualification=qualification, engine_version=CIVIC_ENGINE_VERSION,
        input_hash="c" * 64, output_hash="d" * 64, research_attempts_used=0, now=T0,
        record_kind=CivicRecordKind.TIMELINE)
    conn.commit()

    conn.execute("DELETE FROM civic_candidates WHERE id = ?", (duplicate.repeat.id,))
    conn.commit()
    survivor = _receipt(conn, duplicate.published.id)
    assert survivor.outcome is CivicQualificationOutcome.QUALIFIED
    assert survivor.matched_candidate_id is None and survivor.matched_batch_id is None
