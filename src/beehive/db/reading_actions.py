"""Short-lived recovery for the Owner's latest individual read or unwatch action."""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.db import app_state

_KEY = "reading_action"
_WINDOW = timedelta(minutes=10)
_KINDS = frozenset({"read", "unread", "unwatch", "unwatch_inflight"})
_WATCH_COLUMNS = (
    "item_id", "watched_at", "reminder_sent_for_closing_at", "reminder_sent_at",
    "claim_token", "claim_closing_at", "claim_expires_at", "last_error",
)


@dataclass(frozen=True)
class ReadingAction:
    token: str
    kind: str
    can_undo: bool


def _record(
    conn: sqlite3.Connection, item_id: int, kind: str, now: datetime, *,
    before: dict | None = None, expected: dict | None = None,
) -> ReadingAction:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    token = secrets.token_urlsafe(16)
    payload = {
        "token": token, "item_id": item_id, "kind": kind, "at": now.isoformat(),
        "before": before, "expected": expected,
    }
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_KEY, json.dumps(payload)),
    )
    return ReadingAction(token, kind, before is not None)


def _load(conn: sqlite3.Connection, token: str | None, now: datetime) -> dict | None:
    if not token or len(token) > 64:
        return None
    try:
        value = json.loads(app_state.get(conn, _KEY) or "null")
        if not isinstance(value, dict) or value.get("kind") not in _KINDS:
            return None
        if type(value.get("item_id")) is not int or value["item_id"] <= 0:
            return None
        before = value.get("before")
        if before is not None:
            if not isinstance(before, dict):
                return None
            if value["kind"] in {"read", "unread"}:
                if before.get("is_read") not in (0, 1) or not isinstance(value.get("expected"), dict):
                    return None
            elif value["kind"] == "unwatch":
                if (
                    not set(_WATCH_COLUMNS) <= before.keys()
                    or before["item_id"] != value["item_id"]
                    or not isinstance(before["watched_at"], str)
                    or any(before[key] is not None for key in ("claim_token", "claim_closing_at", "claim_expires_at"))
                    or any(before[key] is not None and not isinstance(before[key], str) for key in _WATCH_COLUMNS[1:])
                    or (before["reminder_sent_at"] is None) != (before["reminder_sent_for_closing_at"] is None)
                ):
                    return None
            else:
                return None
        age = now - datetime.fromisoformat(value["at"])
        if not timedelta(0) <= age <= _WINDOW:
            return None
        if not secrets.compare_digest(str(value["token"]).encode(), token.encode()):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return value


def get_reading_action(
    conn: sqlite3.Connection, token: str | None, now: datetime,
) -> ReadingAction | None:
    record = _load(conn, token, now)
    if record is None:
        return None
    return ReadingAction(record["token"], record["kind"], record.get("before") is not None)


def discard_reading_action(conn: sqlite3.Connection, item_id: int | None = None) -> None:
    if item_id is not None:
        try:
            record = json.loads(app_state.get(conn, _KEY) or "null")
        except ValueError:
            return
        if not isinstance(record, dict) or record.get("item_id") != item_id:
            return
    app_state.delete(conn, _KEY)


def _read_state(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT items.is_read, items.opened_at, votes.voted_at "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        "WHERE items.id = ? AND channels.kind = 'editorial'",
        (item_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def change_read_state(
    conn: sqlite3.Connection, item_id: int, is_read: bool, now: datetime,
) -> ReadingAction:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        before = _read_state(conn, item_id)
        if before is None:
            raise LookupError("Editorial item not found")
        conn.execute("UPDATE items SET is_read = ? WHERE id = ?", (int(is_read), item_id))
        expected = {**before, "is_read": int(is_read)}
        return _record(
            conn, item_id, "read" if is_read else "unread", now,
            before=before if before != expected else None, expected=expected,
        )


def remove_watch_with_undo(
    conn: sqlite3.Connection, item_id: int, now: datetime,
) -> ReadingAction:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM auction_watches WHERE item_id = ?", (item_id,),
        ).fetchone()
        sending = row is not None and row["claim_token"] is not None
        # A delivery already in flight cannot be recalled or safely replayed by an undo.
        before = {key: row[key] for key in _WATCH_COLUMNS} if row and not sending else None
        conn.execute("DELETE FROM auction_watches WHERE item_id = ?", (item_id,))
        return _record(
            conn, item_id, "unwatch_inflight" if sending else "unwatch", now, before=before,
        )


def undo_reading_action(
    conn: sqlite3.Connection, token: str, now: datetime,
) -> bool:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        record = _load(conn, token, now)
        if record is None or record.get("before") is None:
            return False
        item_id, before = record["item_id"], record["before"]
        restored = False
        if record["kind"] in {"read", "unread"}:
            if _read_state(conn, item_id) == record["expected"]:
                conn.execute("UPDATE items SET is_read = ? WHERE id = ?", (before["is_read"], item_id))
                restored = True
        elif record["kind"] == "unwatch":
            exists = conn.execute(
                "SELECT 1 FROM items JOIN sources ON sources.id = items.source_id "
                "JOIN channels ON channels.id = sources.channel_id "
                "WHERE items.id = ? AND channels.kind = 'tracker'",
                (item_id,),
            ).fetchone()
            if exists:
                # Preserve sent/deadline state, but never overwrite a later watch selection.
                cursor = conn.execute(
                    f"INSERT OR IGNORE INTO auction_watches ({', '.join(_WATCH_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _WATCH_COLUMNS)})",
                    [before[key] for key in _WATCH_COLUMNS],
                )
                restored = cursor.rowcount > 0
        conn.execute("DELETE FROM app_state WHERE key = ?", (_KEY,))
        return restored
