# src/beehive/db/summary_rewrites.py
"""Repository for schema.sql's summary_rewrite_log table -- the audit trail behind
collector/summary_rewrite.py's idempotent-rerun and rollback guarantees.

`was_migrated` is what a run consults BEFORE spending an LLM call on a candidate: if a row
already exists for (run_id, item_id), a resumed or re-invoked run with that same run_id skips
straight past it rather than re-summarizing (and re-billing) something it already rewrote.

`apply_summary_rewrite` is the single atomic seam for actually applying a rewrite: it performs
the items.ai_summary UPDATE (re-checking unread/high-water eligibility) and the corresponding
summary_rewrite_log INSERT in ONE transaction (one conn.commit()), so a crash, an unrelated
exception, or a failure in the log INSERT itself between the two statements can never leave a
rewritten ai_summary with no matching audit/rollback row -- either both persist together, or
the except/rollback below undoes the UPDATE too. Earlier revisions of this module split those
two writes across separate committing functions (items.update_ai_summary_if_unread then a
since-removed record_rewrite helper here), which is exactly the gap this closes: a crash (or
any exception) landing between those two independent commits used to leave a live rewritten
summary with no log row to resume from or roll back to. It is also idempotent per
(run_id, item_id): a duplicate/concurrent call for an item already logged under this run_id is
a clean no-op, touching neither items nor the log again, so it can never overwrite the
original previous_summary or double-apply the same rewrite.

Rollback uses `revert_summary_rewrite_entry`, which restores an unchanged summary and consumes its
log entry in the same transaction. Entries whose live summary changed later remain available for
an ordered retry after the later change has itself been rolled back."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SummaryRewriteLogEntry:
    id: int
    run_id: str
    item_id: int
    previous_summary: str
    replacement_summary: str
    migrated_at: str


def _row_to_entry(row: sqlite3.Row) -> SummaryRewriteLogEntry:
    return SummaryRewriteLogEntry(
        id=row["id"],
        run_id=row["run_id"],
        item_id=row["item_id"],
        previous_summary=row["previous_summary"],
        replacement_summary=row["replacement_summary"],
        migrated_at=row["migrated_at"])


def was_migrated(conn: sqlite3.Connection, run_id: str, item_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM summary_rewrite_log WHERE run_id = ? AND item_id = ?",
        (run_id, item_id)).fetchone()
    return row is not None


def apply_summary_rewrite(conn: sqlite3.Connection, run_id: str, item_id: int,
                           replacement_summary: str, high_water_item_id: int,
                           now: datetime) -> str | None:
    """Atomically applies one rewrite: re-checks eligibility (still unread, still
    <= high_water_item_id) and re-reads the CURRENT ai_summary as previous_summary at the
    moment of this same call's UPDATE -- never a value the caller read earlier and might have
    gone stale -- then writes the items.ai_summary UPDATE and the summary_rewrite_log INSERT
    together, committing only once both have succeeded. Any exception raised by either
    statement (including a forced failure in the log INSERT) rolls back the whole transaction,
    so the UPDATE can never persist without its log row.

    Returns the previous_summary that was overwritten, or None -- with NOTHING written -- if
    either: this (run_id, item_id) is already logged (idempotent no-op, checked first, before
    touching items at all), or the item was no longer eligible (read, or above the high-water
    mark) at the moment of the UPDATE."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        if was_migrated(conn, run_id, item_id):
            conn.rollback()
            return None

        row = conn.execute(
            "SELECT ai_summary FROM items "
            "WHERE id = ? AND is_read = 0 AND id <= ? "
            "AND ai_score IS NOT NULL AND ai_summary IS NOT NULL",
            (item_id, high_water_item_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        previous_summary = row["ai_summary"]

        cur = conn.execute(
            "UPDATE items SET ai_summary = ? "
            "WHERE id = ? AND is_read = 0 AND id <= ? "
            "AND ai_score IS NOT NULL AND ai_summary = ?",
            (replacement_summary, item_id, high_water_item_id, previous_summary))
        if cur.rowcount == 0:
            conn.rollback()
            return None

        conn.execute(
            "INSERT INTO summary_rewrite_log "
            "(run_id, item_id, previous_summary, replacement_summary, migrated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, item_id, previous_summary, replacement_summary, now.isoformat()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return previous_summary


def revert_summary_rewrite_entry(conn: sqlite3.Connection,
                                  entry: SummaryRewriteLogEntry) -> bool:
    """Restores one unchanged summary and consumes its log entry atomically.

    A changed or missing item is left untouched and the log entry is retained so an earlier run
    can be retried after any later rewrite has been rolled back.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        log_exists = conn.execute(
            "SELECT 1 FROM summary_rewrite_log WHERE id = ?", (entry.id,)).fetchone()
        if log_exists is None:
            conn.rollback()
            return False

        cur = conn.execute(
            "UPDATE items SET ai_summary = ? WHERE id = ? AND ai_summary = ?",
            (entry.previous_summary, entry.item_id, entry.replacement_summary),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False

        conn.execute("DELETE FROM summary_rewrite_log WHERE id = ?", (entry.id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def list_for_run(conn: sqlite3.Connection, run_id: str) -> list[SummaryRewriteLogEntry]:
    """Oldest-first (insertion order via the autoincrement `id`) so a rollback replays a run's
    rewrites in the same order they were originally applied."""
    rows = conn.execute(
        "SELECT * FROM summary_rewrite_log WHERE run_id = ? ORDER BY id ASC",
        (run_id,)).fetchall()
    return [_row_to_entry(r) for r in rows]


def count_for_run(conn: sqlite3.Connection, run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM summary_rewrite_log WHERE run_id = ?", (run_id,)).fetchone()[0]


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM summary_rewrite_log WHERE id = ?", (entry_id,))
    conn.commit()
