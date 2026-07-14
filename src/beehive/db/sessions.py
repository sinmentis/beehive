"""DB-backed session store (Slice 3): the cookie only carries a signed session_id (see
auth/tokens.py); this table is the actual source of truth for whether a session is still valid,
which is what makes real server-side logout (delete_session) and restart-survival possible."""
from __future__ import annotations

import sqlite3


def create_session(conn: sqlite3.Connection, session_id: str, csrf_token: str,
                    expires_at: str) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, csrf_token, expires_at) VALUES (?, ?, ?)",
        (session_id, csrf_token, expires_at))
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    conn.commit()
