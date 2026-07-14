"""INSERT OR IGNORE against the UNIQUE(source_id, external_id) constraint is the dedup
enforcement point: a duplicate fetch is silently dropped at the DB layer, never
re-scored. Ordering by ai_score DESC puts NULL (not-yet-ranked) rows last for free — SQLite
sorts NULL as the smallest value, so DESC naturally pushes them to the bottom without needing
NULLS LAST syntax."""
from __future__ import annotations

import json
import sqlite3

from beehive.connectors.base import RawItem


def insert_new(conn: sqlite3.Connection, source_id: int, raw_item: RawItem) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (source_id, external_id, title, url, body, "
        "created_at, raw_metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, raw_item.external_id, raw_item.title, raw_item.url, raw_item.body,
         raw_item.created_at.isoformat() if raw_item.created_at else None,
         json.dumps(raw_item.raw_metadata)))
    conn.commit()
    return cur.rowcount > 0


def update_ai_ranking(conn: sqlite3.Connection, source_id: int, external_id: str,
                       score: float, summary: str, rationale: str) -> None:
    conn.execute(
        "UPDATE items SET ai_score = ?, ai_summary = ?, ai_rationale = ? "
        "WHERE source_id = ? AND external_id = ?",
        (score, summary, rationale, source_id, external_id))
    conn.commit()


def update_ai_ranking_by_id(conn: sqlite3.Connection, item_id: int, score: float,
                             summary: str, rationale: str) -> None:
    conn.execute(
        "UPDATE items SET ai_score = ?, ai_summary = ?, ai_rationale = ? WHERE id = ?",
        (score, summary, rationale, item_id))
    conn.commit()


def mark_item_opened(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE items SET opened_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
        "WHERE id = ? AND opened_at IS NULL", (item_id,))
    conn.commit()


def update_best_comment(conn: sqlite3.Connection, item_id: int, summary: str) -> None:
    conn.execute(
        "UPDATE items SET best_comment_summary = ? WHERE id = ?", (summary, item_id))
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["raw_metadata"] = json.loads(d["raw_metadata"])
    return d


def list_by_channel(conn: sqlite3.Connection, channel_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "votes.value AS vote_value, votes.reason AS vote_reason "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        "WHERE sources.channel_id = ? ORDER BY items.ai_score DESC, items.id ASC",
        (channel_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_new_since(conn: sqlite3.Connection, channel_id: int, since_iso: str) -> list[dict]:
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "WHERE sources.channel_id = ? AND items.fetched_at > ? "
        "ORDER BY items.ai_score DESC, items.id ASC",
        (channel_id, since_iso)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "votes.value AS vote_value, votes.reason AS vote_reason "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        "WHERE items.id = ?",
        (item_id,)).fetchone()
    return _row_to_dict(row) if row else None


def mark_read(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE items SET is_read = 1 WHERE id = ?", (item_id,))
    conn.commit()


def mark_channel_read(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute(
        "UPDATE items SET is_read = 1 WHERE is_read = 0 AND source_id IN "
        "(SELECT id FROM sources WHERE channel_id = ?)",
        (channel_id,))
    conn.commit()


def list_archive(conn: sqlite3.Connection, channel_id: int | None = None,
                  date_from: str | None = None, date_to: str | None = None,
                  read_state: str | None = None, search: str | None = None, page: int = 1,
                  page_size: int = 30) -> tuple[list[dict], int]:
    # date(...) truncates fetched_at's full ISO-T timestamp to just its date part before
    # comparing, so a bare "YYYY-MM-DD" date_to/date_from correctly includes/excludes an
    # entire day — comparing the raw timestamp strings directly would make date_to exclude
    # every item fetched ON that day (e.g. "2026-07-01T08:00:00" <= "2026-07-01" is False,
    # since the longer string with a matching prefix sorts as greater).
    where = ["1=1"]
    params: list = []
    if channel_id is not None:
        where.append("sources.channel_id = ?")
        params.append(channel_id)
    if date_from is not None:
        where.append("date(items.fetched_at) >= date(?)")
        params.append(date_from)
    if date_to is not None:
        where.append("date(items.fetched_at) <= date(?)")
        params.append(date_to)
    if read_state == "read":
        where.append("items.is_read = 1")
    elif read_state == "unread":
        where.append("items.is_read = 0")
    if search:
        # falsy check (not `is not None`): an empty search string from a blank form field
        # must behave like "no search filter", not "match nothing"
        where.append("(items.title LIKE ? OR items.ai_summary LIKE ? OR items.body LIKE ?)")
        like_pattern = f"%{search}%"
        params.extend([like_pattern, like_pattern, like_pattern])
    where_clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM items JOIN sources ON sources.id = items.source_id "
        f"WHERE {where_clause}", params).fetchone()[0]

    rows = conn.execute(
        f"SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        f"channels.name AS channel_name, channels.id AS item_channel_id, "
        f"votes.value AS vote_value, votes.reason AS vote_reason "
        f"FROM items JOIN sources ON sources.id = items.source_id "
        f"JOIN channels ON channels.id = sources.channel_id "
        f"LEFT JOIN votes ON votes.item_id = items.id "
        f"WHERE {where_clause} ORDER BY items.fetched_at DESC, items.id DESC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()

    return [_row_to_dict(r) for r in rows], total


def list_dashboard_highlights(
    conn: sqlite3.Connection,
    limit: int = 5,
    minimum_score: float | None = None,
) -> list[dict]:
    score_filter = "" if minimum_score is None else "AND items.ai_score >= ? "
    params = () if minimum_score is None else (minimum_score,)
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "channels.name AS channel_name, channels.id AS item_channel_id, "
        "votes.value AS vote_value, votes.reason AS vote_reason "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        "WHERE items.opened_at IS NULL AND items.ai_summary IS NOT NULL "
        "AND (votes.value IS NULL OR votes.value != -1) "
        f"{score_filter}"
        "ORDER BY items.ai_score DESC LIMIT ?",
        params + (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_dashboard_signals(
    conn: sqlite3.Connection,
    minimum_score: float | None = None,
) -> int:
    score_filter = "" if minimum_score is None else "AND items.ai_score >= ? "
    params = () if minimum_score is None else (minimum_score,)
    return conn.execute(
        "SELECT COUNT(*) FROM items "
        "LEFT JOIN votes ON votes.item_id = items.id "
        "WHERE items.opened_at IS NULL AND items.ai_summary IS NOT NULL "
        "AND (votes.value IS NULL OR votes.value != -1) "
        f"{score_filter}",
        params,
    ).fetchone()[0]
