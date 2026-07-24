"""last_fetch_at is only touched by record_fetch_success (a successful cycle) — this is
what powers the Dashboard's "fetched Nh ago" freshness line. last_fetch_raw_count and
last_fetch_new_count share that same success-only lifecycle (both written by
record_fetch_success, left untouched on failure) and back the per-fetch stats line.
last_fetch_error is set by
record_fetch_error and cleared by the next successful fetch, and is what the digest reads
to render the "this source is currently failing" warning line (per the per-Source failure
isolation in ADR-0002 — a failure never touches last_fetch_at). last_attempt_at and
last_fetch_status add a success-independent view of those SAME two calls: every attempt stamps
last_attempt_at and sets last_fetch_status to 'ok' or 'error', so the Channel editor can show
"attempted Nm ago, last succeeded Nh ago" even while a Source keeps failing. The one deliberate
exception to all of the above is reset_fetch_state_by_channel, which blanks every one of these
fetch fields at once as part of the admin "clear channel data" action.

paused_at and name are lifecycle/display state that outlive a fetch: paused_at, when set, takes a
Source out of every collector cycle (see collector/run_cycle.py) and every warning summary without
touching its config or history, and name is an optional Owner display label. Neither is cleared by
reset_fetch_state_by_channel — clearing a Channel's fetched data must not silently resume a paused
Source or forget its name."""
from __future__ import annotations

import json
import sqlite3

from beehive.channels import require_channel_kind
from beehive.channels.source_policy import assert_source_allowed

FETCH_STATUS_OK = "ok"
FETCH_STATUS_ERROR = "error"


def _normalized_name(name: str | None) -> str | None:
    """Collapse a blank/whitespace-only display name to NULL so "unnamed" is one canonical state."""
    if name is None:
        return None
    stripped = name.strip()
    return stripped or None


def _assert_source_allowed_for_channel(conn: sqlite3.Connection, channel_id: int,
                                       type: str) -> None:
    """Load the target Channel's kind and gate the Source type through the shared fail-closed
    policy. A missing Channel or an incompatible Source type raises ValueError, so an incompatible
    Source can never reach the table regardless of which caller (create or update) asks for it."""
    row = conn.execute(
        "SELECT kind FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise ValueError(f"cannot persist Source: Channel {channel_id} does not exist")
    assert_source_allowed(type, require_channel_kind(row["kind"]))


def create_source(conn: sqlite3.Connection, channel_id: int, type: str, config: dict,
                  name: str | None = None) -> int:
    """Persist a Source, gating on Source/Channel compatibility first (the persistence safety
    seam): the target Channel's kind is loaded and the shared policy must allow this Source type
    before the INSERT. A missing Channel or an incompatible Source type raises ValueError, so an
    incompatible Source can never reach the table regardless of which caller (admin UI, channel
    duplication, a test) asks for it."""
    _assert_source_allowed_for_channel(conn, channel_id, type)
    cur = conn.execute(
        "INSERT INTO sources (channel_id, type, config, name) VALUES (?, ?, ?, ?)",
        (channel_id, type, json.dumps(config), _normalized_name(name)))
    conn.commit()
    return cur.lastrowid


def update_source(conn: sqlite3.Connection, source_id: int, type: str, config: dict,
                  name: str | None = None) -> None:
    """Re-point an existing Source at a (possibly different but still compatible) type/config,
    re-running the same fail-closed compatibility gate as create_source against the Source's own
    Channel. Fetch bookkeeping is deliberately left untouched: an edit changes what a Source
    fetches, not the record of what it last did."""
    row = conn.execute(
        "SELECT channel_id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"cannot update Source: Source {source_id} does not exist")
    _assert_source_allowed_for_channel(conn, row["channel_id"], type)
    conn.execute(
        "UPDATE sources SET type = ?, config = ?, name = ? WHERE id = ?",
        (type, json.dumps(config), _normalized_name(name), source_id))
    conn.commit()


def find_duplicate_source(conn: sqlite3.Connection, channel_id: int, type: str, config: dict,
                          *, exclude_source_id: int | None = None) -> int | None:
    """The id of an existing Source in the same Channel that would fetch exactly the same thing
    (same type and same config), or None. Config equality is key-order independent — the stored
    JSON is parsed back to a dict and compared by value, so ``{"a": 1, "b": 2}`` and
    ``{"b": 2, "a": 1}`` are the same Source. exclude_source_id skips one row so an edit that leaves
    a Source's target unchanged is never flagged as a duplicate of itself."""
    rows = conn.execute(
        "SELECT id, config FROM sources WHERE channel_id = ? AND type = ?",
        (channel_id, type)).fetchall()
    for row in rows:
        if exclude_source_id is not None and row["id"] == exclude_source_id:
            continue
        if json.loads(row["config"]) == config:
            return row["id"]
    return None


def list_by_channel(conn: sqlite3.Connection, channel_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sources WHERE channel_id = ? ORDER BY id", (channel_id,)).fetchall()
    return [dict(r) for r in rows]


def get_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def set_source_paused(conn: sqlite3.Connection, source_id: int, paused: bool,
                      *, now_iso: str | None = None) -> None:
    """Pause (paused_at = now) or resume (paused_at = NULL) a Source. A paused Source keeps its
    config, items, and fetch history intact; it is simply skipped by every collector cycle and left
    out of every warning summary until resumed. now_iso is required to pause (the moment it went
    dormant) and ignored when resuming."""
    if paused:
        if now_iso is None:
            raise ValueError("pausing a Source needs a timestamp")
        conn.execute(
            "UPDATE sources SET paused_at = ? WHERE id = ?", (now_iso, source_id))
    else:
        conn.execute("UPDATE sources SET paused_at = NULL WHERE id = ?", (source_id,))
    conn.commit()


def record_fetch_success(conn: sqlite3.Connection, source_id: int, fetched_at: str,
                          raw_count: int = 0, new_count: int = 0) -> None:
    conn.execute(
        "UPDATE sources SET last_fetch_at = ?, last_fetch_error = NULL, "
        "last_fetch_raw_count = ?, last_fetch_new_count = ?, "
        "last_attempt_at = ?, last_fetch_status = ? WHERE id = ?",
        (fetched_at, raw_count, new_count, fetched_at, FETCH_STATUS_OK, source_id))
    conn.commit()


def record_fetch_error(conn: sqlite3.Connection, source_id: int, error: str,
                        attempted_at: str) -> None:
    conn.execute(
        "UPDATE sources SET last_fetch_error = ?, last_attempt_at = ?, "
        "last_fetch_status = ? WHERE id = ?",
        (error, attempted_at, FETCH_STATUS_ERROR, source_id))
    conn.commit()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()


def source_impact_counts(conn: sqlite3.Connection, source_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT items.id) AS items,
            COUNT(DISTINCT votes.item_id) AS votes,
            COUNT(DISTINCT deep_reads.item_id) AS deep_reads,
            COUNT(DISTINCT auction_watches.item_id) AS watches,
            COUNT(DISTINCT item_events.id) AS events
        FROM sources
        LEFT JOIN items ON items.source_id = sources.id
        LEFT JOIN votes ON votes.item_id = items.id
        LEFT JOIN deep_reads ON deep_reads.item_id = items.id
        LEFT JOIN auction_watches ON auction_watches.item_id = items.id
        LEFT JOIN item_events ON item_events.item_id = items.id
        WHERE sources.id = ?
        """,
        (source_id,),
    ).fetchone()
    return {key: int(row[key]) for key in row.keys()}


def reset_fetch_state_by_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    """Clears every Source's fetch bookkeeping for this Channel, pairing with
    items.delete_by_channel so a "clear channel data" admin action leaves no stale
    last_fetch_*/error/count/attempt/status fields behind -- the next fetch (manual or scheduled)
    starts as if the Sources were freshly added. Deliberately does NOT touch paused_at or name:
    clearing fetched data must not resume a paused Source or forget its display name."""
    conn.execute(
        "UPDATE sources SET last_fetch_at = NULL, last_fetch_error = NULL, "
        "last_fetch_raw_count = NULL, last_fetch_new_count = NULL, "
        "last_attempt_at = NULL, last_fetch_status = NULL WHERE channel_id = ?",
        (channel_id,))
    conn.commit()
