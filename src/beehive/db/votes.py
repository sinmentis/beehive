"""Vote is keyed 1:1 on item_id (schema.sql), so casting a new vote always overwrites any
prior one for the same Item (mutable, no history). Un-voting is a delete, not a
value of 0 — there is no neutral row, only "a vote exists" or "it doesn't"."""
from __future__ import annotations

import sqlite3


def upsert_vote(conn: sqlite3.Connection, item_id: int, value: int,
                 reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO votes (item_id, value, reason, voted_at) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now')) "
        "ON CONFLICT(item_id) DO UPDATE SET value = excluded.value, reason = excluded.reason, "
        "voted_at = excluded.voted_at",
        (item_id, value, reason))
    conn.commit()


def delete_vote(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM votes WHERE item_id = ?", (item_id,))
    conn.commit()


def get_vote(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM votes WHERE item_id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


_TARGET_PER_POLARITY = 15
_CAP = 30


def get_vote_examples_for_channel(conn: sqlite3.Connection, channel_id: int) -> list[dict]:
    def _fetch(value: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT votes.value, votes.reason, items.title FROM votes "
            "JOIN items ON items.id = votes.item_id "
            "JOIN sources ON sources.id = items.source_id "
            "WHERE sources.channel_id = ? AND votes.value = ? "
            "ORDER BY votes.voted_at DESC",
            (channel_id, value)).fetchall()

    ups = _fetch(1)
    downs = _fetch(-1)

    selected_ups = ups[:_TARGET_PER_POLARITY]
    selected_downs = downs[:_TARGET_PER_POLARITY]

    shortfall = (2 * _TARGET_PER_POLARITY) - len(selected_ups) - len(selected_downs)
    if shortfall > 0:
        if len(selected_ups) < _TARGET_PER_POLARITY:
            selected_downs = list(selected_downs) + list(
                downs[_TARGET_PER_POLARITY:_TARGET_PER_POLARITY + shortfall])
        elif len(selected_downs) < _TARGET_PER_POLARITY:
            selected_ups = list(selected_ups) + list(
                ups[_TARGET_PER_POLARITY:_TARGET_PER_POLARITY + shortfall])

    combined = list(selected_ups) + list(selected_downs)
    return [{"title": r["title"], "value": r["value"], "reason": r["reason"]} for r in combined]
