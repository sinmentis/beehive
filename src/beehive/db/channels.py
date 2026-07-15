from __future__ import annotations

import sqlite3


def _validate_display_settings(highlight_count: int, minimum_score: int) -> None:
    if not 1 <= highlight_count <= 50:
        raise ValueError("highlight_count must be between 1 and 50")
    if not 0 <= minimum_score <= 100:
        raise ValueError("minimum_score must be between 0 and 100")


def create_channel(conn: sqlite3.Connection, name: str, profile: str,
                    fetch_interval_hours: int = 3, highlight_count: int = 8,
                    minimum_score: int = 0) -> int:
    _validate_display_settings(highlight_count, minimum_score)
    cur = conn.execute(
        "INSERT INTO channels "
        "(name, profile, fetch_interval_hours, highlight_count, minimum_score) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, profile, fetch_interval_hours, highlight_count, minimum_score))
    conn.commit()
    return cur.lastrowid


def get_channel(conn: sqlite3.Connection, channel_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_channel(conn: sqlite3.Connection, channel_id: int, name: str, profile: str,
                   fetch_interval_hours: int, digest_email: str | None,
                   highlight_count: int | None = None,
                   minimum_score: int | None = None) -> None:
    if highlight_count is None or minimum_score is None:
        current = get_channel(conn, channel_id)
        if current is None:
            return
        highlight_count = (
            current["highlight_count"] if highlight_count is None else highlight_count
        )
        minimum_score = current["minimum_score"] if minimum_score is None else minimum_score
    _validate_display_settings(highlight_count, minimum_score)
    conn.execute(
        "UPDATE channels SET name = ?, profile = ?, fetch_interval_hours = ?, "
        "digest_email = ?, highlight_count = ?, minimum_score = ? WHERE id = ?",
        (
            name,
            profile,
            fetch_interval_hours,
            digest_email or None,
            highlight_count,
            minimum_score,
            channel_id,
        ))
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
