"""Conversation Memory persistence: mutable and versioned in place (unlike
research_syntheses.py/research_messages.py's append-only tables) -- exactly one row per
Research Session, and each update bumps version rather than inserting a new row. There is no
history of past memory contents to preserve, only "the current compression" and the version
number a chat request can pin (research_chat_requests.py)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.domain.research import ConversationMemory


def _row_to_memory(row: sqlite3.Row) -> ConversationMemory:
    return ConversationMemory(
        session_id=row["session_id"],
        version=row["version"],
        content=row["content"],
        covers_through_message_id=row["covers_through_message_id"],
        updated_at=datetime.fromisoformat(row["updated_at"]))


def get_conversation_memory(conn: sqlite3.Connection,
                             session_id: int) -> ConversationMemory | None:
    row = conn.execute(
        "SELECT * FROM research_conversation_memory WHERE session_id = ?",
        (session_id,)).fetchone()
    return _row_to_memory(row) if row else None


def _upsert_memory(conn: sqlite3.Connection, session_id: int, content: str,
                    covers_through_message_id: int | None, now: datetime) -> None:
    """Non-committing primitive: bumps version (0 if no row exists yet) and upserts the
    session's single memory row. Callers must already hold the write lock and are responsible
    for committing/rolling back -- research_chat_requests.py's atomic reply+memory completion
    calls this inside its own transaction."""
    conn.execute(
        "INSERT INTO research_conversation_memory (session_id, version, content, "
        "covers_through_message_id, updated_at) VALUES (?, 1, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "version = version + 1, content = excluded.content, "
        "covers_through_message_id = excluded.covers_through_message_id, "
        "updated_at = excluded.updated_at",
        (session_id, content, covers_through_message_id, now.isoformat()))


def update_conversation_memory(conn: sqlite3.Connection, session_id: int, content: str,
                                covers_through_message_id: int | None,
                                now: datetime) -> ConversationMemory:
    conn.execute("BEGIN IMMEDIATE")
    try:
        _upsert_memory(conn, session_id, content, covers_through_message_id, now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_conversation_memory(conn, session_id)
