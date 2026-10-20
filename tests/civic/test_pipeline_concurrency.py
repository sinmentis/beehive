# tests/civic/test_pipeline_concurrency.py
"""Real multi-connection races over the evidence pipeline's durable seams.

Single-connection tests can only show these invariants are *coded* correctly. These run several
independent sqlite3 connections against one on-disk WAL database, released together from a
threading.Barrier (the pattern established by tests/db/test_civic_concurrency.py), so it is the
BEGIN IMMEDIATE fencing inside the pipeline -- not test ordering -- that has to make the outcome
right:

  * two workers ingesting the same documents produce one Candidate, not two;
  * two workers racing on one Candidate spend its single research attempt once;
  * two workers qualifying one Candidate write one decision and settle it once;
  * a recovered claim writes nothing while the live claim proceeds.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.evidence import CivicClaimCategory, CivicContentClass, CivicSourceKind
from beehive.civic.pipeline import (begin_civic_research, ingest_civic_documents,
                                    qualify_civic_candidate)
from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_candidates import add_candidate_evidence, get_candidate, list_run_candidates
from beehive.db.civic_consumed_items import list_consumed_item_keys
from beehive.db.civic_qualifications import list_qualifications
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier

from tests.civic.test_pipeline import REGISTRY, claim_for, doc, evidence

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
_THREADS = 8


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "civic-pipeline-concurrency.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def run(conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    enqueued = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, enqueued.id, T0, 600, 3600)
    get_or_create_batch_if_claimed(conn, enqueued.id, lease.run.claim_token, T0)
    return type("Ctx", (), {
        "watch_id": watch_id, "id": enqueued.id, "token": lease.run.claim_token})()


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


# ============================================================================
# Ingest
# ============================================================================


def test_racing_workers_ingesting_one_document_set_create_one_candidate(db_path, conn, run):
    documents = [
        doc("a", "Government announces new housing policy"),
        doc("b", "Government announces new housing policy today", source="nz_stuff",
            tier=CivicEvidenceTier.REPUTABLE_MEDIA),
    ]

    def worker(connection, _index):
        return ingest_civic_documents(
            connection, run_id=run.id, claim_token=run.token, now=T0, documents=documents)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    created = sum(len(result.created_candidates) for result in results)
    assert created == 1, "the consumed-item claim must let exactly one worker build it"
    assert len(list_run_candidates(conn, run.id)) == 1
    assert sorted(list_consumed_item_keys(conn, run.watch_id)) == [
        "nz_beehive_releases:a", "nz_stuff:b"]


def test_only_the_live_claim_ingests(db_path, conn, run):
    """A worker whose lease was recovered writes nothing, and says so with None rather than an
    exception -- losing a claim is a race, not a fault."""
    def worker(connection, index):
        token = run.token if index == 0 else f"stale-{index}"
        return ingest_civic_documents(
            connection, run_id=run.id, claim_token=token, now=T0,
            documents=[doc("a", "Government announces new housing policy")])

    results = _race(db_path, worker)
    assert results[0] is not None and len(results[0].created_candidates) == 1
    assert all(result is None for result in results[1:])
    assert len(list_run_candidates(conn, run.id)) == 1


# ============================================================================
# Qualification
# ============================================================================


def _candidate(conn, run):
    result = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0,
        documents=[doc("a", "Government announces new housing policy")])
    return result.created_candidates[0]


def test_racing_qualifications_record_one_decision_and_settle_once(db_path, conn, run):
    candidate = _candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    claim = claim_for(candidate)

    def worker(connection, _index):
        return qualify_civic_candidate(
            connection, run_id=run.id, claim_token=run.token, claim=claim, registry=REGISTRY,
            now=T0)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    decided = [result for result in results if not result.replayed]
    assert len(decided) == 1, "only one worker may take the decision"
    assert len(list_qualifications(conn, candidate.id)) == 1
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFYING
    assert {result.record.id for result in results} == {decided[0].record.id}


def test_racing_qualifications_of_a_screened_candidate_settle_it_once(db_path, conn, run):
    candidate = _candidate(conn, run)
    evidence(conn, candidate.id, "e1")
    claim = claim_for(
        candidate, content_class=CivicContentClass.OPINION,
        category=CivicClaimCategory.OPINION)

    def worker(connection, _index):
        return qualify_civic_candidate(
            connection, run_id=run.id, claim_token=run.token, claim=claim, registry=REGISTRY,
            now=T0)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    settled = get_candidate(conn, candidate.id)
    assert settled.status is CivicCandidateStatus.REJECTED
    assert settled.rejection_code == "screened_out"
    assert settled.settled_at == T0
    assert len(list_qualifications(conn, candidate.id)) == 1


# ============================================================================
# The bounded research attempt
# ============================================================================


def test_racing_workers_spend_the_research_attempt_exactly_once(db_path, conn, run):
    candidate = _candidate(conn, run)
    add_candidate_evidence(
        conn, candidate.id, "media_feed", "m1", "https://example.test/m1", "Report",
        CivicEvidenceTier.REPUTABLE_MEDIA, CivicEvidenceStance.SUPPORTING, T0,
        publisher="Stuff", publisher_id="stuff",
        source_kind=CivicSourceKind.INDEPENDENT_NEWS.value, subject_id="green-party",
        claim_category=CivicClaimCategory.PARTY_TIMELINE_EVENT.value, event_date=T0.date(),
        canonical_url="https://example.test/m1", accessible_full_text=True)
    qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, claim=claim_for(candidate),
        registry=REGISTRY, now=T0)
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.RESEARCHING

    def worker(connection, _index):
        return begin_civic_research(
            connection, run_id=run.id, claim_token=run.token, candidate_id=candidate.id, now=T0)

    results = _race(db_path, worker)
    assert not [result for result in results if isinstance(result, BaseException)]
    granted = [result for result in results if result is not None]
    assert granted == [1], "the single bounded pass may be granted to exactly one worker"
    assert get_candidate(conn, candidate.id).research_attempt_count == 1


def test_a_stale_claim_never_takes_the_research_attempt(db_path, conn, run):
    candidate = _candidate(conn, run)
    add_candidate_evidence(
        conn, candidate.id, "media_feed", "m1", "https://example.test/m1", "Report",
        CivicEvidenceTier.REPUTABLE_MEDIA, CivicEvidenceStance.SUPPORTING, T0,
        publisher="Stuff", publisher_id="stuff",
        source_kind=CivicSourceKind.INDEPENDENT_NEWS.value, subject_id="green-party",
        claim_category=CivicClaimCategory.PARTY_TIMELINE_EVENT.value, event_date=T0.date(),
        canonical_url="https://example.test/m1", accessible_full_text=True)
    qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, claim=claim_for(candidate),
        registry=REGISTRY, now=T0)

    def worker(connection, index):
        return begin_civic_research(
            connection, run_id=run.id, claim_token=f"stale-{index}", candidate_id=candidate.id,
            now=T0)

    results = _race(db_path, worker)
    assert all(result is None for result in results)
    assert get_candidate(conn, candidate.id).research_attempt_count == 0


# ============================================================================
# Deadline fencing under contention
# ============================================================================


def test_a_worker_that_blocks_past_the_deadline_writes_nothing(db_path, conn, run):
    """`now` is sampled after BEGIN IMMEDIATE. A worker that queued for the write lock while the
    Run's budget ran out must fence on the time it got the lock, not the time it asked."""
    from beehive.db.civic_runs import get_civic_run

    deadline = get_civic_run(conn, run.id).deadline_at
    clock = iter([T0, deadline + timedelta(seconds=1)])
    lock = threading.Lock()

    def now_fn():
        with lock:
            return next(clock, deadline + timedelta(seconds=1))

    def worker(connection, index):
        return ingest_civic_documents(
            connection, run_id=run.id, claim_token=run.token, now=T0, now_fn=now_fn,
            documents=[doc(f"k{index}", f"Development number {index} of many distinct ones")])

    results = _race(db_path, worker, threads=2)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert sum(1 for result in results if result is None) == 1
    assert len(list_run_candidates(conn, run.id)) == 1
