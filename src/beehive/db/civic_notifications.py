"""Civic Notification persistence: Owner-facing messages about a Civic Watch's own operation.

Deliberately a separate queue from the Civic Batch delivery path, with its own lease and its own
retry budget, so a failing mail provider can never stall, duplicate, or block a publication. A
Notification failing permanently is an operational annoyance; a Batch failing permanently is a
missed publication -- coupling them would let the smaller problem cause the larger one.

`dedupe_key` is unique per Watch, which makes queueing idempotent: a worker that retries the
"run failed" path after a crash re-queues the same key and gets the existing row back rather
than sending twice. The key is the caller's to choose (typically run id + kind), so "one message
per run per kind" is expressible without this module knowing what any kind means.

The lease shape is identical to a Civic Batch delivery claim: claim -> SENDING with a bounded
lease and attempt_count spent up front, every later write fenced on (id, claim_token,
status='sending'), release back to PENDING with a backoff while attempts remain, terminal FAILED
once they are exhausted, and a recovery sweep that reconciles abandoned claims."""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.domain.civic import (CivicNotification, CivicNotificationStatus,
                                   is_terminal_notification_status,
                                   require_notification_transition)

_DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class CivicNotificationLease:
    notification: CivicNotification
    lease_expires_at: str
    attempt_count: int


@dataclass(frozen=True)
class CivicNotificationRecoveryResult:
    released_count: int
    failed_count: int


def _row_to_notification(row: sqlite3.Row) -> CivicNotification:
    return CivicNotification(
        id=row["id"],
        watch_id=row["watch_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        dedupe_key=row["dedupe_key"],
        subject=row["subject"],
        body=row["body"],
        status=CivicNotificationStatus(row["status"]),
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        queued_at=datetime.fromisoformat(row["queued_at"]),
        claim_token=row["claim_token"],
        settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
        next_attempt_at=(
            datetime.fromisoformat(row["next_attempt_at"]) if row["next_attempt_at"] else None),
        error_code=row["error_code"],
        error_detail=row["error_detail"])


def _fetch(conn: sqlite3.Connection, notification_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM civic_notifications WHERE id = ?", (notification_id,)).fetchone()


def queue_notification(
    conn: sqlite3.Connection,
    watch_id: int,
    kind: str,
    dedupe_key: str,
    subject: str,
    now: datetime,
    *,
    run_id: int | None = None,
    body: str = "",
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> CivicNotification:
    """Queues one Notification, or returns the Watch's existing one for the same dedupe_key.

    Idempotent by design: a worker retrying the same operational path after a crash presents the
    same key and gets the already-queued (or already-sent) row back, so the Owner is never
    notified twice about one event. Raises ValueError for an unknown Watch."""
    if not dedupe_key.strip():
        raise ValueError("Civic Notification needs a dedupe_key")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
                "SELECT 1 FROM civic_watches WHERE id = ?", (watch_id,)).fetchone() is None:
            raise ValueError(f"no Civic Watch with id={watch_id}")
        existing = conn.execute(
            "SELECT * FROM civic_notifications WHERE watch_id = ? AND dedupe_key = ?",
            (watch_id, dedupe_key)).fetchone()
        if existing is not None:
            notification = _row_to_notification(existing)
        else:
            cur = conn.execute(
                "INSERT INTO civic_notifications (watch_id, run_id, kind, dedupe_key, subject, "
                "body, status, max_attempts, queued_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (watch_id, run_id, kind, dedupe_key, subject, body, max_attempts,
                 now.isoformat()))
            notification = _row_to_notification(_fetch(conn, cur.lastrowid))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return notification


def get_notification(
    conn: sqlite3.Connection, notification_id: int,
) -> CivicNotification | None:
    row = _fetch(conn, notification_id)
    return _row_to_notification(row) if row else None


def list_sendable_notifications(
    conn: sqlite3.Connection, now: datetime, limit: int = 25,
) -> list[CivicNotification]:
    """PENDING Notifications whose backoff (if any) has elapsed, oldest first."""
    rows = conn.execute(
        "SELECT * FROM civic_notifications WHERE status = 'pending' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?",
        (now.isoformat(), limit)).fetchall()
    return [_row_to_notification(row) for row in rows]


def claim_notification(
    conn: sqlite3.Connection,
    notification_id: int,
    now: datetime,
    lease_seconds: int,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicNotificationLease | None:
    """Claims one PENDING Notification for sending, spending an attempt up front so a worker that
    dies mid-send leaks exactly one attempt. Returns None when the row is not PENDING, is still
    inside its backoff, or has exhausted max_attempts -- in which case it is atomically
    terminalized FAILED rather than left looking claimable."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        now_iso = now.isoformat()
        row = conn.execute(
            "SELECT attempt_count, max_attempts, next_attempt_at FROM civic_notifications "
            "WHERE id = ? AND status = 'pending'", (notification_id,)).fetchone()
        if row is None:
            result = None
        elif (row["next_attempt_at"] is not None
                and datetime.fromisoformat(row["next_attempt_at"]) > now):
            result = None
        elif row["attempt_count"] >= row["max_attempts"]:
            conn.execute(
                "UPDATE civic_notifications SET status = 'failed', settled_at = ?, "
                "error_code = 'max_attempts_exhausted' WHERE id = ? AND status = 'pending'",
                (now_iso, notification_id))
            result = None
        else:
            claim_token = secrets.token_urlsafe(32)
            cur = conn.execute(
                "UPDATE civic_notifications SET status = 'sending', claim_token = ?, "
                "lease_expires_at = ?, attempt_count = attempt_count + 1, next_attempt_at = NULL "
                "WHERE id = ? AND status = 'pending'",
                (claim_token, (now + timedelta(seconds=lease_seconds)).isoformat(),
                 notification_id))
            if cur.rowcount == 0:
                result = None
            else:
                claimed = _fetch(conn, notification_id)
                result = CivicNotificationLease(
                    notification=_row_to_notification(claimed),
                    lease_expires_at=claimed["lease_expires_at"],
                    attempt_count=claimed["attempt_count"])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def mark_notification_sent(
    conn: sqlite3.Connection, notification_id: int, claim_token: str, now: datetime,
) -> bool:
    """Terminalizes a SENDING Notification as SENT, fenced on the claim. Returns False, writing
    nothing, when the claim no longer matches -- so a worker whose lease was reclaimed can never
    mark a message sent that another worker is now handling."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_notifications "
            "WHERE id = ? AND claim_token = ? AND status = 'sending'",
            (notification_id, claim_token)).fetchone()
        if row is None:
            ok = False
        else:
            require_notification_transition(
                CivicNotificationStatus(row["status"]), CivicNotificationStatus.SENT)
            cur = conn.execute(
                "UPDATE civic_notifications SET status = 'sent', claim_token = NULL, "
                "lease_expires_at = NULL, settled_at = ?, error_code = NULL, error_detail = NULL "
                "WHERE id = ? AND claim_token = ? AND status = 'sending'",
                (now.isoformat(), notification_id, claim_token))
            ok = cur.rowcount > 0
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ok


def release_notification_claim(
    conn: sqlite3.Connection,
    notification_id: int,
    claim_token: str,
    now: datetime,
    *,
    retry_after_seconds: int,
    error_code: str,
    error_detail: str | None = None,
) -> CivicNotification | None:
    """Hands a failed send back, fenced on the claim: PENDING with a backoff while attempts
    remain, terminal FAILED once they are exhausted."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, attempt_count, max_attempts FROM civic_notifications "
            "WHERE id = ? AND claim_token = ? AND status = 'sending'",
            (notification_id, claim_token)).fetchone()
        if row is None:
            released = None
        else:
            exhausted = row["attempt_count"] >= row["max_attempts"]
            target = (CivicNotificationStatus.FAILED if exhausted
                      else CivicNotificationStatus.PENDING)
            require_notification_transition(CivicNotificationStatus(row["status"]), target)
            now_iso = now.isoformat()
            if exhausted:
                conn.execute(
                    "UPDATE civic_notifications SET status = 'failed', claim_token = NULL, "
                    "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                    "error_code = ?, error_detail = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'sending'",
                    (now_iso, error_code, error_detail, notification_id, claim_token))
            else:
                conn.execute(
                    "UPDATE civic_notifications SET status = 'pending', claim_token = NULL, "
                    "lease_expires_at = NULL, next_attempt_at = ?, error_code = ?, "
                    "error_detail = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'sending'",
                    ((now + timedelta(seconds=retry_after_seconds)).isoformat(), error_code,
                     error_detail, notification_id, claim_token))
            released = _row_to_notification(_fetch(conn, notification_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return released


def suppress_notification(
    conn: sqlite3.Connection, notification_id: int, now: datetime, *, reason: str,
) -> CivicNotification | None:
    """Settles a Notification as SUPPRESSED without sending it -- e.g. its Watch was paused, or
    the condition it describes resolved before anyone was told. Terminal, so a suppressed message
    is never resurrected by a later sweep."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, claim_token FROM civic_notifications WHERE id = ?",
            (notification_id,)).fetchone()
        if row is None:
            suppressed = None
        else:
            require_notification_transition(
                CivicNotificationStatus(row["status"]), CivicNotificationStatus.SUPPRESSED)
            conn.execute(
                "UPDATE civic_notifications SET status = 'suppressed', claim_token = NULL, "
                "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                "error_code = ? WHERE id = ?", (now.isoformat(), reason, notification_id))
            suppressed = _row_to_notification(_fetch(conn, notification_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return suppressed


def recover_expired_notifications(
    conn: sqlite3.Connection, now: datetime, *, retry_after_seconds: int = 0,
) -> CivicNotificationRecoveryResult:
    """Reconciliation sweep over SENDING rows whose lease has expired (lease_expires_at <= now --
    equality IS expired). Attempts remaining -> back to PENDING with a backoff; exhausted ->
    terminal FAILED. attempt_count is not incremented here: it was already spent at claim time."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now_iso = now.isoformat()
        failed = conn.execute(
            "UPDATE civic_notifications SET status = 'failed', claim_token = NULL, "
            "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
            "error_code = 'send_lease_expired' "
            "WHERE status = 'sending' AND lease_expires_at <= ? AND attempt_count >= max_attempts",
            (now_iso, now_iso))
        released = conn.execute(
            "UPDATE civic_notifications SET status = 'pending', claim_token = NULL, "
            "lease_expires_at = NULL, next_attempt_at = ?, error_code = 'send_lease_expired' "
            "WHERE status = 'sending' AND lease_expires_at <= ? AND attempt_count < max_attempts",
            ((now + timedelta(seconds=retry_after_seconds)).isoformat(), now_iso))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return CivicNotificationRecoveryResult(
        released_count=released.rowcount, failed_count=failed.rowcount)


def is_terminal(notification: CivicNotification) -> bool:
    return is_terminal_notification_status(notification.status)
