from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.research_runs import (TerminalCompletionFailureReason,
                                       advance_research_run_phase, claim_research_run,
                                       complete_research_run, complete_research_run_if_claimed,
                                       count_active_processing_runs, enqueue_research_run,
                                       get_research_run, heartbeat_research_run,
                                       list_pending_research_runs, list_research_runs,
                                       recover_expired_research_runs,
                                       request_cancel_research_run, requeue_research_run,
                                       reserve_deep_fetch)
from beehive.db.research_sessions import archive_research_session, create_research_session
from beehive.domain.research import ResearchRunPhase, ResearchRunStatus

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def session_id(conn):
    return create_research_session(conn, "Q", T0).id


# -- enqueue / claim --------------------------------------------------------------------

def test_enqueue_research_run_creates_pending_row(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    assert run.status == ResearchRunStatus.PENDING
    assert run.phase is None
    assert run.claim_token is None
    assert run.requested_at == T0


def test_enqueue_research_run_rejects_archived_session(conn, session_id):
    archive_research_session(conn, session_id, T0)
    with pytest.raises(ValueError, match="non-active Research Session"):
        enqueue_research_run(conn, session_id, T0)
    assert list_research_runs(conn, session_id) == []


def test_enqueue_research_run_rejects_nonexistent_session(conn):
    with pytest.raises(ValueError, match="non-active Research Session"):
        enqueue_research_run(conn, 999, T0)


def test_enqueue_research_run_rejects_a_second_pending_run(conn, session_id):
    enqueue_research_run(conn, session_id, T0)
    with pytest.raises(ValueError, match="already has an active Research Run"):
        enqueue_research_run(conn, session_id, T0)
    assert len(list_research_runs(conn, session_id)) == 1


def test_enqueue_research_run_rejects_a_second_run_while_processing(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    with pytest.raises(ValueError, match="already has an active Research Run"):
        enqueue_research_run(conn, session_id, T0)
    assert len(list_research_runs(conn, session_id)) == 1


@pytest.mark.parametrize("status", [
    ResearchRunStatus.COMPLETED, ResearchRunStatus.CANCELLED, ResearchRunStatus.FAILED,
])
def test_enqueue_research_run_allows_refresh_once_prior_run_is_terminal(
        conn, session_id, status):
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    complete_research_run(conn, run.id, lease.run.claim_token, status, T0)
    second = enqueue_research_run(conn, session_id, T0 + timedelta(minutes=1))
    assert second.id != run.id
    assert second.status == ResearchRunStatus.PENDING
    assert len(list_research_runs(conn, session_id)) == 2


def test_partial_unique_index_rejects_a_second_active_run_inserted_directly(conn, session_id):
    """Defense in depth: even bypassing enqueue_research_run's own application-level check, a
    second pending/processing row for the same session must be rejected by the database
    itself."""
    import sqlite3

    enqueue_research_run(conn, session_id, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_runs (session_id, status, requested_at) "
            "VALUES (?, 'pending', ?)", (session_id, T0.isoformat()))


def test_claim_research_run_sets_phase_lease_started_and_deadline(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert claimed.run.status == ResearchRunStatus.PROCESSING
    assert claimed.run.phase == ResearchRunPhase.PLANNING
    assert claimed.run.claim_token is not None
    assert claimed.run.started_at == T0
    assert claimed.run.deadline_at == T0 + timedelta(seconds=3600)
    assert claimed.lease_expires_at == (T0 + timedelta(seconds=60)).isoformat()
    assert claimed.attempt_count == 0


def test_claim_research_run_fails_if_already_claimed(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    second = claim_research_run(
        conn, run.id, T0 + timedelta(seconds=1), lease_seconds=60, deadline_seconds=3600)
    assert second is None


def test_claim_research_run_fails_for_missing_run(conn):
    assert claim_research_run(conn, 999, T0, lease_seconds=60, deadline_seconds=3600) is None


def test_claim_research_run_fails_and_terminates_a_pending_run_exactly_at_its_deadline(
        conn, session_id):
    """A requeued run keeps its original fixed deadline_at (recover_expired_research_runs never
    resets it). If nobody reclaims it before that deadline arrives, claim_research_run must not
    grant a fresh lease at all -- `now == deadline_at` is treated as "arrived", exactly like
    heartbeat_research_run's own `now >= deadline_at` fencing -- it must instead atomically fail
    the run outright (error_code='deadline_exceeded') and return None, never a zero-length or
    already-expired lease."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at

    # lease expires, deadline does not -- requeues, keeping the same fixed deadline_at.
    recover_result = recover_expired_research_runs(conn, T0 + timedelta(seconds=40))
    assert recover_result.requeued_count == 1
    requeued = get_research_run(conn, run.id)
    assert requeued.status == ResearchRunStatus.PENDING
    assert requeued.deadline_at == deadline_at

    # Exactly at the deadline instant: claim must fail, never succeed with a zero-length lease.
    result = claim_research_run(conn, run.id, deadline_at, lease_seconds=60, deadline_seconds=3600)
    assert result is None

    terminal = get_research_run(conn, run.id)
    assert terminal.status == ResearchRunStatus.FAILED
    assert terminal.phase is None
    assert terminal.claim_token is None
    assert terminal.completed_at == deadline_at


def test_claim_research_run_still_succeeds_a_pending_run_one_instant_before_its_deadline(
        conn, session_id):
    """Defense against an off-by-one in the other direction: a pending run whose deadline has
    NOT yet strictly arrived must still be claimable, with a lease that is never zero-length."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at
    recover_expired_research_runs(conn, T0 + timedelta(seconds=40))

    just_before = deadline_at - timedelta(microseconds=1)
    result = claim_research_run(
        conn, run.id, just_before, lease_seconds=60, deadline_seconds=3600)
    assert result is not None
    assert result.run.status == ResearchRunStatus.PROCESSING
    assert result.lease_expires_at == deadline_at.isoformat()  # capped, never past the deadline
    assert just_before < deadline_at


# -- Task A: claim_research_run's cancellation-finalization grace for a past-deadline reclaim --

def test_claim_research_run_reclaims_a_cancelled_pending_run_past_its_deadline(
        conn, session_id):
    """A pending, cancel-requested run whose persisted deadline_at has already arrived (e.g.
    requeued by recover_expired_research_runs' own matching grace) must still be reclaimable --
    with a normal, forward-looking bounded lease based on `now`, never a zero-length or
    already-expired one -- so the new claim holder gets a genuine window to run orchestration
    straight into `_finalize` and terminalize CANCELLED. deadline_at itself stays fixed."""
    run = enqueue_research_run(conn, session_id, T0)
    first = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    deadline_at = first.run.deadline_at
    assert request_cancel_research_run(conn, run.id) is True
    # lease expires AND deadline has also passed -- recover_expired_research_runs' own
    # cancellation grace requeues rather than fails (exercised directly in its own test module;
    # here the pending row is put in that exact shape to isolate claim_research_run's behavior).
    conn.execute(
        "UPDATE research_runs SET status = 'pending', phase = NULL, claim_token = NULL, "
        "lease_expires_at = NULL WHERE id = ?", (run.id,))
    conn.commit()

    past_deadline = deadline_at + timedelta(seconds=5)
    result = claim_research_run(
        conn, run.id, past_deadline, lease_seconds=30, deadline_seconds=3600)

    assert result is not None
    assert result.run.status == ResearchRunStatus.PROCESSING
    assert result.run.deadline_at == deadline_at  # never reset -- not a fresh work budget
    assert result.lease_expires_at == (past_deadline + timedelta(seconds=30)).isoformat()
    assert datetime.fromisoformat(result.lease_expires_at) > deadline_at


def test_claim_research_run_without_cancellation_still_fails_a_pending_run_past_its_deadline(
        conn, session_id):
    """Coherence check: the grace above is strictly gated on cancel_requested -- an otherwise
    identical, non-cancelled pending run past its deadline is unaffected (already covered in
    detail by the exact-equality test above; this pins the strictly-past-deadline case too)."""
    run = enqueue_research_run(conn, session_id, T0)
    first = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    deadline_at = first.run.deadline_at
    conn.execute(
        "UPDATE research_runs SET status = 'pending', phase = NULL, claim_token = NULL, "
        "lease_expires_at = NULL WHERE id = ?", (run.id,))
    conn.commit()

    past_deadline = deadline_at + timedelta(seconds=5)
    result = claim_research_run(
        conn, run.id, past_deadline, lease_seconds=30, deadline_seconds=3600)

    assert result is None
    terminal = get_research_run(conn, run.id)
    assert terminal.status == ResearchRunStatus.FAILED
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "deadline_exceeded"


def test_global_cap_refuses_a_fourth_concurrent_claim(conn, session_id):
    # One run per session: the global three-processing cap is fleet-wide, not per-session, and
    # each session may only ever have one active run at a time.
    session_ids = [session_id] + [create_research_session(conn, f"Q{i}", T0).id for i in range(3)]
    runs = [enqueue_research_run(conn, sid, T0) for sid in session_ids]
    claims = [
        claim_research_run(conn, r.id, T0, lease_seconds=60, deadline_seconds=3600)
        for r in runs
    ]
    assert claims[0] is not None
    assert claims[1] is not None
    assert claims[2] is not None
    assert claims[3] is None  # the fourth is refused: three are already processing
    assert count_active_processing_runs(conn, T0) == 3
    # the refused run is untouched and still claimable once one of the first three finishes
    still_pending = get_research_run(conn, runs[3].id)
    assert still_pending.status == ResearchRunStatus.PENDING


def test_global_cap_ignores_runs_with_expired_leases(conn, session_id):
    session_ids = [session_id] + [create_research_session(conn, f"Q{i}", T0).id for i in range(3)]
    runs = [enqueue_research_run(conn, sid, T0) for sid in session_ids]
    for r in runs[:3]:
        claim_research_run(conn, r.id, T0, lease_seconds=10, deadline_seconds=3600)
    later = T0 + timedelta(seconds=20)  # all three leases now expired
    claimed = claim_research_run(conn, runs[3].id, later, lease_seconds=60, deadline_seconds=3600)
    assert claimed is not None


def test_list_pending_research_runs_orders_oldest_first(conn, session_id):
    other_session = create_research_session(conn, "Other", T0).id
    first = enqueue_research_run(conn, session_id, T0)
    second = enqueue_research_run(conn, other_session, T0 + timedelta(minutes=1))
    pending = list_pending_research_runs(conn)
    assert [r.id for r in pending] == [first.id, second.id]


def test_list_pending_research_runs_excludes_processing(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert list_pending_research_runs(conn) == []


def test_list_research_runs_scoped_to_session(conn, session_id):
    other_session = create_research_session(conn, "Other", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    enqueue_research_run(conn, other_session, T0)
    assert [r.id for r in list_research_runs(conn, session_id)] == [run.id]


# -- heartbeat / phase / deep-fetch reservations -----------------------------------------

def test_heartbeat_extends_lease_only_for_matching_claim(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)

    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, T0 + timedelta(seconds=30), lease_seconds=60)
    assert ok is True

    wrong_token = heartbeat_research_run(
        conn, run.id, "not-the-real-token", T0 + timedelta(seconds=30), lease_seconds=60)
    assert wrong_token is False


def test_heartbeat_uses_now_fn_over_pre_sampled_now(conn, session_id):
    """`now_fn`, when given, is authoritative: a heartbeat whose positional `now` looks well
    within budget must still fail the run once `now_fn()` reveals the deadline has arrived."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, T0, lease_seconds=60,
        now_fn=lambda: past_deadline)

    assert ok is False
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "deadline_exceeded"


# -- Task B: an expired lease can never be revived by a late heartbeat -------------------

def test_heartbeat_never_revives_a_lease_that_expired_independently_of_the_deadline(
        conn, session_id):
    """A heartbeat call that itself arrives late enough for its OWN short lease to have already
    elapsed -- while the run's much longer fixed deadline_at is still comfortably in the future
    -- must refuse to renew: it returns False and writes nothing at all, leaving
    recover_expired_research_runs to decide requeue vs. fail. This is the fix for the "A expires,
    D claims the freed slot, a delayed heartbeat for A revives it" race: count_active_processing_
    runs already excludes this row (lease_expires_at <= now) before any sweep ever touches it, so
    a naive renewal here could push the fleet past the three-run cap."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=10, deadline_seconds=3600)
    lease_expired_but_deadline_far_off = T0 + timedelta(seconds=20)

    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, lease_expired_but_deadline_far_off,
        lease_seconds=60)

    assert ok is False
    # Nothing was written: the run remains 'processing' with its ORIGINAL (still-expired)
    # lease_expires_at untouched -- never revived, and never itself failed/requeued by this call
    # (that transition belongs solely to recover_expired_research_runs).
    row = conn.execute(
        "SELECT status, lease_expires_at, claim_token FROM research_runs WHERE id = ?",
        (run.id,)).fetchone()
    assert row["status"] == ResearchRunStatus.PROCESSING.value
    assert row["claim_token"] == claimed.run.claim_token
    assert row["lease_expires_at"] == claimed.lease_expires_at


def test_heartbeat_still_renews_a_lease_that_has_not_yet_expired(conn, session_id):
    """Sanity check paired with the test above: a heartbeat that arrives comfortably within its
    own still-live lease renews normally -- Task B's fix must never reject an ordinary, on-time
    heartbeat."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=10, deadline_seconds=3600)

    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, T0 + timedelta(seconds=5), lease_seconds=10)

    assert ok is True
    row = conn.execute(
        "SELECT lease_expires_at FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["lease_expires_at"] == (T0 + timedelta(seconds=15)).isoformat()


# -- Task A: cancellation-finalization grace at the heartbeat's own deadline watchdog ----

def test_heartbeat_grants_a_finalize_only_grace_lease_past_deadline_when_cancelled(
        conn, session_id):
    """A live, unexpired, cancel-requested claim that reaches its own fixed deadline_at must NOT
    be hard-terminated by heartbeat_research_run the way a non-cancelled claim would be -- it
    instead renews the lease PAST deadline_at (a short, finalize-only grace), returns True, and
    leaves the run itself untouched (still 'processing', deadline_at unchanged) so orchestration
    can finish sealing already-collected evidence and terminalize CANCELLED on its own."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    assert request_cancel_research_run(conn, run.id) is True
    at_deadline = claimed.run.deadline_at

    # Observe cancellation while the original lease is still live, so the grace is established
    # before the deadline-capped lease expires.
    before_deadline = at_deadline - timedelta(seconds=10)
    assert heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, before_deadline, lease_seconds=30) is True

    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, at_deadline, lease_seconds=30)

    assert ok is True
    row = conn.execute(
        "SELECT status, deadline_at, lease_expires_at, claim_token, error_code "
        "FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["status"] == ResearchRunStatus.PROCESSING.value
    assert row["claim_token"] == claimed.run.claim_token
    assert row["error_code"] is None
    assert row["deadline_at"] == at_deadline.isoformat()  # never reset -- not a new work budget
    assert datetime.fromisoformat(row["lease_expires_at"]) == at_deadline + timedelta(seconds=30)
    assert datetime.fromisoformat(row["lease_expires_at"]) > at_deadline


def test_heartbeat_grace_lease_is_uncapped_well_past_the_deadline_too(conn, session_id):
    """The grace extension is computed from `now`, not clamped back down to deadline_at -- a
    cancelled claim discovered well after its own deadline (e.g. a slow reconciliation cadence)
    still gets a fresh, forward-looking finalize-only window rather than an already-expired one."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=60)
    assert request_cancel_research_run(conn, run.id) is True
    well_past_deadline = claimed.run.deadline_at + timedelta(seconds=45)

    assert heartbeat_research_run(
        conn, run.id, claimed.run.claim_token,
        claimed.run.deadline_at - timedelta(seconds=10), lease_seconds=120) is True
    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, well_past_deadline, lease_seconds=30)

    assert ok is True
    row = conn.execute(
        "SELECT lease_expires_at FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert (datetime.fromisoformat(row["lease_expires_at"])
            == well_past_deadline + timedelta(seconds=30))


def test_heartbeat_without_cancellation_still_hard_terminates_at_the_deadline(conn, session_id):
    """Coherence check: the grace above is strictly gated on cancel_requested -- an otherwise
    identical, non-cancelled claim reaching its deadline is unaffected and still hard-terminated
    exactly as before."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    at_deadline = claimed.run.deadline_at

    ok = heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, at_deadline, lease_seconds=30)

    assert ok is False
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    assert final.claim_token is None
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "deadline_exceeded"


def test_advance_research_run_phase_is_fenced_by_claim_token(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)

    ok = advance_research_run_phase(
        conn, run.id, claimed.run.claim_token, ResearchRunPhase.COLLECTING)
    assert ok is True
    assert get_research_run(conn, run.id).phase == ResearchRunPhase.COLLECTING

    stale = advance_research_run_phase(conn, run.id, "wrong-token", ResearchRunPhase.ENRICHING)
    assert stale is False
    assert get_research_run(conn, run.id).phase == ResearchRunPhase.COLLECTING


def test_requeue_research_run_gives_back_a_still_valid_claim(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)

    ok = requeue_research_run(conn, run.id, claimed.run.claim_token)
    assert ok is True
    requeued = get_research_run(conn, run.id)
    assert requeued.status == ResearchRunStatus.PENDING
    assert requeued.phase is None
    assert requeued.claim_token is None
    # Requeuing is a continuation of the same attempt/budget, not a fresh one: started_at and
    # deadline_at (and attempt_count) are left exactly as claim_research_run first set them.
    assert requeued.started_at == claimed.run.started_at
    assert requeued.deadline_at == claimed.run.deadline_at


def test_requeue_research_run_rejects_a_stale_or_wrong_claim_token(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)

    ok = requeue_research_run(conn, run.id, "wrong-token")
    assert ok is False
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PROCESSING


def test_requeue_research_run_rejects_an_already_terminal_run(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)
    complete_research_run(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, T0)

    ok = requeue_research_run(conn, run.id, claimed.run.claim_token)
    assert ok is False
    assert get_research_run(conn, run.id).status == ResearchRunStatus.COMPLETED


def test_reserve_deep_fetch_accumulates_and_caps_at_30(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)

    assert reserve_deep_fetch(conn, run.id, claimed.run.claim_token, count=20) is True
    assert get_research_run(conn, run.id).deep_fetch_count == 20

    assert reserve_deep_fetch(conn, run.id, claimed.run.claim_token, count=10) is True
    assert get_research_run(conn, run.id).deep_fetch_count == 30

    # the 31st reservation must be refused, not truncated
    assert reserve_deep_fetch(conn, run.id, claimed.run.claim_token, count=1) is False
    assert get_research_run(conn, run.id).deep_fetch_count == 30


def test_reserve_deep_fetch_rejects_wrong_claim_token(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert reserve_deep_fetch(conn, run.id, "wrong-token", count=1) is False


def test_reserve_deep_fetch_requires_positive_count(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    with pytest.raises(ValueError):
        reserve_deep_fetch(conn, run.id, claimed.run.claim_token, count=0)


# -- cancel / complete --------------------------------------------------------------------

def test_request_cancel_sets_flag_and_rejects_terminal_runs(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    assert request_cancel_research_run(conn, run.id) is True
    assert get_research_run(conn, run.id).cancel_requested is True

    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    complete_research_run(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, T0)
    assert request_cancel_research_run(conn, run.id) is False


def test_complete_research_run_clears_lease_and_phase(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)

    ok = complete_research_run(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
        T0 + timedelta(minutes=1))
    assert ok is True
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.COMPLETED
    assert final.phase is None
    assert final.claim_token is None
    assert final.completed_at == T0 + timedelta(minutes=1)


def test_complete_research_run_rejects_stale_claim(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    ok = complete_research_run(
        conn, run.id, "wrong-token", ResearchRunStatus.COMPLETED, T0)
    assert ok is False
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PROCESSING


def test_complete_research_run_rejects_non_terminal_status(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    with pytest.raises(ValueError, match="must be terminal"):
        complete_research_run(
            conn, run.id, claimed.run.claim_token, ResearchRunStatus.PENDING, T0)


def test_complete_research_run_stores_error_details_on_failure(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    complete_research_run(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED, T0,
        error_code="connector_error", error_detail="timed out")
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED


# -- complete_research_run_if_claimed: lock-held deadline + cancellation reread ----------

def test_complete_research_run_if_claimed_commits_completed_when_not_cancelled(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
        T0 + timedelta(seconds=5))

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.COMPLETED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.COMPLETED
    assert final.phase is None
    assert final.claim_token is None
    assert final.completed_at == T0 + timedelta(seconds=5)


def test_complete_research_run_if_claimed_rereads_cancel_requested_and_commits_cancelled(
        conn, session_id):
    """A requested COMPLETED must be committed as CANCELLED instead the instant
    cancel_requested is set -- read fresh under this call's own lock, not from any
    application-level snapshot the caller may have taken earlier."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert request_cancel_research_run(conn, run.id) is True

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, T0)

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.CANCELLED
    assert get_research_run(conn, run.id).status == ResearchRunStatus.CANCELLED


def test_complete_research_run_if_claimed_never_second_guesses_an_explicit_cancelled_request(
        conn, session_id):
    """A `requested_status=CANCELLED` is never re-derived from cancel_requested -- it commits
    exactly as requested even when cancel_requested itself is still False (e.g. the caller
    already decided CANCELLED for its own reasons, such as _finalize's no-evidence path)."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert get_research_run(conn, run.id).cancel_requested is False

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.CANCELLED, T0)

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.CANCELLED
    assert get_research_run(conn, run.id).status == ResearchRunStatus.CANCELLED


def test_complete_research_run_if_claimed_default_honors_cancel_over_an_explicit_failure(
        conn, session_id):
    """The final cancellation race this closes: a `requested_status=FAILED` (an explicit
    failure, e.g. error_code='no_evidence_collected'/'synthesis_failed') must NOT commit FAILED
    when cancel_requested is set -- by default (`honor_cancel=True`), cancellation wins over
    BOTH the deadline check and whatever terminal status was actually requested, committing
    CANCELLED with error_code/error_detail cleared to None. This is what makes sure a
    cancellation that committed before `_finalize`'s no-evidence path or
    `_synthesize_and_terminate`'s synthesis-failed path reaches its own terminal write can never
    surface as FAILED_NO_EVIDENCE/SYNTHESIS_FAILED."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert request_cancel_research_run(conn, run.id) is True

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED, T0,
        error_code="synthesis_failed")

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.CANCELLED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.CANCELLED
    row = conn.execute(
        "SELECT error_code, error_detail FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] is None
    assert row["error_detail"] is None


def test_complete_research_run_if_claimed_honor_cancel_false_keeps_an_explicit_failure(
        conn, session_id):
    """`honor_cancel=False` is the explicit opt-out reserved for a true integrity/operational
    failure (e.g. `_resume_sealed_run`'s 'sealed_snapshot_missing_revision', or the research
    worker's 'ResearchSessionMissing') that must commit exactly as requested, never silently
    hidden behind a cancellation that merely happened to race it."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    assert request_cancel_research_run(conn, run.id) is True

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED, T0,
        error_code="synthesis_failed", honor_cancel=False)

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.FAILED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "synthesis_failed"


def test_complete_research_run_if_claimed_honor_cancel_false_still_applies_the_deadline_check(
        conn, session_id):
    """`honor_cancel=False` disables cancellation-awareness entirely for this call -- a
    requested COMPLETED discovered past the deadline still fails outright with
    error_code='deadline_exceeded', exactly as if cancel_requested had never been set at all."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    assert request_cancel_research_run(conn, run.id) is True
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, past_deadline,
        honor_cancel=False)

    assert result.ok is False
    assert result.failure_reason == TerminalCompletionFailureReason.DEADLINE_EXCEEDED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "deadline_exceeded"


def test_complete_research_run_if_claimed_rejects_stale_claim(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)

    result = complete_research_run_if_claimed(
        conn, run.id, "wrong-token", ResearchRunStatus.COMPLETED, T0)

    assert result.ok is False
    assert result.failure_reason == TerminalCompletionFailureReason.STALE_CLAIM
    assert result.committed_status is None
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PROCESSING


def test_complete_research_run_if_claimed_rejects_non_terminal_status(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=3600)
    with pytest.raises(ValueError, match="must be terminal"):
        complete_research_run_if_claimed(
            conn, run.id, claimed.run.claim_token, ResearchRunStatus.PENDING, T0)


def test_complete_research_run_if_claimed_completed_past_deadline_without_cancellation_fails(
        conn, session_id):
    """A requested COMPLETED discovered, under this call's own lock, to be past the run's fixed
    deadline_at -- with cancel_requested NOT set -- must never commit COMPLETED: it is atomically
    failed instead with error_code='deadline_exceeded'. This is the baseline, non-cancelled
    deadline invariant Task A leaves unchanged; see the cancellation-precedence sibling test
    below for the one case (cancel_requested=1) where this DEADLINE_EXCEEDED failure is instead
    superseded by a committed CANCELLED."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, past_deadline)

    assert result.ok is False
    assert result.failure_reason == TerminalCompletionFailureReason.DEADLINE_EXCEEDED
    assert result.committed_status is None
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.FAILED
    assert final.phase is None
    assert final.claim_token is None
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "deadline_exceeded"


def test_complete_research_run_if_claimed_cancellation_precedes_deadline_exceeded(
        conn, session_id):
    """Task A -- cancellation-first precedence: a requested COMPLETED discovered, under this
    call's own lock, to be past the run's fixed deadline_at, but with cancel_requested ALSO set,
    must commit CANCELLED -- never be overwritten by a deadline watchdog into a false FAILED/
    deadline_exceeded. This is what lets an Owner's cancellation that committed while the run was
    still active preserve completed evidence rather than losing a race to the run's own deadline
    arriving at (or after) the very same instant."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    assert request_cancel_research_run(conn, run.id) is True
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, past_deadline)

    assert result.ok is True
    assert result.failure_reason is None
    assert result.committed_status == ResearchRunStatus.CANCELLED
    final = get_research_run(conn, run.id)
    assert final.status == ResearchRunStatus.CANCELLED
    assert final.phase is None
    assert final.claim_token is None
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] is None


def test_complete_research_run_if_claimed_completed_at_exact_deadline_equality_fails_closed(
        conn, session_id):
    """`now == deadline_at` must be treated as "the deadline has arrived", exactly like
    heartbeat_research_run's and claim_research_run's own `now >= deadline_at` fencing."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
        claimed.run.deadline_at)

    assert result.ok is False
    assert result.failure_reason == TerminalCompletionFailureReason.DEADLINE_EXCEEDED


def test_complete_research_run_if_claimed_cancelled_request_commits_even_past_deadline(
        conn, session_id):
    """A cooperative cancellation (`requested_status=CANCELLED`, already decided by the caller
    before ever reaching this call -- e.g. _finalize's no-evidence path) is deliberately allowed
    to complete even once the run's own deadline has also since arrived: cancellation only stops
    further AI-call budget from being spent, per _synthesize_and_terminate's own design, it is
    never itself invalidated by the deadline having passed."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.CANCELLED, past_deadline)

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.CANCELLED
    assert get_research_run(conn, run.id).status == ResearchRunStatus.CANCELLED


def test_complete_research_run_if_claimed_failed_request_commits_even_past_deadline(
        conn, session_id):
    """Mirrors the CANCELLED case: an explicit FAILED request (e.g. 'no_evidence_collected')
    commits with its own error_code even once the deadline has also since arrived -- the
    deadline check only ever protects a requested COMPLETED from silently succeeding."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.FAILED, past_deadline,
        error_code="no_evidence_collected")

    assert result.ok is True
    assert result.committed_status == ResearchRunStatus.FAILED
    row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["error_code"] == "no_evidence_collected"


def test_complete_research_run_if_claimed_uses_now_fn_over_pre_sampled_now(conn, session_id):
    """`now_fn`, when given, is authoritative -- the deadline/cancellation decision must reflect
    whatever `now_fn()` returns, not the (here, deliberately wrong/stale) positional `now`."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=60)
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    # The positional `now` looks well within budget; only now_fn() reveals the deadline has
    # actually arrived.
    result = complete_research_run_if_claimed(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, T0,
        now_fn=lambda: past_deadline)

    assert result.ok is False
    assert result.failure_reason == TerminalCompletionFailureReason.DEADLINE_EXCEEDED


# -- recovery: deadline fixed, lease renewed, attempt_count bumped -----------------------

def test_recover_expired_requeues_and_increments_attempt_count_without_resetting_deadline(
        conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=3600)

    later = T0 + timedelta(seconds=60)  # lease expired, deadline (1h) has not
    result = recover_expired_research_runs(conn, later)
    assert result.requeued_count == 1
    assert result.deadline_exceeded_count == 0

    recovered = get_research_run(conn, run.id)
    assert recovered.status == ResearchRunStatus.PENDING
    assert recovered.phase is None
    assert recovered.claim_token is None
    assert recovered.deadline_at == claimed.run.deadline_at  # unchanged
    assert recovered.started_at == claimed.run.started_at  # unchanged

    reclaimed = claim_research_run(
        conn, run.id, later, lease_seconds=30, deadline_seconds=99999)
    assert reclaimed.attempt_count == 1
    assert reclaimed.run.deadline_at == claimed.run.deadline_at  # still fixed on reclaim


def test_recover_expired_fails_run_once_deadline_has_passed(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)

    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)
    result = recover_expired_research_runs(conn, past_deadline)
    assert result.requeued_count == 0
    assert result.deadline_exceeded_count == 1


def test_recover_expired_fails_run_at_exact_deadline_equality_never_requeues(conn, session_id):
    """`now == deadline_at` must be treated as "the deadline has arrived", exactly like
    heartbeat_research_run's and claim_research_run's own `now >= deadline_at` fencing -- it
    must fail the run outright, never requeue it for one more attempt."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)

    result = recover_expired_research_runs(conn, claimed.run.deadline_at)
    assert result.requeued_count == 0
    assert result.deadline_exceeded_count == 1

    failed = get_research_run(conn, run.id)
    assert failed.status == ResearchRunStatus.FAILED
    assert failed.completed_at == claimed.run.deadline_at


# -- Task A: recovery's cancellation-finalization grace -- an expired, cancelled run past its
# own deadline is requeued (never failed), so it can be reclaimed for cancellation finalization --

def test_recover_expired_requeues_a_cancelled_run_even_past_its_own_deadline(conn, session_id):
    """An expired-lease, cancel-requested 'processing' run whose deadline has ALSO already
    passed must be requeued to 'pending' -- never failed outright -- so a future
    claim_research_run call (which carries the matching grace) can reclaim it and finalize
    whatever evidence it already collected before terminalizing CANCELLED. attempt_count is
    still incremented and deadline_at/started_at are still left untouched, exactly like an
    ordinary (non-cancelled, still-within-deadline) requeue."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    deadline_at = claimed.run.deadline_at
    assert request_cancel_research_run(conn, run.id) is True

    past_deadline = deadline_at + timedelta(seconds=5)
    result = recover_expired_research_runs(conn, past_deadline)

    assert result.requeued_count == 1
    assert result.deadline_exceeded_count == 0
    requeued = get_research_run(conn, run.id)
    assert requeued.status == ResearchRunStatus.PENDING
    assert requeued.claim_token is None
    assert requeued.deadline_at == deadline_at  # never reset
    assert requeued.cancel_requested is True
    row = conn.execute(
        "SELECT attempt_count FROM research_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["attempt_count"] == 1


def test_recover_expired_requeues_a_cancelled_run_at_exact_deadline_equality_too(
        conn, session_id):
    """The cancellation grace applies at the exact deadline instant too (`now == deadline_at`),
    consistent with every other equality-inclusive deadline check in this module."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    assert request_cancel_research_run(conn, run.id) is True

    result = recover_expired_research_runs(conn, claimed.run.deadline_at)

    assert result.requeued_count == 1
    assert result.deadline_exceeded_count == 0
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PENDING


def test_recover_expired_without_cancellation_still_fails_a_run_past_its_deadline(
        conn, session_id):
    """Coherence check: the grace above is strictly gated on cancel_requested -- an otherwise
    identical, non-cancelled expired run past its deadline is unaffected (already covered by
    `test_recover_expired_fails_run_once_deadline_has_passed`; this test just makes the contrast
    with its cancelled sibling explicit)."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)

    past_deadline = claimed.run.deadline_at + timedelta(seconds=5)
    result = recover_expired_research_runs(conn, past_deadline)

    assert result.requeued_count == 0
    assert result.deadline_exceeded_count == 1
    assert get_research_run(conn, run.id).status == ResearchRunStatus.FAILED


def test_recover_expired_requeues_a_lease_that_expires_at_exact_recovery_instant(conn, session_id):
    """`lease_expires_at == now` must also be treated as "expired" (equality-inclusive), not left
    untouched as still-healthy -- while the run's deadline is still strictly in the future, this
    must requeue, never fail."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=3600)
    lease_expires_at = T0 + timedelta(seconds=30)
    assert claimed.lease_expires_at == lease_expires_at.isoformat()

    result = recover_expired_research_runs(conn, lease_expires_at)
    assert result.requeued_count == 1
    assert result.deadline_exceeded_count == 0
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PENDING


def test_recover_expired_leaves_healthy_leases_untouched(conn, session_id):
    run = enqueue_research_run(conn, session_id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=300, deadline_seconds=3600)
    result = recover_expired_research_runs(conn, T0 + timedelta(seconds=10))
    assert result.requeued_count == 0
    assert result.deadline_exceeded_count == 0
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PROCESSING


def test_recover_expired_uses_now_fn_over_pre_sampled_now(conn, session_id):
    """`now_fn`, when given, is authoritative: a sweep whose positional `now` looks well within
    budget must still fail (never requeue) a run once `now_fn()` reveals its deadline has
    arrived."""
    run = enqueue_research_run(conn, session_id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=30, deadline_seconds=60)
    past_deadline = claimed.run.deadline_at + timedelta(seconds=1)

    result = recover_expired_research_runs(conn, T0, now_fn=lambda: past_deadline)

    assert result.requeued_count == 0
    assert result.deadline_exceeded_count == 1
    assert get_research_run(conn, run.id).status == ResearchRunStatus.FAILED
