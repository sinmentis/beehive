"""Conversation Message persistence (append-only). Citations live in a separate concrete table
(research_message_citations), the sibling of research_synthesis_citations -- see
research_syntheses.py's module docstring for why citations are never polymorphic.

_insert_message/_insert_message_citations are the non-committing primitives this module's own
append_message wraps in its own BEGIN IMMEDIATE transaction; research_chat_requests.py reuses
these same primitives (uncommitted) to atomically write a chat reply, complete the chat
request, and bump conversation memory all inside one transaction."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.db.research_sessions import _is_session_active
from beehive.domain.research import (ConversationMessage, ConversationMessageStatus,
                                      ConversationRole, EvidenceCitation)


def _row_to_message(conn: sqlite3.Connection, row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        id=row["id"],
        session_id=row["session_id"],
        sequence_number=row["sequence_number"],
        role=ConversationRole(row["role"]),
        status=ConversationMessageStatus(row["status"]),
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]))


def _insert_message(conn: sqlite3.Connection, session_id: int, role: ConversationRole,
                     status: ConversationMessageStatus, content: str, now: datetime) -> int:
    """Non-committing primitive: allocates sequence_number as MAX(sequence_number)+1 for the
    session and inserts the message row. Callers must already hold the write lock (BEGIN
    IMMEDIATE) and are responsible for committing/rolling back."""
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
        "FROM research_messages WHERE session_id = ?",
        (session_id,)).fetchone()
    sequence_number = row["next_seq"]
    cur = conn.execute(
        "INSERT INTO research_messages (session_id, sequence_number, role, status, content, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, sequence_number, role.value, status.value, content, now.isoformat()))
    return cur.lastrowid


def _insert_message_citations(conn: sqlite3.Connection, message_id: int,
                               citations: tuple[EvidenceCitation, ...]) -> None:
    """Non-committing primitive, same contract as _insert_message."""
    if not citations:
        return
    conn.executemany(
        "INSERT INTO research_message_citations (message_id, evidence_item_id, "
        "citation_number) VALUES (?, ?, ?)",
        [(message_id, c.evidence_item_id, c.citation_number) for c in citations])


def append_message(conn: sqlite3.Connection, session_id: int, role: ConversationRole,
                    content: str, now: datetime,
                    status: ConversationMessageStatus = ConversationMessageStatus.READY,
                    citations: tuple[EvidenceCitation, ...] = ()) -> ConversationMessage:
    """Appends one Conversation Message (and its citations, if any) in a single BEGIN IMMEDIATE
    transaction. Owner messages must be READY (domain.research.ConversationMessage enforces
    this on construction); an assistant message may be written PENDING and later completed via
    research_chat_requests.py's atomic reply+memory completion. Raises ValueError if the
    Research Session is not 'active' (archived or nonexistent) -- an archived session can never
    have a new message appended to it directly through this entry point. (The reply message
    written by complete_chat_request_with_reply goes through the _insert_message primitive
    below instead, and is never at risk of targeting an archived session -- archiving a session
    is itself rejected while it has a processing chat request.)"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _is_session_active(conn, session_id):
            raise ValueError(
                f"cannot append a Conversation Message to a non-active Research Session "
                f"{session_id}")
        message_id = _insert_message(conn, session_id, role, status, content, now)
        _insert_message_citations(conn, message_id, citations)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_message(conn, message_id)


def get_message(conn: sqlite3.Connection, message_id: int) -> ConversationMessage | None:
    row = conn.execute(
        "SELECT * FROM research_messages WHERE id = ?", (message_id,)).fetchone()
    return _row_to_message(conn, row) if row else None


def list_messages(conn: sqlite3.Connection, session_id: int) -> list[ConversationMessage]:
    rows = conn.execute(
        "SELECT * FROM research_messages WHERE session_id = ? ORDER BY sequence_number",
        (session_id,)).fetchall()
    return [_row_to_message(conn, r) for r in rows]


def list_message_citations(conn: sqlite3.Connection,
                            message_id: int) -> tuple[EvidenceCitation, ...]:
    rows = conn.execute(
        "SELECT evidence_item_id, citation_number FROM research_message_citations "
        "WHERE message_id = ? ORDER BY evidence_item_id",
        (message_id,)).fetchall()
    return tuple(
        EvidenceCitation(evidence_item_id=r["evidence_item_id"],
                          citation_number=r["citation_number"])
        for r in rows)
