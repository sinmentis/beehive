from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from beehive.db.research_notifications import (
    list_pending_completion_notifications,
    mark_completion_notification_attempted,
    mark_completion_notification_sent,
)
from beehive.email_routing import ResolvedRecipient
from beehive.localization import Localizer
from beehive.notify import Notifier


class ResearchCompletionDeliveryError(RuntimeError):
    def __init__(self, run_id: int, error: Exception):
        super().__init__(f"Research completion email for run {run_id} failed: {error}")
        self.run_id = run_id
        self.error = error


def send_research_completion_notifications(
    conn: sqlite3.Connection,
    notifier: Notifier,
    recipient: ResolvedRecipient,
    localizer: Localizer,
    *,
    now: datetime | None = None,
) -> int:
    if recipient.address is None:
        return 0
    sent_at = (now or datetime.now(timezone.utc)).isoformat()
    sent_count = 0
    failures: list[Exception] = []
    for pending in list_pending_completion_notifications(conn):
        mark_completion_notification_attempted(conn, pending["run_id"], sent_at)
        subject = localizer.text(
            "background.research_complete_subject",
            question=pending["question"],
        )
        body = localizer.text(
            "background.research_complete_body",
            question=pending["question"],
            session_id=pending["session_id"],
        )
        try:
            notifier.send(subject, body, to_addr=recipient.address)
        except Exception as exc:
            failures.append(ResearchCompletionDeliveryError(pending["run_id"], exc))
            continue
        mark_completion_notification_sent(conn, pending["run_id"], sent_at)
        sent_count += 1
    if failures:
        raise ExceptionGroup("One or more Research completion emails failed", failures)
    return sent_count
