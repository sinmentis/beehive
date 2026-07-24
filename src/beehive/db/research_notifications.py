from __future__ import annotations

import sqlite3


def list_pending_completion_notifications(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            research_runs.id AS run_id,
            research_runs.session_id,
            research_runs.completed_at,
            research_sessions.question
        FROM research_runs
        JOIN research_sessions ON research_sessions.id = research_runs.session_id
        LEFT JOIN research_completion_notifications
            ON research_completion_notifications.run_id = research_runs.id
        WHERE research_runs.status = 'completed'
          AND research_completion_notifications.sent_at IS NULL
        ORDER BY research_runs.completed_at, research_runs.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_completion_notification_attempted(
    conn: sqlite3.Connection,
    run_id: int,
    attempted_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO research_completion_notifications (run_id, attempted_at)
        VALUES (?, ?)
        ON CONFLICT(run_id) DO UPDATE SET attempted_at = excluded.attempted_at
        """,
        (run_id, attempted_at),
    )
    conn.commit()


def mark_completion_notification_sent(
    conn: sqlite3.Connection,
    run_id: int,
    sent_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO research_completion_notifications (run_id, attempted_at, sent_at)
        VALUES (?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE
        SET attempted_at = excluded.attempted_at,
            sent_at = excluded.sent_at
        """,
        (run_id, sent_at, sent_at),
    )
    conn.commit()
