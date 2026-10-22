"""Email delivery for queued Civic Notifications.

Civic Runs queue operational notices (source failures, deferred hydration, cluster overflow).
They go out as one combined email at most every DIGEST_INTERVAL, so a daily Run's three notices
arrive together instead of as separate messages. Notices older than STALE_AFTER are suppressed
rather than sent: a backlog of old operational state is noise, not news.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from beehive.db import app_state
from beehive.db.civic_notifications import (
    claim_notification,
    list_sendable_notifications,
    mark_notification_sent,
    release_notification_claim,
    suppress_notification,
)
from beehive.email_routing import ResolvedRecipient
from beehive.localization import Localizer
from beehive.notify import Notifier

DIGEST_INTERVAL = timedelta(hours=6)
STALE_AFTER = timedelta(hours=48)
MAX_NOTICES_PER_EMAIL = 25
LEASE_SECONDS = 300
RETRY_AFTER_SECONDS = 15 * 60
LAST_SENT_KEY = "civic_notifications_last_sent_at"


class CivicNotificationDeliveryError(RuntimeError):
    def __init__(self, count: int, error: Exception):
        super().__init__(f"Civic notification email ({count} notices) failed: {error}")
        self.error = error


def send_civic_notifications(
    conn: sqlite3.Connection,
    notifier: Notifier,
    recipient: ResolvedRecipient,
    localizer: Localizer,
    *,
    now: datetime | None = None,
) -> int:
    """Send every sendable notice in one email. Returns how many were sent."""
    if recipient.address is None:
        return 0
    now = now or datetime.now(timezone.utc)
    pending = list_sendable_notifications(conn, now, limit=MAX_NOTICES_PER_EMAIL * 4)
    fresh = []
    for notification in pending:
        if now - notification.queued_at > STALE_AFTER:
            suppress_notification(conn, notification.id, now, reason="stale")
        else:
            fresh.append(notification)
    if not fresh:
        return 0
    last_sent = app_state.get(conn, LAST_SENT_KEY)
    if last_sent is not None and now - datetime.fromisoformat(last_sent) < DIGEST_INTERVAL:
        return 0

    leases = []
    for notification in fresh[:MAX_NOTICES_PER_EMAIL]:
        lease = claim_notification(conn, notification.id, now, LEASE_SECONDS)
        if lease is not None:
            leases.append(lease)
    if not leases:
        return 0

    product = localizer.text("common.product_name")
    subject = localizer.text(
        "background.civic_notices_subject", count=len(leases), product=product)
    body = "\n\n".join(
        f"{lease.notification.subject}\n{lease.notification.body}".strip()
        for lease in leases)
    try:
        notifier.send(subject, body, to_addr=recipient.address)
    except Exception as exc:
        for lease in leases:
            release_notification_claim(
                conn, lease.notification.id, lease.notification.claim_token, now,
                retry_after_seconds=RETRY_AFTER_SECONDS, error_code="delivery_failed",
                error_detail=str(exc)[:500])
        raise CivicNotificationDeliveryError(len(leases), exc) from exc
    for lease in leases:
        mark_notification_sent(conn, lease.notification.id, lease.notification.claim_token, now)
    app_state.set(conn, LAST_SENT_KEY, now.isoformat())
    return len(leases)
