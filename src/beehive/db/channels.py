from __future__ import annotations

import sqlite3


def create_channel(conn: sqlite3.Connection, name: str, profile: str,
                    fetch_interval_hours: int = 3) -> int:
    cur = conn.execute(
        "INSERT INTO channels (name, profile, fetch_interval_hours) VALUES (?, ?, ?)",
        (name, profile, fetch_interval_hours))
    conn.commit()
    return cur.lastrowid


def get_channel(conn: sqlite3.Connection, channel_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_channel(conn: sqlite3.Connection, channel_id: int, name: str, profile: str,
                   fetch_interval_hours: int, digest_email: str | None) -> None:
    conn.execute(
        "UPDATE channels SET name = ?, profile = ?, fetch_interval_hours = ?, "
        "digest_email = ? WHERE id = ?",
        (name, profile, fetch_interval_hours, digest_email or None, channel_id))
    conn.commit()


def mark_digest_sent(conn: sqlite3.Connection, channel_ids: list[int],
                     sent_at: str, digest_date: str) -> None:
    if not channel_ids:
        return
    conn.executemany(
        "UPDATE channels SET last_digest_sent_at = ?, last_digest_date = ? WHERE id = ?",
        [(sent_at, digest_date, channel_id) for channel_id in channel_ids])
    conn.commit()


def delete_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
