"""last_fetch_at is only touched by record_fetch_success (a successful cycle) — this is
what powers the Dashboard's "fetched Nh ago" freshness line. last_fetch_raw_count and
last_fetch_new_count share that same success-only lifecycle (both written by
record_fetch_success, left untouched on failure) and back the per-fetch stats line.
last_fetch_error is set by
record_fetch_error and cleared by the next successful fetch, and is what the digest reads
to render the "this source is currently failing" warning line (per the per-Source failure
isolation in ADR-0002 — a failure never touches last_fetch_at). The one deliberate exception
to all of the above is reset_fetch_state_by_channel, which blanks every one of these fields
at once as part of the admin "clear channel data" action."""
from __future__ import annotations

import json
import sqlite3

from beehive.channels import require_channel_kind
from beehive.channels.source_policy import assert_source_allowed


def create_source(conn: sqlite3.Connection, channel_id: int, type: str, config: dict) -> int:
    """Persist a Source, gating on Source/Channel compatibility first (the persistence safety
    seam): the target Channel's kind is loaded and the shared policy must allow this Source type
    before the INSERT. A missing Channel or an incompatible Source type raises ValueError, so an
    incompatible Source can never reach the table regardless of which caller (admin UI, channel
    duplication, a test) asks for it."""
    row = conn.execute(
        "SELECT kind FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise ValueError(f"cannot create Source: Channel {channel_id} does not exist")
    assert_source_allowed(type, require_channel_kind(row["kind"]))
    cur = conn.execute(
        "INSERT INTO sources (channel_id, type, config) VALUES (?, ?, ?)",
        (channel_id, type, json.dumps(config)))
    conn.commit()
    return cur.lastrowid


def list_by_channel(conn: sqlite3.Connection, channel_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ? ORDER BY id", (channel_id,)).fetchall()
    return [dict(r) for r in rows]


def get_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def record_fetch_success(conn: sqlite3.Connection, source_id: int, fetched_at: str,
                          raw_count: int = 0, new_count: int = 0) -> None:
    conn.execute(
        "UPDATE sources SET last_fetch_at = ?, last_fetch_error = NULL, "
        "last_fetch_raw_count = ?, last_fetch_new_count = ? WHERE id = ?",
        (fetched_at, raw_count, new_count, source_id))
    conn.commit()


def record_fetch_error(conn: sqlite3.Connection, source_id: int, error: str,
                        attempted_at: str) -> None:
    conn.execute("UPDATE sources SET last_fetch_error = ? WHERE id = ?", (error, source_id))
    conn.commit()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()


def reset_fetch_state_by_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    """Clears every Source's fetch bookkeeping for this Channel, pairing with
    items.delete_by_channel so a "clear channel data" admin action leaves no stale
    last_fetch_*/error/count fields behind -- the next fetch (manual or scheduled) starts as
    if the Sources were freshly added."""
    conn.execute(
        "UPDATE sources SET last_fetch_at = NULL, last_fetch_error = NULL, "
        "last_fetch_raw_count = NULL, last_fetch_new_count = NULL WHERE channel_id = ?",
        (channel_id,))
    conn.commit()
