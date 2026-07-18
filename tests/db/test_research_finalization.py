# tests/db/test_research_finalization.py
"""Single-connection, deterministic-outcome unit tests for
db.research_finalization.finalize_snapshot_if_claimed -- the one atomic clusters+seal+revision
write research.orchestrator.py's finalization step uses. Genuine multi-connection races against
this function live in test_research_concurrency.py."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_clusters import list_evidence_clusters
from beehive.db.evidence_curation import set_evidence_curation
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import list_evidence_state_revisions
from beehive.db.research_finalization import (FinalizationFailureReason,
                                               finalize_snapshot_if_claimed)
from beehive.db.research_runs import (claim_research_run, enqueue_research_run, get_research_run,
                                       request_cancel_research_run)
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, get_snapshot
from beehive.db.research_sessions import create_research_session
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
def scenario(conn):
    """A claimed run with a building snapshot holding two near-duplicate items (a, b -- destined
    for one cluster) and one unrelated item (c, a singleton, never clustered) -- already added
    to the snapshot's membership, exactly like the orchestrator's own
    add_snapshot_items_if_claimed call always does before finalize_snapshot_if_claimed ever
    runs against them."""
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)
    snapshot_id = create_snapshot(conn, session_id, run.id, T0).id
    a = upsert_evidence_item(
        conn, session_id, source_id, "a", "T-a", "https://x/a", EvidenceQuality.REPORTING, T0)
    b = upsert_evidence_item(
        conn, session_id, source_id, "b", "T-b", "https://x/b", EvidenceQuality.REPORTING, T0)
    c = upsert_evidence_item(
        conn, session_id, source_id, "c", "T-c", "https://x/c", EvidenceQuality.REPORTING, T0)
    add_snapshot_items(conn, snapshot_id, [a.id, b.id, c.id], T0)
    return {
        "session_id": session_id, "run_id": run.id, "claim_token": lease.run.claim_token,
        "deadline_at": lease.run.deadline_at, "snapshot_id": snapshot_id,
        "a": a.id, "b": b.id, "c": c.id,
    }


# ============================================================================
# Success path
# ============================================================================

def test_finalize_persists_clusters_seals_snapshot_and_pins_a_revision(conn, scenario):
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], T0)

    assert result.ok
    assert result.failure_reason is None

    assert result.snapshot.status == EvidenceSnapshotStatus.SEALED
    assert result.snapshot.sealed_at == T0
    persisted_snapshot = get_snapshot(conn, scenario["snapshot_id"])
    assert persisted_snapshot.status == EvidenceSnapshotStatus.SEALED

    assert len(result.clusters) == 1
    assert set(result.clusters[0].evidence_item_ids) == {scenario["a"], scenario["b"]}
    persisted_clusters = list_evidence_clusters(conn, scenario["snapshot_id"])
    assert len(persisted_clusters) == 1

    assert result.revision.session_id == scenario["session_id"]
    assert result.revision.snapshot_id == scenario["snapshot_id"]
    assert set(result.revision.evidence_item_ids) == {
        scenario["a"], scenario["b"], scenario["c"]}
    assert len(list_evidence_state_revisions(conn, scenario["session_id"])) == 1


def test_finalize_with_no_qualifying_cluster_groups_seals_zero_clusters(conn, scenario):
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [], [scenario["a"], scenario["b"], scenario["c"]], T0)
    assert result.ok
    assert result.clusters == ()
    assert result.snapshot.status == EvidenceSnapshotStatus.SEALED


def test_finalize_curation_filtered_active_ids_can_be_a_strict_subset(conn, scenario):
    """The caller (orchestrator._apply_curation_overlay) may pass fewer active ids than the
    snapshot's full membership -- an Owner-excluded item stays out of the pinned revision even
    though it remains part of the sealed snapshot itself. This is only accepted because it
    matches what `research_evidence_curation` authoritatively says under this same lock: b and c
    are excluded here, so [a] alone is the correct current active set, not merely a caller's
    arbitrary choice."""
    set_evidence_curation(conn, scenario["b"], True, "excluded", T0)
    set_evidence_curation(conn, scenario["c"], True, "excluded", T0)
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]], [scenario["a"]], T0)
    assert result.ok
    assert result.revision.evidence_item_ids == (scenario["a"],)


def test_finalize_rejects_active_ids_that_disagree_with_current_curation(conn, scenario):
    """Even though every id in `active_evidence_item_ids` is a member of the snapshot, passing
    an active set that no longer matches what `research_evidence_curation` authoritatively says
    right now (b is excluded, but the caller's stale precomputation still includes it) must fail
    closed -- this is exactly the curation-race fence Task A closes: a caller must reread
    curation and recompute, never have its stale, over-inclusive active set silently accepted."""
    set_evidence_curation(conn, scenario["b"], True, "excluded after precomputation", T0)
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], T0)
    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.MEMBERSHIP_CHANGED
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


@pytest.mark.parametrize(
    ("cluster_groups", "active_ids"),
    [
        ([[]], ["a"]),
        ([["a", "b"]], ["a", "a"]),
    ],
)
def test_finalize_rejects_invalid_membership_inputs_without_writing(
        conn, scenario, cluster_groups, active_ids):
    ids = {key: scenario[key] for key in ("a", "b")}
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"],
        [[ids[item] for item in group] for group in cluster_groups],
        [ids[item] for item in active_ids], T0,
        expected_snapshot_item_ids=[scenario["a"], scenario["b"], scenario["c"]])

    assert result.failure_reason == FinalizationFailureReason.MEMBERSHIP_CHANGED
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


# ============================================================================
# Snapshot membership integrity (Task B): empty or cross-session membership never finalizes
# ============================================================================

def test_finalize_rejects_an_empty_snapshot_without_writing(conn):
    """A defense-in-depth invariant, not a normal race -- research.orchestrator.py's own
    _finalize already short-circuits before ever calling this when a run collected zero
    evidence, but finalize_snapshot_if_claimed itself must still fail closed if it is ever
    reached with a snapshot that currently has no members at all."""
    session_id = create_research_session(conn, "Q", T0).id
    run = enqueue_research_run(conn, session_id, T0)
    lease = claim_research_run(conn, run.id, T0, lease_seconds=600, deadline_seconds=3600)
    empty_snapshot_id = create_snapshot(conn, session_id, run.id, T0).id

    result = finalize_snapshot_if_claimed(
        conn, run.id, lease.run.claim_token, session_id, empty_snapshot_id, [], [], T0)

    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.INVALID_MEMBERSHIP
    assert result.snapshot is None
    assert result.clusters == ()
    assert result.revision is None
    assert get_snapshot(conn, empty_snapshot_id).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, empty_snapshot_id) == []
    assert list_evidence_state_revisions(conn, session_id) == []
    # the run's claim itself is untouched -- an invalid snapshot is never treated as claim loss
    assert get_research_run(conn, run.id).status == ResearchRunStatus.PROCESSING


def test_finalize_rejects_a_snapshot_with_a_foreign_session_member_without_writing(
        conn, scenario):
    """A defense-in-depth invariant: if the snapshot's membership somehow includes an Evidence
    Item belonging to a DIFFERENT session (e.g. a caller bypassing the claim-fenced
    add_snapshot_items_if_claimed and using the raw, unfenced add_snapshot_items directly),
    finalize_snapshot_if_claimed must still fail closed rather than seal a snapshot, cluster, and
    pin a revision mixing evidence from two Research Sessions."""
    other_session_id = create_research_session(conn, "Other", T0).id
    other_source_id = create_research_source(
        conn, other_session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    foreign_item = upsert_evidence_item(
        conn, other_session_id, other_source_id, "foreign", "T-foreign",
        "https://x/foreign", EvidenceQuality.REPORTING, T0)
    add_snapshot_items(conn, scenario["snapshot_id"], [foreign_item.id], T0)

    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], T0)

    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.INVALID_MEMBERSHIP
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []
    assert get_research_run(conn, scenario["run_id"]).status == ResearchRunStatus.PROCESSING


# ============================================================================
# Fence failures: claim lost -- zero writes
# ============================================================================

def test_finalize_fails_closed_for_a_stale_claim_token(conn, scenario):
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], "not-the-real-token", scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"]], T0)
    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.CLAIM_LOST
    assert result.snapshot is None
    assert result.clusters == ()
    assert result.revision is None
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


def test_finalize_fails_closed_once_the_run_is_no_longer_processing(conn, scenario):
    conn.execute(
        "UPDATE research_runs SET status = 'failed', phase = NULL, claim_token = NULL, "
        "lease_expires_at = NULL, completed_at = ?, error_code = 'synthesis_failed' "
        "WHERE id = ?", (T0.isoformat(), scenario["run_id"]))
    conn.commit()

    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"]], T0)
    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.CLAIM_LOST
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []


def test_finalize_fails_closed_for_a_snapshot_belonging_to_a_different_run(conn, scenario):
    """The claimed run itself is fully valid -- only the snapshot_id argument names a snapshot
    that belongs to a DIFFERENT run (e.g. a caller bug, or a snapshot resolved against stale
    state). This must fail exactly like every other ineligible-snapshot case: no writes."""
    other_session_id = create_research_session(conn, "Other", T0).id
    other_run = enqueue_research_run(conn, other_session_id, T0)
    claim_research_run(conn, other_run.id, T0, lease_seconds=600, deadline_seconds=3600)
    other_snapshot_id = create_snapshot(conn, other_session_id, other_run.id, T0).id

    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        other_snapshot_id, [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"]], T0)
    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.CLAIM_LOST
    assert get_snapshot(conn, other_snapshot_id).status == EvidenceSnapshotStatus.BUILDING


def test_finalize_fails_closed_for_an_already_sealed_snapshot(conn, scenario):
    first = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], T0)
    assert first.ok

    # A second, stale attempt against the very same (now-sealed) snapshot must never duplicate
    # clusters or mint a second revision.
    second = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], T0 + timedelta(seconds=1))
    assert not second.ok
    assert second.failure_reason == FinalizationFailureReason.CLAIM_LOST
    assert len(list_evidence_clusters(conn, scenario["snapshot_id"])) == 1
    assert len(list_evidence_state_revisions(conn, scenario["session_id"])) == 1


# ============================================================================
# Fence failures: deadline exceeded -- run is atomically failed, zero writes
# ============================================================================

def test_finalize_fails_the_run_outright_when_the_deadline_has_arrived(conn, scenario):
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"]], scenario["deadline_at"])

    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.DEADLINE_EXCEEDED
    assert result.snapshot is None
    assert result.clusters == ()
    assert result.revision is None

    run_row = get_research_run(conn, scenario["run_id"])
    assert run_row.status == ResearchRunStatus.FAILED
    assert run_row.phase is None
    assert run_row.claim_token is None
    assert run_row.completed_at == scenario["deadline_at"]

    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


def test_finalize_fails_the_run_outright_when_the_deadline_has_already_passed(conn, scenario):
    past_deadline = scenario["deadline_at"] + timedelta(seconds=1)
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"]], past_deadline)
    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.DEADLINE_EXCEEDED
    assert get_research_run(conn, scenario["run_id"]).status == ResearchRunStatus.FAILED


def test_finalize_succeeds_one_instant_before_the_deadline(conn, scenario):
    just_before = scenario["deadline_at"] - timedelta(microseconds=1)
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], just_before)
    assert result.ok
    assert get_research_run(conn, scenario["run_id"]).status == ResearchRunStatus.PROCESSING


# ============================================================================
# Task A: cancellation-finalization grace -- a cancelled run's local finalization is still
# permitted past its own deadline; a non-cancelled run's is not
# ============================================================================

def test_finalize_still_succeeds_past_the_deadline_when_the_run_is_cancelled(conn, scenario):
    """A cancelled run's already-collected evidence must still be seal-able (clusters + seal +
    revision) even once its own fixed deadline_at has arrived or passed -- this is what lets
    orchestration preserve completed evidence instead of discarding it to a bare deadline_exceeded
    failure. The run's own status is untouched by this call (still 'processing') -- terminalizing
    it CANCELLED is the caller's subsequent complete_research_run_if_claimed's job."""
    assert request_cancel_research_run(conn, scenario["run_id"]) is True
    past_deadline = scenario["deadline_at"] + timedelta(seconds=5)

    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], past_deadline)

    assert result.ok
    assert result.failure_reason is None
    assert result.snapshot.status == EvidenceSnapshotStatus.SEALED
    assert len(result.clusters) == 1
    assert set(result.revision.evidence_item_ids) == {
        scenario["a"], scenario["b"], scenario["c"]}

    run_row = get_research_run(conn, scenario["run_id"])
    assert run_row.status == ResearchRunStatus.PROCESSING  # unchanged -- not this call's job
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.SEALED
    assert len(list_evidence_clusters(conn, scenario["snapshot_id"])) == 1
    assert len(list_evidence_state_revisions(conn, scenario["session_id"])) == 1


def test_finalize_grace_still_enforces_every_other_fence(conn, scenario):
    """The cancellation grace only ever bypasses the deadline check -- every other fence
    (claim/session match, snapshot eligibility, membership/curation agreement) still applies in
    full even for a cancelled, past-deadline run. Here a stale claim_token must still be rejected
    with CLAIM_LOST, not silently accepted because cancel_requested happens to be set."""
    assert request_cancel_research_run(conn, scenario["run_id"]) is True
    past_deadline = scenario["deadline_at"] + timedelta(seconds=5)

    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], "not-the-real-token", scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], past_deadline)

    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.CLAIM_LOST
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
    assert list_evidence_clusters(conn, scenario["snapshot_id"]) == []
    assert list_evidence_state_revisions(conn, scenario["session_id"]) == []


def test_finalize_without_cancellation_still_fails_outright_past_the_deadline(conn, scenario):
    """Coherence check: the grace above is strictly gated on cancel_requested -- an otherwise
    identical, non-cancelled run past its deadline is unaffected (already covered in detail by
    the tests above this section; this pins the exact-equality instant too for symmetry with the
    cancelled test)."""
    result = finalize_snapshot_if_claimed(
        conn, scenario["run_id"], scenario["claim_token"], scenario["session_id"],
        scenario["snapshot_id"], [[scenario["a"], scenario["b"]]],
        [scenario["a"], scenario["b"], scenario["c"]], scenario["deadline_at"])

    assert not result.ok
    assert result.failure_reason == FinalizationFailureReason.DEADLINE_EXCEEDED
    assert get_research_run(conn, scenario["run_id"]).status == ResearchRunStatus.FAILED
    assert get_snapshot(conn, scenario["snapshot_id"]).status == EvidenceSnapshotStatus.BUILDING
