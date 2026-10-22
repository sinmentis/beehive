from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.notifications import (
    DIGEST_INTERVAL,
    STALE_AFTER,
    CivicNotificationDeliveryError,
    send_civic_notifications,
)
from beehive.db.civic_notifications import get_notification, queue_notification
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicNotificationStatus
from beehive.email_routing import ResolvedRecipient
from beehive.localization import localizer_for
from beehive.notify import Notifier

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
RECIPIENT = ResolvedRecipient(address="owner@example.com", source="environment")


class _Notifier(Notifier):
    def __init__(self, error: Exception | None = None):
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def send(self, subject, plain_text, html=None, *, to_addr=None):
        if self.error is not None:
            raise self.error
        self.sent.append((subject, plain_text))


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "notices.db"))
    init_schema(connection)
    yield connection
    connection.close()


def _queue(conn, key, *, at=T0):
    watch = create_civic_watch(conn, f"watch {key}", "Q", at)
    return queue_notification(
        conn, watch.id, "civic_source_failures", key, f"Subject {key}", at, body=f"Body {key}")


def _send(conn, notifier, now):
    return send_civic_notifications(conn, notifier, RECIPIENT, localizer_for("en"), now=now)


def test_pending_notices_go_out_together_in_one_email(conn):
    first, second = _queue(conn, "a"), _queue(conn, "b")
    notifier = _Notifier()

    assert _send(conn, notifier, T0) == 2

    assert len(notifier.sent) == 1
    subject, body = notifier.sent[0]
    assert "2 notices" in subject
    assert "Subject a" in body and "Body b" in body
    for notice in (first, second):
        assert get_notification(conn, notice.id).status is CivicNotificationStatus.SENT


def test_new_notices_wait_for_the_digest_interval(conn):
    _queue(conn, "a")
    notifier = _Notifier()
    _send(conn, notifier, T0)
    later = _queue(conn, "b", at=T0 + timedelta(hours=1))

    assert _send(conn, notifier, T0 + timedelta(hours=1)) == 0
    assert get_notification(conn, later.id).status is CivicNotificationStatus.PENDING
    assert _send(conn, notifier, T0 + DIGEST_INTERVAL) == 1
    assert len(notifier.sent) == 2


def test_stale_notices_are_suppressed_not_sent(conn):
    old = _queue(conn, "old", at=T0 - STALE_AFTER - timedelta(minutes=1))
    notifier = _Notifier()

    assert _send(conn, notifier, T0) == 0

    assert notifier.sent == []
    assert get_notification(conn, old.id).status is CivicNotificationStatus.SUPPRESSED


def test_a_failed_send_returns_the_notices_for_a_later_retry(conn):
    notice = _queue(conn, "a")

    with pytest.raises(CivicNotificationDeliveryError):
        _send(conn, _Notifier(RuntimeError("provider down")), T0)

    retried = get_notification(conn, notice.id)
    assert retried.status is CivicNotificationStatus.PENDING
    assert retried.next_attempt_at is not None
