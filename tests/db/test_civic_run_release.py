# tests/db/test_civic_run_release.py
"""What a Civic Run hands back when it ends without sealing.

A Run that goes terminal before its seal is holding two kinds of Candidate that nothing else can
reach: carryovers it adopted from an earlier slot, and Candidates it verified itself. Both point
at an envelope that can never be sealed, and both still own their source items -- and
`civic_consumed_items` claims are deliberately never released, so the next Run skips exactly the
documents that produced them. Without a release path the material is not delayed, it is gone.

Every terminal path has to do this, not just the tidy one: explicit FAILED and CANCELLED
completion, the deadline branch of `heartbeat_civic_run`, the deadline branch of
`claim_civic_run` (a requeued Run can already be holding carryovers), the deadline branch of
`recover_expired_civic_runs`, and `fail_pending_civic_run`. The requeue branch must do the
opposite: a Run whose lease expired but whose deadline is still ahead is coming back to finish
its work, and taking it away would turn a retry into a redistribution.
"""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import (DEADLINE_RELEASE_REASON, adopt_capacity_deferrals,
                                      count_pending_capacity_deferrals, get_civic_batch,
                                      get_or_create_batch_if_claimed,
                                      list_candidate_capacity_deferrals,
                                      list_pending_capacity_deferrals, list_sealed_candidate_ids,
                                      release_terminal_run_holds, seal_batch_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, create_candidate, get_candidate,
                                         list_candidate_evidence, set_candidate_status,
                                         mark_candidate_verified)
from beehive.db.civic_consumed_items import (claim_consumed_item, get_consuming_candidate_id,
                                             list_candidate_items)
from beehive.db.civic_runs import (claim_civic_run, complete_civic_run, enqueue_civic_run,
                                   fail_pending_civic_run, get_civic_run, heartbeat_civic_run,
                                   recover_expired_civic_runs, request_cancel_civic_run)
from beehive.db.civic_verifications import list_verifications, record_verification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicBatchStatus, CivicCandidateStatus, CivicEvidenceStance,
                                  CivicEvidenceTier, CivicRunStatus, CivicVerificationVerdict)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


class Slot:
    def __init__(self, conn, watch_id, at, *, lease=600, deadline=3600, mode="normal"):
        run = enqueue_civic_run(conn, watch_id, at, at, run_mode=mode)
        lease_result = claim_civic_run(conn, run.id, at, lease, deadline)
        self.id = run.id
        self.at = at
        self.token = lease_result.run.claim_token if lease_result else None
        self.batch = (
            get_or_create_batch_if_claimed(conn, run.id, self.token, at)
            if self.token else None)

    def seal(self, conn, now=None):
        return seal_batch_if_claimed(conn, self.batch.id, self.id, self.token, now or self.at)

    def adopt(self, conn, now=None):
        return adopt_capacity_deferrals(
            conn, self.batch.id, self.id, self.token, now or self.at)

    def finish(self, conn, status=CivicRunStatus.COMPLETED, now=None, **kwargs):
        return complete_civic_run(conn, self.id, self.token, status, now or self.at, **kwargs)


def _verified(conn, slot, key, *, now=None, consume=None):
    at = now or slot.at
    candidate = create_candidate(conn, slot.batch.id, key, f"Title {key}", f"Body {key}", at)
    add_candidate_evidence(
        conn, candidate.id, "official_feed", f"e-{key}", f"https://example.test/{key}",
        "Release", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, at,
        publisher="Beehive")
    if consume:
        conn.execute("BEGIN IMMEDIATE")
        claim_consumed_item(
            conn, watch_id=slot.batch.watch_id, run_id=slot.id, candidate_id=candidate.id,
            item_key=consume, source_key=consume.split(":")[0],
            external_key=consume.split(":")[1], canonical_url=f"https://example.test/{consume}",
            now=at)
        conn.commit()
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, at)
    record_verification(conn, candidate.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", at)
    return mark_candidate_verified(conn, candidate.id, at)


def _status(conn, candidate_id):
    return get_candidate(conn, candidate_id).status


@pytest.fixture
def carried(conn, watch_id):
    """A Watch whose first slot overflowed, with the two carryovers adopted into a second Run
    that has not sealed yet -- the exact state a terminal Run must not strand."""
    first = Slot(conn, watch_id, T0)
    candidates = [_verified(conn, first, f"dk-{index:02d}") for index in range(8)]
    first.seal(conn)
    first.finish(conn)

    second = Slot(conn, watch_id, T0 + DAY)
    adopted = second.adopt(conn, now=T0 + DAY)
    assert adopted.adopted_count == 2
    return type("Ctx", (), {
        "watch_id": watch_id, "first": first, "second": second, "candidates": candidates,
        "overflow": candidates[6:]})()


# ============================================================================
# Every terminal path releases
# ============================================================================


def test_failed_completion_returns_the_carryovers_to_the_queue(conn, carried):
    assert carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY,
                                 error_code="worker_crashed") is True
    for candidate in carried.overflow:
        assert _status(conn, candidate.id) is CivicCandidateStatus.DEFERRED_CAPACITY
    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 2
    assert [entry.reason for entry in list_pending_capacity_deferrals(conn, carried.watch_id)] == [
        "run_failed", "run_failed"]


def test_cancelled_completion_returns_the_carryovers_to_the_queue(conn, carried):
    assert carried.second.finish(conn, CivicRunStatus.CANCELLED, now=T0 + DAY) is True
    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 2
    assert {entry.reason for entry in list_pending_capacity_deferrals(conn, carried.watch_id)} == {
        "run_cancelled"}


def test_an_owner_cancellation_beats_the_requested_status(conn, carried):
    """complete_civic_run commits CANCELLED when a cancel was requested, whatever the caller
    asked for -- and the release reason has to follow the status that actually committed."""
    request_cancel_civic_run(conn, carried.second.id)
    carried.second.finish(conn, CivicRunStatus.COMPLETED, now=T0 + DAY)
    assert get_civic_run(conn, carried.second.id).status is CivicRunStatus.CANCELLED
    assert {entry.reason for entry in list_pending_capacity_deferrals(conn, carried.watch_id)} == {
        "run_cancelled"}


def test_expired_run_recovery_releases_on_the_deadline_branch(conn, carried):
    past_deadline = get_civic_run(conn, carried.second.id).deadline_at + timedelta(seconds=1)
    result = recover_expired_civic_runs(conn, past_deadline)
    assert result.deadline_exceeded_count == 1
    assert result.requeued_count == 0
    assert get_civic_run(conn, carried.second.id).status is CivicRunStatus.FAILED
    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 2
    assert {entry.reason for entry in list_pending_capacity_deferrals(conn, carried.watch_id)} == {
        DEADLINE_RELEASE_REASON}


def test_recovery_requeue_leaves_a_resumable_run_alone(conn, watch_id):
    """The opposite case, and the one that must not fire: the lease expired but the deadline is
    still ahead, so the Run comes back and finishes its own work."""
    first = Slot(conn, watch_id, T0)
    candidates = [_verified(conn, first, f"dk-{index:02d}") for index in range(8)]
    first.seal(conn)
    first.finish(conn)

    second = Slot(conn, watch_id, T0 + DAY, lease=60, deadline=86400)
    second.adopt(conn, now=T0 + DAY)
    result = recover_expired_civic_runs(conn, T0 + DAY + timedelta(minutes=5))

    assert result.requeued_count == 1
    assert result.deadline_exceeded_count == 0
    assert get_civic_run(conn, second.id).status is CivicRunStatus.PENDING
    for candidate in candidates[6:]:
        assert _status(conn, candidate.id) is CivicCandidateStatus.VERIFIED
        assert get_candidate(conn, candidate.id).batch_id == second.batch.id
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


def test_the_heartbeat_deadline_branch_releases(conn, carried):
    past_deadline = get_civic_run(conn, carried.second.id).deadline_at + timedelta(seconds=1)
    assert heartbeat_civic_run(
        conn, carried.second.id, carried.second.token, past_deadline, 600) is False
    assert get_civic_run(conn, carried.second.id).status is CivicRunStatus.FAILED
    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 2


def test_the_claim_deadline_branch_releases_a_requeued_run(conn, watch_id):
    """A requeued Run is holding everything it adopted on its first attempt. Failing it at the
    next claim because its budget ran out is still a terminal path."""
    first = Slot(conn, watch_id, T0)
    candidates = [_verified(conn, first, f"dk-{index:02d}") for index in range(8)]
    first.seal(conn)
    first.finish(conn)

    second = Slot(conn, watch_id, T0 + DAY, lease=60, deadline=600)
    second.adopt(conn, now=T0 + DAY)
    recover_expired_civic_runs(conn, T0 + DAY + timedelta(minutes=2))
    assert get_civic_run(conn, second.id).status is CivicRunStatus.PENDING
    assert count_pending_capacity_deferrals(conn, watch_id) == 0

    assert claim_civic_run(conn, second.id, T0 + DAY + timedelta(hours=2), 600, 3600) is None
    assert get_civic_run(conn, second.id).status is CivicRunStatus.FAILED
    assert count_pending_capacity_deferrals(conn, watch_id) == 2
    for candidate in candidates[6:]:
        assert _status(conn, candidate.id) is CivicCandidateStatus.DEFERRED_CAPACITY


def test_failing_a_requeued_pending_run_releases(conn, watch_id):
    first = Slot(conn, watch_id, T0)
    candidates = [_verified(conn, first, f"dk-{index:02d}") for index in range(8)]
    first.seal(conn)
    first.finish(conn)

    second = Slot(conn, watch_id, T0 + DAY, lease=60, deadline=86400)
    second.adopt(conn, now=T0 + DAY)
    recover_expired_civic_runs(conn, T0 + DAY + timedelta(minutes=5))
    assert fail_pending_civic_run(
        conn, second.id, T0 + DAY + timedelta(hours=1), "watch_retired") is True
    assert count_pending_capacity_deferrals(conn, watch_id) == 2
    for candidate in candidates[6:]:
        assert _status(conn, candidate.id) is CivicCandidateStatus.DEFERRED_CAPACITY


# ============================================================================
# Candidates the Run verified itself, not just carryovers
# ============================================================================


def test_a_run_that_fails_after_verifying_keeps_its_own_material_too(conn, watch_id):
    """Not just the probe's carryovers. A Candidate this Run built and verified holds consumed
    items exactly the same way, so losing it loses the story permanently."""
    slot = Slot(conn, watch_id, T0)
    fresh = _verified(conn, slot, "dk-own", consume="nz_beehive_releases:1")
    slot.finish(conn, CivicRunStatus.FAILED, error_code="spool_unreachable")

    assert _status(conn, fresh.id) is CivicCandidateStatus.DEFERRED_CAPACITY
    assert count_pending_capacity_deferrals(conn, watch_id) == 1
    history = list_candidate_capacity_deferrals(conn, fresh.id)
    assert len(history) == 1
    assert history[0].from_batch_id == slot.batch.id
    assert history[0].reason == "run_failed"
    assert history[0].is_open

    later = Slot(conn, watch_id, T0 + DAY)
    assert later.adopt(conn, now=T0 + DAY).adopted_candidate_ids == (fresh.id,)
    sealed = later.seal(conn, now=T0 + DAY)
    assert list_sealed_candidate_ids(conn, sealed.id) == [fresh.id]


def test_unsettled_candidates_are_settled_rather_than_stranded(conn, watch_id):
    """Never verified, so there is nothing publishable to keep -- but leaving them unsettled
    would park them in an envelope no seal can ever close. The rejection code names the Run's
    end, so the record does not imply the material was judged and refused."""
    slot = Slot(conn, watch_id, T0)
    open_candidate = create_candidate(conn, slot.batch.id, "dk-open", "Open", "Body", T0)
    verifying = create_candidate(conn, slot.batch.id, "dk-verifying", "Mid", "Body", T0)
    set_candidate_status(conn, verifying.id, CivicCandidateStatus.VERIFYING, T0)
    slot.finish(conn, CivicRunStatus.FAILED, error_code="boom")

    for candidate in (open_candidate, verifying):
        settled = get_candidate(conn, candidate.id)
        assert settled.status is CivicCandidateStatus.REJECTED
        assert settled.rejection_code == "run_failed"
        assert settled.settled_at == T0
    assert count_pending_capacity_deferrals(conn, watch_id) == 0


# ============================================================================
# Nothing else moves
# ============================================================================


def test_a_released_candidate_keeps_everything_it_had(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    fresh = _verified(conn, slot, "dk-own", consume="nz_stuff:7")
    evidence_before = list_candidate_evidence(conn, fresh.id)
    slot.finish(conn, CivicRunStatus.FAILED, error_code="boom")

    after = get_candidate(conn, fresh.id)
    assert list_candidate_evidence(conn, fresh.id) == evidence_before
    assert len(list_verifications(conn, fresh.id)) == 1
    assert [item.item_key for item in list_candidate_items(conn, fresh.id)] == ["nz_stuff:7"]
    assert get_consuming_candidate_id(conn, watch_id, "nz_stuff:7") == fresh.id
    assert after.dedupe_key == fresh.dedupe_key
    assert after.created_at == fresh.created_at
    assert after.settled_at == fresh.settled_at
    assert after.sealed_into_batch_at is None


def test_a_release_never_touches_an_already_sealed_envelope(conn, carried):
    sealed = get_civic_batch(conn, carried.first.batch.id)
    published = list_sealed_candidate_ids(conn, sealed.id)
    carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY, error_code="boom")

    assert get_civic_batch(conn, sealed.id).content_hash == sealed.content_hash
    assert get_civic_batch(conn, sealed.id).candidate_count == sealed.candidate_count
    assert list_sealed_candidate_ids(conn, sealed.id) == published
    for candidate in carried.candidates[:6]:
        assert _status(conn, candidate.id) is CivicCandidateStatus.VERIFIED
        assert get_candidate(conn, candidate.id).sealed_into_batch_at is not None


def test_a_release_is_scoped_to_its_own_watch(conn):
    """No cross-Watch rescue: a Run failing on one Watch cannot reach into another's queue."""
    mine = create_civic_watch(conn, "NZ election", "Q", T0).id
    theirs = create_civic_watch(conn, "Local body", "Q", T0).id
    their_slot = Slot(conn, theirs, T0)
    their_candidate = _verified(conn, their_slot, "dk-theirs")

    my_slot = Slot(conn, mine, T0)
    _verified(conn, my_slot, "dk-mine")
    my_slot.finish(conn, CivicRunStatus.FAILED, error_code="boom")

    assert count_pending_capacity_deferrals(conn, mine) == 1
    assert count_pending_capacity_deferrals(conn, theirs) == 0
    assert _status(conn, their_candidate.id) is CivicCandidateStatus.VERIFIED


# ============================================================================
# Idempotency and audit truth
# ============================================================================


def test_repeated_terminal_handling_releases_exactly_once(conn, carried):
    carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY, error_code="boom")
    # Every other terminal path, replayed against the same already-failed Run.
    assert carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY) is False
    recover_expired_civic_runs(conn, T0 + DAY + timedelta(days=2))
    heartbeat_civic_run(conn, carried.second.id, carried.second.token, T0 + DAY, 600)
    release_terminal_run_holds(conn, carried.second.id, T0 + DAY + timedelta(hours=1))
    release_terminal_run_holds(conn, carried.second.id, T0 + DAY + timedelta(hours=2))

    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 2
    for candidate in carried.overflow:
        history = list_candidate_capacity_deferrals(conn, candidate.id)
        assert [(entry.from_batch_id, entry.reason) for entry in history] == [
            (carried.first.batch.id, "batch_cap_reached"),
            (carried.second.batch.id, "run_failed")]


def test_a_failed_hop_is_recorded_without_implying_publication(conn, carried):
    """The closed row says the Candidate really did move into the second Batch, because it did.
    Publication is `sealed_into_batch_at`, and it never got one."""
    candidate = carried.overflow[0]
    carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY, error_code="boom")

    history = list_candidate_capacity_deferrals(conn, candidate.id)
    assert history[0].to_batch_id == carried.second.batch.id
    assert not history[0].is_open
    assert history[1].from_batch_id == carried.second.batch.id
    assert history[1].to_batch_id is None
    assert history[1].is_open
    assert get_candidate(conn, candidate.id).sealed_into_batch_at is None
    assert list_sealed_candidate_ids(conn, carried.second.batch.id) == []


def test_repeated_failure_and_adoption_cycles_stay_auditable(conn, carried):
    carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY, error_code="boom")
    third = Slot(conn, carried.watch_id, T0 + 2 * DAY)
    third.adopt(conn, now=T0 + 2 * DAY)
    third.finish(conn, CivicRunStatus.CANCELLED, now=T0 + 2 * DAY)

    fourth = Slot(conn, carried.watch_id, T0 + 3 * DAY)
    fourth.adopt(conn, now=T0 + 3 * DAY)
    sealed = fourth.seal(conn, now=T0 + 3 * DAY)
    assert sealed.candidate_count == 2

    history = list_candidate_capacity_deferrals(conn, carried.overflow[0].id)
    assert [(entry.from_batch_id, entry.to_batch_id, entry.reason) for entry in history] == [
        (carried.first.batch.id, carried.second.batch.id, "batch_cap_reached"),
        (carried.second.batch.id, third.batch.id, "run_failed"),
        (third.batch.id, fourth.batch.id, "run_cancelled")]
    assert all(not entry.is_open for entry in history)
    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 0


def test_a_later_run_publishes_the_released_work_on_its_original_receipts(conn, carried):
    candidate = carried.overflow[0]
    evidence_before = list_candidate_evidence(conn, candidate.id)
    verifications_before = list_verifications(conn, candidate.id)
    carried.second.finish(conn, CivicRunStatus.FAILED, now=T0 + DAY, error_code="boom")

    third = Slot(conn, carried.watch_id, T0 + 2 * DAY)
    assert third.adopt(conn, now=T0 + 2 * DAY).adopted_count == 2
    sealed = third.seal(conn, now=T0 + 2 * DAY)

    assert candidate.id in list_sealed_candidate_ids(conn, sealed.id)
    assert list_candidate_evidence(conn, candidate.id) == evidence_before
    assert list_verifications(conn, candidate.id) == verifications_before
    assert get_civic_batch(conn, sealed.id).status is CivicBatchStatus.SEALED


def test_release_refuses_a_run_that_can_still_resume(conn, carried):
    """The standalone sweep must never take work from a live claim."""
    assert release_terminal_run_holds(conn, carried.second.id, T0 + DAY) is None
    assert count_pending_capacity_deferrals(conn, carried.watch_id) == 0
    for candidate in carried.overflow:
        assert _status(conn, candidate.id) is CivicCandidateStatus.VERIFIED


def test_release_of_an_unknown_run_reports_nothing(conn):
    assert release_terminal_run_holds(conn, 9999, T0) is None


def test_a_normally_completed_sealed_run_releases_nothing(conn, watch_id):
    slot = Slot(conn, watch_id, T0)
    candidates = [_verified(conn, slot, f"dk-{index:02d}") for index in range(3)]
    sealed = slot.seal(conn)
    slot.finish(conn)

    assert count_pending_capacity_deferrals(conn, watch_id) == 0
    assert list_pending_capacity_deferrals(conn, watch_id) == []
    assert list_sealed_candidate_ids(conn, sealed.id) == [c.id for c in candidates]
    for candidate in candidates:
        assert _status(conn, candidate.id) is CivicCandidateStatus.VERIFIED
