"""The Owner's last "mark all as read", kept for a few minutes so it can be taken back.

Marking a Channel or the featured list read can clear thousands of stories in one click. The
batch's item ids go into app_state, one batch at a time, so a new one replaces the last. Undoing
it marks exactly those stories unread again."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.db import app_state

UNDO_WINDOW = timedelta(minutes=10)
_KEY = "read_batch"
# SQLite binds at most 32,766 values per statement; stay well under it.
_CHUNK = 900


@dataclass(frozen=True, slots=True)
class ReadBatch:
    token: str
    count: int


def record_read_batch(
    conn: sqlite3.Connection, item_ids: list[int], now: datetime
) -> ReadBatch | None:
    """Keeps `item_ids` as the batch to undo, or keeps nothing when no story changed."""
    if not item_ids:
        return None
    token = secrets.token_urlsafe(9)
    app_state.set(
        conn, _KEY, json.dumps({"token": token, "at": now.isoformat(), "ids": item_ids})
    )
    return ReadBatch(token=token, count=len(item_ids))


def _load(conn: sqlite3.Connection, token: str | None, now: datetime) -> list[int] | None:
    if not token:
        return None
    try:
        batch = json.loads(app_state.get(conn, _KEY) or "null")
        marked_at = datetime.fromisoformat(batch["at"])
        ids = [int(item_id) for item_id in batch["ids"]]
    except (TypeError, ValueError, KeyError):
        return None
    # Compared as bytes: compare_digest refuses a str with anything outside ASCII.
    if not secrets.compare_digest(str(batch.get("token", "")).encode(), token.encode()):
        return None
    if now - marked_at > UNDO_WINDOW:
        return None
    return ids


def get_read_batch(
    conn: sqlite3.Connection, token: str | None, now: datetime
) -> ReadBatch | None:
    """The batch `token` names, while it can still be undone."""
    ids = _load(conn, token, now)
    return ReadBatch(token=token, count=len(ids)) if ids and token else None


def undo_read_batch(conn: sqlite3.Connection, token: str | None, now: datetime) -> int:
    """Marks the batch's stories unread again and forgets the batch. Returns how many it
    restored: 0 when the batch has expired or was already undone."""
    ids = _load(conn, token, now)
    if not ids:
        return 0
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(f"UPDATE items SET is_read = 0 WHERE id IN ({placeholders})", chunk)
    conn.commit()
    app_state.delete(conn, _KEY)
    return len(ids)
