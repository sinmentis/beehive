# tests/db/test_civic_concurrency.py
"""Real multi-connection, multi-thread concurrency regressions for the Civic invariants that a
single-connection test can only assert are *coded* correctly, not that they actually hold under
a genuine race:

  * one Civic Run per Watch slot,
  * exclusive Civic Run claims and the global processing cap,
  * exclusive Civic Batch delivery claims,
  * exactly one Civic Delivery Receipt per idempotency key (no double publication),
  * one Civic Batch per Run,
  * Watch-wide Civic Candidate deduplication,
  * the bounded research budget,
  * per-Candidate Civic Verification numbering,
  * exclusive Civic Notification send claims.

Each test opens several independent sqlite3 connections against the same on-disk WAL database
(mirroring tests/db/test_research_concurrency.py's established pattern) and uses
threading.Barrier to line every thread up on the same starting line, so the BEGIN IMMEDIATE
fencing inside the repository functions -- not test ordering -- is what has to make the outcome
correct.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (bind_batch_payload_hash, claim_batch_for_delivery,
                                       get_batch_for_run, get_civic_batch,
                                       get_or_create_batch_if_claimed,
                                       list_sealed_candidate_ids, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate,
                                          list_batch_candidates, mark_candidate_verified,
                                          set_candidate_status)
from beehive.db.civic_notifications import (claim_notification, mark_notification_sent,
                                             queue_notification)
from beehive.db.civic_receipts import list_receipts, record_delivery_receipt
from beehive.db.civic_runs import (claim_civic_run, complete_civic_run,
                                    count_active_processing_runs, enqueue_civic_run,
                                    get_civic_run, list_civic_runs, pin_vocabulary_version)
from beehive.db.civic_verifications import (MAX_RESEARCH_ATTEMPTS, list_verifications,
                                             record_verification, reserve_research_attempt)
from beehive.db.civic_vocabulary import (add_vocabulary_term, create_vocabulary_version,
                                          publish_vocabulary_version)
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicBatchStatus, CivicCandidateStatus, CivicDeliveryChannel,
                                   CivicEvidenceStance, CivicEvidenceTier, CivicRunStatus,
                                   CivicTermCategory, CivicTermRole, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
_THREADS = 8


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "civic-concurrency.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


def _race(db_path, worker, *, threads=_THREADS):
    """Runs `worker(conn, index)` on `threads` independent connections, all released from one
    barrier. Returns the list of results (or the exception each thread raised)."""
    barrier = threading.Barrier(threads)
    results: list = [None] * threads

    def run(index: int) -> None:
        connection = connect(db_path)
        try:
            barrier.wait()
            results[index] = worker(connection, index)
        except BaseException as exc:  # recorded, then asserted on by the caller
            results[index] = exc
        finally:
            connection.close()

    workers = [threading.Thread(target=run, args=(index,)) for index in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in workers), "a racing thread deadlocked"
    return results


def _sealed_batch(conn, subject="NZ election"):
    watch_id = create_civic_watch(conn, subject, "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    # A seal only admits settled work, and only VERIFIED Candidates enter the envelope.
    candidate = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0, publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, candidate.id, T0)
    sealed = seal_batch_if_claimed(
        conn, batch.id, run.id, lease.run.claim_token, T0)
    return bind_batch_payload_hash(conn, sealed.id, "f" * 64, T0)


def _published_vocabulary(conn, watch_id, term="Green Party"):
    version = create_vocabulary_version(conn, watch_id, T0)
    add_vocabulary_term(
        conn, version.id, term, CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
    return publish_vocabulary_version(conn, version.id, T0)


# ============================================================================
# One Civic Run per slot
# ============================================================================


def test_racing_schedulers_create_exactly_one_run_per_slot(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id

    results = _race(db_path, lambda c, _i: enqueue_civic_run(c, watch_id, T0, T0).id)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert len(set(results)) == 1
    assert len(list_civic_runs(conn, watch_id)) == 1


def test_racing_schedulers_for_different_slots_leave_exactly_one_active_run(db_path, conn):
    """A Watch works one slot at a time: the losers must fail loudly, not create a parallel Run."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id

    results = _race(
        db_path, lambda c, i: enqueue_civic_run(c, watch_id, T0 + timedelta(days=i), T0).id)

    created = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, ValueError)]
    assert len(created) == 1
    assert len(refused) == _THREADS - 1
    assert len(list_civic_runs(conn, watch_id)) == 1


# ============================================================================
# Run claims and the global cap
# ============================================================================


def test_only_one_worker_can_claim_a_run(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)

    results = _race(db_path, lambda c, _i: claim_civic_run(c, run.id, T0, 600, 3600))

    granted = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(granted) == 1
    assert len({lease.run.claim_token for lease in granted}) == 1
    assert count_active_processing_runs(conn, T0) == 1


def test_the_global_processing_cap_holds_under_a_genuine_race(db_path, conn):
    runs = []
    for index in range(_THREADS):
        watch_id = create_civic_watch(conn, f"watch-{index}", "Q", T0).id
        runs.append(enqueue_civic_run(conn, watch_id, T0, T0).id)

    results = _race(db_path, lambda c, i: claim_civic_run(c, runs[i], T0, 600, 3600))

    granted = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(granted) == 2, f"cap breached: {len(granted)} simultaneous claims"
    assert count_active_processing_runs(conn, T0) == 2


# ============================================================================
# One Civic Batch per Run
# ============================================================================


def test_racing_workers_share_one_batch_per_run(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    token = lease.run.claim_token

    results = _race(
        db_path, lambda c, _i: get_or_create_batch_if_claimed(c, run.id, token, T0).id)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert len(set(results)) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_batches WHERE run_id = ?", (run.id,)
    ).fetchone()["n"] == 1
    assert get_batch_for_run(conn, run.id).id == results[0]


# ============================================================================
# Candidate deduplication
# ============================================================================


def test_racing_workers_create_one_candidate_per_dedupe_key(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)

    results = _race(
        db_path,
        lambda c, i: create_candidate(c, batch.id, "dk-1", f"Title {i}", f"Statement {i}", T0).id)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert len(set(results)) == 1
    assert len(list_batch_candidates(conn, batch.id)) == 1


def test_racing_workers_record_one_evidence_row_per_external_key(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)

    results = _race(
        db_path,
        lambda c, _i: add_candidate_evidence(
            c, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
            CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0,
            publisher="Beehive").id)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert len(set(results)) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_candidate_evidence WHERE candidate_id = ?",
        (candidate.id,)).fetchone()["n"] == 1


# ============================================================================
# Research budget and verification numbering
# ============================================================================


def test_the_research_budget_is_never_overspent_under_a_race(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)

    results = _race(db_path, lambda c, _i: reserve_research_attempt(c, candidate.id))

    reserved = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(reserved) == MAX_RESEARCH_ATTEMPTS
    assert sorted(reserved) == list(range(1, MAX_RESEARCH_ATTEMPTS + 1))
    assert conn.execute(
        "SELECT research_attempt_count FROM civic_candidates WHERE id = ?",
        (candidate.id,)).fetchone()["research_attempt_count"] == MAX_RESEARCH_ATTEMPTS


def test_racing_verifiers_get_distinct_attempt_numbers(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0, publisher="Beehive")

    results = _race(
        db_path,
        lambda c, _i: record_verification(
            c, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0).attempt_number)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert sorted(results) == list(range(1, _THREADS + 1))
    assert len(list_verifications(conn, candidate.id)) == _THREADS


def test_racing_publications_settle_a_candidate_exactly_once(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    candidate = create_candidate(conn, batch.id, "dk-1", "T", "S", T0)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0, publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)

    results = _race(db_path, lambda c, _i: mark_candidate_verified(c, candidate.id, T0).status)

    published = [r for r in results if r is CivicCandidateStatus.VERIFIED]
    refused = [r for r in results if isinstance(r, ValueError)]
    assert len(published) == 1
    assert len(refused) == _THREADS - 1


# ============================================================================
# Delivery claims and receipt replay
# ============================================================================


def test_only_one_worker_can_claim_a_batch_for_delivery(db_path, conn):
    sealed = _sealed_batch(conn)

    results = _race(db_path, lambda c, _i: claim_batch_for_delivery(c, sealed.id, T0, 600))

    granted = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(granted) == 1
    assert get_civic_batch(conn, sealed.id).attempt_count == 1


def test_a_batch_is_never_published_twice_under_a_race(db_path, conn):
    """The property the whole receipt design exists for: however many workers race a delivery,
    exactly one receipt exists for the envelope's idempotency key."""
    sealed = _sealed_batch(conn)

    def deliver(c, index):
        lease = claim_batch_for_delivery(c, sealed.id, T0, 600)
        if lease is None:
            return None
        return record_delivery_receipt(
            c, sealed.id, lease.batch.claim_token, CivicDeliveryChannel.SPOOL,
            f"/spool/{index}", T0)

    results = _race(db_path, deliver)

    written = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(written) == 1
    assert not written[0].replayed
    assert len(list_receipts(conn)) == 1
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.DELIVERED


def test_racing_replays_after_a_delivery_write_no_second_receipt(db_path, conn):
    """Every racing worker sees the same envelope as already published, and none of them writes
    a second receipt -- whichever one wins the claim reports a replay."""
    sealed = _sealed_batch(conn)
    lease = claim_batch_for_delivery(conn, sealed.id, T0, 600)
    record_delivery_receipt(
        conn, sealed.id, lease.batch.claim_token, CivicDeliveryChannel.SPOOL, "/spool", T0)
    # Simulate a downstream system that lost its own record and asks for redelivery.
    conn.execute(
        "UPDATE civic_batches SET status = 'sealed', settled_at = NULL WHERE id = ?",
        (sealed.id,))
    conn.commit()

    def redeliver(c, index):
        claim = claim_batch_for_delivery(c, sealed.id, T0, 600)
        if claim is None:
            return None
        return record_delivery_receipt(
            c, sealed.id, claim.batch.claim_token, CivicDeliveryChannel.SPOOL,
            f"/spool/{index}", T0)

    results = _race(db_path, redeliver)

    replays = [r for r in results if r is not None and not isinstance(r, BaseException)]
    assert len(replays) == 1
    assert replays[0].replayed
    assert len(list_receipts(conn)) == 1


# ============================================================================
# The authoritative clock is sampled only AFTER the write lock is held
# ============================================================================


def _hold_write_lock(connection, lock_acquired_event, release_event, lock_released_event):
    """Acquires BEGIN IMMEDIATE on `connection` -- a deterministic stand-in for "some other
    writer is mid-transaction" -- signals `lock_acquired_event` so the caller knows the write
    lock is genuinely held, waits for `release_event`, then sets `lock_released_event` BEFORE
    actually rolling back. That ordering is what makes the test deterministic rather than a
    timing gamble: `lock_released_event` is guaranteed to already be set by the time the DB-level
    lock is actually released (rollback()), so ANY transaction that only manages to acquire its
    own BEGIN IMMEDIATE after this one lets go is guaranteed to see it set."""
    connection.execute("BEGIN IMMEDIATE")
    lock_acquired_event.set()
    release_event.wait(timeout=5)
    lock_released_event.set()
    connection.rollback()


def test_complete_civic_run_never_samples_now_fn_before_acquiring_the_write_lock(db_path, conn):
    """A claimed Run whose deadline arrives while its terminal write is queued behind another
    writer. `now_fn` is rigged to return a NOT-yet-arrived reading if it is ever invoked while
    that lock is still held, and the genuinely-arrived reading only once the holder releases.
    complete_civic_run must never observe the "not yet arrived" reading: it is only allowed to
    call `now_fn()` AFTER its own BEGIN IMMEDIATE has actually acquired the lock, which cannot
    happen until the holder releases -- so by the time it samples anything, the deadline has
    already arrived and the requested COMPLETED must commit as FAILED('deadline_exceeded').
    A regression that samples the clock BEFORE attempting to acquire the lock would instead
    capture the "not yet arrived" reading while still blocked, then publish a Run whose budget
    had already expired."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 100)
    deadline_at = lease.run.deadline_at

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = connect(db_path)
    holder = threading.Thread(
        target=_hold_write_lock,
        args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)  # the holder now genuinely holds the write lock

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_complete():
        result["ok"] = complete_civic_run(
            conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED,
            deadline_at - timedelta(seconds=30), now_fn=now_fn)

    completer = threading.Thread(target=do_complete)
    completer.start()
    # Give the completer every chance to run ahead -- including, if buggy, sampling now_fn()
    # before ever attempting BEGIN IMMEDIATE -- while the lock is still genuinely held.
    time.sleep(0.2)
    release_holder.set()
    completer.join(timeout=10)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]  # now_fn was invoked exactly once, and only after the lock was free
    assert result["ok"] is True
    terminal = get_civic_run(conn, run.id)
    assert terminal.status is CivicRunStatus.FAILED
    assert terminal.error_code == "deadline_exceeded"
    assert terminal.completed_at == deadline_at
    assert terminal.claim_token is None


def test_complete_civic_run_commits_completed_when_the_lock_frees_inside_the_deadline(
        db_path, conn):
    """The other half of the same seam: a Run whose deadline has NOT arrived by the time the lock
    frees still commits COMPLETED, and stamps the lock-held instant rather than the caller's
    stale pre-lock sample."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    committed_at = T0 + timedelta(seconds=120)

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = connect(db_path)
    holder = threading.Thread(
        target=_hold_write_lock,
        args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return committed_at if lock_released.is_set() else T0

    result = {}

    def do_complete():
        result["ok"] = complete_civic_run(
            conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0, now_fn=now_fn)

    completer = threading.Thread(target=do_complete)
    completer.start()
    time.sleep(0.2)
    release_holder.set()
    completer.join(timeout=10)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]
    assert result["ok"] is True
    terminal = get_civic_run(conn, run.id)
    assert terminal.status is CivicRunStatus.COMPLETED
    assert terminal.completed_at == committed_at
    assert terminal.error_code is None


def test_a_vocabulary_superseded_while_a_pin_is_blocked_is_refused(db_path, conn):
    """The pin-time vocabulary check runs INSIDE the pinning transaction, so a version that was
    published when the caller read it but superseded before that caller reached the write lock
    must be refused -- never pinned on the strength of a stale pre-lock read."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    first = _published_vocabulary(conn, watch_id, term="Green Party")
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)

    lock_acquired = threading.Event()
    pin_started = threading.Event()

    def supersede_then_release(connection):
        """Holds the write lock, publishes a replacement vocabulary (superseding `first`), and
        only then commits -- so the blocked pin can never have seen `first` as still in force.
        The supersede lands before the new published row because only one version per Watch may
        be 'published' at a time."""
        connection.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        pin_started.wait(timeout=5)
        time.sleep(0.2)
        connection.execute(
            "UPDATE civic_vocabulary_versions SET status = 'superseded', superseded_at = ? "
            "WHERE id = ?", (T0.isoformat(), first.id))
        connection.execute(
            "INSERT INTO civic_vocabulary_versions (watch_id, version, status, content_hash, "
            "created_at, published_at) VALUES (?, 2, 'published', 'h2', ?, ?)",
            (watch_id, T0.isoformat(), T0.isoformat()))
        connection.commit()

    holder_conn = connect(db_path)
    holder = threading.Thread(target=supersede_then_release, args=(holder_conn,))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    result = {}

    def do_pin():
        pin_started.set()
        try:
            result["pinned"] = pin_vocabulary_version(
                conn, run.id, lease.run.claim_token, first.id)
        except ValueError as exc:
            result["error"] = str(exc)

    pinner = threading.Thread(target=do_pin)
    pinner.start()
    pinner.join(timeout=10)
    holder.join(timeout=5)
    holder_conn.close()

    assert "pinned" not in result
    assert "not 'published'" in result["error"]
    assert get_civic_run(conn, run.id).vocabulary_version_id is None
    assert not conn.in_transaction


def test_racing_pins_leave_exactly_one_published_version_pinned(db_path, conn):
    """Many workers racing to pin the same in-force vocabulary: one wins, the rest see the
    already-pinned fence and return False. None of them raises, because the version they all
    presented is genuinely published."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    version = _published_vocabulary(conn, watch_id)
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    token = lease.run.claim_token

    results = _race(
        db_path, lambda c, _i: pin_vocabulary_version(c, run.id, token, version.id))

    assert all(not isinstance(r, BaseException) for r in results), results
    assert sum(1 for r in results if r is True) == 1
    assert get_civic_run(conn, run.id).vocabulary_version_id == version.id


def test_racing_seals_produce_one_identical_envelope(db_path, conn):
    """Seal is idempotent, so a reclaimed worker re-sealing concurrently with the original can
    never produce a second, different envelope."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    token = lease.run.claim_token
    batch = get_or_create_batch_if_claimed(conn, run.id, token, T0)
    candidate = create_candidate(conn, batch.id, "dk-1", "Title", "Statement", T0)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "e1", "https://example.test/1", "Release",
        CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0, publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, candidate.id, T0)

    results = _race(
        db_path, lambda c, _i: seal_batch_if_claimed(c, batch.id, run.id, token, T0))

    assert all(not isinstance(r, BaseException) for r in results), results
    hashes = {sealed.content_hash for sealed in results}
    counts = {sealed.candidate_count for sealed in results}
    assert len(hashes) == 1
    assert counts == {1}
    assert get_civic_batch(conn, batch.id).content_hash == hashes.pop()
    assert list_sealed_candidate_ids(conn, batch.id) == [candidate.id]


# ============================================================================
# Notification claims
# ============================================================================


def test_racing_queueing_creates_one_notification_per_dedupe_key(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id

    results = _race(
        db_path,
        lambda c, i: queue_notification(
            c, watch_id, "run_failed", "run-1", f"Subject {i}", T0).id)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert len(set(results)) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_notifications").fetchone()["n"] == 1


def test_only_one_worker_can_send_a_notification(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    notification = queue_notification(conn, watch_id, "run_failed", "run-1", "Failed", T0)

    def send(c, _index):
        lease = claim_notification(c, notification.id, T0, 600)
        if lease is None:
            return None
        return mark_notification_sent(c, notification.id, lease.notification.claim_token, T0)

    results = _race(db_path, send)

    sent = [r for r in results if r is True]
    assert len(sent) == 1
    assert conn.execute(
        "SELECT attempt_count FROM civic_notifications WHERE id = ?",
        (notification.id,)).fetchone()["attempt_count"] == 1
