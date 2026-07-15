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


# ============================================================================
# Summary-only rewrite (collector/summary_rewrite.py): candidate lookup + rollback support for
# the unread-summary rewrite tool. Eligibility is exactly is_read = 0 AND ai_score IS NOT NULL
# AND ai_summary IS NOT NULL -- an item that was never ranked (ai_score/ai_summary still NULL)
# is not "rewritten", it is still awaiting its first ranking pass, so it must never be touched
# here. Only ai_summary is ever read/written by this section; score, rationale, votes,
# best_comment_summary, is_read and opened_at are never referenced, let alone written.
#
# The actual rewrite WRITE (re-checking this same eligibility) lives in
# db/summary_rewrites.py's apply_summary_rewrite, not here -- it has to happen in the same
# transaction as that module's summary_rewrite_log INSERT, which a standalone,
# separately-committing function in this file could not guarantee (a prior revision here,
# update_ai_summary_if_unread, committed independently of its caller's later log insert,
# which is exactly the crash/failure gap apply_summary_rewrite's single seam closes).
# ============================================================================

def list_unread_rewrite_candidates(conn: sqlite3.Connection, high_water_item_id: int,
                                    after_id: int = 0, limit: int = 20) -> list[dict]:
    """Deterministic oldest-first page of rewrite candidates, keyset-paginated on `items.id`
    (the autoincrement PK, so ascending id IS oldest-first) rather than LIMIT/OFFSET -- an
    OFFSET page would silently skip or repeat rows if an earlier item's is_read flips between
    two pages of the same run, while `id > after_id` only ever walks forward. `high_water_item_id`
    is the caller-supplied pre-deployment watermark: any item with a newer (larger) id than
    this was never touched by the old prompt contract, so it is excluded from candidacy
    entirely, not just from being rewritten."""
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "WHERE items.is_read = 0 AND items.ai_score IS NOT NULL AND items.ai_summary IS NOT NULL "
        "AND items.id > ? AND items.id <= ? "
        "ORDER BY items.id ASC LIMIT ?",
        (after_id, high_water_item_id, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def revert_ai_summary_if_unchanged(conn: sqlite3.Connection, item_id: int,
                                    expected_current_summary: str,
                                    restored_summary: str) -> bool:
    """Rollback's write half: restores ai_summary to its pre-rewrite value, but only if the
    row's ai_summary still equals exactly what this run wrote (`expected_current_summary`) --
    guarding against clobbering a later, unrelated edit (e.g. a further rewrite run, or a
    manual admin fix) that happened to land on this same item after this run touched it."""
    cur = conn.execute(
        "UPDATE items SET ai_summary = ? WHERE id = ? AND ai_summary = ?",
        (restored_summary, item_id, expected_current_summary))
    conn.commit()
    return cur.rowcount > 0


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
    published_from: str | None = None,
    published_to: str | None = None,
    read_state: str = "all",
) -> list[dict]:
    where, params = _dashboard_signal_filters(
        minimum_score=minimum_score,
        published_from=published_from,
        published_to=published_to,
        read_state=read_state,
    )
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "channels.name AS channel_name, channels.id AS item_channel_id, "
        "votes.value AS vote_value, votes.reason AS vote_reason "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY items.ai_score DESC, items.id ASC LIMIT ?",
        params + [limit]).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_dashboard_signals(
    conn: sqlite3.Connection,
    minimum_score: float | None = None,
    published_from: str | None = None,
    published_to: str | None = None,
    read_state: str = "all",
) -> int:
    where, params = _dashboard_signal_filters(
        minimum_score=minimum_score,
        published_from=published_from,
        published_to=published_to,
        read_state=read_state,
    )
    return conn.execute(
        "SELECT COUNT(*) FROM items "
        "JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchone()[0]


def _dashboard_signal_filters(
    *,
    minimum_score: float | None,
    published_from: str | None,
    published_to: str | None,
    read_state: str,
) -> tuple[list[str], list]:
    where = [
        "items.ai_summary IS NOT NULL",
        "items.ai_score >= channels.minimum_score",
        "(votes.value IS NULL OR votes.value != -1)",
    ]
    params: list = []
    if minimum_score is not None:
        where.append("items.ai_score >= ?")
        params.append(minimum_score)
    publication_time = "COALESCE(datetime(items.created_at), datetime(items.fetched_at))"
    if published_from is not None:
        where.append(f"{publication_time} >= datetime(?)")
        params.append(published_from)
    if published_to is not None:
        where.append(f"{publication_time} < datetime(?)")
        params.append(published_to)
    if read_state == "read":
        where.append("items.is_read = 1")
    elif read_state == "unread":
        where.append("items.is_read = 0")
    return where, params
