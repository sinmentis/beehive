"""INSERT OR IGNORE against the UNIQUE(source_id, external_id) constraint is the dedup
enforcement point: a duplicate fetch is silently dropped at the DB layer, never
re-scored. Ordering by ai_score DESC puts NULL (not-yet-ranked) rows last for free — SQLite
sorts NULL as the smallest value, so DESC naturally pushes them to the bottom without needing
NULLS LAST syntax.

Two persistence models share this table. The editorial APPEND model inserts a row once and never
mutates it (insert_new). The monitor/tracker MUTABLE_SNAPSHOT model refetches the same stable
external_id every cycle and refreshes one current row in place (upsert_mutable_item), tracking a
listing's life with last_seen_at / inactive_at and collapsing history with superseded_at. Every
normal "current items" read filters superseded_at IS NULL by default; migration/audit callers opt
back in with include_superseded=True."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from beehive.connectors.base import RawItem


def _serialize_metadata(raw_metadata: dict) -> str:
    return json.dumps(raw_metadata, sort_keys=True, separators=(",", ":"))


def _sql_now(conn: sqlite3.Connection) -> str:
    """One SQLite-formatted UTC timestamp (matching schema.sql's strftime DEFAULTs, so it stays
    lexicographically comparable with fetched_at and the isoformat strings used elsewhere)."""
    return conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now')").fetchone()[0]


def _ranking_metadata_changed(
    before_metadata: dict,
    after_metadata: dict,
    ranking_metadata_keys: frozenset[str] | None,
) -> bool:
    """True when any of ranking_metadata_keys differs between two decoded raw_metadata dicts. A
    missing key reads as None on either side, so gaining or losing one of the watched keys counts
    as a change while a move confined to keys outside the set (e.g. an image URL) does not."""
    if not ranking_metadata_keys:
        return False
    return any(
        before_metadata.get(key) != after_metadata.get(key)
        for key in ranking_metadata_keys
    )


def insert_new(conn: sqlite3.Connection, source_id: int, raw_item: RawItem) -> bool:
    """Insert one APPEND-model item, or drop it silently when (source_id, external_id) already
    exists. Returns True only when a genuinely new row was written. Use insert_new_returning_id
    for the same write when the caller also needs the new row's id (e.g. to stage a discovered
    event for it)."""
    return insert_new_returning_id(conn, source_id, raw_item) is not None


def insert_new_returning_id(
    conn: sqlite3.Connection, source_id: int, raw_item: RawItem
) -> int | None:
    """INSERT OR IGNORE one APPEND-model item and return the new row's id, or None when the
    (source_id, external_id) already existed so the insert was ignored. rowcount -- not lastrowid --
    is the authority on whether a row was actually written: lastrowid can still report a previously
    inserted row after an ignored insert, so it is only trusted when rowcount confirms the write."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (source_id, external_id, title, url, body, "
        "created_at, raw_metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            raw_item.external_id,
            raw_item.title,
            raw_item.url,
            raw_item.body,
            raw_item.created_at.isoformat() if raw_item.created_at else None,
            _serialize_metadata(raw_item.raw_metadata),
        ),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount > 0 else None


def upsert_refreshable_item(
    conn: sqlite3.Connection, source_id: int, raw_item: RawItem
) -> bool:
    """Legacy in-place refresh for a stable item, kept as a thin compatibility path for the
    auction (tracker) collector until it moves to upsert_mutable_item. Returns True if the row was
    inserted or changed, False if a re-fetch found it identical. Unlike upsert_mutable_item it
    resets is_read on any change and does not maintain last_seen_at / inactive_at -- callers that
    need the full MUTABLE_SNAPSHOT lifecycle must use upsert_mutable_item instead."""
    existing = conn.execute(
        "SELECT id, title, url, body, created_at, raw_metadata FROM items "
        "WHERE source_id = ? AND external_id = ?",
        (source_id, raw_item.external_id),
    ).fetchone()
    if existing is None:
        return insert_new(conn, source_id, raw_item)

    created_at = raw_item.created_at.isoformat() if raw_item.created_at else None
    raw_metadata = _serialize_metadata(raw_item.raw_metadata)
    unchanged = (
        existing["title"] == raw_item.title
        and existing["url"] == raw_item.url
        and existing["body"] == raw_item.body
        and existing["created_at"] == created_at
        and existing["raw_metadata"] == raw_metadata
    )
    if unchanged:
        return False

    conn.execute(
        "UPDATE items SET title = ?, url = ?, body = ?, created_at = ?, "
        "raw_metadata = ?, fetched_at = strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
        "ai_score = NULL, ai_summary = NULL, ai_rationale = NULL, is_read = 0 "
        "WHERE id = ?",
        (
            raw_item.title,
            raw_item.url,
            raw_item.body,
            created_at,
            raw_metadata,
            existing["id"],
        ),
    )
    conn.commit()
    return True


class MutableUpsertOutcome(str, Enum):
    """Whether upsert_mutable_item created, changed, or merely re-saw a listing. Distinct from a
    bare bool so a caller can tell a first sighting (INSERTED) apart from a state change (UPDATED)
    apart from an unchanged re-fetch (UNCHANGED) -- the three drive different downstream events."""

    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class MutableUpsertResult:
    """The full outcome of one MUTABLE_SNAPSHOT upsert. before_metadata/after_metadata are the
    decoded raw_metadata on either side of the write (before is None for an insert), so later
    event detection can compare price/availability even when the change did not warrant an AI
    rerank. ranking_reset is True only when ranking-relevant content changed and the AI fields were
    cleared; reappeared is True when a previously inactive listing was seen again (a back-in-stock
    signal)."""

    outcome: MutableUpsertOutcome
    item_id: int
    ranking_reset: bool
    reappeared: bool
    before_metadata: dict | None
    after_metadata: dict


def upsert_mutable_item(
    conn: sqlite3.Connection,
    source_id: int,
    raw_item: RawItem,
    *,
    now_iso: str | None = None,
    ranking_metadata_keys: frozenset[str] | None = None,
) -> MutableUpsertResult:
    """Refresh the single current row for a stable (source_id, external_id), or insert it.

    Every call bumps last_seen_at and clears inactive_at (the listing is present in this snapshot).
    Read state is preserved -- unlike the editorial refresh, a monitor/tracker item is a state
    snapshot, not a "signal to read", so is_read is never forced back to 0. The AI fields are reset
    (the listing re-enters the ranking backlog) ONLY when ranking-relevant content changes; any
    other move refreshes raw_metadata in place, keeps the existing score, and is surfaced to the
    caller through before_metadata/after_metadata so event detection can still act on it.

    "Ranking-relevant" is always title/url/body. ranking_metadata_keys optionally extends it with
    the raw_metadata keys the caller's ranker actually consumes (a Channel-level concern the caller
    supplies -- this module never hard-codes which listing fields matter): a change to any of those
    keys also resets ranking, while a change confined to keys outside the set (e.g. an image URL)
    refreshes in place without reranking. Omitting it keeps the historical title/url/body-only
    behavior for direct callers."""
    if now_iso is None:
        now_iso = _sql_now(conn)

    after_metadata = dict(raw_item.raw_metadata)
    after_metadata_json = _serialize_metadata(raw_item.raw_metadata)
    created_at = raw_item.created_at.isoformat() if raw_item.created_at else None

    existing = conn.execute(
        "SELECT id, title, url, body, created_at, raw_metadata, inactive_at FROM items "
        "WHERE source_id = ? AND external_id = ?",
        (source_id, raw_item.external_id),
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            "INSERT INTO items (source_id, external_id, title, url, body, created_at, "
            "raw_metadata, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                raw_item.external_id,
                raw_item.title,
                raw_item.url,
                raw_item.body,
                created_at,
                after_metadata_json,
                now_iso,
            ),
        )
        conn.commit()
        return MutableUpsertResult(
            outcome=MutableUpsertOutcome.INSERTED,
            item_id=cur.lastrowid,
            ranking_reset=False,
            reappeared=False,
            before_metadata=None,
            after_metadata=after_metadata,
        )

    before_metadata = json.loads(existing["raw_metadata"])
    reappeared = existing["inactive_at"] is not None
    ranking_relevant_changed = (
        existing["title"] != raw_item.title
        or existing["url"] != raw_item.url
        or existing["body"] != raw_item.body
        or _ranking_metadata_changed(
            before_metadata, after_metadata, ranking_metadata_keys
        )
    )
    content_changed = (
        ranking_relevant_changed
        or existing["created_at"] != created_at
        or existing["raw_metadata"] != after_metadata_json
    )

    set_clauses = [
        "title = ?",
        "url = ?",
        "body = ?",
        "created_at = ?",
        "raw_metadata = ?",
        "last_seen_at = ?",
        "inactive_at = NULL",
    ]
    params: list = [
        raw_item.title,
        raw_item.url,
        raw_item.body,
        created_at,
        after_metadata_json,
        now_iso,
    ]
    if ranking_relevant_changed:
        # Only a change to what the listing *is* re-enters the ranking backlog (ai_* NULL) and
        # counts as freshly fetched. A price/stock move keeps its score and its fetched_at.
        set_clauses += [
            "ai_score = NULL",
            "ai_summary = NULL",
            "ai_rationale = NULL",
            "fetched_at = ?",
        ]
        params.append(now_iso)

    conn.execute(
        f"UPDATE items SET {', '.join(set_clauses)} WHERE id = ?",
        [*params, existing["id"]],
    )
    conn.commit()

    outcome = (
        MutableUpsertOutcome.UPDATED
        if (content_changed or reappeared)
        else MutableUpsertOutcome.UNCHANGED
    )
    return MutableUpsertResult(
        outcome=outcome,
        item_id=existing["id"],
        ranking_reset=ranking_relevant_changed,
        reappeared=reappeared,
        before_metadata=before_metadata,
        after_metadata=after_metadata,
    )


def mark_absent_items_inactive(
    conn: sqlite3.Connection,
    source_id: int,
    present_external_ids: list[str],
    *,
    now_iso: str | None = None,
) -> list[int]:
    """Reconcile a Source's current listings against one complete, successful snapshot: every
    active (superseded_at IS NULL, inactive_at IS NULL) row whose external_id is absent from
    present_external_ids is marked inactive_at = now and never deleted, so its id, ranking, and
    interactions survive and it can be revived later by upsert_mutable_item. Returns the ids just
    marked inactive. An empty present_external_ids means the snapshot legitimately contained
    nothing, so every currently-active listing for the Source goes inactive."""
    if now_iso is None:
        now_iso = _sql_now(conn)

    # A temp table (rather than a NOT IN (?, ?, ...) list) keeps a large snapshot well clear of
    # SQLite's bound-parameter limit and lets the empty-snapshot case fall out naturally.
    conn.execute("DROP TABLE IF EXISTS _reconcile_present")
    conn.execute("CREATE TEMP TABLE _reconcile_present (external_id TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO _reconcile_present (external_id) VALUES (?)",
        [(external_id,) for external_id in present_external_ids],
    )
    absent_filter = (
        "source_id = ? AND superseded_at IS NULL AND inactive_at IS NULL "
        "AND external_id NOT IN (SELECT external_id FROM _reconcile_present)"
    )
    affected = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM items WHERE {absent_filter}", (source_id,)
        ).fetchall()
    ]
    if affected:
        conn.execute(
            f"UPDATE items SET inactive_at = ? WHERE {absent_filter}",
            (now_iso, source_id),
        )
    conn.execute("DROP TABLE _reconcile_present")
    conn.commit()
    return affected


def update_ai_ranking(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
    score: float,
    summary: str,
    rationale: str,
) -> None:
    conn.execute(
        "UPDATE items SET ai_score = ?, ai_summary = ?, ai_rationale = ? "
        "WHERE source_id = ? AND external_id = ?",
        (score, summary, rationale, source_id, external_id),
    )
    conn.commit()


def update_ai_ranking_by_id(
    conn: sqlite3.Connection, item_id: int, score: float, summary: str, rationale: str
) -> None:
    conn.execute(
        "UPDATE items SET ai_score = ?, ai_summary = ?, ai_rationale = ? WHERE id = ?",
        (score, summary, rationale, item_id),
    )
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


def list_unread_rewrite_candidates(
    conn: sqlite3.Connection,
    high_water_item_id: int,
    after_id: int = 0,
    limit: int = 20,
) -> list[dict]:
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
        "AND items.superseded_at IS NULL "
        "AND items.id > ? AND items.id <= ? "
        "ORDER BY items.id ASC LIMIT ?",
        (after_id, high_water_item_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def revert_ai_summary_if_unchanged(
    conn: sqlite3.Connection,
    item_id: int,
    expected_current_summary: str,
    restored_summary: str,
) -> bool:
    """Rollback's write half: restores ai_summary to its pre-rewrite value, but only if the
    row's ai_summary still equals exactly what this run wrote (`expected_current_summary`) --
    guarding against clobbering a later, unrelated edit (e.g. a further rewrite run, or a
    manual admin fix) that happened to land on this same item after this run touched it."""
    cur = conn.execute(
        "UPDATE items SET ai_summary = ? WHERE id = ? AND ai_summary = ?",
        (restored_summary, item_id, expected_current_summary),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_item_opened(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE items SET opened_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
        "WHERE id = ? AND opened_at IS NULL",
        (item_id,),
    )
    conn.commit()


def update_best_comment(conn: sqlite3.Connection, item_id: int, summary: str) -> None:
    conn.execute(
        "UPDATE items SET best_comment_summary = ? WHERE id = ?", (summary, item_id)
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["raw_metadata"] = json.loads(d["raw_metadata"])
    return d


def list_by_channel(
    conn: sqlite3.Connection, channel_id: int, *, include_superseded: bool = False
) -> list[dict]:
    """Every item in the Channel, highest AI score first. Superseded history rows (older mutable
    duplicates collapsed by the stable-id compaction) are hidden by default; include_superseded is
    the explicit opt-in used by migration/audit callers that need to see the collapsed rows."""
    superseded_filter = "" if include_superseded else "AND items.superseded_at IS NULL "
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "channels.kind AS channel_kind, "
        "votes.value AS vote_value, votes.reason AS vote_reason "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        f"WHERE sources.channel_id = ? {superseded_filter}"
        "ORDER BY items.ai_score DESC, items.id ASC",
        (channel_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_new_since(
    conn: sqlite3.Connection, channel_id: int, since_iso: str
) -> list[dict]:
    rows = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "channels.kind AS channel_kind "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "WHERE sources.channel_id = ? AND items.superseded_at IS NULL "
        "AND items.fetched_at > ? "
        "ORDER BY items.ai_score DESC, items.id ASC",
        (channel_id, since_iso),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        "channels.kind AS channel_kind, "
        "votes.value AS vote_value, votes.reason AS vote_reason "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id "
        "LEFT JOIN votes ON votes.item_id = items.id "
        "WHERE items.id = ?",
        (item_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def mark_read(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE items SET is_read = 1 WHERE id = ?", (item_id,))
    conn.commit()


def mark_channel_read(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute(
        "UPDATE items SET is_read = 1 WHERE is_read = 0 AND source_id IN "
        "(SELECT id FROM sources WHERE channel_id = ?)",
        (channel_id,),
    )
    conn.commit()


def delete_by_channel(conn: sqlite3.Connection, channel_id: int) -> int:
    """Wipes every item fetched under this Channel (e.g. so an admin can re-test a changed
    ranking profile against a clean slate). Votes, deep-read state, and summary-rewrite log
    rows all cascade away with their parent item via ON DELETE CASCADE. Returns the number of
    items removed so the caller can surface it in a confirmation message."""
    cur = conn.execute(
        "DELETE FROM items WHERE source_id IN "
        "(SELECT id FROM sources WHERE channel_id = ?)",
        (channel_id,),
    )
    conn.commit()
    return cur.rowcount


def list_archive(
    conn: sqlite3.Connection,
    channel_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    read_state: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 30,
) -> tuple[list[dict], int]:
    # date(...) truncates fetched_at's full ISO-T timestamp to just its date part before
    # comparing, so a bare "YYYY-MM-DD" date_to/date_from correctly includes/excludes an
    # entire day — comparing the raw timestamp strings directly would make date_to exclude
    # every item fetched ON that day (e.g. "2026-07-01T08:00:00" <= "2026-07-01" is False,
    # since the longer string with a matching prefix sorts as greater).
    where = ["1=1"]
    params: list = []
    where.append("channels.kind = 'editorial'")
    # Superseded history rows (older mutable duplicates) are never part of the archive's
    # current-item view.
    where.append("items.superseded_at IS NULL")
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
        where.append(
            "(items.title LIKE ? OR items.ai_summary LIKE ? OR items.body LIKE ?)"
        )
        like_pattern = f"%{search}%"
        params.extend([like_pattern, like_pattern, like_pattern])
    where_clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM items JOIN sources ON sources.id = items.source_id "
        f"JOIN channels ON channels.id = sources.channel_id "
        f"WHERE {where_clause}",
        params,
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT items.*, sources.type AS source_type, sources.config AS source_config, "
        f"channels.name AS channel_name, channels.id AS item_channel_id, "
        f"channels.kind AS channel_kind, "
        f"votes.value AS vote_value, votes.reason AS vote_reason "
        f"FROM items JOIN sources ON sources.id = items.source_id "
        f"JOIN channels ON channels.id = sources.channel_id "
        f"LEFT JOIN votes ON votes.item_id = items.id "
        f"WHERE {where_clause} ORDER BY items.fetched_at DESC, items.id DESC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()

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
        params + [limit],
    ).fetchall()
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
        "channels.kind = 'editorial'",
        "items.superseded_at IS NULL",
        "items.ai_summary IS NOT NULL",
        "items.ai_score >= channels.minimum_score",
        "(votes.value IS NULL OR votes.value != -1)",
    ]
    params: list = []
    if minimum_score is not None:
        where.append("items.ai_score >= ?")
        params.append(minimum_score)
    publication_time = (
        "COALESCE(datetime(items.created_at), datetime(items.fetched_at))"
    )
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
