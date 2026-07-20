from __future__ import annotations

import sqlite3

_KINDS = ("editorial", "monitor")


def _validate_display_settings(highlight_count: int, minimum_score: int) -> None:
    if not 1 <= highlight_count <= 50:
        raise ValueError("highlight_count must be between 1 and 50")
    if not 0 <= minimum_score <= 100:
        raise ValueError("minimum_score must be between 0 and 100")


def _validate_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS}, got {kind!r}")


def create_channel(conn: sqlite3.Connection, name: str, profile: str,
                    fetch_interval_hours: int = 3, highlight_count: int = 8,
                    minimum_score: int = 0, kind: str = "editorial") -> int:
    _validate_display_settings(highlight_count, minimum_score)
    _validate_kind(kind)
    cur = conn.execute(
        "INSERT INTO channels "
        "(name, profile, fetch_interval_hours, highlight_count, minimum_score, kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, profile, fetch_interval_hours, highlight_count, minimum_score, kind))
    conn.commit()
    return cur.lastrowid


def get_channel(conn: sqlite3.Connection, channel_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def list_channels(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    """kind=None (the default) returns every Channel regardless of kind -- the collector's
    fetch loop relies on this to keep polling 'monitor' Channels' sources exactly like
    'editorial' ones. Reading-oriented views (Home's per-channel nav shelf, the Channel nav
    shelf, Archive's channel filter) also pass kind=None now, since a 'monitor' Channel gets
    AI-ranked content on its own page too -- see run_channel_cycle and web/public.py. Only
    Home's cross-channel highlights feed still restricts to kind='editorial' (see
    db/items.py's _dashboard_signal_filters), since that feed's "read this" framing doesn't
    fit a shopping deal the way a channel-scoped page does."""
    if kind is not None:
        _validate_kind(kind)
        rows = conn.execute(
            "SELECT * FROM channels WHERE kind = ? ORDER BY id", (kind,)).fetchall()
    else:
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
