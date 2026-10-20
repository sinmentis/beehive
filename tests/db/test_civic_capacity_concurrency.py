# tests/db/test_civic_capacity_concurrency.py
"""Real multi-connection races over the daily cap and the carryover it produces.

Two workers that both believe they own a Run is the failure the claim token exists to prevent,
and the two writes the cap adds -- deferring the overflow at seal, adopting it at the next Run --
are exactly the kind that must not double up. A Candidate adopted twice would appear in two
envelopes; a deferral written twice would make "why was this held back?" ambiguous.

Same shape as tests/db/test_civic_concurrency.py: independent sqlite3 connections on one on-disk
WAL database, all released from a single threading.Barrier, so the BEGIN IMMEDIATE fencing has to
be what makes the outcome right.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (adopt_capacity_deferrals, count_pending_capacity_deferrals,
                                      get_or_create_batch_if_claimed,
                                      list_batch_capacity_deferrals,
                                      list_candidate_capacity_deferrals, list_sealed_candidate_ids,
                                      seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate, get_candidate,
                                         list_batch_candidates, mark_candidate_verified,
                                         set_candidate_status)
from beehive.db.civic_runs import (claim_civic_run, complete_civic_run, enqueue_civic_run,
                                   get_civic_run)
from beehive.db.civic_verifications import record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier,
                                  CivicRunStatus, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
_THREADS = 8


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "civic-capacity-concurrency.db")
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
    barrier = threading.Barrier(threads)
    results: list = [None] * threads

    def go(index: int) -> None:
        connection = connect(db_path)
        try:
            barrier.wait()
            results[index] = worker(connection, index)
        except BaseException as exc:  # recorded, then asserted on by the caller
            results[index] = exc
        finally:
            connection.close()

    workers = [threading.Thread(target=go, args=(index,)) for index in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in workers), "a racing thread deadlocked"
    return results


def _slot(conn, watch_id, at, mode="normal"):
    run = enqueue_civic_run(conn, watch_id, at, at, run_mode=mode)
    lease = claim_civic_run(conn, run.id, at, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, at)
    return run.id, lease.run.claim_token, batch


def _verified(conn, batch_id, key, at=T0):
    candidate = create_candidate(conn, batch_id, key, f"Title {key}", f"Statement {key}", at)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", f"e-{key}", f"https://example.test/{key}",
        "Release", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, at,
        publisher="Beehive")
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, at)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", at)
    return mark_candidate_verified(conn, candidate.id, at)


@pytest.fixture
def overflowing(conn):
    """A Watch whose first slot is sealed over the cap, leaving two carryovers waiting."""
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run_id, token, batch = _slot(conn, watch_id, T0)
    candidates = [_verified(conn, batch.id, f"dk-{index:02d}") for index in range(8)]
    sealed = seal_batch_if_claimed(conn, batch.id, run_id, token, T0)
    complete_civic_run(conn, run_id, token, CivicRunStatus.COMPLETED, T0)
    return watch_id, sealed, candidates


# ============================================================================
# Sealing
# ============================================================================


def test_racing_seals_publish_one_envelope_and_defer_the_overflow_once(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run_id, token, batch = _slot(conn, watch_id, T0)
    for index in range(8):
        _verified(conn, batch.id, f"dk-{index:02d}")

    def worker(connection, _index):
        return seal_batch_if_claimed(connection, batch.id, run_id, token, T0)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    hashes = {result.content_hash for result in results}
    assert len(hashes) == 1, "every racing seal must agree on the envelope"
    assert {result.candidate_count for result in results} == {6}
    assert len(list_sealed_candidate_ids(conn, batch.id)) == 6
    assert len(list_batch_capacity_deferrals(conn, batch.id)) == 2
    assert count_pending_capacity_deferrals(conn, watch_id) == 2


def test_a_stale_claim_can_neither_seal_nor_defer(db_path, conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run_id, token, batch = _slot(conn, watch_id, T0)
    for index in range(8):
        _verified(conn, batch.id, f"dk-{index:02d}")

    def worker(connection, index):
        return seal_batch_if_claimed(
            connection, batch.id, run_id, token if index == 0 else f"stale-{index}", T0)

    results = _race(db_path, worker)
    assert results[0] is not None and results[0].candidate_count == 6
    assert all(result is None for result in results[1:])
    assert count_pending_capacity_deferrals(conn, watch_id) == 2


# ============================================================================
# Adoption
# ============================================================================


def test_racing_adoptions_take_each_carryover_exactly_once(db_path, conn, overflowing):
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)

    def worker(connection, _index):
        return adopt_capacity_deferrals(connection, batch.id, run_id, token, tomorrow)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    adopted = [
        candidate_id for result in results for candidate_id in result.adopted_candidate_ids]
    assert sorted(adopted) == [candidates[6].id, candidates[7].id]
    assert len(adopted) == len(set(adopted)), "no Candidate may be adopted twice"
    assert len(list_batch_candidates(conn, batch.id)) == 2
    assert count_pending_capacity_deferrals(conn, watch_id) == 0
    for candidate in candidates[6:]:
        history = list_candidate_capacity_deferrals(conn, candidate.id)
        assert len(history) == 1 and history[0].to_batch_id == batch.id


@pytest.mark.parametrize("expiry", ["lease", "deadline"])
def test_early_adoption_requires_a_live_lease_and_deadline(conn, overflowing, expiry):
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)
    if expiry == "lease":
        now = tomorrow + timedelta(seconds=600)
    else:
        now = tomorrow + timedelta(seconds=60)
        conn.execute(
            "UPDATE civic_runs SET deadline_at=? WHERE id=?", (now.isoformat(), run_id))
        conn.commit()
    before = [get_candidate(conn, candidate.id) for candidate in candidates[6:]]
    history = [list_candidate_capacity_deferrals(conn, candidate.id) for candidate in before]
    assert adopt_capacity_deferrals(conn, batch.id, run_id, token, now) is None
    assert [get_candidate(conn, candidate.id) for candidate in before] == before
    assert [list_candidate_capacity_deferrals(conn, candidate.id) for candidate in before] == history


@pytest.mark.parametrize("expires_before_commit", [False, True])
def test_adoption_resamples_time_without_leaving_partial_moves(
    conn, overflowing, expires_before_commit,
):
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)
    expired = tomorrow + timedelta(seconds=600)
    readings = iter((tomorrow, expired) if expires_before_commit else (expired,))
    before = [get_candidate(conn, candidate.id) for candidate in candidates[6:]]
    history = [list_candidate_capacity_deferrals(conn, candidate.id) for candidate in before]
    assert adopt_capacity_deferrals(
        conn, batch.id, run_id, token, tomorrow, now_fn=lambda: next(readings)) is None
    assert [get_candidate(conn, candidate.id) for candidate in before] == before
    assert [list_candidate_capacity_deferrals(conn, candidate.id) for candidate in before] == history
    assert list_batch_candidates(conn, batch.id) == []


def test_ingest_readoption_is_a_noop_after_early_adoption(conn, overflowing):
    from beehive.civic.pipeline import ingest_civic_documents

    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)
    early = adopt_capacity_deferrals(
        conn, batch.id, run_id, token, tomorrow, now_fn=lambda: tomorrow)
    assert early.adopted_candidate_ids == tuple(candidate.id for candidate in candidates[6:])
    before = list_batch_candidates(conn, batch.id)
    history = [list_candidate_capacity_deferrals(conn, candidate.id) for candidate in before]
    ingested = ingest_civic_documents(
        conn, run_id=run_id, claim_token=token, documents=(), now=tomorrow,
        now_fn=lambda: tomorrow)
    assert ingested.adopted_candidate_ids == ()
    assert ingested.created_candidates == ()
    assert list_batch_candidates(conn, batch.id) == before
    assert [list_candidate_capacity_deferrals(conn, candidate.id) for candidate in before] == history


def test_a_carryover_cannot_be_adopted_into_two_runs(db_path, conn, overflowing):
    """Two Watch-level workers, two different Runs, one carryover each at most. The Candidate's
    row can only point at one Batch, and the status guard is what decides which."""
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    first = _slot(conn, watch_id, tomorrow)
    complete_civic_run(conn, first[0], first[1], CivicRunStatus.COMPLETED, tomorrow)
    day_after = T0 + timedelta(days=2)
    second = _slot(conn, watch_id, day_after)

    # The first Run's claim is gone, so only the live one may adopt.
    def worker(connection, index):
        run_id, token, batch = first if index % 2 == 0 else second
        return adopt_capacity_deferrals(connection, batch.id, run_id, token, day_after)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert all(
        result is None for index, result in enumerate(results) if index % 2 == 0)
    adopted = [
        candidate_id for index, result in enumerate(results) if index % 2
        for candidate_id in result.adopted_candidate_ids]
    assert sorted(adopted) == [candidates[6].id, candidates[7].id]
    for candidate in candidates[6:]:
        assert get_candidate(conn, candidate.id).batch_id == second[2].id


def test_adoption_racing_a_seal_never_splits_a_candidate_across_envelopes(db_path, conn,
                                                                         overflowing):
    """The dangerous interleaving: one worker sealing this Batch while another adopts into it.
    Both take the same write lock, so a Candidate either arrives in time to be published or stays
    waiting -- it can never be half in."""
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)

    def worker(connection, index):
        if index % 2:
            return ("seal", seal_batch_if_claimed(connection, batch.id, run_id, token, tomorrow))
        return ("adopt", adopt_capacity_deferrals(
            connection, batch.id, run_id, token, tomorrow))

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    sealed_ids = list_sealed_candidate_ids(conn, batch.id)
    for candidate in candidates[6:]:
        current = get_candidate(conn, candidate.id)
        if current.batch_id == batch.id and current.sealed_into_batch_at is not None:
            assert candidate.id in sealed_ids
        else:
            assert candidate.id not in sealed_ids
            assert current.status in (
                CivicCandidateStatus.VERIFIED, CivicCandidateStatus.DEFERRED_CAPACITY)
    # Whatever the interleaving, the envelope and its membership agree.
    from beehive.db.civic_batches import get_civic_batch, recompute_content_hash
    batch_now = get_civic_batch(conn, batch.id)
    if batch_now.content_hash is not None:
        assert recompute_content_hash(conn, batch.id) == batch_now.content_hash
        assert batch_now.candidate_count == len(sealed_ids)


# ============================================================================
# A Run ending while another is adopting from it
# ============================================================================


def _adopted_into_second_run(conn, overflowing):
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)
    adopt_capacity_deferrals(conn, batch.id, run_id, token, tomorrow)
    return watch_id, candidates, run_id, token, batch, tomorrow


def test_a_terminal_completion_racing_adoption_never_strands_a_carryover(db_path, conn,
                                                                        overflowing):
    """The reachable interleaving: a worker adopting yesterday's overflow into this Run at the
    very moment the Run is being failed. A Watch only ever has one active Run, so this -- not two
    Runs pulling against each other -- is the race that actually happens.

    Whichever lands first, the Candidates end up back in the queue: adopt-then-fail releases
    them, fail-then-adopt refuses the adoption because the claim is gone."""
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)

    def worker(connection, index):
        if index % 2:
            return ("fail", complete_civic_run(
                connection, run_id, token, CivicRunStatus.FAILED, tomorrow,
                error_code="worker_crashed"))
        return ("adopt", adopt_capacity_deferrals(
            connection, batch.id, run_id, token, tomorrow))

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert sum(1 for kind, value in results if kind == "fail" and value) == 1

    adopted = [
        candidate_id for kind, value in results if kind == "adopt" and value
        for candidate_id in value.adopted_candidate_ids]
    assert len(adopted) == len(set(adopted)), "no Candidate may be adopted twice"
    assert count_pending_capacity_deferrals(conn, watch_id) == 2
    for candidate in candidates[6:]:
        current = get_candidate(conn, candidate.id)
        assert current.status is CivicCandidateStatus.DEFERRED_CAPACITY
        assert current.sealed_into_batch_at is None
    assert list_sealed_candidate_ids(conn, batch.id) == []


def test_the_next_run_publishes_everything_the_race_handed_back(db_path, conn, overflowing):
    """The end-to-end consequence: after the race a fresh Run adopts the carryovers and seals
    them, so nothing the failed Run touched was lost."""
    watch_id, _sealed, candidates = overflowing
    tomorrow = T0 + timedelta(days=1)
    run_id, token, batch = _slot(conn, watch_id, tomorrow)

    def worker(connection, index):
        if index % 2:
            return complete_civic_run(
                connection, run_id, token, CivicRunStatus.CANCELLED, tomorrow)
        return adopt_capacity_deferrals(connection, batch.id, run_id, token, tomorrow)

    _race(db_path, worker)
    assert get_civic_run(conn, run_id).status is CivicRunStatus.CANCELLED

    day_after = T0 + timedelta(days=2)
    later_run, later_token, later_batch = _slot(conn, watch_id, day_after)
    assert adopt_capacity_deferrals(
        conn, later_batch.id, later_run, later_token, day_after).adopted_count == 2
    sealed = seal_batch_if_claimed(conn, later_batch.id, later_run, later_token, day_after)
    assert sorted(list_sealed_candidate_ids(conn, sealed.id)) == sorted(
        candidate.id for candidate in candidates[6:])
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


def test_racing_terminal_completions_release_the_carryovers_once(db_path, conn, overflowing):
    watch_id, candidates, run_id, token, batch, tomorrow = _adopted_into_second_run(
        conn, overflowing)

    def worker(connection, _index):
        return complete_civic_run(
            connection, run_id, token, CivicRunStatus.FAILED, tomorrow, error_code="boom")

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert sum(1 for result in results if result) == 1
    assert count_pending_capacity_deferrals(conn, watch_id) == 2
    for candidate in candidates[6:]:
        history = list_candidate_capacity_deferrals(conn, candidate.id)
        assert [entry.reason for entry in history] == ["batch_cap_reached", "run_failed"]
