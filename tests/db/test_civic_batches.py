# tests/db/test_civic_batches.py
"""Civic Batch persistence: one envelope per Run, sealing that actually freezes it, and a
delivery lease with a bounded retry budget."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (
    CivicPayloadHashMismatchError,
    bind_batch_payload_hash,
    claim_batch_for_delivery,
    get_batch_by_idempotency_key,
    get_batch_for_run,
    get_civic_batch,
    get_frozen_publication,
    get_or_create_batch_if_claimed,
    heartbeat_batch_delivery,
    iter_deliverable_batches,
    list_deliverable_batches,
    list_publication_rejections,
    list_sealed_candidate_ids,
    mark_batch_delivered,
    quarantine_batch,
    recompute_content_hash,
    recover_expired_batch_deliveries,
    release_batch_claim,
    seal_batch_if_claimed,
)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate, get_candidate,
                                          list_batch_candidates, mark_candidate_verified,
                                          reject_candidate, set_candidate_status,
                                          supersede_candidate)
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicBatchStatus, CivicCandidateStatus, CivicEvidenceStance,
                                   CivicEvidenceTier, CivicRunStatus, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def claimed_run(conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    return lease.run


@pytest.fixture
def building_batch(conn, claimed_run):
    return get_or_create_batch_if_claimed(conn, claimed_run.id, claimed_run.claim_token, T0)


def _verified(conn, batch, dedupe_key, title, statement, now=T0):
    """A Candidate taken all the way to VERIFIED -- the only state a seal admits into the
    envelope. Evidence first, then a supporting verification, then publication."""
    candidate = create_candidate(conn, batch.id, dedupe_key, title, statement, now)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", f"e-{dedupe_key}",
        f"https://example.test/{dedupe_key}", "Official release", CivicEvidenceTier.OFFICIAL,
        CivicEvidenceStance.SUPPORTING, now, publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, now)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", now)
    return mark_candidate_verified(conn, candidate.id, now)


def _sealed(conn, claimed_run, batch, *, candidates=(("dk-1", "T", "S"),)):
    for dedupe_key, title, statement in candidates:
        _verified(conn, batch, dedupe_key, title, statement)
    return seal_batch_if_claimed(conn, batch.id, claimed_run.id, claimed_run.claim_token, T0)


def test_payload_hash_is_write_once_and_mismatch_quarantines(
    conn, claimed_run, building_batch,
):
    sealed = _sealed(conn, claimed_run, building_batch)
    first = bind_batch_payload_hash(conn, sealed.id, "a" * 64, T0)
    replay = bind_batch_payload_hash(conn, sealed.id, "a" * 64, T0)
    assert first.payload_hash == replay.payload_hash == "a" * 64

    with pytest.raises(CivicPayloadHashMismatchError):
        bind_batch_payload_hash(conn, sealed.id, "b" * 64, T0)
    quarantined = get_civic_batch(conn, sealed.id)
    assert quarantined.status is CivicBatchStatus.QUARANTINED
    assert quarantined.payload_hash == "a" * 64
    assert quarantined.error_code == "payload_hash_mismatch"


# ============================================================================
# One envelope per Run
# ============================================================================


def test_creates_one_building_batch_for_a_claimed_run(conn, claimed_run):
    batch = get_or_create_batch_if_claimed(conn, claimed_run.id, claimed_run.claim_token, T0)
    assert batch.status is CivicBatchStatus.BUILDING
    assert batch.content_hash is None and batch.sealed_at is None
    assert batch.idempotency_key
    assert not batch.is_sealed


def test_a_reclaimed_run_resumes_the_same_envelope(conn, claimed_run):
    first = get_or_create_batch_if_claimed(conn, claimed_run.id, claimed_run.claim_token, T0)
    second = get_or_create_batch_if_claimed(conn, claimed_run.id, claimed_run.claim_token, T0)
    assert first.id == second.id
    assert first.idempotency_key == second.idempotency_key


def test_batch_creation_is_fenced_on_the_run_claim(conn, claimed_run):
    assert get_or_create_batch_if_claimed(conn, claimed_run.id, "stale", T0) is None
    assert get_batch_for_run(conn, claimed_run.id) is None


def test_the_database_refuses_a_second_batch_for_one_run(conn, claimed_run, building_batch):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_batches (run_id, watch_id, status, idempotency_key, created_at) "
            "VALUES (?, ?, 'building', 'other-key', ?)",
            (claimed_run.id, claimed_run.watch_id, T0.isoformat()))


def test_idempotency_keys_are_unique_across_batches(conn, building_batch):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_batches (run_id, watch_id, status, idempotency_key, created_at) "
            "VALUES (999, 999, 'building', ?, ?)",
            (building_batch.idempotency_key, T0.isoformat()))


def test_lookup_by_idempotency_key(conn, building_batch):
    assert get_batch_by_idempotency_key(
        conn, building_batch.idempotency_key).id == building_batch.id
    assert get_batch_by_idempotency_key(conn, "nope") is None


# ============================================================================
# Sealing
# ============================================================================


def test_sealing_computes_the_hash_and_candidate_count(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch,
                     candidates=(("dk-1", "T1", "S1"), ("dk-2", "T2", "S2")))
    assert sealed.status is CivicBatchStatus.SEALED
    assert sealed.sealed_at == T0
    assert sealed.candidate_count == 2
    assert sealed.content_hash == recompute_content_hash(conn, sealed.id)
    assert len(sealed.content_hash) == 64
    assert sealed.is_sealed


def test_partymap_low_level_seal_defers_missing_qualification_without_bypassing_it(conn):
    watch = create_civic_watch(conn, "partymap", "What changed?", T0)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = _verified(conn, batch, "operator-only", "Title", "Statement")

    sealed = seal_batch_if_claimed(
        conn, batch.id, run.id, lease.run.claim_token, T0)

    assert sealed.candidate_count == 0
    assert list_sealed_candidate_ids(conn, sealed.id) == []
    assert get_frozen_publication(conn, sealed.id) is not None
    settled = get_candidate(conn, candidate.id)
    assert settled.status is CivicCandidateStatus.DEFERRED_AI
    assert settled.ai_refresh_reason == "source_refresh:source_refresh_uncapturable"
    assert settled.ai_revision == 0
    assert list_publication_rejections(conn, sealed.id) == []
    assert conn.execute(
        "SELECT reason FROM civic_capacity_deferrals WHERE candidate_id = ?",
        (candidate.id,)).fetchone()[0] == "source_refresh_required"


def test_an_empty_batch_can_still_be_sealed(conn, claimed_run, building_batch):
    """A quiet civic day publishes nothing -- that is a valid, hashable envelope, not an error."""
    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)
    assert sealed.candidate_count == 0
    assert sealed.content_hash


def test_sealing_is_fenced_on_the_run_claim(conn, claimed_run, building_batch):
    assert seal_batch_if_claimed(conn, building_batch.id, claimed_run.id, "stale", T0) is None
    assert get_civic_batch(conn, building_batch.id).status is CivicBatchStatus.BUILDING


def test_a_sealed_envelope_accepts_no_more_candidates(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    with pytest.raises(ValueError, match="not 'building'"):
        create_candidate(conn, sealed.id, "dk-late", "Late", "Late statement", T0)


def test_the_hash_covers_the_published_fields_only():
    """Two envelopes holding the same statements hash identically; changing a statement changes
    the hash."""
    def build(statement):
        path_conn = connect(":memory:")
        init_schema(path_conn)
        watch_id = create_civic_watch(path_conn, "W", "Q", T0).id
        run = enqueue_civic_run(path_conn, watch_id, T0, T0)
        lease = claim_civic_run(path_conn, run.id, T0, 600, 3600)
        batch = get_or_create_batch_if_claimed(
            path_conn, run.id, lease.run.claim_token, T0)
        _verified(path_conn, batch, "dk-1", "T", statement)
        sealed = seal_batch_if_claimed(
            path_conn, batch.id, run.id, lease.run.claim_token, T0)
        path_conn.close()
        return sealed.content_hash

    assert build("same statement") == build("same statement")
    assert build("statement one") != build("statement two")


# ============================================================================
# Sealing admits only settled, verified work
# ============================================================================


def test_sealing_is_refused_while_any_candidate_is_unsettled(conn, claimed_run, building_batch):
    """Sealing a Batch with work still in flight would either publish an unverified statement or
    silently drop one that was on its way to being verified. It waits instead."""
    _verified(conn, building_batch, "dk-1", "T1", "S1")
    create_candidate(conn, building_batch.id, "dk-2", "T2", "S2", T0)

    with pytest.raises(ValueError, match="still unsettled"):
        seal_batch_if_claimed(
            conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)

    unchanged = get_civic_batch(conn, building_batch.id)
    assert unchanged.status is CivicBatchStatus.BUILDING
    assert unchanged.content_hash is None and unchanged.sealed_at is None


@pytest.mark.parametrize(
    "unsettled_status",
    [CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
     CivicCandidateStatus.VERIFYING],
    ids=lambda status: status.value)
def test_every_unsettled_status_blocks_the_seal(
        conn, claimed_run, building_batch, unsettled_status):
    candidate = create_candidate(conn, building_batch.id, "dk-1", "T", "S", T0)
    if unsettled_status is not CivicCandidateStatus.DRAFTED:
        set_candidate_status(conn, candidate.id, unsettled_status, T0)
    with pytest.raises(ValueError, match="still unsettled"):
        seal_batch_if_claimed(
            conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)


def test_the_envelope_holds_verified_candidates_and_excludes_rejected_ones(
        conn, claimed_run, building_batch):
    verified = _verified(conn, building_batch, "dk-1", "Published", "Published statement")
    dropped = create_candidate(conn, building_batch.id, "dk-2", "Dropped", "Dropped", T0)
    reject_candidate(conn, dropped.id, "insufficient_evidence", T0)

    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)

    assert sealed.candidate_count == 1
    assert list_sealed_candidate_ids(conn, sealed.id) == [verified.id]
    assert get_candidate(conn, verified.id).is_sealed_into_batch
    assert not get_candidate(conn, dropped.id).is_sealed_into_batch
    # The rejected Candidate stays in the table as the record of what was considered.
    assert len(list_batch_candidates(conn, sealed.id)) == 2


def test_a_rejected_candidate_never_moves_the_hash(conn, claimed_run):
    """An envelope publishing one statement hashes identically whether or not other Candidates
    were considered and rejected alongside it."""
    def build(with_rejections):
        path_conn = connect(":memory:")
        init_schema(path_conn)
        watch_id = create_civic_watch(path_conn, "W", "Q", T0).id
        run = enqueue_civic_run(path_conn, watch_id, T0, T0)
        lease = claim_civic_run(path_conn, run.id, T0, 600, 3600)
        batch = get_or_create_batch_if_claimed(path_conn, run.id, lease.run.claim_token, T0)
        _verified(path_conn, batch, "dk-1", "Published", "Published statement")
        if with_rejections:
            for index in range(3):
                dropped = create_candidate(
                    path_conn, batch.id, f"dk-drop-{index}", "Drop", "Drop", T0)
                reject_candidate(path_conn, dropped.id, "off_topic", T0)
        sealed = seal_batch_if_claimed(path_conn, batch.id, run.id, lease.run.claim_token, T0)
        path_conn.close()
        return sealed.content_hash, sealed.candidate_count

    assert build(with_rejections=False) == build(with_rejections=True)


def test_an_all_rejected_batch_seals_as_an_empty_envelope(conn, claimed_run, building_batch):
    """A civic day where nothing survived verification publishes nothing -- a valid, hashable
    envelope with candidate_count 0, not an error."""
    for index in range(2):
        dropped = create_candidate(
            conn, building_batch.id, f"dk-{index}", "Drop", "Drop", T0)
        reject_candidate(conn, dropped.id, "insufficient_evidence", T0)

    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)

    assert sealed.status is CivicBatchStatus.SEALED
    assert sealed.candidate_count == 0
    assert sealed.content_hash == recompute_content_hash(conn, sealed.id)
    assert list_sealed_candidate_ids(conn, sealed.id) == []


def test_an_all_rejected_envelope_hashes_the_same_as_an_empty_one(conn, claimed_run):
    def build(with_rejections):
        path_conn = connect(":memory:")
        init_schema(path_conn)
        watch_id = create_civic_watch(path_conn, "W", "Q", T0).id
        run = enqueue_civic_run(path_conn, watch_id, T0, T0)
        lease = claim_civic_run(path_conn, run.id, T0, 600, 3600)
        batch = get_or_create_batch_if_claimed(path_conn, run.id, lease.run.claim_token, T0)
        if with_rejections:
            dropped = create_candidate(path_conn, batch.id, "dk-1", "Drop", "Drop", T0)
            reject_candidate(path_conn, dropped.id, "off_topic", T0)
        sealed = seal_batch_if_claimed(path_conn, batch.id, run.id, lease.run.claim_token, T0)
        path_conn.close()
        return sealed.content_hash

    assert build(with_rejections=False) == build(with_rejections=True)


def test_a_candidate_superseded_before_the_seal_is_excluded(conn, claimed_run, building_batch):
    """This same Run already replaced the statement, so the envelope publishes the replacement
    only -- the superseded Candidate is settled, but was never sealed in."""
    original = _verified(conn, building_batch, "dk-1", "Original", "Original statement")
    replacement = _verified(conn, building_batch, "dk-2", "Correction", "Corrected statement")
    supersede_candidate(conn, original.id, replacement.id, T0)

    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)

    assert sealed.candidate_count == 1
    assert list_sealed_candidate_ids(conn, sealed.id) == [replacement.id]
    assert not get_candidate(conn, original.id).is_sealed_into_batch


# ============================================================================
# The sealed envelope is immutable afterwards
# ============================================================================


def test_a_later_correction_never_changes_the_sealed_hash(conn, claimed_run, building_batch):
    """The regression this whole `sealed_into_batch_at` design exists for: publishing tomorrow's
    correction moves a published Candidate VERIFIED -> SUPERSEDED, and an envelope that was
    already delivered and receipted must still verify byte-for-byte."""
    published = _verified(conn, building_batch, "dk-1", "Original", "Original statement")
    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)
    complete_civic_run(
        conn, claimed_run.id, claimed_run.claim_token, CivicRunStatus.COMPLETED, T0)

    # Tomorrow's Run publishes the correction in its own envelope.
    tomorrow = T0 + timedelta(days=1)
    next_run = enqueue_civic_run(conn, claimed_run.watch_id, tomorrow, tomorrow)
    next_lease = claim_civic_run(conn, next_run.id, tomorrow, 600, 3600)
    next_batch = get_or_create_batch_if_claimed(
        conn, next_run.id, next_lease.run.claim_token, tomorrow)
    replacement = _verified(
        conn, next_batch, "dk-2", "Correction", "Corrected", now=tomorrow)
    supersede_candidate(conn, published.id, replacement.id, tomorrow)

    assert get_candidate(conn, published.id).status is CivicCandidateStatus.SUPERSEDED
    assert get_candidate(conn, published.id).is_sealed_into_batch
    assert recompute_content_hash(conn, sealed.id) == sealed.content_hash
    assert list_sealed_candidate_ids(conn, sealed.id) == [published.id]
    assert get_civic_batch(conn, sealed.id).candidate_count == 1


def test_recompute_matches_seal_for_every_envelope_shape(conn, claimed_run, building_batch):
    _verified(conn, building_batch, "dk-1", "T1", "S1")
    _verified(conn, building_batch, "dk-2", "T2", "S2")
    dropped = create_candidate(conn, building_batch.id, "dk-3", "T3", "S3", T0)
    reject_candidate(conn, dropped.id, "off_topic", T0)

    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)

    assert sealed.candidate_count == 2
    assert recompute_content_hash(conn, sealed.id) == sealed.content_hash


def test_recompute_detects_a_tampered_sealed_statement(conn, claimed_run, building_batch):
    published = _verified(conn, building_batch, "dk-1", "T", "S")
    sealed = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)
    # Bypass the repository entirely, as a corrupted or hand-edited database would.
    conn.execute(
        "UPDATE civic_candidates SET statement = 'tampered' WHERE id = ?", (published.id,))
    conn.commit()
    assert recompute_content_hash(conn, sealed.id) != sealed.content_hash


def test_resealing_a_sealed_batch_returns_the_identical_envelope(
        conn, claimed_run, building_batch):
    """Idempotent by design, so a worker that sealed, crashed before advancing its phase and was
    reclaimed can simply call it again."""
    sealed = _sealed(conn, claimed_run, building_batch)

    again = seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token,
        T0 + timedelta(hours=1))

    assert again == sealed
    assert again.content_hash == sealed.content_hash
    assert again.candidate_count == sealed.candidate_count
    assert again.sealed_at == T0


def test_a_reseal_never_re_stamps_envelope_membership(conn, claimed_run, building_batch):
    published = _verified(conn, building_batch, "dk-1", "T", "S")
    seal_batch_if_claimed(conn, building_batch.id, claimed_run.id, claimed_run.claim_token, T0)
    stamped_at = get_candidate(conn, published.id).sealed_into_batch_at

    seal_batch_if_claimed(
        conn, building_batch.id, claimed_run.id, claimed_run.claim_token,
        T0 + timedelta(hours=1))

    assert get_candidate(conn, published.id).sealed_into_batch_at == stamped_at


def test_a_batch_past_sealed_can_never_be_resealed(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    with pytest.raises(ValueError, match="not 'building'"):
        seal_batch_if_claimed(
            conn, sealed.id, claimed_run.id, claimed_run.claim_token, T0)
    assert get_civic_batch(conn, sealed.id).content_hash == sealed.content_hash


# ============================================================================
# Delivery claim, heartbeat, retry
# ============================================================================


def test_only_sealed_batches_are_deliverable(conn, claimed_run, building_batch):
    assert list_deliverable_batches(conn, T0) == []
    sealed = _sealed(conn, claimed_run, building_batch)
    assert [b.id for b in list_deliverable_batches(conn, T0)] == [sealed.id]


def test_deliverable_batches_page_past_an_unsettled_prefix(conn):
    watch_id = create_civic_watch(conn, "Unsettled backlog", "Q", T0).id
    now = T0 + timedelta(days=30)
    sealed_ids = []
    for day in range(23):
        slot = T0 + timedelta(days=day)
        run = claim_civic_run(
            conn, enqueue_civic_run(conn, watch_id, slot, slot).id, slot, 600, 3600).run
        batch = get_or_create_batch_if_claimed(conn, run.id, run.claim_token, slot)
        sealed_ids.append(
            seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, slot).id)
        assert complete_civic_run(
            conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, slot)
    claim_batch_for_delivery(conn, sealed_ids[3], now, 60)

    first = list_deliverable_batches(conn, now)
    later = list_deliverable_batches(conn, now, after_id=first[-1].id)
    expected = [batch_id for batch_id in sealed_ids if batch_id != sealed_ids[3]]

    assert [batch.id for batch in first] == expected[:20]
    assert [batch.id for batch in later] == expected[20:]
    assert [batch.id for batch in iter_deliverable_batches(conn, now, page_size=5)] == expected


def test_claiming_spends_an_attempt_up_front(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    assert lease.batch.status is CivicBatchStatus.DELIVERING
    assert lease.attempt_count == 1
    assert lease.lease_expires_at == (T0 + timedelta(seconds=60)).isoformat()
    assert claim_batch_for_delivery(conn, sealed.id, T0, 60) is None


def test_a_batch_inside_its_backoff_is_neither_listed_nor_claimable(
        conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    release_batch_claim(
        conn, sealed.id, lease.batch.claim_token, T0, retry_after_seconds=300,
        error_code="upstream_500")

    assert list_deliverable_batches(conn, T0 + timedelta(seconds=299)) == []
    assert claim_batch_for_delivery(conn, sealed.id, T0 + timedelta(seconds=299), 60) is None
    assert [b.id for b in list_deliverable_batches(conn, T0 + timedelta(seconds=300))] == [
        sealed.id]


def test_releasing_returns_the_envelope_unchanged_for_the_next_attempt(
        conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    released = release_batch_claim(
        conn, sealed.id, lease.batch.claim_token, T0, retry_after_seconds=60,
        error_code="upstream_500", error_detail="bad gateway")
    assert released.status is CivicBatchStatus.SEALED
    assert released.content_hash == sealed.content_hash
    assert released.claim_token is None
    assert released.error_code == "upstream_500"
    assert released.next_attempt_at == T0 + timedelta(seconds=60)


def test_the_retry_budget_is_bounded(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    conn.execute("UPDATE civic_batches SET max_attempts = 2 WHERE id = ?", (sealed.id,))
    conn.commit()

    first = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    released = release_batch_claim(
        conn, sealed.id, first.batch.claim_token, T0, retry_after_seconds=0, error_code="x")
    assert released.status is CivicBatchStatus.SEALED

    second = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    exhausted = release_batch_claim(
        conn, sealed.id, second.batch.claim_token, T0, retry_after_seconds=0, error_code="x")
    assert exhausted.status is CivicBatchStatus.FAILED
    assert exhausted.settled_at == T0
    assert claim_batch_for_delivery(conn, sealed.id, T0, 60) is None


def test_a_batch_at_its_attempt_cap_is_failed_rather_than_claimed(
        conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    conn.execute(
        "UPDATE civic_batches SET max_attempts = 1, attempt_count = 1 WHERE id = ?", (sealed.id,))
    conn.commit()
    assert claim_batch_for_delivery(conn, sealed.id, T0, 60) is None
    failed = get_civic_batch(conn, sealed.id)
    assert failed.status is CivicBatchStatus.FAILED
    assert failed.error_code == "max_attempts_exhausted"


def test_release_and_delivery_are_fenced_on_the_claim(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    assert release_batch_claim(
        conn, sealed.id, "stale", T0, retry_after_seconds=0, error_code="x") is None
    assert not mark_batch_delivered(conn, sealed.id, "stale", T0)
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.DELIVERING


def test_delivery_heartbeat_extends_and_never_revives(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    token = lease.batch.claim_token
    assert heartbeat_batch_delivery(conn, sealed.id, token, T0 + timedelta(seconds=30), 60)
    assert not heartbeat_batch_delivery(conn, sealed.id, "stale", T0 + timedelta(seconds=31), 60)
    assert not heartbeat_batch_delivery(conn, sealed.id, token, T0 + timedelta(seconds=91), 60)


def test_marking_delivered_terminalizes_the_envelope(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    assert mark_batch_delivered(conn, sealed.id, lease.batch.claim_token, T0)
    delivered = get_civic_batch(conn, sealed.id)
    assert delivered.status is CivicBatchStatus.DELIVERED
    assert delivered.settled_at == T0
    assert delivered.claim_token is None
    assert list_deliverable_batches(conn, T0) == []


# ============================================================================
# Quarantine
# ============================================================================


def test_quarantine_is_terminal_and_never_retried(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    quarantined = quarantine_batch(conn, sealed.id, T0, "content_hash_mismatch")
    assert quarantined.status is CivicBatchStatus.QUARANTINED
    assert quarantined.settled_at == T0
    assert list_deliverable_batches(conn, T0 + timedelta(days=7)) == []
    assert claim_batch_for_delivery(conn, sealed.id, T0 + timedelta(days=7), 60) is None


def test_a_delivering_batch_can_be_quarantined(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    quarantined = quarantine_batch(conn, sealed.id, T0, "integrity")
    assert quarantined.status is CivicBatchStatus.QUARANTINED
    assert quarantined.claim_token is None


def test_a_delivered_batch_cannot_be_quarantined(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    mark_batch_delivered(conn, sealed.id, lease.batch.claim_token, T0)
    with pytest.raises(ValueError, match="invalid Civic Batch transition"):
        quarantine_batch(conn, sealed.id, T0, "too late")


def test_quarantining_a_missing_batch_returns_none(conn):
    assert quarantine_batch(conn, 999, T0, "x") is None


# ============================================================================
# Delivery recovery
# ============================================================================


def test_recovery_releases_an_abandoned_delivery_without_spending_another_attempt(
        conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    result = recover_expired_batch_deliveries(conn, T0 + timedelta(seconds=61))
    assert (result.released_count, result.failed_count) == (1, 0)
    released = get_civic_batch(conn, sealed.id)
    assert released.status is CivicBatchStatus.SEALED
    assert released.attempt_count == 1
    assert released.error_code == "delivery_lease_expired"


def test_recovery_treats_the_exact_expiry_instant_as_expired(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    assert recover_expired_batch_deliveries(conn, T0 + timedelta(seconds=60)).released_count == 1


def test_recovery_leaves_a_live_delivery_alone(conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    result = recover_expired_batch_deliveries(conn, T0 + timedelta(seconds=59))
    assert (result.released_count, result.failed_count) == (0, 0)
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.DELIVERING


def test_recovery_fails_an_abandoned_delivery_that_exhausted_its_budget(
        conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    conn.execute("UPDATE civic_batches SET max_attempts = 1 WHERE id = ?", (sealed.id,))
    conn.commit()
    claim_batch_for_delivery(conn, sealed.id, T0, 60)
    result = recover_expired_batch_deliveries(conn, T0 + timedelta(seconds=61))
    assert (result.released_count, result.failed_count) == (0, 1)
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.FAILED


def test_a_recovered_worker_can_no_longer_mark_the_batch_delivered(
        conn, claimed_run, building_batch):
    sealed = _sealed(conn, claimed_run, building_batch)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 60)
    stale_token = lease.batch.claim_token
    recover_expired_batch_deliveries(conn, T0 + timedelta(seconds=61))
    fresh = claim_batch_for_delivery(conn, sealed.id, T0 + timedelta(seconds=61), 60)
    assert fresh.batch.claim_token != stale_token
    assert not mark_batch_delivered(conn, sealed.id, stale_token, T0 + timedelta(seconds=62))
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.DELIVERING
