from __future__ import annotations

import json
import sqlite3

from beehive.channels import require_channel_kind
from beehive.db.email_groups import assign_channel, get_channel_group
from beehive.db.sources import create_source, list_by_channel
from beehive.scheduling import (
    DEFAULT_CHANNEL_FETCH_TIME,
    DEFAULT_SCHEDULE_TIMEZONE,
    CalendarSchedule,
    ScheduleMode,
    require_schedule_mode,
)


def _validate_display_settings(highlight_count: int, minimum_score: int) -> None:
    if not 1 <= highlight_count <= 50:
        raise ValueError("highlight_count must be between 1 and 50")
    if not 0 <= minimum_score <= 100:
        raise ValueError("minimum_score must be between 0 and 100")


def _validate_fetch_schedule(
    fetch_schedule_mode: str,
    fetch_schedule_timezone: str,
    fetch_schedule_time: str,
) -> tuple[str, str, str]:
    """Reject a fetch schedule the scheduler could not later resolve, and return the normalized
    values to store. Parsing goes through the same CalendarSchedule the collector uses, so an
    unusable timezone or HH:MM can never reach the table -- validation happens even in interval
    mode, since the stored calendar settings become live the moment an Owner switches modes."""
    mode = require_schedule_mode(fetch_schedule_mode)
    timezone_name = fetch_schedule_timezone.strip() or DEFAULT_SCHEDULE_TIMEZONE
    time_text = fetch_schedule_time.strip() or DEFAULT_CHANNEL_FETCH_TIME
    calendar = CalendarSchedule.parse(
        timezone_name=timezone_name,
        time_text=time_text,
        weekdays_text=None,
        default_time=DEFAULT_CHANNEL_FETCH_TIME,
    )
    return mode.value, timezone_name, f"{calendar.hour:02d}:{calendar.minute:02d}"


def _validate_kind(kind: str) -> str:
    """Validate a Channel kind against the canonical ChannelDefinition registry (the single
    source of truth for the accepted kinds) and return its normalized stored string. Rejects any
    kind without a definition, including a future kind added to the enum but never declared."""
    return require_channel_kind(kind).value


def create_channel(conn: sqlite3.Connection, name: str, profile: str,
                    fetch_interval_hours: int = 3, highlight_count: int = 8,
                    minimum_score: int = 0, kind: str = "editorial", *,
                    fetch_schedule_mode: str = ScheduleMode.INTERVAL.value,
                    fetch_schedule_timezone: str = DEFAULT_SCHEDULE_TIMEZONE,
                    fetch_schedule_time: str = DEFAULT_CHANNEL_FETCH_TIME) -> int:
    _validate_display_settings(highlight_count, minimum_score)
    normalized_kind = _validate_kind(kind)
    schedule_mode, schedule_timezone, schedule_time = _validate_fetch_schedule(
        fetch_schedule_mode, fetch_schedule_timezone, fetch_schedule_time)
    cur = conn.execute(
        "INSERT INTO channels "
        "(name, profile, fetch_interval_hours, highlight_count, minimum_score, kind, "
        "fetch_schedule_mode, fetch_schedule_timezone, fetch_schedule_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            profile,
            fetch_interval_hours,
            highlight_count,
            minimum_score,
            normalized_kind,
            schedule_mode,
            schedule_timezone,
            schedule_time,
        ))
    conn.commit()
    return cur.lastrowid


def get_channel(conn: sqlite3.Connection, channel_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def list_channels(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    """    kind=None (the default) returns every Channel regardless of kind. The collector and Channel
    navigation use this so every workflow remains reachable and scheduled. Reading-only surfaces
    such as Home highlights and Archive request Editorial data explicitly."""
    if kind is not None:
        normalized_kind = _validate_kind(kind)
        rows = conn.execute(
            "SELECT * FROM channels WHERE kind = ? ORDER BY id", (normalized_kind,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_channel(conn: sqlite3.Connection, channel_id: int, name: str, profile: str,
                   fetch_interval_hours: int, digest_email: str | None,
                   highlight_count: int | None = None,
                   minimum_score: int | None = None, *,
                   fetch_schedule_mode: str | None = None,
                   fetch_schedule_timezone: str | None = None,
                   fetch_schedule_time: str | None = None) -> None:
    """Any optional argument left as None keeps the Channel's stored value, so a caller that only
    edits display settings never has to restate the fetch schedule (and vice versa)."""
    optional = (
        highlight_count,
        minimum_score,
        fetch_schedule_mode,
        fetch_schedule_timezone,
        fetch_schedule_time,
    )
    if any(value is None for value in optional):
        current = get_channel(conn, channel_id)
        if current is None:
            return
        highlight_count = (
            current["highlight_count"] if highlight_count is None else highlight_count
        )
        minimum_score = current["minimum_score"] if minimum_score is None else minimum_score
        fetch_schedule_mode = (
            current["fetch_schedule_mode"]
            if fetch_schedule_mode is None
            else fetch_schedule_mode
        )
        fetch_schedule_timezone = (
            current["fetch_schedule_timezone"]
            if fetch_schedule_timezone is None
            else fetch_schedule_timezone
        )
        fetch_schedule_time = (
            current["fetch_schedule_time"]
            if fetch_schedule_time is None
            else fetch_schedule_time
        )
    _validate_display_settings(highlight_count, minimum_score)
    schedule_mode, schedule_timezone, schedule_time = _validate_fetch_schedule(
        fetch_schedule_mode, fetch_schedule_timezone, fetch_schedule_time)
    conn.execute(
        "UPDATE channels SET name = ?, profile = ?, fetch_interval_hours = ?, "
        "digest_email = ?, highlight_count = ?, minimum_score = ?, "
        "fetch_schedule_mode = ?, fetch_schedule_timezone = ?, fetch_schedule_time = ? "
        "WHERE id = ?",
        (
            name,
            profile,
            fetch_interval_hours,
            digest_email or None,
            highlight_count,
            minimum_score,
            schedule_mode,
            schedule_timezone,
            schedule_time,
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


def channel_impact_counts(conn: sqlite3.Connection, channel_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT sources.id) AS sources,
            COUNT(DISTINCT items.id) AS items,
            COUNT(DISTINCT votes.item_id) AS votes,
            COUNT(DISTINCT deep_reads.item_id) AS deep_reads,
            COUNT(DISTINCT auction_watches.item_id) AS watches,
            COUNT(DISTINCT item_events.id) AS events
        FROM channels
        LEFT JOIN sources ON sources.channel_id = channels.id
        LEFT JOIN items ON items.source_id = sources.id
        LEFT JOIN votes ON votes.item_id = items.id
        LEFT JOIN deep_reads ON deep_reads.item_id = items.id
        LEFT JOIN auction_watches ON auction_watches.item_id = items.id
        LEFT JOIN item_events ON item_events.item_id = items.id
        WHERE channels.id = ?
        """,
        (channel_id,),
    ).fetchone()
    if row is None:
        return {
            "sources": 0,
            "items": 0,
            "votes": 0,
            "deep_reads": 0,
            "watches": 0,
            "events": 0,
        }
    return {key: int(row[key]) for key in row.keys()}


def _unique_duplicate_name(conn: sqlite3.Connection, original_name: str) -> str:
    existing = {
        row["name"] for row in conn.execute("SELECT name FROM channels").fetchall()
    }
    candidate = f"{original_name} (copy)"
    suffix = 2
    while candidate in existing:
        candidate = f"{original_name} (copy {suffix})"
        suffix += 1
    return candidate


def duplicate_channel(conn: sqlite3.Connection, channel_id: int) -> int | None:
    """Copies a Channel's own settings and every one of its Sources (config verbatim, so any
    brand/collection filter survives) into a brand new Channel -- a fresh start for history,
    though: no Items, votes, or digest/fetch watermarks are copied. Email-group membership is
    copied too (see db/email_groups.py's duplicate-time hook), so a Channel already enrolled in
    a periodic digest keeps its duplicate enrolled as well."""
    original = get_channel(conn, channel_id)
    if original is None:
        return None
    new_name = _unique_duplicate_name(conn, original["name"])
    new_id = create_channel(
        conn,
        new_name,
        original["profile"],
        fetch_interval_hours=original["fetch_interval_hours"],
        highlight_count=original["highlight_count"],
        minimum_score=original["minimum_score"],
        kind=original["kind"],
        fetch_schedule_mode=original["fetch_schedule_mode"],
        fetch_schedule_timezone=original["fetch_schedule_timezone"],
        fetch_schedule_time=original["fetch_schedule_time"],
    )
    if original["digest_email"]:
        conn.execute(
            "UPDATE channels SET digest_email = ? WHERE id = ?",
            (original["digest_email"], new_id))
        conn.commit()
    for source in list_by_channel(conn, channel_id):
        create_source(conn, new_id, source["type"], json.loads(source["config"]))
    original_group = get_channel_group(conn, channel_id)
    if original_group is not None:
        assign_channel(conn, original_group["id"], new_id)
    return new_id
