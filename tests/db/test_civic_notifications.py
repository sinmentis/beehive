# tests/db/test_civic_notifications.py
"""Civic Notification persistence: idempotent queueing, a send lease with a bounded retry
budget, and a recovery sweep -- all independent of the Civic Batch delivery path."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_notifications import (claim_notification, get_notification,
                                             list_sendable_notifications,
                                             mark_notification_sent, queue_notification,
                                             recover_expired_notifications,
                                             release_notification_claim, suppress_notification)
from beehive.db.civic_runs import enqueue_civic_run
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicNotificationStatus

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


def _queued(conn, watch_id, dedupe_key="run-1", **overrides):
    kwargs = dict(kind="run_failed", subject="Civic run failed")
    kwargs.update(overrides)
    return queue_notification(conn, watch_id, dedupe_key=dedupe_key, now=T0, **kwargs)


# ============================================================================
# Queueing
# ============================================================================


def test_queueing_creates_a_pending_notification(conn, watch_id):
    notification = _queued(conn, watch_id)
    assert notification.status is CivicNotificationStatus.PENDING
    assert notification.attempt_count == 0
    assert notification.settled_at is None
    assert notification.claim_token is None


def test_queueing_the_same_key_twice_returns_the_same_row(conn, watch_id):
    first = _queued(conn, watch_id, subject="First")
    second = _queued(conn, watch_id, subject="Second")
    assert first.id == second.id
    assert second.subject == "First"


def test_an_already_sent_notification_is_never_requeued_by_the_same_key(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    mark_notification_sent(conn, notification.id, lease.notification.claim_token, T0)
    again = _queued(conn, watch_id)
    assert again.id == notification.id
    assert again.status is CivicNotificationStatus.SENT


def test_two_watches_may_use_the_same_dedupe_key(conn):
    first = create_civic_watch(conn, "A", "Q", T0).id
    second = create_civic_watch(conn, "B", "Q", T0).id
    assert _queued(conn, first).id != _queued(conn, second).id


def test_an_empty_dedupe_key_is_rejected(conn, watch_id):
    with pytest.raises(ValueError, match="dedupe_key"):
        _queued(conn, watch_id, dedupe_key="  ")


def test_queueing_for_a_missing_watch_raises(conn):
    with pytest.raises(ValueError, match="no Civic Watch"):
        _queued(conn, 999)


def test_a_notification_may_reference_its_run(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    notification = _queued(conn, watch_id, run_id=run.id)
    assert notification.run_id == run.id


def test_deleting_a_run_keeps_its_notification(conn, watch_id):
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    notification = _queued(conn, watch_id, run_id=run.id)
    conn.execute("DELETE FROM civic_runs WHERE id = ?", (run.id,))
    conn.commit()
    assert get_notification(conn, notification.id).run_id is None


# ============================================================================
# Claiming and sending
# ============================================================================


def test_claiming_spends_an_attempt_up_front(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    assert lease.notification.status is CivicNotificationStatus.SENDING
    assert lease.attempt_count == 1
    assert lease.lease_expires_at == (T0 + timedelta(seconds=60)).isoformat()
    assert claim_notification(conn, notification.id, T0, 60) is None


def test_only_pending_notifications_are_listed(conn, watch_id):
    notification = _queued(conn, watch_id)
    assert [n.id for n in list_sendable_notifications(conn, T0)] == [notification.id]
    claim_notification(conn, notification.id, T0, 60)
    assert list_sendable_notifications(conn, T0) == []


def test_marking_sent_terminalizes_and_is_fenced_on_the_claim(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    assert not mark_notification_sent(conn, notification.id, "stale", T0)
    assert mark_notification_sent(conn, notification.id, lease.notification.claim_token, T0)
    sent = get_notification(conn, notification.id)
    assert sent.status is CivicNotificationStatus.SENT
    assert sent.settled_at == T0
    assert sent.claim_token is None


def test_release_returns_it_to_the_queue_with_a_backoff(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    released = release_notification_claim(
        conn, notification.id, lease.notification.claim_token, T0, retry_after_seconds=300,
        error_code="smtp_unavailable")
    assert released.status is CivicNotificationStatus.PENDING
    assert released.next_attempt_at == T0 + timedelta(seconds=300)
    assert released.error_code == "smtp_unavailable"
    assert list_sendable_notifications(conn, T0 + timedelta(seconds=299)) == []
    assert claim_notification(conn, notification.id, T0 + timedelta(seconds=299), 60) is None
    assert claim_notification(conn, notification.id, T0 + timedelta(seconds=300), 60) is not None


def test_release_is_fenced_on_the_claim(conn, watch_id):
    notification = _queued(conn, watch_id)
    claim_notification(conn, notification.id, T0, 60)
    assert release_notification_claim(
        conn, notification.id, "stale", T0, retry_after_seconds=0, error_code="x") is None
    assert get_notification(conn, notification.id).status is CivicNotificationStatus.SENDING


def test_the_retry_budget_is_bounded(conn, watch_id):
    notification = _queued(conn, watch_id, max_attempts=2)
    for _ in range(2):
        lease = claim_notification(conn, notification.id, T0, 60)
        released = release_notification_claim(
            conn, notification.id, lease.notification.claim_token, T0, retry_after_seconds=0,
            error_code="smtp_unavailable")
    assert released.status is CivicNotificationStatus.FAILED
    assert released.settled_at == T0
    assert claim_notification(conn, notification.id, T0, 60) is None


def test_a_notification_at_its_cap_is_failed_rather_than_claimed(conn, watch_id):
    notification = _queued(conn, watch_id, max_attempts=1)
    conn.execute(
        "UPDATE civic_notifications SET attempt_count = 1 WHERE id = ?", (notification.id,))
    conn.commit()
    assert claim_notification(conn, notification.id, T0, 60) is None
    failed = get_notification(conn, notification.id)
    assert failed.status is CivicNotificationStatus.FAILED
    assert failed.error_code == "max_attempts_exhausted"


def test_now_fn_is_sampled_after_the_write_lock_is_held(conn, watch_id):
    notification = _queued(conn, watch_id)
    late = T0 + timedelta(seconds=500)
    lease = claim_notification(conn, notification.id, T0, 60, now_fn=lambda: late)
    assert lease.lease_expires_at == (late + timedelta(seconds=60)).isoformat()


# ============================================================================
# Suppression
# ============================================================================


def test_a_pending_notification_can_be_suppressed(conn, watch_id):
    notification = _queued(conn, watch_id)
    suppressed = suppress_notification(conn, notification.id, T0, reason="watch_paused")
    assert suppressed.status is CivicNotificationStatus.SUPPRESSED
    assert suppressed.settled_at == T0
    assert list_sendable_notifications(conn, T0) == []


def test_a_sending_notification_can_be_suppressed_and_the_claim_is_dropped(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    suppressed = suppress_notification(conn, notification.id, T0, reason="resolved")
    assert suppressed.status is CivicNotificationStatus.SUPPRESSED
    assert suppressed.claim_token is None
    assert not mark_notification_sent(conn, notification.id, lease.notification.claim_token, T0)


def test_a_sent_notification_can_never_be_suppressed(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    mark_notification_sent(conn, notification.id, lease.notification.claim_token, T0)
    with pytest.raises(ValueError, match="invalid Civic Notification transition"):
        suppress_notification(conn, notification.id, T0, reason="too late")


def test_suppressing_a_missing_notification_returns_none(conn):
    assert suppress_notification(conn, 999, T0, reason="x") is None


# ============================================================================
# Recovery
# ============================================================================


def test_recovery_releases_an_abandoned_send(conn, watch_id):
    notification = _queued(conn, watch_id)
    claim_notification(conn, notification.id, T0, 60)
    result = recover_expired_notifications(conn, T0 + timedelta(seconds=61))
    assert (result.released_count, result.failed_count) == (1, 0)
    released = get_notification(conn, notification.id)
    assert released.status is CivicNotificationStatus.PENDING
    assert released.attempt_count == 1
    assert released.error_code == "send_lease_expired"


def test_recovery_treats_the_exact_expiry_instant_as_expired(conn, watch_id):
    notification = _queued(conn, watch_id)
    claim_notification(conn, notification.id, T0, 60)
    assert recover_expired_notifications(conn, T0 + timedelta(seconds=60)).released_count == 1


def test_recovery_leaves_a_live_send_alone(conn, watch_id):
    notification = _queued(conn, watch_id)
    claim_notification(conn, notification.id, T0, 60)
    result = recover_expired_notifications(conn, T0 + timedelta(seconds=59))
    assert (result.released_count, result.failed_count) == (0, 0)
    assert get_notification(conn, notification.id).status is CivicNotificationStatus.SENDING


def test_recovery_fails_an_abandoned_send_that_exhausted_its_budget(conn, watch_id):
    notification = _queued(conn, watch_id, max_attempts=1)
    claim_notification(conn, notification.id, T0, 60)
    result = recover_expired_notifications(conn, T0 + timedelta(seconds=61))
    assert (result.released_count, result.failed_count) == (0, 1)
    assert get_notification(conn, notification.id).status is CivicNotificationStatus.FAILED


def test_a_recovered_worker_can_no_longer_mark_it_sent(conn, watch_id):
    notification = _queued(conn, watch_id)
    lease = claim_notification(conn, notification.id, T0, 60)
    stale_token = lease.notification.claim_token
    recover_expired_notifications(conn, T0 + timedelta(seconds=61))
    fresh = claim_notification(conn, notification.id, T0 + timedelta(seconds=61), 60)
    assert fresh.notification.claim_token != stale_token
    assert not mark_notification_sent(conn, notification.id, stale_token, T0)
    assert get_notification(conn, notification.id).status is CivicNotificationStatus.SENDING
