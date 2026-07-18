from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.research_runs import (claim_research_run, complete_research_run,
                                       enqueue_research_run, get_research_run,
                                       request_cancel_research_run)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import (SnapshotAppendFailureReason, SnapshotClaimFailureReason,
                                            add_snapshot_items, add_snapshot_items_if_claimed,
                                            create_snapshot, get_building_snapshot_for_run,
                                            get_latest_snapshot, get_or_create_snapshot_if_claimed,
                                            get_snapshot, get_snapshot_for_run,
                                            list_snapshot_item_ids, list_snapshots, seal_snapshot)
from beehive.db.research_sources import create_research_source
from beehive.domain.research import (EvidenceQuality, EvidenceSnapshotStatus, ResearchRunStatus,
                                      ResearchSourceOrigin)

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def session_source_run(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    return session_id, source_id, run_id


def _terminalize(conn, run_id, now):
    """Claims and immediately completes an already-'pending' Research Run -- frees up the
    session's one-active-run slot (db.research_runs.enqueue_research_run forbids a second
    active pending/processing run per session) so a fresh run can be enqueued to FK-anchor a
    SECOND Evidence Snapshot in the same session: research_snapshots now enforces UNIQUE(run_id)
    (at most one Evidence Snapshot per run), so two snapshots in one session must reference two
    different runs."""
    lease = claim_research_run(conn, run_id, now, lease_seconds=600, deadline_seconds=3600)
    complete_research_run(conn, run_id, lease.run.claim_token, ResearchRunStatus.COMPLETED, now)


def _terminal_run_id(conn, session_id, now):
    """Enqueues, claims, and immediately completes a fresh Research Run for the given session --
    used purely to FK-anchor a second Evidence Snapshot; callers must first `_terminalize` any
    still-pending/processing run this session already has, or this raises the same "already has
    an active Research Run" ValueError enqueue_research_run always raises for that case."""
    run = enqueue_research_run(conn, session_id, now)
    _terminalize(conn, run.id, now)
    return run.id


def test_create_snapshot_starts_building_at_sequence_one(conn, session_source_run):
    session_id, _, run_id = session_source_run
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    assert snapshot.status == EvidenceSnapshotStatus.BUILDING
    assert snapshot.sequence_number == 1
    assert snapshot.sealed_at is None


def test_create_snapshot_increments_sequence_number_per_session(conn, session_source_run):
    session_id, _, run_id = session_source_run
    create_snapshot(conn, session_id, run_id, T0)
    _terminalize(conn, run_id, T0 + timedelta(seconds=1))
    run_id_2 = _terminal_run_id(conn, session_id, T0 + timedelta(minutes=1))
    second = create_snapshot(conn, session_id, run_id_2, T0 + timedelta(minutes=1))
    assert second.sequence_number == 2


def test_create_snapshot_rejects_a_second_snapshot_for_the_same_run(conn, session_source_run):
    """The domain invariant enforced by the schema's UNIQUE(run_id) index: a Research Run may
    only ever have ONE Evidence Snapshot. A run must resume its own existing snapshot
    (get_snapshot_for_run), never create a second one."""
    session_id, _, run_id = session_source_run
    first = create_snapshot(conn, session_id, run_id, T0)
    with pytest.raises(ValueError, match="already has an Evidence Snapshot"):
        create_snapshot(conn, session_id, run_id, T0 + timedelta(minutes=1))
    # the rejected attempt left the first snapshot, and only the first snapshot, in place
    assert [s.id for s in list_snapshots(conn, session_id)] == [first.id]


def test_add_snapshot_items_and_list_snapshot_item_ids(conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    item1 = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    item2 = upsert_evidence_item(
        conn, session_id, source_id, "e2", "T2", "https://x/2",
        EvidenceQuality.PRIMARY, T0)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    add_snapshot_items(conn, snapshot.id, [item1.id, item2.id], T0)
    assert list_snapshot_item_ids(conn, snapshot.id) == sorted([item1.id, item2.id])


def test_add_snapshot_items_is_idempotent(conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    add_snapshot_items(conn, snapshot.id, [item.id], T0)
    add_snapshot_items(conn, snapshot.id, [item.id], T0)  # re-adding must not error or duplicate
    assert list_snapshot_item_ids(conn, snapshot.id) == [item.id]


def test_add_snapshot_items_after_sealing_raises(conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    seal_snapshot(conn, snapshot.id, T0)
    with pytest.raises(ValueError, match="not 'building'"):
        add_snapshot_items(conn, snapshot.id, [item.id], T0)
    # the sealed snapshot's membership is untouched by the rejected write
    assert list_snapshot_item_ids(conn, snapshot.id) == []


def test_add_snapshot_items_missing_snapshot_raises(conn, session_source_run):
    session_id, source_id, _ = session_source_run
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    with pytest.raises(ValueError, match="no Evidence Snapshot"):
        add_snapshot_items(conn, 999, [item.id], T0)


def test_create_snapshot_copy_forward_carries_cumulative_membership(conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    item1 = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    item2 = upsert_evidence_item(
        conn, session_id, source_id, "e2", "T2", "https://x/2",
        EvidenceQuality.PRIMARY, T0)
    first = create_snapshot(conn, session_id, run_id, T0)
    add_snapshot_items(conn, first.id, [item1.id], T0)

    _terminalize(conn, run_id, T0 + timedelta(seconds=1))
    run_id_2 = _terminal_run_id(conn, session_id, T0 + timedelta(minutes=1))
    second = create_snapshot(
        conn, session_id, run_id_2, T0 + timedelta(minutes=1), copy_forward_from=first.id)
    add_snapshot_items(conn, second.id, [item2.id], T0 + timedelta(minutes=1))

    # second snapshot is cumulative: it has both the carried-forward item and the new one
    assert list_snapshot_item_ids(conn, second.id) == sorted([item1.id, item2.id])
    # the first snapshot itself is untouched
    assert list_snapshot_item_ids(conn, first.id) == [item1.id]


def test_seal_snapshot_transitions_to_sealed(conn, session_source_run):
    session_id, _, run_id = session_source_run
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    sealed = seal_snapshot(conn, snapshot.id, T0 + timedelta(minutes=1))
    assert sealed.status == EvidenceSnapshotStatus.SEALED
    assert sealed.sealed_at == T0 + timedelta(minutes=1)


def test_seal_snapshot_twice_raises(conn, session_source_run):
    session_id, _, run_id = session_source_run
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    seal_snapshot(conn, snapshot.id, T0)
    with pytest.raises(ValueError, match="invalid Evidence Snapshot transition"):
        seal_snapshot(conn, snapshot.id, T0)


def test_seal_snapshot_missing_id_raises(conn):
    with pytest.raises(ValueError, match="no Evidence Snapshot"):
        seal_snapshot(conn, 999, T0)


def test_get_snapshot_returns_none_for_missing_id(conn):
    assert get_snapshot(conn, 999) is None


def test_list_snapshots_ordered_by_sequence_number(conn, session_source_run):
    session_id, _, run_id = session_source_run
    first = create_snapshot(conn, session_id, run_id, T0)
    _terminalize(conn, run_id, T0 + timedelta(seconds=1))
    run_id_2 = _terminal_run_id(conn, session_id, T0 + timedelta(minutes=1))
    second = create_snapshot(conn, session_id, run_id_2, T0 + timedelta(minutes=1))
    assert [s.id for s in list_snapshots(conn, session_id)] == [first.id, second.id]


def test_get_latest_snapshot_returns_highest_sequence(conn, session_source_run):
    session_id, _, run_id = session_source_run
    create_snapshot(conn, session_id, run_id, T0)
    _terminalize(conn, run_id, T0 + timedelta(seconds=1))
    run_id_2 = _terminal_run_id(conn, session_id, T0 + timedelta(minutes=1))
    second = create_snapshot(conn, session_id, run_id_2, T0 + timedelta(minutes=1))
    assert get_latest_snapshot(conn, session_id).id == second.id


def test_get_latest_snapshot_none_when_no_snapshots(conn, session_source_run):
    session_id, _, _ = session_source_run
    assert get_latest_snapshot(conn, session_id) is None


def test_create_snapshot_rejects_cross_session_copy_forward_from(conn, session_source_run):
    session_id, _, run_id = session_source_run
    first = create_snapshot(conn, session_id, run_id, T0)
    other_session_id = create_research_session(conn, "Other question", T0).id
    other_run_id = enqueue_research_run(conn, other_session_id, T0).id
    with pytest.raises(ValueError, match="belongs to Research Session"):
        create_snapshot(
            conn, other_session_id, other_run_id, T0, copy_forward_from=first.id)
    # the rejected attempt must not have left a partially-created snapshot for the other session
    assert list_snapshots(conn, other_session_id) == []


def test_create_snapshot_missing_copy_forward_from_raises(conn, session_source_run):
    session_id, _, run_id = session_source_run
    with pytest.raises(ValueError, match="no Evidence Snapshot"):
        create_snapshot(conn, session_id, run_id, T0, copy_forward_from=999)


# ============================================================================
# get_snapshot_for_run: any status, at most one per run
# ============================================================================

def test_get_snapshot_for_run_returns_a_building_snapshot(conn, session_source_run):
    session_id, _, run_id = session_source_run
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    found = get_snapshot_for_run(conn, run_id)
    assert found is not None
    assert found.id == snapshot.id
    assert found.status == EvidenceSnapshotStatus.BUILDING


def test_get_snapshot_for_run_returns_a_sealed_snapshot(conn, session_source_run):
    """Unlike get_building_snapshot_for_run, this must still find the run's own snapshot once
    it has been sealed -- exactly the case a crash-recovering orchestrator needs to detect that
    a PRIOR attempt of this run already won atomic finalization."""
    session_id, _, run_id = session_source_run
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    seal_snapshot(conn, snapshot.id, T0)
    found = get_snapshot_for_run(conn, run_id)
    assert found is not None
    assert found.id == snapshot.id
    assert found.status == EvidenceSnapshotStatus.SEALED
    # get_building_snapshot_for_run's narrower view sees nothing once it is sealed
    assert get_building_snapshot_for_run(conn, run_id) is None


def test_get_snapshot_for_run_none_when_the_run_has_no_snapshot(conn, session_source_run):
    _, _, run_id = session_source_run
    assert get_snapshot_for_run(conn, run_id) is None


# ============================================================================
# add_snapshot_items_if_claimed: claim/deadline fencing plus session-ownership fencing
# ============================================================================

def test_add_snapshot_items_if_claimed_appends_when_the_claim_is_active(conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)

    result = add_snapshot_items_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, snapshot.id, [item.id], T0)

    assert result.ok
    assert list_snapshot_item_ids(conn, snapshot.id) == [item.id]


def test_add_snapshot_items_if_claimed_rejects_a_stale_claim_token(conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)

    result = add_snapshot_items_if_claimed(
        conn, run_id, "not-the-real-token", session_id, snapshot.id, [item.id], T0)

    assert not result.ok
    assert result.failure_reason == SnapshotAppendFailureReason.CLAIM_LOST
    assert list_snapshot_item_ids(conn, snapshot.id) == []


def test_add_snapshot_items_if_claimed_rejects_a_foreign_session_item(conn, session_source_run):
    """Every requested Evidence Item must belong to the exact session_id claimed for this run --
    a foreign-session id fails closed with INVALID_ITEMS and appends nothing, but does not fail
    the run itself (a caller/input mismatch, not a claim or deadline problem)."""
    session_id, source_id, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    own_item = upsert_evidence_item(
        conn, session_id, source_id, "own", "T-own", "https://x/own",
        EvidenceQuality.REPORTING, T0)

    other_session_id = create_research_session(conn, "Other", T0).id
    other_source_id = create_research_source(
        conn, other_session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    foreign_item = upsert_evidence_item(
        conn, other_session_id, other_source_id, "foreign", "T-foreign",
        "https://x/foreign", EvidenceQuality.REPORTING, T0)

    result = add_snapshot_items_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, snapshot.id,
        [own_item.id, foreign_item.id], T0)

    assert not result.ok
    assert result.failure_reason == SnapshotAppendFailureReason.INVALID_ITEMS
    # zero writes -- not even the co-requested item that DID belong to this session
    assert list_snapshot_item_ids(conn, snapshot.id) == []
    # the run itself is untouched: a caller/input mismatch is never treated as a claim loss
    run_row = conn.execute(
        "SELECT status FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    assert run_row["status"] == ResearchRunStatus.PROCESSING.value


def test_add_snapshot_items_if_claimed_rejects_a_missing_item_id(conn, session_source_run):
    session_id, _, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot = create_snapshot(conn, session_id, run_id, T0)

    result = add_snapshot_items_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, snapshot.id, [999999], T0)

    assert not result.ok
    assert result.failure_reason == SnapshotAppendFailureReason.INVALID_ITEMS
    assert list_snapshot_item_ids(conn, snapshot.id) == []


# ============================================================================
# get_or_create_snapshot_if_claimed: claim-fenced atomic get-or-create (Task D)
# ============================================================================

def test_get_or_create_snapshot_if_claimed_creates_a_fresh_building_snapshot(
        conn, session_source_run):
    session_id, _, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)

    result = get_or_create_snapshot_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, T0)

    assert result.ok
    assert result.failure_reason is None
    assert result.snapshot.status == EvidenceSnapshotStatus.BUILDING
    assert result.snapshot.run_id == run_id
    assert result.snapshot.sequence_number == 1
    assert list_snapshot_item_ids(conn, result.snapshot.id) == []
    assert [s.id for s in list_snapshots(conn, session_id)] == [result.snapshot.id]


def test_get_or_create_snapshot_if_claimed_copies_forward_the_latest_sealed_snapshot(
        conn, session_source_run):
    session_id, source_id, run_id = session_source_run
    first_lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    first = create_snapshot(conn, session_id, run_id, T0)
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)
    add_snapshot_items(conn, first.id, [item.id], T0)
    seal_snapshot(conn, first.id, T0)
    complete_research_run(
        conn, run_id, first_lease.run.claim_token, ResearchRunStatus.COMPLETED, T0)

    later = T0 + timedelta(minutes=1)
    second_run = enqueue_research_run(conn, session_id, later)
    second_lease = claim_research_run(conn, second_run.id, later, lease_seconds=600,
                                       deadline_seconds=3600)

    result = get_or_create_snapshot_if_claimed(
        conn, second_run.id, second_lease.run.claim_token, session_id, later)

    assert result.ok
    assert result.snapshot.status == EvidenceSnapshotStatus.BUILDING
    assert result.snapshot.sequence_number == 2
    # cumulative: the new snapshot already carries the prior sealed snapshot's membership
    assert list_snapshot_item_ids(conn, result.snapshot.id) == [item.id]
    assert list_snapshot_item_ids(conn, first.id) == [item.id]  # the first is untouched


def test_get_or_create_snapshot_if_claimed_resumes_its_own_existing_building_snapshot(
        conn, session_source_run):
    """Calling this twice for the same run must never create a second row -- the run's own
    still-'building' snapshot (e.g. staged by an earlier attempt before a crash) is returned
    as-is both times."""
    session_id, source_id, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)

    first = get_or_create_snapshot_if_claimed(conn, run_id, lease.run.claim_token, session_id, T0)
    assert first.ok
    add_snapshot_items(conn, first.snapshot.id, [item.id], T0)

    second = get_or_create_snapshot_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, T0 + timedelta(seconds=1))

    assert second.ok
    assert second.snapshot.id == first.snapshot.id
    assert second.snapshot.status == EvidenceSnapshotStatus.BUILDING
    # the item staged between the two calls is untouched -- never recreated or reset
    assert list_snapshot_item_ids(conn, second.snapshot.id) == [item.id]
    assert len(list_snapshots(conn, session_id)) == 1


def test_get_or_create_snapshot_if_claimed_resumes_its_own_existing_sealed_snapshot(
        conn, session_source_run):
    """Unlike `create_snapshot` (which would raise), a run whose own snapshot is already sealed
    -- a prior attempt already won finalize_snapshot_if_claimed -- must have that sealed
    snapshot returned as-is, never re-created, re-sealed, or rejected."""
    session_id, source_id, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot = create_snapshot(conn, session_id, run_id, T0)
    seal_snapshot(conn, snapshot.id, T0)

    result = get_or_create_snapshot_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, T0 + timedelta(seconds=1))

    assert result.ok
    assert result.snapshot.id == snapshot.id
    assert result.snapshot.status == EvidenceSnapshotStatus.SEALED
    assert len(list_snapshots(conn, session_id)) == 1


def test_get_or_create_snapshot_if_claimed_rejects_a_stale_claim_token(conn, session_source_run):
    session_id, _, run_id = session_source_run
    claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=3600)

    result = get_or_create_snapshot_if_claimed(
        conn, run_id, "not-the-real-token", session_id, T0)

    assert not result.ok
    assert result.failure_reason == SnapshotClaimFailureReason.CLAIM_LOST
    assert result.snapshot is None
    assert list_snapshots(conn, session_id) == []


def test_get_or_create_snapshot_if_claimed_fails_the_run_outright_past_the_deadline(
        conn, session_source_run):
    session_id, _, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=60)
    past_deadline = lease.run.deadline_at + timedelta(seconds=1)

    result = get_or_create_snapshot_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, past_deadline)

    assert not result.ok
    assert result.failure_reason == SnapshotClaimFailureReason.DEADLINE_EXCEEDED
    assert result.snapshot is None
    assert list_snapshots(conn, session_id) == []
    run_row = get_research_run(conn, run_id)
    assert run_row.status == ResearchRunStatus.FAILED
    assert run_row.claim_token is None
    raw_row = conn.execute(
        "SELECT error_code FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    assert raw_row["error_code"] == "deadline_exceeded"


def test_get_or_create_snapshot_if_claimed_grants_grace_past_the_deadline_when_cancelled(
        conn, session_source_run):
    """Task A's cancellation-finalization grace applies here too: an already-cancelled run whose
    deadline has also passed must still be able to create its own snapshot, so `_finalize` can go
    on to seal whatever evidence exists (or correctly report no evidence) instead of being
    hard-failed before it ever gets the chance to finalize locally."""
    session_id, _, run_id = session_source_run
    lease = claim_research_run(conn, run_id, T0, lease_seconds=600, deadline_seconds=60)
    assert request_cancel_research_run(conn, run_id) is True
    past_deadline = lease.run.deadline_at + timedelta(seconds=1)

    result = get_or_create_snapshot_if_claimed(
        conn, run_id, lease.run.claim_token, session_id, past_deadline)

    assert result.ok
    assert result.failure_reason is None
    assert result.snapshot.status == EvidenceSnapshotStatus.BUILDING
    run_row = get_research_run(conn, run_id)
    assert run_row.status == ResearchRunStatus.PROCESSING  # untouched -- not this call's job
    assert run_row.claim_token == lease.run.claim_token
