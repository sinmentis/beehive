"""Research Session persistence (ADR-0006, ADR-0008). One row per Research Session; only
status, last_activity_at, and archived_at ever change after insert -- the question itself is
immutable, matching domain/research.py's ResearchSession contract.

archive_research_session/unarchive_research_session reject the transition outright if the
session has an active (non-terminal) Research Run or a pending/processing chat request, so an
Owner can never archive a session out from under in-flight work -- there is nothing to recover
from later, the caller must wait for the run/chat to reach a terminal state (or cancel it)
first.

hard_delete_research_session relies entirely on ON DELETE CASCADE across every table in this
family: deleting the session row cascades to sources, runs, plan revisions, evidence items,
snapshots, curation, evidence-state revisions, clusters, syntheses, messages, chat requests,
and conversation memory in one transaction. This is also how a hard delete "revokes" any
in-flight worker claim -- the run/chat_request row a claim_token was validated against simply
ceases to exist, so any subsequent heartbeat/complete/fail call against that claim matches zero
rows and reports failure instead of silently succeeding."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from beehive.domain.research import (ResearchSession, ResearchSessionStatus,
                                      require_session_transition)


def _row_to_session(row: sqlite3.Row) -> ResearchSession:
    return ResearchSession(
        id=row["id"],
        question=row["question"],
        status=ResearchSessionStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_activity_at=datetime.fromisoformat(row["last_activity_at"]),
        archived_at=datetime.fromisoformat(row["archived_at"]) if row["archived_at"] else None)


def create_research_session(conn: sqlite3.Connection, question: str,
                             now: datetime) -> ResearchSession:
    now_iso = now.isoformat()
    cur = conn.execute(
        "INSERT INTO research_sessions (question, status, created_at, last_activity_at) "
        "VALUES (?, 'active', ?, ?)",
        (question, now_iso, now_iso))
    conn.commit()
    return get_research_session(conn, cur.lastrowid)


def create_research_session_with_sources(
        conn: sqlite3.Connection, question: str,
        sources: list[tuple[str, dict]], now: datetime) -> tuple[int, int]:
    """Atomically creates a brand-new active Research Session together with its Owner-selected
    Research Sources (origin='owner') and its first pending Research Run, all in one BEGIN
    IMMEDIATE transaction -- either every row commits together or none do, so the create-session
    web route (web/research.py) can never leave behind a session with no sources, or sources
    with nothing enqueued yet to collect for them.

    Writes raw INSERT statements for research_sources/research_runs directly rather than calling
    research_sources.py's create_research_source or research_runs.py's enqueue_research_run:
    research_runs.py already imports this module's own _is_session_active, so importing back
    from here into research_runs.py (or research_sources.py, transitively) would create an import
    cycle. Callers must pass sources already validated/normalized by
    research.connector_policy.normalize_and_validate_sources -- this function does not
    re-validate connector_type/config itself, exactly like create_research_source doesn't.

    Requires a non-empty question and at least one source (a Research Session always starts with
    at least one Owner-selected Research Source); raises ValueError otherwise, before opening any
    transaction. Returns (session_id, run_id) -- callers hydrate the full ResearchSession/
    ResearchSource/ResearchRun objects afterward via this package's normal get_*/list_*
    functions."""
    if not question.strip():
        raise ValueError("Research Question must not be empty")
    if not sources:
        raise ValueError("a new Research Session needs at least one Research Source")
    now_iso = now.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO research_sessions (question, status, created_at, last_activity_at) "
            "VALUES (?, 'active', ?, ?)",
            (question, now_iso, now_iso))
        session_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO research_sources (session_id, connector_type, config, origin, "
            "created_at) VALUES (?, ?, ?, 'owner', ?)",
            [(session_id, connector_type, json.dumps(config), now_iso)
             for connector_type, config in sources])
        run_cur = conn.execute(
            "INSERT INTO research_runs (session_id, status, requested_at) VALUES (?, 'pending', ?)",
            (session_id, now_iso))
        run_id = run_cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return session_id, run_id


def get_research_session(conn: sqlite3.Connection, session_id: int) -> ResearchSession | None:
    row = conn.execute(
        "SELECT * FROM research_sessions WHERE id = ?", (session_id,)).fetchone()
    return _row_to_session(row) if row else None


def list_research_sessions(conn: sqlite3.Connection,
                            status: ResearchSessionStatus | None = None) -> list[ResearchSession]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM research_sessions ORDER BY last_activity_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM research_sessions WHERE status = ? "
            "ORDER BY last_activity_at DESC, id DESC",
            (status.value,)).fetchall()
    return [_row_to_session(r) for r in rows]


def unread_completed_research_session_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        """
        SELECT research_sessions.id
        FROM research_sessions
        WHERE EXISTS (
            SELECT 1
            FROM research_runs
            WHERE research_runs.session_id = research_sessions.id
              AND research_runs.status = 'completed'
              AND research_runs.completed_at > COALESCE(
                  research_sessions.last_viewed_at,
                  research_sessions.created_at
              )
        )
        """
    ).fetchall()
    return {int(row["id"]) for row in rows}


def count_unread_completed_research_sessions(conn: sqlite3.Connection) -> int:
    return len(unread_completed_research_session_ids(conn))


def mark_research_session_viewed(
    conn: sqlite3.Connection,
    session_id: int,
    now: datetime,
) -> None:
    conn.execute(
        "UPDATE research_sessions SET last_viewed_at = ? WHERE id = ?",
        (now.isoformat(), session_id),
    )
    conn.commit()


def touch_research_session_activity(conn: sqlite3.Connection, session_id: int,
                                     now: datetime) -> None:
    """Bump last_activity_at (e.g. after a new run, message, or chat reply). Never touches
    status/archived_at."""
    conn.execute(
        "UPDATE research_sessions SET last_activity_at = ? WHERE id = ?",
        (now.isoformat(), session_id))
    conn.commit()


def _is_session_active(conn: sqlite3.Connection, session_id: int) -> bool:
    """Non-committing helper: True only if session_id exists and is currently 'active'.
    research_runs.py/research_chat_requests.py/research_messages.py call this (under their own
    BEGIN IMMEDIATE) before enqueueing a new Research Run, chat request, or Conversation Message
    respectively, so none of those can ever be created against an archived (or nonexistent)
    Research Session -- an Owner must unarchive first."""
    row = conn.execute(
        "SELECT 1 FROM research_sessions WHERE id = ? AND status = 'active'",
        (session_id,)).fetchone()
    return row is not None


def _has_active_run_or_chat(conn: sqlite3.Connection, session_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM research_runs WHERE session_id = ? "
        "AND status IN ('pending', 'processing') LIMIT 1",
        (session_id,)).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM research_chat_requests WHERE session_id = ? "
        "AND status IN ('pending', 'processing') LIMIT 1",
        (session_id,)).fetchone()
    return row is not None


def archive_research_session(conn: sqlite3.Connection, session_id: int,
                              now: datetime) -> ResearchSession:
    """Transitions active -> archived. Raises ValueError if the session does not exist, is
    already archived (require_session_transition), or has an active Research Run or a
    pending/processing chat request -- archiving is rejected outright rather than cancelling
    in-flight work on the Owner's behalf."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        session = get_research_session(conn, session_id)
        if session is None:
            raise ValueError(f"no Research Session with id={session_id}")
        require_session_transition(session.status, ResearchSessionStatus.ARCHIVED)
        if _has_active_run_or_chat(conn, session_id):
            raise ValueError(
                "cannot archive a Research Session with an active run or chat request")
        now_iso = now.isoformat()
        conn.execute(
            "UPDATE research_sessions SET status = 'archived', archived_at = ?, "
            "last_activity_at = ? WHERE id = ?",
            (now_iso, now_iso, session_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_research_session(conn, session_id)


def unarchive_research_session(conn: sqlite3.Connection, session_id: int,
                                now: datetime) -> ResearchSession:
    """Transitions archived -> active. No active-run/chat check is needed here (an archived
    session cannot have one -- archiving already rejected while any existed, and _is_session_
    active is checked by research_runs.py/research_chat_requests.py/research_messages.py before
    they ever create a new run/chat request/message against an archived session)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        session = get_research_session(conn, session_id)
        if session is None:
            raise ValueError(f"no Research Session with id={session_id}")
        require_session_transition(session.status, ResearchSessionStatus.ACTIVE)
        now_iso = now.isoformat()
        conn.execute(
            "UPDATE research_sessions SET status = 'active', archived_at = NULL, "
            "last_activity_at = ? WHERE id = ?",
            (now_iso, session_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_research_session(conn, session_id)


def hard_delete_research_session(conn: sqlite3.Connection, session_id: int) -> bool:
    """Permanently deletes a Research Session and, via ON DELETE CASCADE, everything scoped to
    it. Returns True if a row was actually deleted."""
    cur = conn.execute("DELETE FROM research_sessions WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0
