"""Generic persistence for manually watched Tracker items.

The physical table keeps its original ``auction_watches`` name so existing databases need no
destructive migration. Its columns already store generic watch and deadline-version state; this
module supplies the connector-independent API and delegates lifecycle semantics to TrackerAdapter.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from beehive.channels.tracker import adapter_for_source
from beehive.domain.channels import ChannelKind

_CLAIM_LEASE = timedelta(minutes=15)
_DEFAULT_CLAIM_LIMIT = 100


@dataclass(frozen=True)
class TrackerReminderClaim:
    token: str | None
    items: list[dict]


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _joined_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT auction_watches.*, items.title, items.url, items.body,
               items.raw_metadata, items.inactive_at,
               sources.type AS source_type, sources.config AS source_config,
               channels.id AS channel_id, channels.name AS channel_name,
               channels.kind AS channel_kind
        FROM auction_watches
        JOIN items ON items.id = auction_watches.item_id
        JOIN sources ON sources.id = items.source_id
        JOIN channels ON channels.id = sources.channel_id
        """
    ).fetchall()


def _joined_row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT auction_watches.*, items.title, items.url, items.body,
               items.raw_metadata, items.inactive_at,
               sources.type AS source_type, sources.config AS source_config,
               channels.id AS channel_id, channels.name AS channel_name,
               channels.kind AS channel_kind
        FROM auction_watches
        JOIN items ON items.id = auction_watches.item_id
        JOIN sources ON sources.id = items.source_id
        JOIN channels ON channels.id = sources.channel_id
        WHERE auction_watches.item_id = ?
        """,
        (item_id,),
    ).fetchone()


def _row_to_dict(row: sqlite3.Row, now: datetime) -> dict:
    item = dict(row)
    metadata = json.loads(item["raw_metadata"])
    facts = adapter_for_source(item["source_type"]).facts(
        metadata,
        is_present=item["inactive_at"] is None,
        now=now,
    )
    item["raw_metadata"] = metadata
    item["deadline"] = facts.deadline.isoformat() if facts.deadline is not None else None
    item["closing_at"] = item["deadline"]
    item["is_active"] = facts.active
    item["is_closed"] = facts.deadline is None or facts.deadline <= now
    item["is_watchable"] = facts.watchable
    item["reminder_key"] = facts.reminder_key
    item["reminder_due_at"] = (
        facts.reminder_due_at.isoformat() if facts.reminder_due_at is not None else None
    )
    return item


def add_tracker_watch(conn: sqlite3.Connection, item_id: int, now: datetime) -> bool:
    utc_now = _require_aware(now)
    row = conn.execute(
        """
        SELECT items.raw_metadata, items.inactive_at,
               sources.type AS source_type, channels.kind AS channel_kind
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN channels ON channels.id = sources.channel_id
        WHERE items.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Tracker item does not exist")
    if row["channel_kind"] != ChannelKind.TRACKER.value:
        raise ValueError("only Tracker items can be watched")
    metadata = json.loads(row["raw_metadata"])
    facts = adapter_for_source(row["source_type"]).facts(
        metadata,
        is_present=row["inactive_at"] is None,
        now=utc_now,
    )
    if not facts.watchable:
        raise ValueError("Tracker item is not watchable")

    cursor = conn.execute(
        "INSERT OR IGNORE INTO auction_watches (item_id, watched_at) VALUES (?, ?)",
        (item_id, utc_now.isoformat()),
    )
    conn.commit()
    return cursor.rowcount > 0


def remove_tracker_watch(conn: sqlite3.Connection, item_id: int) -> bool:
    cursor = conn.execute("DELETE FROM auction_watches WHERE item_id = ?", (item_id,))
    conn.commit()
    return cursor.rowcount > 0


def remove_tracker_watches(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    unique_ids = sorted(set(item_ids))
    if not unique_ids:
        return 0
    placeholders = ", ".join("?" for _ in unique_ids)
    cursor = conn.execute(
        f"DELETE FROM auction_watches WHERE item_id IN ({placeholders})",
        unique_ids,
    )
    conn.commit()
    return cursor.rowcount


def remove_closed_tracker_watches(conn: sqlite3.Connection, now: datetime) -> int:
    closed_ids = [
        item["item_id"] for item in list_tracker_watches(conn, now) if item["is_closed"]
    ]
    return remove_tracker_watches(conn, closed_ids)


def get_watched_item_ids(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    if not item_ids:
        return set()
    placeholders = ", ".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT item_id FROM auction_watches WHERE item_id IN ({placeholders})",
        item_ids,
    ).fetchall()
    return {row["item_id"] for row in rows}


def list_tracker_watches(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    utc_now = _require_aware(now)
    items = [_row_to_dict(row, utc_now) for row in _joined_rows(conn)]
    latest = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(
        items,
        key=lambda item: (
            not item["is_active"],
            item["is_closed"],
            datetime.fromisoformat(item["deadline"]) if item["deadline"] else latest,
            item["watched_at"],
        ),
    )


def claim_due_tracker_reminders(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    limit: int = _DEFAULT_CLAIM_LIMIT,
) -> TrackerReminderClaim:
    utc_now = _require_aware(now)
    if limit < 1:
        raise ValueError("limit must be positive")
    now_iso = utc_now.isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE auction_watches
            SET claim_token = NULL, claim_closing_at = NULL, claim_expires_at = NULL
            WHERE claim_token IS NOT NULL AND claim_expires_at <= ?
            """,
            (now_iso,),
        )
        due_rows: list[tuple[sqlite3.Row, str]] = []
        for row in _joined_rows(conn):
            if row["claim_token"] is not None:
                continue
            metadata = json.loads(row["raw_metadata"])
            facts = adapter_for_source(row["source_type"]).facts(
                metadata,
                is_present=row["inactive_at"] is None,
                now=utc_now,
            )
            if (
                not facts.active
                or facts.deadline is None
                or facts.reminder_key is None
                or facts.reminder_due_at is None
                or not (facts.reminder_due_at <= utc_now < facts.deadline)
                or row["reminder_sent_for_closing_at"] == facts.reminder_key
            ):
                continue
            due_rows.append((row, facts.reminder_key))
            if len(due_rows) >= limit:
                break

        if not due_rows:
            conn.commit()
            return TrackerReminderClaim(token=None, items=[])

        token = secrets.token_urlsafe(32)
        claim_expires_at = (utc_now + _CLAIM_LEASE).isoformat()
        claimed_items = []
        for row, reminder_key in due_rows:
            cursor = conn.execute(
                """
                UPDATE auction_watches
                SET claim_token = ?, claim_closing_at = ?, claim_expires_at = ?,
                    last_error = NULL
                WHERE item_id = ? AND claim_token IS NULL
                """,
                (token, reminder_key, claim_expires_at, row["item_id"]),
            )
            if cursor.rowcount:
                claimed = _row_to_dict(row, utc_now)
                claimed["claimed_closing_at"] = reminder_key
                claimed_items.append(claimed)
        conn.commit()
        return TrackerReminderClaim(token=token, items=claimed_items)
    except BaseException:
        conn.rollback()
        raise


def claim_tracker_reminder(
    conn: sqlite3.Connection,
    item_id: int,
    now: datetime,
) -> TrackerReminderClaim:
    """Claim one due reminder for an owner-requested retry."""
    utc_now = _require_aware(now)
    now_iso = utc_now.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE auction_watches
            SET claim_token = NULL, claim_closing_at = NULL, claim_expires_at = NULL
            WHERE item_id = ? AND claim_token IS NOT NULL AND claim_expires_at <= ?
            """,
            (item_id, now_iso),
        )
        row = _joined_row(conn, item_id)
        if row is None or row["claim_token"] is not None:
            conn.commit()
            return TrackerReminderClaim(token=None, items=[])

        metadata = json.loads(row["raw_metadata"])
        facts = adapter_for_source(row["source_type"]).facts(
            metadata,
            is_present=row["inactive_at"] is None,
            now=utc_now,
        )
        if (
            not facts.active
            or facts.deadline is None
            or facts.reminder_key is None
            or facts.reminder_due_at is None
            or not (facts.reminder_due_at <= utc_now < facts.deadline)
            or row["reminder_sent_for_closing_at"] == facts.reminder_key
        ):
            conn.commit()
            return TrackerReminderClaim(token=None, items=[])

        token = secrets.token_urlsafe(32)
        cursor = conn.execute(
            """
            UPDATE auction_watches
            SET claim_token = ?, claim_closing_at = ?, claim_expires_at = ?,
                last_error = NULL
            WHERE item_id = ? AND claim_token IS NULL
            """,
            (
                token,
                facts.reminder_key,
                (utc_now + _CLAIM_LEASE).isoformat(),
                item_id,
            ),
        )
        if not cursor.rowcount:
            conn.commit()
            return TrackerReminderClaim(token=None, items=[])
        claimed = _row_to_dict(row, utc_now)
        claimed["claimed_closing_at"] = facts.reminder_key
        conn.commit()
        return TrackerReminderClaim(token=token, items=[claimed])
    except BaseException:
        conn.rollback()
        raise


def complete_tracker_reminder_claim(
    conn: sqlite3.Connection, claim_token: str, sent_at: datetime
) -> int:
    utc_sent_at = _require_aware(sent_at)
    cursor = conn.execute(
        """
        UPDATE auction_watches
        SET reminder_sent_for_closing_at = claim_closing_at,
            reminder_sent_at = ?,
            claim_token = NULL,
            claim_closing_at = NULL,
            claim_expires_at = NULL,
            last_error = NULL
        WHERE claim_token = ?
        """,
        (utc_sent_at.isoformat(), claim_token),
    )
    conn.commit()
    return cursor.rowcount


def fail_tracker_reminder_claim(
    conn: sqlite3.Connection, claim_token: str, error: str
) -> int:
    cursor = conn.execute(
        """
        UPDATE auction_watches
        SET claim_token = NULL,
            claim_closing_at = NULL,
            claim_expires_at = NULL,
            last_error = ?
        WHERE claim_token = ?
        """,
        (error, claim_token),
    )
    conn.commit()
    return cursor.rowcount
