"""Civic Watch persistence: the Owner's recurring commitment to one civic subject.

One row per Watch, and `subject` is its identity -- the same civic subject can never be
monitored twice in parallel under two competing schedules, so `create_civic_watch` raises rather
than silently creating a near-duplicate.

Status changes go through domain.civic.require_watch_transition, so an illegal move (most
importantly reviving a RETIRED Watch) fails here with a clear error rather than being caught
only by the schema. The paused_at/retired_at stamps are kept in lockstep with the status in the
same UPDATE, matching the CHECK constraints on civic_watches and CivicWatch's own invariants:
resuming a paused Watch clears paused_at, and retiring one clears paused_at while setting
retired_at, so a retired Watch is never observably "paused and retired" at once.

Retiring is deliberately terminal and deliberately non-destructive: the Watch's Runs, Batches,
Candidates and receipts all remain, because they are the published record. Only
`hard_delete_civic_watch` removes them, and it relies entirely on ON DELETE CASCADE across the
civic_* family."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.db.connection import write_transaction
from beehive.domain.civic import CivicWatch, CivicWatchStatus, require_watch_transition

_DEFAULT_SCHEDULE_TIMEZONE = "Pacific/Auckland"
_DEFAULT_SCHEDULE_TIME = "06:30"


def _row_to_watch(row: sqlite3.Row) -> CivicWatch:
    return CivicWatch(
        id=row["id"],
        subject=row["subject"],
        question=row["question"],
        status=CivicWatchStatus(row["status"]),
        schedule_timezone=row["schedule_timezone"],
        schedule_time=row["schedule_time"],
        created_at=datetime.fromisoformat(row["created_at"]),
        paused_at=datetime.fromisoformat(row["paused_at"]) if row["paused_at"] else None,
        retired_at=datetime.fromisoformat(row["retired_at"]) if row["retired_at"] else None)


def create_civic_watch(
    conn: sqlite3.Connection,
    subject: str,
    question: str,
    now: datetime,
    *,
    schedule_timezone: str = _DEFAULT_SCHEDULE_TIMEZONE,
    schedule_time: str = _DEFAULT_SCHEDULE_TIME,
) -> CivicWatch:
    """Creates an active Civic Watch. Validates subject/question/schedule through CivicWatch
    itself before writing, so a rejected shape never reaches the database, and raises ValueError
    on a duplicate subject rather than surfacing a raw IntegrityError."""
    CivicWatch(
        id=None, subject=subject, question=question, status=CivicWatchStatus.ACTIVE,
        schedule_timezone=schedule_timezone, schedule_time=schedule_time, created_at=now)
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT id FROM civic_watches WHERE subject = ?", (subject,)).fetchone()
        if existing is not None:
            raise ValueError(f"a Civic Watch for subject {subject!r} already exists")
        cur = conn.execute(
            "INSERT INTO civic_watches (subject, question, status, schedule_timezone, "
            "schedule_time, created_at) VALUES (?, ?, 'active', ?, ?, ?)",
            (subject, question, schedule_timezone, schedule_time, now.isoformat()))
        watch_id = cur.lastrowid
    return get_civic_watch(conn, watch_id)


def get_civic_watch(conn: sqlite3.Connection, watch_id: int) -> CivicWatch | None:
    row = conn.execute("SELECT * FROM civic_watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_watch(row) if row else None


def get_civic_watch_by_subject(conn: sqlite3.Connection, subject: str) -> CivicWatch | None:
    row = conn.execute("SELECT * FROM civic_watches WHERE subject = ?", (subject,)).fetchone()
    return _row_to_watch(row) if row else None


def list_civic_watches(
    conn: sqlite3.Connection, status: CivicWatchStatus | None = None,
) -> list[CivicWatch]:
    if status is None:
        rows = conn.execute("SELECT * FROM civic_watches ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM civic_watches WHERE status = ? ORDER BY id",
            (status.value,)).fetchall()
    return [_row_to_watch(row) for row in rows]


def list_schedulable_civic_watches(conn: sqlite3.Connection) -> list[CivicWatch]:
    """Only ACTIVE Watches are ever offered to the scheduler: pausing must stop new slots being
    served immediately, without touching any Run already in flight."""
    return list_civic_watches(conn, CivicWatchStatus.ACTIVE)


def set_civic_watch_status(
    conn: sqlite3.Connection, watch_id: int, target: CivicWatchStatus, now: datetime,
) -> CivicWatch:
    """Transitions a Watch, validating the move against domain.civic's transition table under the
    same BEGIN IMMEDIATE that performs the write -- so two racing status changes can never both
    read 'active' and both apply. Raises ValueError for a missing Watch or an illegal move."""
    with write_transaction(conn):
        row = conn.execute(
            "SELECT status FROM civic_watches WHERE id = ?", (watch_id,)).fetchone()
        if row is None:
            raise ValueError(f"no Civic Watch with id={watch_id}")
        require_watch_transition(CivicWatchStatus(row["status"]), target)
        now_iso = now.isoformat()
        paused_at = now_iso if target is CivicWatchStatus.PAUSED else None
        retired_at = now_iso if target is CivicWatchStatus.RETIRED else None
        conn.execute(
            "UPDATE civic_watches SET status = ?, paused_at = ?, retired_at = ? WHERE id = ?",
            (target.value, paused_at, retired_at, watch_id))
    return get_civic_watch(conn, watch_id)


def pause_civic_watch(conn: sqlite3.Connection, watch_id: int, now: datetime) -> CivicWatch:
    return set_civic_watch_status(conn, watch_id, CivicWatchStatus.PAUSED, now)


def resume_civic_watch(conn: sqlite3.Connection, watch_id: int, now: datetime) -> CivicWatch:
    return set_civic_watch_status(conn, watch_id, CivicWatchStatus.ACTIVE, now)


def retire_civic_watch(conn: sqlite3.Connection, watch_id: int, now: datetime) -> CivicWatch:
    """Permanently ends a Watch. Deliberately non-destructive: every Run, Batch, Candidate and
    receipt survives, because they are the published record."""
    return set_civic_watch_status(conn, watch_id, CivicWatchStatus.RETIRED, now)


def hard_delete_civic_watch(conn: sqlite3.Connection, watch_id: int) -> bool:
    """Removes a Watch and, by ON DELETE CASCADE, its entire civic_* subtree. This is also how
    an in-flight worker claim is revoked: the run row a claim_token would be validated against
    simply ceases to exist, so every later heartbeat/complete call matches zero rows and reports
    failure instead of silently succeeding."""
    cur = conn.execute("DELETE FROM civic_watches WHERE id = ?", (watch_id,))
    conn.commit()
    return cur.rowcount > 0
