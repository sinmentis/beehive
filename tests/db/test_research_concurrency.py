"""Real multi-connection, multi-thread concurrency regression tests for the invariants that a
single-connection, single-thread test can only assert are *coded* correctly, not that they
actually hold under a genuine race: the global three-processing-run cap, the one-active-
Research-Run-per-session rule, session-wide citation_number allocation, the one-active-
chat-request-per-session rule, and Research Run finalization's claim+deadline fence. Each test
opens several independent sqlite3 connections against the same on-disk WAL database (mirroring
test_deep_reads.py's established pattern) and uses threading.Barrier to line every thread up on
the same starting line, so the BEGIN IMMEDIATE fencing inside the repository functions -- not
test ordering -- is what has to make the outcome correct."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_clusters import list_evidence_clusters
from beehive.db.evidence_curation import get_evidence_curation, set_evidence_curation
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import (create_evidence_state_revision,
                                       get_latest_evidence_state_revision,
                                       list_evidence_state_revisions)
from beehive.db.research_chat_requests import (enqueue_chat_request, list_chat_requests,
                                                submit_chat_request)
from beehive.db.research_finalization import (FinalizationFailureReason,
                                               finalize_snapshot_if_claimed)
from beehive.db.research_messages import append_message, list_messages
from beehive.db.research_runs import (TerminalCompletionFailureReason, claim_research_run,
                                       complete_research_run, complete_research_run_if_claimed,
                                       count_active_processing_runs, enqueue_research_run,
                                       get_research_run, heartbeat_research_run,
                                       list_research_runs, recover_expired_research_runs,
                                       request_cancel_research_run, requeue_research_run)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import (SnapshotAppendFailureReason, SnapshotClaimFailureReason,
                                            add_snapshot_items, add_snapshot_items_if_claimed,
                                            create_snapshot, get_or_create_snapshot_if_claimed,
                                            get_snapshot, list_snapshot_item_ids,
                                            list_snapshots, seal_snapshot)
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import (SynthesisAdmissionStatus, SynthesisPersistFailureReason,
                                            admit_synthesis_if_claimed, create_synthesis,
                                            create_synthesis_if_claimed, get_latest_synthesis,
                                            list_syntheses)
from beehive.domain.research import (ClaimProvenance, ConversationRole, EvidenceCitation,
                                      EvidenceQuality, EvidenceSnapshotStatus, ResearchRunStatus,
                                      ResearchSourceOrigin, SufficiencyState, SynthesisClaim,
                                      SynthesisSection)
from beehive.research import synthesis as synth


T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _extra_connection(tmp_path):
    other = connect(str(tmp_path / "test.db"))
    init_schema(other)
    return other


# -- global three-processing-run cap (ADR-0009) -----------------------------------------

def test_concurrent_claims_across_connections_never_exceed_the_three_run_cap(tmp_path, conn):
    """Five Research Runs (each on its own session -- a session may only ever have one active
    run), five independent connections, all racing to claim_research_run at the same instant:
    exactly three may win (the ADR-0009 cap), never four or more, and the losers must cleanly
    get None back rather than an error or a phantom claim."""
    session_ids = [create_research_session(conn, f"Q{i}", T0).id for i in range(5)]
    run_ids = [enqueue_research_run(conn, sid, T0).id for sid in session_ids]

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(4)]
    barrier = threading.Barrier(5)
    results = {}
    errors = []

    def call(label, connection, run_id):
        barrier.wait(timeout=5)
        try:
            results[label] = claim_research_run(
                connection, run_id, T0, lease_seconds=60, deadline_seconds=3600)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=call, args=(i, connections[i], run_ids[i]))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert errors == []
    successes = [r for r in results.values() if r is not None]
    failures = [r for r in results.values() if r is None]
    assert len(successes) == 3
    assert len(failures) == 2
    assert count_active_processing_runs(conn, T0) == 3


def test_expired_lease_heartbeat_cannot_revive_after_replacement_claims_the_freed_slot(
        tmp_path, conn):
    """Task B, the exact bug this closes: three Research Runs (A, B, C) are already
    'processing'. A's short lease elapses (while B and C's much longer leases have not), which
    makes count_active_processing_runs -- and therefore claim_research_run's own global cap
    check -- immediately stop counting A, even though nobody has run recover_expired_research_
    runs yet and A's row is still nominally 'processing' with its original claim_token. A fourth
    run, D, is claimed into what looks like the one freed slot. A's own worker, unaware its
    effective capacity slot is gone (it may be paused on a GC cycle, a slow write queue, or
    simply hasn't noticed yet), then heartbeats using its ORIGINAL, now-independently-expired
    lease: this must be refused (Task B's "never revive an expired lease" fix) so the fleet can
    never end up with four simultaneously-active processing runs."""
    session_ids = [create_research_session(conn, f"Q{i}", T0).id for i in range(4)]
    runs = [enqueue_research_run(conn, sid, T0).id for sid in session_ids]
    connections = [conn] + [_extra_connection(tmp_path) for _ in range(3)]

    # A: a short-lived lease that will expire long before the run's own (much longer) deadline.
    lease_a = claim_research_run(
        connections[0], runs[0], T0, lease_seconds=10, deadline_seconds=3600)
    # B, C: long-lived leases -- comfortably still active throughout this test.
    claim_research_run(connections[1], runs[1], T0, lease_seconds=3600, deadline_seconds=3600)
    claim_research_run(connections[2], runs[2], T0, lease_seconds=3600, deadline_seconds=3600)
    assert count_active_processing_runs(conn, T0) == 3

    # A's lease has now independently expired (B/C have not) -- nobody has reconciled it yet,
    # but the global cap check already excludes it.
    later = T0 + timedelta(seconds=20)
    assert count_active_processing_runs(conn, later) == 2

    # D claims the slot the cap check now reports as free.
    lease_d = claim_research_run(
        connections[3], runs[3], later, lease_seconds=3600, deadline_seconds=3600)
    assert lease_d is not None
    assert count_active_processing_runs(conn, later) == 3

    # A's own (still-live-in-status, but independently-expired-lease) worker finally gets around
    # to heartbeating with its ORIGINAL claim_token.
    delayed_heartbeat_ok = heartbeat_research_run(
        connections[0], runs[0], lease_a.run.claim_token, later, lease_seconds=3600)

    assert delayed_heartbeat_ok is False
    assert count_active_processing_runs(conn, later) == 3  # never four
    a_row = conn.execute(
        "SELECT status, claim_token, lease_expires_at FROM research_runs WHERE id = ?",
        (runs[0],)).fetchone()
    assert a_row["status"] == ResearchRunStatus.PROCESSING.value  # untouched by the refused call
    assert a_row["claim_token"] == lease_a.run.claim_token
    assert a_row["lease_expires_at"] == lease_a.lease_expires_at  # never revived

    for extra in connections[1:]:
        extra.close()


# -- one active Research Run per session -------------------------------------------------

def test_concurrent_enqueue_research_run_allows_exactly_one_winner(tmp_path, conn):
    """Four connections racing to enqueue_research_run for the same brand-new session: exactly
    one must win, every loser must fail with the documented ValueError (never a raw
    sqlite3.IntegrityError bubbling past the application-level check), and only one row is ever
    left behind."""
    session_id = create_research_session(conn, "Q", T0).id

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(3)]
    barrier = threading.Barrier(4)
    results = {}
    errors = {}

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = enqueue_research_run(connection, session_id, T0)
        except ValueError as exc:
            errors[label] = exc

    threads = [threading.Thread(target=call, args=(i, connections[i])) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert len(results) == 1
    assert len(errors) == 3
    assert all("already has an active Research Run" in str(exc) for exc in errors.values())
    assert len(list_research_runs(conn, session_id)) == 1


def test_concurrent_enqueue_research_run_is_independent_across_sessions(tmp_path, conn):
    """Racing enqueue_research_run for several DIFFERENT sessions at the same instant must not
    spuriously block each other -- every session gets its own winning run."""
    session_ids = [create_research_session(conn, f"Q{i}", T0).id for i in range(4)]

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(3)]
    barrier = threading.Barrier(4)
    results = {}
    errors = []

    def call(label, connection, session_id):
        barrier.wait(timeout=5)
        try:
            results[label] = enqueue_research_run(connection, session_id, T0)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=call, args=(i, connections[i], session_ids[i]))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert errors == []
    assert len(results) == 4
    for session_id in session_ids:
        assert len(list_research_runs(conn, session_id)) == 1


def test_concurrent_enqueue_research_run_after_terminal_completion_allows_one_more_winner(
        tmp_path, conn):
    """Once the session's only run reaches a terminal state, a fresh round of racing
    enqueue_research_run calls must again produce exactly one winner -- the terminal run never
    keeps blocking a later refresh."""
    session_id = create_research_session(conn, "Q", T0).id
    first = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, first.id, T0, lease_seconds=60, deadline_seconds=3600)
    complete_research_run(
        conn, first.id, lease.run.claim_token, ResearchRunStatus.COMPLETED, T0)

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(3)]
    barrier = threading.Barrier(4)
    results = {}
    errors = {}

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = enqueue_research_run(
                connection, session_id, T0 + timedelta(minutes=1))
        except ValueError as exc:
            errors[label] = exc

    threads = [threading.Thread(target=call, args=(i, connections[i])) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert len(results) == 1
    assert len(errors) == 3
    # the terminal first run plus exactly one new winner -- never a second active run
    assert len(list_research_runs(conn, session_id)) == 2


# -- session-wide citation_number allocation ---------------------------------------------

def test_concurrent_evidence_item_upserts_allocate_distinct_citation_numbers(tmp_path, conn):
    """Six brand-new Evidence Items (distinct external_keys, so each is a genuine INSERT, not
    a dedup-upsert) collected concurrently across six connections for the same session must
    never collide on citation_number -- each of 1..6 must be allocated exactly once."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(5)]
    barrier = threading.Barrier(6)
    results = {}
    errors = []

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = upsert_evidence_item(
                connection, session_id, source_id, f"e{label}", f"T{label}",
                f"https://x/{label}", EvidenceQuality.REPORTING, T0)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(i, connections[i])) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert errors == []
    citation_numbers = sorted(item.citation_number for item in results.values())
    assert citation_numbers == [1, 2, 3, 4, 5, 6]


# -- one active chat request per session --------------------------------------------------

def test_concurrent_enqueue_chat_request_allows_exactly_one_winner(tmp_path, conn):
    """Four connections racing to enqueue a chat request for the same session: exactly one
    must succeed, and every loser must fail with the documented ValueError (never an
    unhandled sqlite3.IntegrityError bubbling past the application-level check, and never a
    second row silently accepted)."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision_id = create_evidence_state_revision(
        conn, session_id, snapshot_id, [item.id], T0).id
    owner_messages = [
        append_message(conn, session_id, ConversationRole.OWNER, f"Q{i}", T0).id
        for i in range(4)
    ]

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(3)]
    barrier = threading.Barrier(4)
    results = {}
    errors = {}

    def call(label, connection, owner_message_id):
        barrier.wait(timeout=5)
        try:
            results[label] = enqueue_chat_request(
                connection, session_id, owner_message_id, revision_id, None, 0, T0)
        except ValueError as exc:
            errors[label] = exc

    threads = [
        threading.Thread(target=call, args=(i, connections[i], owner_messages[i]))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert len(results) == 1
    assert len(errors) == 3
    assert all("active chat request" in str(exc) for exc in errors.values())
    assert len(list_chat_requests(conn, session_id)) == 1


# -- submit_chat_request: one atomic owner-message + chat-request race --------------------

def test_concurrent_submit_chat_request_allows_exactly_one_winner(tmp_path, conn):
    """Four connections racing to submit_chat_request (the single-transaction owner-message +
    chat-request entry point research.conversation.py's submission phase uses) for the same
    session: exactly one may succeed, every loser must fail with the documented ValueError, and
    -- unlike a caller that appended its own owner message first and then raced on
    enqueue_chat_request separately -- every loser's own owner message must never have been
    written at all, since submission is one BEGIN IMMEDIATE transaction end to end."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], T0)
    claim = SynthesisClaim(
        text="Bottom line", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item.id, item.citation_number),))
    create_synthesis(
        conn, session_id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", T0)

    connections = [conn] + [_extra_connection(tmp_path) for _ in range(3)]
    barrier = threading.Barrier(4)
    results = {}
    errors = {}

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = submit_chat_request(connection, session_id, f"Q{label}?", T0)
        except ValueError as exc:
            errors[label] = exc

    threads = [threading.Thread(target=call, args=(i, connections[i])) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for extra in connections[1:]:
        extra.close()

    assert len(results) == 1
    assert len(errors) == 3
    assert all("active chat request" in str(exc) for exc in errors.values())
    assert len(list_chat_requests(conn, session_id)) == 1
    # every loser's own owner message never got written: exactly one message total
    assert len(list_messages(conn, session_id)) == 1


# -- finalize_snapshot_if_claimed: claim+deadline-fenced atomic finalization -------------

def _finalization_scenario(conn, question="Q"):
    """A claimed run with a still-'building' snapshot holding two near-duplicate items (destined
    for one cluster), already added to the snapshot's membership -- everything
    finalize_snapshot_if_claimed needs."""
    session_id = create_research_session(conn, question, T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    snapshot_id = create_snapshot(conn, session_id, run.id, T0).id
    a = upsert_evidence_item(
        conn, session_id, source_id, "a", "T-a", "https://x/a", EvidenceQuality.REPORTING, T0)
    b = upsert_evidence_item(
        conn, session_id, source_id, "b", "T-b", "https://x/b", EvidenceQuality.REPORTING, T0)
    add_snapshot_items(conn, snapshot_id, [a.id, b.id], T0)
    return {
        "session_id": session_id, "run_id": run.id, "claim_token": lease.run.claim_token,
        "deadline_at": lease.run.deadline_at, "snapshot_id": snapshot_id, "a": a.id, "b": b.id,
    }


def test_concurrent_deadline_heartbeat_and_finalize_race_leaves_no_partial_finalization(
        tmp_path, conn):
    """A real race between a heartbeat that discovers the run's deadline has just arrived (on
    one connection) and finalize_snapshot_if_claimed attempting to finalize that very same run
    at the very same instant (on another) -- precomputation (cluster grouping, curation
    filtering) having already happened before either side reaches the shared BEGIN IMMEDIATE
    lock. Whichever transaction wins the write lock, the outcome converges to the exact same
    observable state: the run ends up FAILED with error_code='deadline_exceeded', and NOTHING is
    ever written for finalization -- no cluster row, no sealed snapshot, no Evidence State
    Revision -- because finalize_snapshot_if_claimed's own deadline check (`now >= deadline_at`)
    fires symmetrically to heartbeat_research_run's, whichever of the two actually reaches the
    row first."""
    scenario = _finalization_scenario(conn)
    conn_finalize = conn
    conn_heartbeat = _extra_connection(tmp_path)

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def run_finalize():
        barrier.wait(timeout=5)
        try:
            results["finalize"] = finalize_snapshot_if_claimed(
                conn_finalize, scenario["run_id"], scenario["claim_token"],
                scenario["session_id"], scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
                [scenario["a"], scenario["b"]], scenario["deadline_at"])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    def run_heartbeat():
        barrier.wait(timeout=5)
        try:
            results["heartbeat"] = heartbeat_research_run(
                conn_heartbeat, scenario["run_id"], scenario["claim_token"],
                scenario["deadline_at"], lease_seconds=60)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run_finalize), threading.Thread(target=run_heartbeat)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_heartbeat.close()

    assert errors == []
    # Whichever side wins the lock, the finalize call must never report success.
    assert results["finalize"].ok is False
    assert results["finalize"].failure_reason in (
        FinalizationFailureReason.CLAIM_LOST, FinalizationFailureReason.DEADLINE_EXCEEDED)
    assert results["finalize"].snapshot is None
    assert results["finalize"].clusters == ()
    assert results["finalize"].revision is None
    # heartbeat_research_run can never report success once the deadline has arrived.
    assert results["heartbeat"] is False

    run_row = conn.execute(
        "SELECT status, phase, claim_token, error_code FROM research_runs WHERE id = ?",
        (scenario["run_id"],)).fetchone()
    assert run_row["status"] == ResearchRunStatus.FAILED.value
    assert run_row["phase"] is None
    assert run_row["claim_token"] is None
    assert run_row["error_code"] == "deadline_exceeded"

    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


def test_concurrent_finalize_before_deadline_yields_exactly_one_sealed_snapshot_and_revision(
        tmp_path, conn):
    """Finalization winning cleanly before the deadline arrives must produce exactly one sealed
    snapshot, one Evidence Cluster with the correct membership, and one Evidence State Revision
    with the correct active membership -- and a second, racing attempt against the very same
    (now-sealed) snapshot -- e.g. a duplicate retry from a worker that briefly believed its
    claim was lost -- must never duplicate either, since only a still-'building' snapshot is
    ever accepted and the whole transition is atomic."""
    scenario = _finalization_scenario(conn)
    conn_other = _extra_connection(tmp_path)
    well_before_deadline = scenario["deadline_at"] - timedelta(seconds=30)

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = finalize_snapshot_if_claimed(
                connection, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
                scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
                [scenario["a"], scenario["b"]], well_before_deadline)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=call, args=("first", conn)),
        threading.Thread(target=call, args=("second", conn_other)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_other.close()

    assert errors == []
    winners = [r for r in results.values() if r.ok]
    losers = [r for r in results.values() if not r.ok]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].failure_reason == FinalizationFailureReason.CLAIM_LOST

    winner = winners[0]
    assert winner.snapshot.status == EvidenceSnapshotStatus.SEALED
    assert len(winner.clusters) == 1
    assert set(winner.clusters[0].evidence_item_ids) == {scenario["a"], scenario["b"]}
    assert set(winner.revision.evidence_item_ids) == {scenario["a"], scenario["b"]}

    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.SEALED
    assert len(list_evidence_clusters(conn, scenario["snapshot_id"])) == 1
    assert len(list_evidence_state_revisions(conn, scenario["session_id"])) == 1


# -- Race 1: authoritative clock sampled only AFTER BEGIN IMMEDIATE holds the write lock -----

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


def test_claim_research_run_never_samples_now_fn_before_acquiring_the_write_lock(
        tmp_path, conn):
    """A requeued, already-deadlined pending run (exactly what recover_expired_research_runs
    leaves behind: deadline_at fixed, status back to 'pending') is reclaimed by `conn` while a
    second connection holds the write lock via its own BEGIN IMMEDIATE. `now_fn` is rigged to
    return a NOT-yet-arrived reading if it is ever invoked while that lock is still held, and
    the genuinely-arrived reading only once the holder has released it. claim_research_run must
    never observe the "not yet arrived" reading: it is only allowed to call `now_fn()` AFTER its
    own BEGIN IMMEDIATE has actually acquired the lock, which cannot happen until the holder
    releases -- so by the time it samples anything, the deadline has already arrived. A
    regression that samples `now_fn()` (or any clock) BEFORE attempting to acquire the lock
    would instead capture the "not yet arrived" reading while still blocked, then incorrectly
    grant a lease using that stale sample the instant the lock frees up."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    first = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    deadline_at = first.run.deadline_at
    assert requeue_research_run(conn, run.id, first.run.claim_token)

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = _extra_connection(tmp_path)
    holder = threading.Thread(
        target=_hold_write_lock, args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)  # the holder now genuinely holds the write lock

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_claim():
        result["lease"] = claim_research_run(
            conn, run.id, deadline_at - timedelta(seconds=30), lease_seconds=600,
            deadline_seconds=3600, now_fn=now_fn)

    claimer = threading.Thread(target=do_claim)
    claimer.start()
    # Give the claimer thread every chance to run ahead -- including, if buggy, sampling
    # now_fn() before ever attempting BEGIN IMMEDIATE -- while the lock is still genuinely held.
    time.sleep(0.2)
    release_holder.set()
    claimer.join(timeout=5)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]  # now_fn was invoked exactly once, and only after the lock was free
    assert result["lease"] is None
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    assert run_row.claim_token is None
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_finalize_never_samples_now_fn_before_acquiring_the_write_lock(tmp_path, conn):
    """Same seam, exercised against finalize_snapshot_if_claimed: a second connection holds the
    write lock while `conn` attempts to finalize a run whose deadline has already arrived.
    `now_fn` returns "not yet arrived" only while that lock is still held and the genuine,
    arrived reading once it is free -- finalize_snapshot_if_claimed must observe only the
    latter, since it cannot call `now_fn()` until its own BEGIN IMMEDIATE has acquired the lock.
    The outcome must be DEADLINE_EXCEEDED (never a successful finalize slipping through on a
    stale pre-lock reading), and nothing is written: no cluster, no seal, no revision."""
    scenario = _finalization_scenario(conn)
    deadline_at = scenario["deadline_at"]

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = _extra_connection(tmp_path)
    holder = threading.Thread(
        target=_hold_write_lock, args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_finalize():
        result["finalized"] = finalize_snapshot_if_claimed(
            conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
            scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
            [scenario["a"], scenario["b"]], deadline_at - timedelta(seconds=30), now_fn=now_fn,
            expected_snapshot_item_ids=[scenario["a"], scenario["b"]])

    finalizer = threading.Thread(target=do_finalize)
    finalizer.start()
    time.sleep(0.2)
    release_holder.set()
    finalizer.join(timeout=5)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]
    finalized = result["finalized"]
    assert not finalized.ok
    assert finalized.failure_reason == FinalizationFailureReason.DEADLINE_EXCEEDED
    assert finalized.snapshot is None
    assert finalized.clusters == ()
    assert finalized.revision is None

    run_row = get_research_run(conn, scenario["run_id"])
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (scenario["run_id"],)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


def _synthesis_scenario(conn, question="Q"):
    """A claimed run with one sealed Evidence Snapshot and pinned Evidence State Revision --
    everything create_synthesis_if_claimed needs to persist a Research Synthesis, standing in
    for research.synthesis.generate_synthesis's own two-AI-calls-then-persist shape (the AI
    calls themselves are pure computation from this repository call's point of view; only
    persistence is claim/deadline-fenced)."""
    session_id = create_research_session(conn, question, T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    snapshot_id = create_snapshot(conn, session_id, run.id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "a", "T-a", "https://x/a", EvidenceQuality.REPORTING, T0)
    add_snapshot_items(conn, snapshot_id, [item.id], T0)
    seal_snapshot(conn, snapshot_id, T0)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], T0)
    claim = SynthesisClaim(
        text="Bottom line", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item.id, item.citation_number),))
    return {
        "session_id": session_id, "run_id": run.id, "claim_token": lease.run.claim_token,
        "deadline_at": lease.run.deadline_at, "revision_id": revision.id, "claim": claim,
    }


def test_create_synthesis_if_claimed_never_samples_now_fn_before_acquiring_the_write_lock(
        tmp_path, conn):
    """Task C: the same lock-wait-crossing-deadline seam as finalize_snapshot_if_claimed's own
    test, exercised against create_synthesis_if_claimed -- a second connection holds the write
    lock while `conn` attempts to persist a Research Synthesis for a run whose deadline has
    already arrived (standing in for the two AI calls having already finished fully within
    budget, with only persistence itself left to contend for the shared BEGIN IMMEDIATE lock).
    `now_fn` returns "not yet arrived" only while that lock is still held and the genuine,
    arrived reading once it is free -- create_synthesis_if_claimed must observe only the latter.
    The outcome must be DEADLINE_EXCEEDED (never a successful persist slipping through on a
    stale pre-lock reading): no synthesis row, no citation row, the run fails outright, and
    get_latest_synthesis must never expose a row that was never written."""
    scenario = _synthesis_scenario(conn)
    deadline_at = scenario["deadline_at"]

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = _extra_connection(tmp_path)
    holder = threading.Thread(
        target=_hold_write_lock, args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_create():
        result["persisted"] = create_synthesis_if_claimed(
            conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
            scenario["revision_id"], SufficiencyState.PARTIAL, (scenario["claim"],), "gpt-5",
            "en", deadline_at - timedelta(seconds=30), now_fn=now_fn)

    creator = threading.Thread(target=do_create)
    creator.start()
    time.sleep(0.2)
    release_holder.set()
    creator.join(timeout=5)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]
    persisted = result["persisted"]
    assert not persisted.ok
    assert persisted.failure_reason == SynthesisPersistFailureReason.DEADLINE_EXCEEDED
    assert persisted.synthesis is None

    assert list_syntheses(conn, scenario["session_id"]) == []
    assert get_latest_synthesis(conn, scenario["session_id"]) is None
    row = conn.execute("SELECT COUNT(*) AS n FROM research_synthesis_citations").fetchone()
    assert row["n"] == 0

    run_row = get_research_run(conn, scenario["run_id"])
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (scenario["run_id"],)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_heartbeat_research_run_never_samples_now_fn_before_acquiring_the_write_lock(
        tmp_path, conn):
    """Same seam, exercised against heartbeat_research_run: a second connection holds the write
    lock while `conn` attempts to heartbeat a run whose deadline has already arrived. `now_fn`
    returns "not yet arrived" only while that lock is still held and the genuine, arrived
    reading once it is free -- heartbeat_research_run must observe only the latter, since it
    cannot call `now_fn()` until its own BEGIN IMMEDIATE has acquired the lock. The renewal must
    be refused and the run hard-terminated (error_code='deadline_exceeded'), never renewed on a
    stale pre-lock reading."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = _extra_connection(tmp_path)
    holder = threading.Thread(
        target=_hold_write_lock, args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_heartbeat():
        result["ok"] = heartbeat_research_run(
            conn, run.id, claimed.run.claim_token, deadline_at - timedelta(seconds=30),
            lease_seconds=60, now_fn=now_fn)

    heartbeater = threading.Thread(target=do_heartbeat)
    heartbeater.start()
    time.sleep(0.2)
    release_holder.set()
    heartbeater.join(timeout=5)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]
    assert result["ok"] is False
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    assert run_row.claim_token is None
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_recover_expired_research_runs_never_samples_now_fn_before_acquiring_the_write_lock(
        tmp_path, conn):
    """Same seam, exercised against recover_expired_research_runs: a second connection holds the
    write lock while `conn` sweeps a run whose lease has expired and whose deadline has already
    arrived. `now_fn` returns "not yet arrived" only while that lock is still held and the
    genuine, arrived reading once it is free -- the sweep must observe only the latter and fail
    the run outright (never requeue it) on the genuinely-arrived reading, never the stale
    pre-lock one."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=1, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = _extra_connection(tmp_path)
    holder = threading.Thread(
        target=_hold_write_lock, args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_recover():
        result["recovery"] = recover_expired_research_runs(
            conn, deadline_at - timedelta(seconds=30), now_fn=now_fn)

    recoverer = threading.Thread(target=do_recover)
    recoverer.start()
    time.sleep(0.2)
    release_holder.set()
    recoverer.join(timeout=5)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]
    assert result["recovery"].requeued_count == 0
    assert result["recovery"].deadline_exceeded_count == 1
    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_complete_research_run_if_claimed_never_samples_now_fn_before_acquiring_the_write_lock(
        tmp_path, conn):
    """Same seam, exercised against complete_research_run_if_claimed: a second connection holds
    the write lock while `conn` attempts to commit a requested COMPLETED for a run whose deadline
    has already arrived. `now_fn` returns "not yet arrived" only while that lock is still held
    and the genuine, arrived reading once it is free -- complete_research_run_if_claimed must
    observe only the latter, since it cannot call `now_fn()` until its own BEGIN IMMEDIATE has
    acquired the lock. The outcome must be DEADLINE_EXCEEDED (never a successful COMPLETED
    slipping through on a stale pre-lock reading)."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at

    lock_acquired = threading.Event()
    release_holder = threading.Event()
    lock_released = threading.Event()
    holder_conn = _extra_connection(tmp_path)
    holder = threading.Thread(
        target=_hold_write_lock, args=(holder_conn, lock_acquired, release_holder, lock_released))
    holder.start()
    assert lock_acquired.wait(timeout=5)

    calls = []

    def now_fn():
        calls.append(lock_released.is_set())
        return deadline_at if lock_released.is_set() else deadline_at - timedelta(seconds=30)

    result = {}

    def do_complete():
        result["terminal"] = complete_research_run_if_claimed(
            conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
            deadline_at - timedelta(seconds=30), now_fn=now_fn)

    completer = threading.Thread(target=do_complete)
    completer.start()
    time.sleep(0.2)
    release_holder.set()
    completer.join(timeout=5)
    holder.join(timeout=5)
    holder_conn.close()

    assert calls == [True]
    terminal = result["terminal"]
    assert not terminal.ok
    assert terminal.failure_reason == TerminalCompletionFailureReason.DEADLINE_EXCEEDED
    assert terminal.committed_status is None

    run_row = get_research_run(conn, run.id)
    assert run_row.status == ResearchRunStatus.FAILED
    assert run_row.claim_token is None
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_cancellation_committing_first_forces_completion_to_choose_cancelled(tmp_path, conn):
    """A real race between an Owner's cancellation request (`request_cancel_research_run`,
    committing on a wholly separate connection) and this run's own terminal
    `complete_research_run_if_claimed(requested_status=COMPLETED)` write, arranged -- via a
    `threading.Event` the cancelling thread sets only once its own commit has actually landed --
    so the cancellation deterministically commits FIRST. The terminal write must reread
    cancel_requested fresh under its own lock and commit CANCELLED, never a stale COMPLETED that
    ignores a cancellation which had, by that point, already committed."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)

    conn_cancel = _extra_connection(tmp_path)
    conn_complete = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(conn_cancel, run.id)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_complete():
        assert cancel_committed.wait(timeout=5)
        try:
            results["terminal"] = complete_research_run_if_claimed(
                conn_complete, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
                T0 + timedelta(seconds=5))
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_complete.close()

    assert errors == []
    assert results["cancel_requested"] is True
    terminal = results["terminal"]
    assert terminal.ok is True
    assert terminal.committed_status == ResearchRunStatus.CANCELLED
    assert get_research_run(conn, run.id).status == ResearchRunStatus.CANCELLED


def test_cancellation_committing_before_the_deadline_forces_the_terminal_write_to_cancel_not_fail(
        tmp_path, conn):
    """Task A -- cancellation precedence, at the exact instant a deadline watchdog would
    otherwise fire: an Owner's cancellation commits on a wholly separate connection (forced to
    land FIRST via a `threading.Event`), and only then does this run's own terminal
    `complete_research_run_if_claimed(requested_status=COMPLETED)` write reach the lock AT
    exactly the run's own deadline_at. The terminal write must still commit CANCELLED -- a
    deadline arriving at (or after) the same instant a cancellation already committed must never
    overwrite it with a false FAILED/deadline_exceeded."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at

    conn_cancel = _extra_connection(tmp_path)
    conn_complete = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(conn_cancel, run.id)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_complete():
        assert cancel_committed.wait(timeout=5)
        try:
            results["terminal"] = complete_research_run_if_claimed(
                conn_complete, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
                deadline_at)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_complete.close()

    assert errors == []
    assert results["cancel_requested"] is True
    terminal = results["terminal"]
    assert terminal.ok is True
    assert terminal.failure_reason is None
    assert terminal.committed_status == ResearchRunStatus.CANCELLED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.CANCELLED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] is None


def test_cancellation_committing_before_deadline_prevents_failure_without_reviving_expired_lease(
        tmp_path, conn):
    """Task A's cancellation-finalization grace, exercised as a real cross-connection race: an
    Owner's cancellation commits FIRST (forced via a `threading.Event`), and only then does a
    heartbeat on a wholly separate connection reach the run's own deadline_at. The original lease
    is expired at that exact instant, so the heartbeat must return False without reviving it, while
    also leaving the cancelled run processing for recovery to requeue into finalize-only grace."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at

    conn_cancel = _extra_connection(tmp_path)
    conn_heartbeat = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(conn_cancel, run.id)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_heartbeat():
        assert cancel_committed.wait(timeout=5)
        try:
            results["heartbeat"] = heartbeat_research_run(
                conn_heartbeat, run.id, claimed.run.claim_token, deadline_at, lease_seconds=30)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_heartbeat)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_heartbeat.close()

    assert errors == []
    assert results["cancel_requested"] is True
    assert results["heartbeat"] is False
    row = conn.execute(
        "SELECT status, claim_token, error_code, lease_expires_at FROM research_runs "
        "WHERE id = ?", (run.id,)).fetchone()
    assert row["status"] == ResearchRunStatus.PROCESSING.value
    assert row["claim_token"] == claimed.run.claim_token
    assert row["error_code"] is None
    assert datetime.fromisoformat(row["lease_expires_at"]) == deadline_at


# -- honor_cancel=True (default) extends cancellation-first precedence to an explicit FAILED --

def test_cancellation_committing_first_forces_a_stale_no_evidence_failure_to_cancel(
        tmp_path, conn):
    """The final cancellation race in `_finalize`'s no-evidence path: an Owner's cancellation
    commits FIRST (forced via a `threading.Event`, on a wholly separate connection), and only
    then does this run's own terminal `complete_research_run_if_claimed(requested_status=FAILED,
    error_code='no_evidence_collected')` write reach the lock -- exactly what `_finalize` sends
    when it has no evidence to point to, regardless of whether cancellation happened to be known
    locally. Before `honor_cancel` extended cancellation-first precedence past COMPLETED, this
    stale FAILED request would have committed FAILED with 'no_evidence_collected' even though a
    real cancellation had, by that point, already committed -- surfacing as FAILED_NO_EVIDENCE
    instead of CANCELLED_NO_EVIDENCE. It must now commit CANCELLED instead, with error_code
    cleared to None."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)

    conn_cancel = _extra_connection(tmp_path)
    conn_complete = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(conn_cancel, run.id)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_complete():
        assert cancel_committed.wait(timeout=5)
        try:
            results["terminal"] = complete_research_run_if_claimed(
                conn_complete, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED,
                T0 + timedelta(seconds=5), error_code="no_evidence_collected")
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_complete.close()

    assert errors == []
    assert results["cancel_requested"] is True
    terminal = results["terminal"]
    assert terminal.ok is True
    assert terminal.committed_status == ResearchRunStatus.CANCELLED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.CANCELLED
    raw_row = conn.execute(
        "SELECT error_code, error_detail FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] is None
    assert raw_row["error_detail"] is None


def test_cancellation_committing_first_forces_a_synthesis_failed_terminal_request_to_cancel(
        tmp_path, conn):
    """The sibling race in `_synthesize_and_terminate`'s synthesis-failed path: an Owner's
    cancellation commits FIRST (forced via a `threading.Event`, on a wholly separate connection),
    and only then does this run's own terminal
    `complete_research_run_if_claimed(requested_status=FAILED, error_code='synthesis_failed')`
    write reach the lock -- exactly what `_synthesize_and_terminate` sends when a Research
    Synthesis attempt raised. It must commit CANCELLED instead of FAILED, with error_code
    cleared to None, so a synthesis failure that races a real cancellation is never reported as
    SYNTHESIS_FAILED."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)

    conn_cancel = _extra_connection(tmp_path)
    conn_complete = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(conn_cancel, run.id)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_complete():
        assert cancel_committed.wait(timeout=5)
        try:
            results["terminal"] = complete_research_run_if_claimed(
                conn_complete, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED,
                T0 + timedelta(seconds=5), error_code="synthesis_failed")
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_complete.close()

    assert errors == []
    assert results["cancel_requested"] is True
    terminal = results["terminal"]
    assert terminal.ok is True
    assert terminal.committed_status == ResearchRunStatus.CANCELLED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.CANCELLED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] is None


def test_honor_cancel_false_keeps_an_integrity_failure_even_when_cancellation_commits_first(
        tmp_path, conn):
    """The one carve-out: a true integrity/operational failure (e.g. `_resume_sealed_run`'s
    'sealed_snapshot_missing_revision', or the research worker's 'ResearchSessionMissing') calls
    `complete_research_run_if_claimed` with `honor_cancel=False` precisely so it is NEVER hidden
    behind a cancellation that merely happened to race it -- even when that cancellation commits
    FIRST, on a wholly separate connection, exactly like the races above."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)

    conn_cancel = _extra_connection(tmp_path)
    conn_complete = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(conn_cancel, run.id)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_complete():
        assert cancel_committed.wait(timeout=5)
        try:
            results["terminal"] = complete_research_run_if_claimed(
                conn_complete, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED,
                T0 + timedelta(seconds=5), error_code="sealed_snapshot_missing_revision",
                honor_cancel=False)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_complete.close()

    assert errors == []
    assert results["cancel_requested"] is True
    terminal = results["terminal"]
    assert terminal.ok is True
    assert terminal.committed_status == ResearchRunStatus.FAILED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert raw_row["error_code"] == "sealed_snapshot_missing_revision"


# -- create_synthesis_if_claimed / admit_synthesis_if_claimed: cancellation committing mid-race -

def test_cancellation_committing_first_discards_a_racing_create_synthesis_without_failing_the_run(
        tmp_path, conn):
    """A real race standing in for `_synthesize_and_terminate`'s "cancel commits during the two
    AI calls" scenario: an Owner's cancellation commits FIRST (forced via a `threading.Event`,
    on a wholly separate connection) while a racing `create_synthesis_if_claimed` call -- standing
    in for persistence of an already-computed `claims` tuple -- is merely blocked behind it. The
    persist attempt must discard `claims` with zero database writes and report CANCEL_REQUESTED,
    but -- unlike CLAIM_LOST/DEADLINE_EXCEEDED -- must NOT fail the run: it stays exactly
    'processing', claimable by the caller's own subsequent cancellation-aware terminal write."""
    scenario = _synthesis_scenario(conn)

    conn_cancel = _extra_connection(tmp_path)
    conn_create = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(
                conn_cancel, scenario["run_id"])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_create():
        assert cancel_committed.wait(timeout=5)
        try:
            results["persisted"] = create_synthesis_if_claimed(
                conn_create, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
                scenario["revision_id"], SufficiencyState.PARTIAL, (scenario["claim"],), "gpt-5",
                "en", T0 + timedelta(seconds=5))
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_create)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_create.close()

    assert errors == []
    assert results["cancel_requested"] is True
    persisted = results["persisted"]
    assert not persisted.ok
    assert persisted.failure_reason == SynthesisPersistFailureReason.CANCEL_REQUESTED
    assert persisted.synthesis is None
    assert list_syntheses(conn, scenario["session_id"]) == []
    row = conn.execute("SELECT COUNT(*) AS n FROM research_synthesis_citations").fetchone()
    assert row["n"] == 0

    run = get_research_run(conn, scenario["run_id"])
    assert run.status == ResearchRunStatus.PROCESSING
    assert run.claim_token == scenario["claim_token"]


def test_cancellation_committing_first_makes_admission_skip_the_llm_call_without_failing_the_run(
        tmp_path, conn):
    """Task 2's own atomic gate, exercised as a real cross-connection race: an Owner's
    cancellation commits FIRST (forced via a `threading.Event`, on a wholly separate connection),
    and only then does `admit_synthesis_if_claimed` -- the check `_synthesize_and_terminate` makes
    immediately before ever starting the two LLM calls a fresh Research Synthesis needs -- reach
    the lock. This is exactly the race an orchestrator's own local, possibly-stale `cancelled`
    boolean (captured earlier, before finalization even began) cannot close on its own: admission
    must report CANCEL_REQUESTED and write nothing, leaving the run exactly 'processing' for the
    caller's own subsequent terminal write -- never ALLOWED, which would waste a full AI
    round-trip whose output persistence was always going to discard anyway."""
    scenario = _synthesis_scenario(conn)

    conn_cancel = _extra_connection(tmp_path)
    conn_admit = _extra_connection(tmp_path)
    cancel_committed = threading.Event()
    results = {}
    errors = []

    def do_cancel():
        try:
            results["cancel_requested"] = request_cancel_research_run(
                conn_cancel, scenario["run_id"])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)
        finally:
            cancel_committed.set()

    def do_admit():
        assert cancel_committed.wait(timeout=5)
        try:
            results["admission"] = admit_synthesis_if_claimed(
                conn_admit, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
                T0 + timedelta(seconds=5))
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_admit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_cancel.close()
    conn_admit.close()

    assert errors == []
    assert results["cancel_requested"] is True
    admission = results["admission"]
    assert admission.allowed is False
    assert admission.status == SynthesisAdmissionStatus.CANCEL_REQUESTED

    run = get_research_run(conn, scenario["run_id"])
    assert run.status == ResearchRunStatus.PROCESSING
    assert run.claim_token == scenario["claim_token"]


# -- Race 2: add_snapshot_items_if_claimed fences a stale worker's append after reclaim ------

def test_stale_claim_append_is_rejected_after_the_run_is_reclaimed_by_a_new_worker(
        tmp_path, conn):
    """A worker claims a run, its lease later expires, and a wholly separate connection
    (a reconciliation sweep followed by a new worker's own claim) reclaims it -- exactly like
    recover_expired_research_runs + claim_research_run already do in production. The ORIGINAL
    worker, still unaware its claim is gone (it may be paused on a GC cycle, a slow network
    call unwinding, or simply hasn't heartbeat-checked yet), then tries to append newly
    collected Evidence Items to the snapshot using its now-stale claim_token. This must be
    rejected -- CLAIM_LOST, zero rows appended -- even though the append happens on the exact
    connection that originally won the claim, proving the fence reads authoritative DB state,
    not anything cached locally. The new worker's own claim_token, by contrast, may append
    freely: the reclaim genuinely transferred ownership."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run = enqueue_research_run(conn, session_id, T0)
    old_lease = claim_research_run(conn, run.id, T0, lease_seconds=1, deadline_seconds=3600)
    old_claim_token = old_lease.run.claim_token
    snapshot_id = create_snapshot(conn, session_id, run.id, T0).id
    existing_item = upsert_evidence_item(
        conn, session_id, source_id, "existing", "T", "https://x/existing",
        EvidenceQuality.REPORTING, T0)
    add_snapshot_items(conn, snapshot_id, [existing_item.id], T0)
    # The new item the STALE worker will (wrongly) try to append after being reclaimed.
    stale_item = upsert_evidence_item(
        conn, session_id, source_id, "stale", "T", "https://x/stale",
        EvidenceQuality.REPORTING, T0)

    later = T0 + timedelta(seconds=5)  # past the 1-second lease, well before the 3600s deadline
    reconciler = _extra_connection(tmp_path)
    recovery = recover_expired_research_runs(reconciler, later)
    assert recovery.requeued_count == 1
    new_lease = claim_research_run(reconciler, run.id, later, lease_seconds=600,
                                    deadline_seconds=3600)
    assert new_lease is not None
    new_claim_token = new_lease.run.claim_token
    assert new_claim_token != old_claim_token

    # The stale worker's own connection -- the one that ORIGINALLY won the claim -- attempts
    # the append using its now-reclaimed token.
    stale_result = add_snapshot_items_if_claimed(
        conn, run.id, old_claim_token, session_id, snapshot_id, [stale_item.id], later,
        now_fn=lambda: later)
    assert not stale_result.ok
    assert stale_result.failure_reason == SnapshotAppendFailureReason.CLAIM_LOST
    assert list_snapshot_item_ids(conn, snapshot_id) == [existing_item.id]

    # The new worker's own claim_token, on the connection that actually won the reclaim, may
    # append freely -- reclaim genuinely transferred ownership.
    new_result = add_snapshot_items_if_claimed(
        reconciler, run.id, new_claim_token, session_id, snapshot_id, [stale_item.id], later,
        now_fn=lambda: later)
    assert new_result.ok
    assert set(list_snapshot_item_ids(conn, snapshot_id)) == {existing_item.id, stale_item.id}
    reconciler.close()


# -- Race 4: get_or_create_snapshot_if_claimed fences a stale worker's create after reclaim --

def test_stale_claim_snapshot_get_or_create_is_rejected_after_reclaim(tmp_path, conn):
    """Task D, the exact race `get_or_create_snapshot_if_claimed` closes: an old worker's
    claim_token pauses (a GC cycle, a slow network call unwinding, or simply hasn't heartbeat-
    checked yet) before it ever calls get-or-create; meanwhile a wholly separate connection
    (a reconciliation sweep followed by a new worker's own claim) reclaims the run -- exactly
    like recover_expired_research_runs + claim_research_run already do in production. The new
    (current) owner's own get-or-create call creates the run's ONE Evidence Snapshot; the old
    worker's later, now-stale call must be rejected outright (CLAIM_LOST) -- never racing to
    create a second snapshot, and never receiving a spurious ValueError from losing that low-
    level race the way the old check-then-create `_resolve_snapshot` production path could."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    old_lease = claim_research_run(conn, run.id, T0, lease_seconds=1, deadline_seconds=3600)
    old_claim_token = old_lease.run.claim_token

    later = T0 + timedelta(seconds=5)  # past the 1-second lease, well before the 3600s deadline
    reconciler = _extra_connection(tmp_path)
    recovery = recover_expired_research_runs(reconciler, later)
    assert recovery.requeued_count == 1
    new_lease = claim_research_run(
        reconciler, run.id, later, lease_seconds=600, deadline_seconds=3600)
    assert new_lease is not None
    new_claim_token = new_lease.run.claim_token
    assert new_claim_token != old_claim_token

    # The new (current) owner resolves -- creates -- the run's one snapshot.
    new_result = get_or_create_snapshot_if_claimed(
        reconciler, run.id, new_claim_token, session_id, later, now_fn=lambda: later)
    assert new_result.ok
    assert new_result.snapshot.status == EvidenceSnapshotStatus.BUILDING

    # The stale worker's own connection -- the one that ORIGINALLY won the claim -- attempts the
    # same resolution using its now-reclaimed token.
    stale_result = get_or_create_snapshot_if_claimed(
        conn, run.id, old_claim_token, session_id, later, now_fn=lambda: later)
    assert not stale_result.ok
    assert stale_result.failure_reason == SnapshotClaimFailureReason.CLAIM_LOST
    assert stale_result.snapshot is None

    # Exactly one snapshot exists, and the current run proceeds under the new owner's claim.
    assert len(list_snapshots(conn, session_id)) == 1
    assert list_snapshots(conn, session_id)[0].id == new_result.snapshot.id
    assert get_research_run(conn, run.id).claim_token == new_claim_token
    reconciler.close()


def test_stale_worker_attempting_first_never_makes_the_current_owner_see_a_valueerror(
        tmp_path, conn):
    """The exact shape of the bug Task D fixes, with the two calls in the OPPOSITE order from
    the test above: the stale (old-token) worker happens to call get-or-create FIRST -- exactly
    the ordering under which the old check-then-create `_resolve_snapshot` production path could
    let a stale worker's own `create_snapshot` call win the row and make the genuinely CURRENT
    owner's later call raise a spurious ValueError purely from losing that low-level race. Here,
    the stale call must still be rejected outright (CLAIM_LOST, creating nothing), and the
    current owner's later call must cleanly create the run's one snapshot -- never a ValueError,
    regardless of which side happens to call get-or-create first."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    old_lease = claim_research_run(conn, run.id, T0, lease_seconds=1, deadline_seconds=3600)
    old_claim_token = old_lease.run.claim_token

    later = T0 + timedelta(seconds=5)
    reconciler = _extra_connection(tmp_path)
    recovery = recover_expired_research_runs(reconciler, later)
    assert recovery.requeued_count == 1
    new_lease = claim_research_run(
        reconciler, run.id, later, lease_seconds=600, deadline_seconds=3600)
    assert new_lease is not None
    new_claim_token = new_lease.run.claim_token

    # The stale worker's call happens FIRST this time -- it must still create nothing at all.
    stale_result = get_or_create_snapshot_if_claimed(
        conn, run.id, old_claim_token, session_id, later, now_fn=lambda: later)
    assert not stale_result.ok
    assert stale_result.failure_reason == SnapshotClaimFailureReason.CLAIM_LOST
    assert stale_result.snapshot is None
    assert list_snapshots(conn, session_id) == []

    # The current owner's call, running second, must cleanly create the one snapshot -- never a
    # ValueError from a stale worker having "won" any row-creation race.
    new_result = get_or_create_snapshot_if_claimed(
        reconciler, run.id, new_claim_token, session_id, later, now_fn=lambda: later)
    assert new_result.ok
    assert new_result.snapshot.status == EvidenceSnapshotStatus.BUILDING
    assert len(list_snapshots(conn, session_id)) == 1
    reconciler.close()


def test_membership_changing_between_precomputation_and_finalize_writes_nothing(
        tmp_path, conn):
    """A real race between a still-active append (`add_snapshot_items_if_claimed`, simulating
    any legitimately concurrent membership change -- not necessarily a stale worker) and
    finalize_snapshot_if_claimed attempting to finalize against membership it already read and
    computed clusters/curation from moments earlier. Whichever transaction wins the write lock,
    the observable outcome must never be a sealed snapshot whose clusters/revision disagree
    with its actual DB membership:

    * If the append wins first, the snapshot's true membership has moved on from what
      finalize's caller precomputed (`expected_snapshot_item_ids`) -- finalize must fail closed
      with MEMBERSHIP_CHANGED, writing no cluster, no seal, no revision, and the run remains
      'processing' (a membership change alone never fails the run).
    * If finalize wins first, it seals the snapshot against the membership it legitimately read
      -- and the now-stale append, finding the snapshot no longer 'building', must itself fail
      closed (CLAIM_LOST) rather than appending to an immutable sealed snapshot."""
    scenario = _finalization_scenario(conn)
    conn_append = _extra_connection(tmp_path)
    well_before_deadline = scenario["deadline_at"] - timedelta(seconds=30)
    source_id = create_research_source(
        conn, scenario["session_id"], "web_search_2", {}, ResearchSourceOrigin.OWNER, T0).id
    extra_item = upsert_evidence_item(
        conn, scenario["session_id"], source_id, "extra", "T-extra", "https://x/extra",
        EvidenceQuality.REPORTING, T0)

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def run_finalize():
        barrier.wait(timeout=5)
        try:
            results["finalize"] = finalize_snapshot_if_claimed(
                conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
                scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
                [scenario["a"], scenario["b"]], well_before_deadline,
                expected_snapshot_item_ids=[scenario["a"], scenario["b"]])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    def run_append():
        barrier.wait(timeout=5)
        try:
            results["append"] = add_snapshot_items_if_claimed(
                conn_append, scenario["run_id"], scenario["claim_token"],
                scenario["session_id"], scenario["snapshot_id"], [extra_item.id],
                well_before_deadline)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run_finalize), threading.Thread(target=run_append)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_append.close()

    assert errors == []
    finalize_result = results["finalize"]
    append_result = results["append"]
    # Exactly one side can have won -- the other must have failed closed.
    assert finalize_result.ok != append_result.ok

    final_snapshot = get_snapshot(conn, scenario["snapshot_id"])
    final_membership = set(list_snapshot_item_ids(conn, scenario["snapshot_id"]))

    if finalize_result.ok:
        assert final_snapshot.status == EvidenceSnapshotStatus.SEALED
        assert final_membership == {scenario["a"], scenario["b"]}
        assert set(finalize_result.revision.evidence_item_ids) == {
            scenario["a"], scenario["b"]}
        assert not append_result.ok
        assert append_result.failure_reason == SnapshotAppendFailureReason.CLAIM_LOST
    else:
        assert final_snapshot.status == EvidenceSnapshotStatus.BUILDING
        assert final_membership == {scenario["a"], scenario["b"], extra_item.id}
        assert finalize_result.failure_reason == FinalizationFailureReason.MEMBERSHIP_CHANGED
        assert finalize_result.snapshot is None
        assert finalize_result.clusters == ()
        assert finalize_result.revision is None
        assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
        assert list_evidence_state_revisions(conn, scenario["session_id"]) == []
        # A membership change alone never fails the run -- unlike CLAIM_LOST/DEADLINE_EXCEEDED,
        # the claim itself remains perfectly live and could retry finalization.
        assert get_research_run(conn, scenario["run_id"]).status == ResearchRunStatus.PROCESSING
        assert append_result.ok


# -- Race 3: a curation mutation committing first must never be pinned into a stale revision --

def test_curation_committing_first_forces_finalize_to_reject_stale_active_ids(tmp_path, conn):
    """Task A -- the curation race at finalization: a real race between a curation mutation
    (`db.evidence_curation.set_evidence_curation`, committing on a wholly separate connection --
    e.g. the Owner excluding an item through the web UI) and `finalize_snapshot_if_claimed`
    attempting to finalize using cluster_groups/active_evidence_item_ids precomputed (curation-
    filtered) moments earlier, before either side reaches finalize's own lock. Whichever
    transaction wins the write lock, the observable outcome must never pin a revision whose
    active membership disagrees with `research_evidence_curation`'s own current state:

    * If curation commits FIRST, finalize's own lock re-reads the authoritative active set fresh
      and finds it no longer matches the caller's stale precomputation (b is now excluded, but
      the precomputed active ids still include it) -- finalize must fail closed with
      MEMBERSHIP_CHANGED, writing no cluster, no seal, no revision, and the run remains
      'processing' (curation changing is never treated as a claim loss).
    * If finalize wins the lock FIRST, its revision is valid for the instant it committed (b was
      not yet excluded) -- the curation mutation still commits once finalize releases the lock,
      it simply cannot retroactively change the now-sealed snapshot's already-pinned revision;
      a future call to `research.synthesis._rebuild_evidence_state_revision` is what builds the
      NEXT, newer revision honoring it, exactly as designed."""
    scenario = _finalization_scenario(conn)
    conn_curation = _extra_connection(tmp_path)
    well_before_deadline = scenario["deadline_at"] - timedelta(seconds=30)

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def run_finalize():
        barrier.wait(timeout=5)
        try:
            results["finalize"] = finalize_snapshot_if_claimed(
                conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
                scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
                [scenario["a"], scenario["b"]], well_before_deadline,
                expected_snapshot_item_ids=[scenario["a"], scenario["b"]])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    def run_curation():
        barrier.wait(timeout=5)
        try:
            set_evidence_curation(
                conn_curation, scenario["b"], True, "excluded mid-race", well_before_deadline)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run_finalize), threading.Thread(target=run_curation)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    conn_curation.close()

    assert errors == []
    finalize_result = results["finalize"]
    # The curation mutation itself is never fenced against anything -- it always commits,
    # regardless of which side won the write lock race.
    curation_row = get_evidence_curation(conn, scenario["b"])
    assert curation_row is not None and curation_row.is_excluded

    final_snapshot = get_snapshot(conn, scenario["snapshot_id"])

    if finalize_result.ok:
        assert final_snapshot.status == EvidenceSnapshotStatus.SEALED
        assert set(finalize_result.revision.evidence_item_ids) == {
            scenario["a"], scenario["b"]}
        assert len(list_evidence_state_revisions(conn, scenario["session_id"])) == 1
    else:
        assert finalize_result.failure_reason == FinalizationFailureReason.MEMBERSHIP_CHANGED
        assert finalize_result.snapshot is None
        assert finalize_result.clusters == ()
        assert finalize_result.revision is None
        assert final_snapshot.status == EvidenceSnapshotStatus.BUILDING
        assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
        assert list_evidence_state_revisions(conn, scenario["session_id"]) == []
        # A curation change alone never fails the run -- the claim itself remains perfectly
        # live and could reread curation and retry finalization.
        assert get_research_run(conn, scenario["run_id"]).status == ResearchRunStatus.PROCESSING


def test_concurrent_exclude_restore_latest_revision_matches_committed_curation(tmp_path, conn):
    """A curation change and the revision derived from it must be one serialized transition.

    The trace callback blocks the exclude connection at its first explicit write-lock request.
    In the old split implementation, that point is after exclusion committed and after the stale
    active ids were computed, but before their revision insert. Restore can therefore commit an
    included revision first, after which exclude inserts a newer stale excluded revision. In the
    atomic implementation, the same block happens before exclude mutates anything, so whichever
    transition commits last also owns the latest revision.
    """
    scenario = _finalization_scenario(conn)
    finalized = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"]], T0,
        expected_snapshot_item_ids=[scenario["a"], scenario["b"]])
    assert finalized.ok

    conn_exclude = _extra_connection(tmp_path)
    conn_restore = _extra_connection(tmp_path)
    reached_write_lock = threading.Event()
    release_write_lock = threading.Event()
    errors = []

    def pause_first_write_lock(sql):
        if sql.strip().upper() == "BEGIN IMMEDIATE" and not reached_write_lock.is_set():
            reached_write_lock.set()
            if not release_write_lock.wait(timeout=5):
                raise TimeoutError("exclude write lock was not released")

    conn_exclude.set_trace_callback(pause_first_write_lock)

    def run_exclude():
        try:
            synth.exclude_evidence_item(
                conn_exclude, scenario["session_id"], scenario["a"], T0,
                note="exclude racing restore")
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    thread = threading.Thread(target=run_exclude)
    thread.start()
    assert reached_write_lock.wait(timeout=5)

    synth.restore_evidence_item(
        conn_restore, scenario["session_id"], scenario["a"], T0 + timedelta(seconds=1))
    release_write_lock.set()
    thread.join(timeout=5)
    conn_exclude.close()
    conn_restore.close()

    assert errors == []
    assert not thread.is_alive()
    curation = get_evidence_curation(conn, scenario["a"])
    latest = get_latest_evidence_state_revision(conn, scenario["session_id"])
    assert curation is not None
    assert latest is not None
    assert (scenario["a"] not in latest.evidence_item_ids) == curation.is_excluded


def test_curation_cannot_publish_stale_snapshot_after_newer_snapshot_finalizes(tmp_path, conn):
    """A curation revision must target the latest sealed snapshot at its commit point.

    The first run establishes sealed S1. The second run builds cumulative S2. The curation
    connection is then paused at its first explicit write-lock request while S2 finalizes. A split
    read-then-create implementation can append a newer revision for stale S1 after S2 and its
    atomic revision already committed. A single curation transaction must instead serialize
    before or after finalization and always leave the newest revision pinned to S2.
    """
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    item_a = upsert_evidence_item(
        conn, session_id, source_id, "a", "A", "https://x/a",
        EvidenceQuality.REPORTING, T0)

    run1 = enqueue_research_run(conn, session_id, T0)
    lease1 = claim_research_run(
        conn, run1.id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot1 = create_snapshot(conn, session_id, run1.id, T0)
    add_snapshot_items(conn, snapshot1.id, [item_a.id], T0)
    finalized1 = finalize_snapshot_if_claimed(
        conn, run1.id, lease1.run.claim_token, session_id, snapshot1.id, [],
        [item_a.id], T0, expected_snapshot_item_ids=[item_a.id])
    assert finalized1.ok
    complete_research_run(
        conn, run1.id, lease1.run.claim_token, ResearchRunStatus.COMPLETED, T0)

    item_b = upsert_evidence_item(
        conn, session_id, source_id, "b", "B", "https://x/b",
        EvidenceQuality.REPORTING, T0)
    run2 = enqueue_research_run(conn, session_id, T0 + timedelta(seconds=1))
    lease2 = claim_research_run(
        conn, run2.id, T0 + timedelta(seconds=1), lease_seconds=600, deadline_seconds=3600)
    snapshot2 = create_snapshot(conn, session_id, run2.id, T0 + timedelta(seconds=1))
    add_snapshot_items(
        conn, snapshot2.id, [item_a.id, item_b.id], T0 + timedelta(seconds=1))

    conn_curation = _extra_connection(tmp_path)
    reached_write_lock = threading.Event()
    release_write_lock = threading.Event()
    errors = []

    def pause_first_write_lock(sql):
        if sql.strip().upper() == "BEGIN IMMEDIATE" and not reached_write_lock.is_set():
            reached_write_lock.set()
            if not release_write_lock.wait(timeout=5):
                raise TimeoutError("curation write lock was not released")

    conn_curation.set_trace_callback(pause_first_write_lock)

    def run_exclude():
        try:
            synth.exclude_evidence_item(
                conn_curation, session_id, item_a.id, T0 + timedelta(seconds=2),
                note="exclude while S2 finalizes")
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    thread = threading.Thread(target=run_exclude)
    thread.start()
    assert reached_write_lock.wait(timeout=5)

    active_ids = (
        [item_b.id]
        if get_evidence_curation(conn, item_a.id) is not None
        and get_evidence_curation(conn, item_a.id).is_excluded
        else [item_a.id, item_b.id])
    finalized2 = finalize_snapshot_if_claimed(
        conn, run2.id, lease2.run.claim_token, session_id, snapshot2.id, [],
        active_ids, T0 + timedelta(seconds=2),
        expected_snapshot_item_ids=[item_a.id, item_b.id])
    assert finalized2.ok

    release_write_lock.set()
    thread.join(timeout=5)
    conn_curation.close()

    assert errors == []
    assert not thread.is_alive()
    latest = get_latest_evidence_state_revision(conn, session_id)
    curation = get_evidence_curation(conn, item_a.id)
    assert latest is not None
    assert latest.snapshot_id == snapshot2.id
    assert curation is not None
    assert (item_a.id not in latest.evidence_item_ids) == curation.is_excluded
