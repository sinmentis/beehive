"""Records every admin login attempt (success and failure) for the forensic audit log (spec
section 7) and feeds auth/rate_limit.py's lockout check. success is stored as 0/1 (SQLite has no
native boolean)."""
from __future__ import annotations

import sqlite3


def record_attempt(conn: sqlite3.Connection, ip: str | None, country: str | None,
                   success: bool, attempted_at: str) -> None:
    conn.execute(
        "INSERT INTO admin_login_attempts (ip, country, success, attempted_at) "
        "VALUES (?, ?, ?, ?)",
        (ip, country, int(success), attempted_at))
    conn.commit()


def count_recent_failures(conn: sqlite3.Connection, ip: str, since_iso: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM admin_login_attempts "
        "WHERE ip = ? AND success = 0 AND attempted_at > ?",
        (ip, since_iso)).fetchone()
    return row[0]


def get_most_recent_attempt(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM admin_login_attempts ORDER BY attempted_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
