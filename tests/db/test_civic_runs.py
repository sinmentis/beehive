# tests/db/test_civic_runs.py
"""Civic Run persistence: one-run-per-slot, claim/heartbeat/recovery, the deadline that is
genuinely hard, and the claim fence every progressive write is subject to."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_runs import (advance_civic_run_phase, claim_civic_run,
                                    complete_civic_run, count_active_processing_runs,
                                    enqueue_civic_run, fail_pending_civic_run,
                                    get_active_civic_run, get_civic_run,
                                    get_civic_run_for_slot, heartbeat_civic_run,
                                    list_civic_runs, list_pending_civic_runs,
                                    pin_vocabulary_version, recover_expired_civic_runs,
                                    request_cancel_civic_run)
from beehive.db.civic_vocabulary import (add_vocabulary_term, create_vocabulary_version,
                                          get_vocabulary_version, publish_vocabulary_version)
from beehive.db.civic_watches import create_civic_watch, pause_civic_watch, retire_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicRunPhase, CivicRunStatus, CivicTermCategory,
                                   CivicTermRole, CivicVocabularyStatus)

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
SLOT_A = T0
SLOT_B = T0 + timedelta(days=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


def _claimed(conn, run_id, now=T0, lease_seconds=60, deadline_seconds=3600):
    lease = claim_civic_run(conn, run_id, now, lease_seconds, deadline_seconds)
    assert lease is not None
    return lease


def _published_vocabulary(conn, watch_id, term="Green Party", now=T0):
    """A Civic Vocabulary version published and therefore in force for `watch_id`. Publishing a
    second one supersedes the first, which is what the pin-time checks below exercise."""
    version = create_vocabulary_version(conn, watch_id, now)
    add_vocabulary_term(
        conn, version.id, term, CivicTermCategory.PARTY, CivicTermRole.REQUIRED, now)
    return publish_vocabulary_version(conn, version.id, now)


# ============================================================================
# One Run per slot
# ============================================================================


def test_enqueue_creates_one_pending_run(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    assert run.status is CivicRunStatus.PENDING
    assert run.slot_at == SLOT_A
    assert run.phase is None and run.claim_token is None


def test_enqueueing_the_same_slot_twice_returns_the_same_run(conn, watch_id):
    first = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    second = enqueue_civic_run(conn, watch_id, SLOT_A, T0 + timedelta(minutes=5))
    assert first.id == second.id
    assert len(list_civic_runs(conn, watch_id)) == 1


def test_re_enqueueing_a_finished_slot_never_reopens_it(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0 + timedelta(minutes=1))

    again = enqueue_civic_run(conn, watch_id, SLOT_A, T0 + timedelta(days=1))
    assert again.id == run.id
    assert again.status is CivicRunStatus.COMPLETED
    assert len(list_civic_runs(conn, watch_id)) == 1


def test_a_second_slot_is_refused_while_one_is_active(conn, watch_id):
    enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    with pytest.raises(ValueError, match="already has an active Civic Run"):
        enqueue_civic_run(conn, watch_id, SLOT_B, T0)


def test_a_second_slot_is_allowed_once_the_first_is_terminal(conn, watch_id):
    first = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, first.id)
    complete_civic_run(
        conn, first.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0 + timedelta(hours=1))
    second = enqueue_civic_run(conn, watch_id, SLOT_B, T0 + timedelta(days=1))
    assert second.id != first.id
    assert [r.slot_at for r in list_civic_runs(conn, watch_id)] == [SLOT_A, SLOT_B]


def test_the_database_itself_refuses_a_duplicate_slot(conn, watch_id):
    """Belt and braces: even a caller bypassing the repository cannot create a second Run for a
    slot, because UNIQUE(watch_id, slot_at) is a schema constraint."""
    import sqlite3
    enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_runs (watch_id, slot_at, status, requested_at) "
            "VALUES (?, ?, 'completed', ?)",
            (watch_id, SLOT_A.isoformat(), T0.isoformat()))


def test_a_paused_or_retired_watch_never_accepts_new_work(conn):
    paused = create_civic_watch(conn, "paused", "Q", T0).id
    retired = create_civic_watch(conn, "retired", "Q", T0).id
    pause_civic_watch(conn, paused, T0)
    retire_civic_watch(conn, retired, T0)
    for watch in (paused, retired):
        with pytest.raises(ValueError, match="Civic Watch"):
            enqueue_civic_run(conn, watch, SLOT_A, T0)


def test_enqueueing_for_a_missing_watch_raises(conn):
    with pytest.raises(ValueError, match="no Civic Watch"):
        enqueue_civic_run(conn, 999, SLOT_A, T0)


def test_lookup_helpers(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    assert get_civic_run(conn, run.id) == run
    assert get_civic_run_for_slot(conn, watch_id, SLOT_A) == run
    assert get_civic_run_for_slot(conn, watch_id, SLOT_B) is None
    assert get_active_civic_run(conn, watch_id) == run


# ============================================================================
# Claiming
# ============================================================================


def test_claiming_sets_the_lease_deadline_phase_and_attempt(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    assert lease.run.status is CivicRunStatus.PROCESSING
    assert lease.run.phase is CivicRunPhase.GATHERING
    assert lease.run.claim_token is not None
    assert lease.run.started_at == T0
    assert lease.run.deadline_at == T0 + timedelta(seconds=3600)
    assert lease.lease_expires_at == (T0 + timedelta(seconds=60)).isoformat()
    assert lease.attempt_count == 1


def test_a_lease_is_never_granted_past_the_deadline(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=9999, deadline_seconds=60)
    assert lease.lease_expires_at == (T0 + timedelta(seconds=60)).isoformat()


def test_a_run_can_only_be_claimed_once(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id)
    assert claim_civic_run(conn, run.id, T0, 60, 3600) is None


def test_claiming_a_missing_run_returns_none(conn):
    assert claim_civic_run(conn, 999, T0, 60, 3600) is None


def test_the_global_processing_cap_is_enforced(conn):
    runs = []
    for index in range(3):
        watch = create_civic_watch(conn, f"watch-{index}", "Q", T0).id
        runs.append(enqueue_civic_run(conn, watch, SLOT_A, T0))

    assert claim_civic_run(conn, runs[0].id, T0, 60, 3600) is not None
    assert claim_civic_run(conn, runs[1].id, T0, 60, 3600) is not None
    assert count_active_processing_runs(conn, T0) == 2
    assert claim_civic_run(conn, runs[2].id, T0, 60, 3600) is None
    assert get_civic_run(conn, runs[2].id).status is CivicRunStatus.PENDING


def test_an_expired_lease_frees_its_slot_against_the_cap_immediately(conn):
    watch_a = create_civic_watch(conn, "a", "Q", T0).id
    run_a = enqueue_civic_run(conn, watch_a, SLOT_A, T0)
    _claimed(conn, run_a.id, lease_seconds=60, deadline_seconds=3600)
    assert count_active_processing_runs(conn, T0) == 1
    assert count_active_processing_runs(conn, T0 + timedelta(seconds=61)) == 0


def test_a_requeued_run_past_its_deadline_fails_instead_of_being_reclaimed(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=120)
    assert lease is not None
    recover_expired_civic_runs(conn, T0 + timedelta(seconds=61))
    assert get_civic_run(conn, run.id).status is CivicRunStatus.PENDING

    assert claim_civic_run(conn, run.id, T0 + timedelta(seconds=200), 60, 3600) is None
    failed = get_civic_run(conn, run.id)
    assert failed.status is CivicRunStatus.FAILED
    assert failed.error_code == "deadline_exceeded"


def test_a_reclaim_keeps_the_original_deadline_and_started_at(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    first = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    recover_expired_civic_runs(conn, T0 + timedelta(seconds=61))
    second = claim_civic_run(conn, run.id, T0 + timedelta(seconds=61), 60, 99999)
    assert second.run.deadline_at == first.run.deadline_at
    assert second.run.started_at == first.run.started_at
    assert second.attempt_count == 2


def test_now_fn_is_sampled_only_after_the_write_lock_is_held(conn, watch_id):
    """A caller that blocked behind another writer must see the clock as it is when it finally
    acquires the lock, not the stale value it sampled before blocking."""
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    late = T0 + timedelta(seconds=500)
    lease = claim_civic_run(conn, run.id, T0, 60, 3600, now_fn=lambda: late)
    assert lease.run.started_at == late
    assert lease.run.deadline_at == late + timedelta(seconds=3600)


# ============================================================================
# Heartbeat
# ============================================================================


def test_heartbeat_extends_a_live_claim(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    assert heartbeat_civic_run(conn, run.id, lease.run.claim_token, T0 + timedelta(seconds=30), 60)
    row = conn.execute(
        "SELECT lease_expires_at FROM civic_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["lease_expires_at"] == (T0 + timedelta(seconds=90)).isoformat()


def test_heartbeat_never_extends_past_the_deadline(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=100)
    assert heartbeat_civic_run(conn, run.id, lease.run.claim_token, T0 + timedelta(seconds=50), 999)
    row = conn.execute(
        "SELECT lease_expires_at FROM civic_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["lease_expires_at"] == (T0 + timedelta(seconds=100)).isoformat()


def test_heartbeat_at_the_deadline_hard_fails_the_run(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=600, deadline_seconds=100)
    assert not heartbeat_civic_run(
        conn, run.id, lease.run.claim_token, T0 + timedelta(seconds=100), 60)
    failed = get_civic_run(conn, run.id)
    assert failed.status is CivicRunStatus.FAILED
    assert failed.error_code == "deadline_exceeded"
    assert failed.claim_token is None and failed.phase is None
    assert failed.completed_at == T0 + timedelta(seconds=100)


def test_heartbeat_refuses_to_revive_an_independently_expired_lease(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    assert not heartbeat_civic_run(
        conn, run.id, lease.run.claim_token, T0 + timedelta(seconds=61), 60)
    still_processing = get_civic_run(conn, run.id)
    assert still_processing.status is CivicRunStatus.PROCESSING
    row = conn.execute(
        "SELECT lease_expires_at FROM civic_runs WHERE id = ?", (run.id,)).fetchone()
    assert row["lease_expires_at"] == (T0 + timedelta(seconds=60)).isoformat()


def test_heartbeat_with_a_foreign_or_stale_token_writes_nothing(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id)
    assert not heartbeat_civic_run(conn, run.id, "not-the-token", T0 + timedelta(seconds=1), 60)
    assert get_civic_run(conn, run.id).status is CivicRunStatus.PROCESSING


# ============================================================================
# Phase advance and claim fencing
# ============================================================================


def test_phase_advances_forward_only(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    token = lease.run.claim_token
    assert advance_civic_run_phase(conn, run.id, token, CivicRunPhase.SCREENING)
    assert get_civic_run(conn, run.id).phase is CivicRunPhase.SCREENING
    with pytest.raises(ValueError, match="phase advance"):
        advance_civic_run_phase(conn, run.id, token, CivicRunPhase.GATHERING)
    with pytest.raises(ValueError, match="phase advance"):
        advance_civic_run_phase(conn, run.id, token, CivicRunPhase.SCREENING)
    assert get_civic_run(conn, run.id).phase is CivicRunPhase.SCREENING


def test_phase_advance_is_fenced_on_the_claim(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id)
    assert not advance_civic_run_phase(conn, run.id, "stale", CivicRunPhase.PUBLISHING)


def test_vocabulary_version_is_pinned_once_and_never_overwritten(conn, watch_id):
    version = _published_vocabulary(conn, watch_id, term="Green Party")

    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    assert pin_vocabulary_version(conn, run.id, lease.run.claim_token, version.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id == version.id

    later = _published_vocabulary(conn, watch_id, term="Labour")
    assert not pin_vocabulary_version(conn, run.id, lease.run.claim_token, later.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id == version.id


def test_pinning_is_fenced_on_the_claim(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id)
    version = _published_vocabulary(conn, watch_id)
    assert not pin_vocabulary_version(conn, run.id, "stale", version.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id is None


# ============================================================================
# Only a published, same-Watch Civic Vocabulary may be pinned
# ============================================================================


def test_pinning_a_published_same_watch_version_succeeds(conn, watch_id):
    version = _published_vocabulary(conn, watch_id)
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    assert pin_vocabulary_version(conn, run.id, lease.run.claim_token, version.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id == version.id


def test_pinning_a_draft_version_is_refused(conn, watch_id):
    """A draft is still being edited and has no content_hash to quote, so a Run screened against
    it could never be explained afterwards."""
    draft = create_vocabulary_version(conn, watch_id, T0)
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    with pytest.raises(ValueError, match="not 'published'"):
        pin_vocabulary_version(conn, run.id, lease.run.claim_token, draft.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id is None


def test_pinning_a_superseded_version_is_refused(conn, watch_id):
    """PUBLISHED *at pin time* -- a version replaced a moment ago is no longer in force, so the
    caller must re-read rather than silently screening against withdrawn terms."""
    first = _published_vocabulary(conn, watch_id, term="Green Party")
    _published_vocabulary(conn, watch_id, term="Labour")
    assert get_vocabulary_version(conn, first.id).status is CivicVocabularyStatus.SUPERSEDED

    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    with pytest.raises(ValueError, match="not 'published'"):
        pin_vocabulary_version(conn, run.id, lease.run.claim_token, first.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id is None


def test_pinning_another_watchs_vocabulary_is_refused(conn, watch_id):
    """Pinning a foreign vocabulary would let a Run publish statements naming entities its own
    Owner never approved."""
    other_watch = create_civic_watch(conn, "Other subject", "Q", T0).id
    foreign = _published_vocabulary(conn, other_watch)

    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    with pytest.raises(ValueError, match="belongs to Civic Watch"):
        pin_vocabulary_version(conn, run.id, lease.run.claim_token, foreign.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id is None


def test_pinning_a_missing_version_is_refused(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    with pytest.raises(ValueError, match="no Civic Vocabulary version"):
        pin_vocabulary_version(conn, run.id, lease.run.claim_token, 999)


def test_a_refused_pin_leaves_the_transaction_clean(conn, watch_id):
    """The whole call is one BEGIN IMMEDIATE, so a rejected version rolls back rather than
    leaving the connection mid-transaction -- the very next write must succeed normally."""
    draft = create_vocabulary_version(conn, watch_id, T0)
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    with pytest.raises(ValueError):
        pin_vocabulary_version(conn, run.id, lease.run.claim_token, draft.id)

    assert not conn.in_transaction
    add_vocabulary_term(
        conn, draft.id, "Green Party", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
    published = publish_vocabulary_version(conn, draft.id, T0)
    assert pin_vocabulary_version(conn, run.id, lease.run.claim_token, published.id)


def test_a_version_pinned_while_published_survives_being_superseded(conn, watch_id):
    """Pin-time validation only: the whole point of pinning is that a vocabulary published
    mid-run cannot change what the run in flight is allowed to say."""
    version = _published_vocabulary(conn, watch_id, term="Green Party")
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    assert pin_vocabulary_version(conn, run.id, lease.run.claim_token, version.id)

    _published_vocabulary(conn, watch_id, term="Labour")
    assert get_vocabulary_version(conn, version.id).status is CivicVocabularyStatus.SUPERSEDED
    assert get_civic_run(conn, run.id).vocabulary_version_id == version.id


def test_enqueue_accepts_a_published_same_watch_version(conn, watch_id):
    version = _published_vocabulary(conn, watch_id)
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=version.id)
    assert run.vocabulary_version_id == version.id


def test_enqueue_refuses_a_draft_version_and_writes_nothing(conn, watch_id):
    draft = create_vocabulary_version(conn, watch_id, T0)
    with pytest.raises(ValueError, match="not 'published'"):
        enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=draft.id)
    assert list_civic_runs(conn, watch_id) == []


def test_enqueue_refuses_a_superseded_version_and_writes_nothing(conn, watch_id):
    first = _published_vocabulary(conn, watch_id, term="Green Party")
    _published_vocabulary(conn, watch_id, term="Labour")
    with pytest.raises(ValueError, match="not 'published'"):
        enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=first.id)
    assert list_civic_runs(conn, watch_id) == []


def test_enqueue_refuses_another_watchs_vocabulary_and_writes_nothing(conn, watch_id):
    other_watch = create_civic_watch(conn, "Other subject", "Q", T0).id
    foreign = _published_vocabulary(conn, other_watch)
    with pytest.raises(ValueError, match="belongs to Civic Watch"):
        enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=foreign.id)
    assert list_civic_runs(conn, watch_id) == []


def test_enqueue_refuses_a_missing_version_and_writes_nothing(conn, watch_id):
    with pytest.raises(ValueError, match="no Civic Vocabulary version"):
        enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=999)
    assert list_civic_runs(conn, watch_id) == []


def test_enqueue_validates_the_version_even_on_the_idempotent_path(conn, watch_id):
    """A scheduler re-ticking a served slot with a vocabulary read that has since gone stale is
    told so, rather than proceeding on terms that are no longer in force."""
    first = _published_vocabulary(conn, watch_id, term="Green Party")
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=first.id)
    _published_vocabulary(conn, watch_id, term="Labour")

    with pytest.raises(ValueError, match="not 'published'"):
        enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=first.id)
    assert get_civic_run(conn, run.id).vocabulary_version_id == first.id


def test_enqueue_never_overwrites_an_existing_runs_pin(conn, watch_id):
    first = _published_vocabulary(conn, watch_id, term="Green Party")
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=first.id)
    second = _published_vocabulary(conn, watch_id, term="Labour")

    again = enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=second.id)
    assert again.id == run.id
    assert again.vocabulary_version_id == first.id


def test_a_refused_enqueue_leaves_the_transaction_clean(conn, watch_id):
    draft = create_vocabulary_version(conn, watch_id, T0)
    with pytest.raises(ValueError):
        enqueue_civic_run(conn, watch_id, SLOT_A, T0, vocabulary_version_id=draft.id)
    assert not conn.in_transaction
    assert enqueue_civic_run(conn, watch_id, SLOT_A, T0).id is not None


# ============================================================================
# Completion
# ============================================================================


def test_completion_clears_the_lease_and_stamps_completed_at(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    end = T0 + timedelta(minutes=5)
    assert complete_civic_run(conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, end)
    done = get_civic_run(conn, run.id)
    assert done.status is CivicRunStatus.COMPLETED
    assert done.completed_at == end
    assert done.claim_token is None and done.phase is None


def test_completion_is_fenced_on_the_claim(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id)
    assert not complete_civic_run(conn, run.id, "stale", CivicRunStatus.COMPLETED, T0)
    assert get_civic_run(conn, run.id).status is CivicRunStatus.PROCESSING


def test_a_non_terminal_completion_status_is_rejected(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    with pytest.raises(ValueError, match="must be terminal"):
        complete_civic_run(conn, run.id, lease.run.claim_token, CivicRunStatus.PROCESSING, T0)


def test_cancellation_takes_precedence_over_a_requested_completion(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    assert request_cancel_civic_run(conn, run.id)
    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0 + timedelta(minutes=1))
    assert get_civic_run(conn, run.id).status is CivicRunStatus.CANCELLED


def test_cancellation_takes_precedence_over_a_requested_failure_and_clears_the_error(
        conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    request_cancel_civic_run(conn, run.id)
    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.FAILED, T0 + timedelta(minutes=1),
        error_code="no_candidates", error_detail="nothing found")
    cancelled = get_civic_run(conn, run.id)
    assert cancelled.status is CivicRunStatus.CANCELLED
    assert cancelled.error_code is None and cancelled.error_detail is None


def test_a_completion_landing_past_the_deadline_fails_instead(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=600, deadline_seconds=100)
    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED,
        T0 + timedelta(seconds=200))
    failed = get_civic_run(conn, run.id)
    assert failed.status is CivicRunStatus.FAILED
    assert failed.error_code == "deadline_exceeded"


def test_completion_uses_the_lock_held_clock_for_its_deadline_check(conn, watch_id):
    """`now_fn` is the authoritative clock: a caller that sampled `now` before the deadline but
    only reached the write lock afterwards must still fail the Run rather than commit COMPLETED
    on a stale reading."""
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=600, deadline_seconds=100)
    arrived = T0 + timedelta(seconds=200)

    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED,
        T0 + timedelta(seconds=10), now_fn=lambda: arrived)

    failed = get_civic_run(conn, run.id)
    assert failed.status is CivicRunStatus.FAILED
    assert failed.error_code == "deadline_exceeded"
    assert failed.completed_at == arrived


def test_completion_stamps_completed_at_from_the_lock_held_clock(conn, watch_id):
    """The recorded terminal time is when the transition actually committed, not when the caller
    started trying."""
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=600, deadline_seconds=3600)
    committed_at = T0 + timedelta(seconds=45)

    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0,
        now_fn=lambda: committed_at)

    assert get_civic_run(conn, run.id).completed_at == committed_at


def test_completion_now_fn_is_sampled_exactly_once(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=600, deadline_seconds=3600)
    calls = []

    def now_fn():
        calls.append(True)
        return T0 + timedelta(seconds=5)

    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0, now_fn=now_fn)
    assert len(calls) == 1


def test_completion_without_now_fn_uses_the_supplied_now(conn, watch_id):
    """The deterministic single-connection path is unchanged for callers that do not pass a
    clock callback."""
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    end = T0 + timedelta(minutes=3)
    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, end)
    assert get_civic_run(conn, run.id).completed_at == end


def test_cancellation_still_wins_over_the_lock_held_deadline(conn, watch_id):
    """Cancellation-first precedence is evaluated against the same lock-held instant: a Run both
    cancelled and past its deadline commits CANCELLED, never a deadline failure."""
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=600, deadline_seconds=100)
    request_cancel_civic_run(conn, run.id)
    arrived = T0 + timedelta(seconds=200)

    assert complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0,
        now_fn=lambda: arrived)

    cancelled = get_civic_run(conn, run.id)
    assert cancelled.status is CivicRunStatus.CANCELLED
    assert cancelled.error_code is None
    assert cancelled.completed_at == arrived


def test_cancel_flag_cannot_be_set_on_a_terminal_run(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id)
    complete_civic_run(
        conn, run.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0 + timedelta(minutes=1))
    assert not request_cancel_civic_run(conn, run.id)


def test_a_pending_run_can_be_failed_without_ever_being_claimed(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    assert fail_pending_civic_run(conn, run.id, T0, "watch_retired")
    failed = get_civic_run(conn, run.id)
    assert failed.status is CivicRunStatus.FAILED
    assert failed.error_code == "watch_retired"


def test_failing_a_pending_run_never_steals_a_live_claim(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id)
    assert not fail_pending_civic_run(conn, run.id, T0, "watch_retired")
    assert get_civic_run(conn, run.id).status is CivicRunStatus.PROCESSING


# ============================================================================
# Recovery
# ============================================================================


def test_recovery_requeues_an_expired_lease_inside_its_deadline(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    result = recover_expired_civic_runs(conn, T0 + timedelta(seconds=61))
    assert (result.requeued_count, result.deadline_exceeded_count) == (1, 0)
    requeued = get_civic_run(conn, run.id)
    assert requeued.status is CivicRunStatus.PENDING
    assert requeued.phase is None and requeued.claim_token is None
    assert requeued.deadline_at == T0 + timedelta(seconds=3600)
    assert conn.execute(
        "SELECT attempt_count FROM civic_runs WHERE id = ?", (run.id,)).fetchone()[0] == 1


def test_recovery_fails_a_run_whose_deadline_has_also_passed(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id, lease_seconds=60, deadline_seconds=100)
    result = recover_expired_civic_runs(conn, T0 + timedelta(seconds=200))
    assert (result.requeued_count, result.deadline_exceeded_count) == (0, 1)
    failed = get_civic_run(conn, run.id)
    assert failed.status is CivicRunStatus.FAILED
    assert failed.error_code == "deadline_exceeded"


def test_recovery_treats_the_exact_expiry_instant_as_expired(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    result = recover_expired_civic_runs(conn, T0 + timedelta(seconds=60))
    assert result.requeued_count == 1


def test_recovery_leaves_a_live_lease_alone(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    result = recover_expired_civic_runs(conn, T0 + timedelta(seconds=59))
    assert (result.requeued_count, result.deadline_exceeded_count) == (0, 0)
    assert get_civic_run(conn, run.id).status is CivicRunStatus.PROCESSING


def test_a_requeued_run_is_offered_by_the_pending_queue_again(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    assert list_pending_civic_runs(conn) == []
    recover_expired_civic_runs(conn, T0 + timedelta(seconds=61))
    assert [r.id for r in list_pending_civic_runs(conn)] == [run.id]


def test_a_stale_worker_can_write_nothing_after_being_recovered(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, SLOT_A, T0)
    lease = _claimed(conn, run.id, lease_seconds=60, deadline_seconds=3600)
    stale_token = lease.run.claim_token
    recover_expired_civic_runs(conn, T0 + timedelta(seconds=61))
    fresh = claim_civic_run(conn, run.id, T0 + timedelta(seconds=61), 60, 3600)
    assert fresh.run.claim_token != stale_token

    later = T0 + timedelta(seconds=70)
    assert not heartbeat_civic_run(conn, run.id, stale_token, later, 60)
    assert not advance_civic_run_phase(conn, run.id, stale_token, CivicRunPhase.PUBLISHING)
    assert not complete_civic_run(conn, run.id, stale_token, CivicRunStatus.COMPLETED, later)
    assert get_civic_run(conn, run.id).status is CivicRunStatus.PROCESSING


def test_pending_queue_is_oldest_slot_first(conn):
    for index, slot in enumerate([SLOT_B, SLOT_A]):
        watch = create_civic_watch(conn, f"watch-{index}", "Q", T0).id
        enqueue_civic_run(conn, watch, slot, T0)
    assert [r.slot_at for r in list_pending_civic_runs(conn)] == [SLOT_A, SLOT_B]
