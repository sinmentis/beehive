"""Canonical, session-scoped Evidence Item persistence (ADR-0010). "Canonical" means one row
per distinct piece of source material for the life of a Research Session -- re-collecting the
same item in a later run/snapshot upserts this same row (matched on
(research_source_id, external_key)) instead of inserting a duplicate, which is what lets
citation_number stay session-wide and stable: once assigned it is never reassigned, even if the
item is later excluded via evidence_curation.py's curation or dropped from a later cluster.

upsert_evidence_item_if_claimed is the claim-fenced entry point a Research Run worker should
use while actively collecting: it is upsert_evidence_item's logic plus a
(run_id, claim_token, status='processing') check in the same BEGIN IMMEDIATE transaction, so a
stale worker whose lease was recovered and reclaimed by someone else can never clobber (or
duplicate-insert) evidence after losing its claim -- it simply gets None back. Plain
upsert_evidence_item remains available for callers that are not run-claim-fenced (e.g. Owner-
authored or backfill evidence outside a worker's claim)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from beehive.domain.research import EvidenceItem, EvidenceQuality


def _row_to_item(row: sqlite3.Row) -> EvidenceItem:
    return EvidenceItem(
        id=row["id"],
        session_id=row["session_id"],
        research_source_id=row["research_source_id"],
        external_key=row["external_key"],
        title=row["title"],
        url=row["url"],
        citation_number=row["citation_number"],
        quality=EvidenceQuality(row["quality"]),
        snippet=row["snippet"],
        full_text=row["full_text"],
        raw_metadata=json.loads(row["raw_metadata"]))


def _fetch(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM research_evidence_items WHERE id = ?", (item_id,)).fetchone()


def upsert_evidence_item(conn: sqlite3.Connection, session_id: int, research_source_id: int,
                          external_key: str, title: str, url: str, quality: EvidenceQuality,
                          now: datetime, snippet: str = "", full_text: str | None = None,
                          raw_metadata: dict | None = None) -> EvidenceItem:
    """Inserts a new canonical Evidence Item, or refreshes an existing one's content in place
    if (research_source_id, external_key) was already collected -- either way the row's
    citation_number never changes once assigned. citation_number for a genuinely new item is
    allocated as MAX(citation_number)+1 for the session, and the whole read-check-write
    sequence runs under BEGIN IMMEDIATE so two concurrent collectors can never allocate the
    same citation_number twice."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM research_evidence_items "
            "WHERE research_source_id = ? AND external_key = ?",
            (research_source_id, external_key)).fetchone()
        raw_metadata_json = json.dumps(raw_metadata or {})
        if existing is not None:
            item_id = existing["id"]
            conn.execute(
                "UPDATE research_evidence_items SET title = ?, url = ?, snippet = ?, "
                "full_text = ?, quality = ?, raw_metadata = ? WHERE id = ?",
                (title, url, snippet, full_text, quality.value, raw_metadata_json, item_id))
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(citation_number), 0) + 1 AS next_number "
                "FROM research_evidence_items WHERE session_id = ?",
                (session_id,)).fetchone()
            citation_number = row["next_number"]
            cur = conn.execute(
                "INSERT INTO research_evidence_items "
                "(session_id, research_source_id, external_key, title, url, snippet, "
                "full_text, quality, raw_metadata, citation_number, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, research_source_id, external_key, title, url, snippet, full_text,
                 quality.value, raw_metadata_json, citation_number, now.isoformat()))
            item_id = cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_item(_fetch(conn, item_id))


def upsert_evidence_item_if_claimed(
        conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
        research_source_id: int, external_key: str, title: str, url: str,
        quality: EvidenceQuality, now: datetime, *, snippet: str = "",
        full_text: str | None = None, raw_metadata: dict | None = None,
        preserve_existing_full_text: bool = False) -> EvidenceItem | None:
    """Claim-fenced sibling of upsert_evidence_item, closing the stale-worker evidence-clobber
    race: before writing anything, verifies -- under the same BEGIN IMMEDIATE transaction as
    the write itself -- that run_id is still 'processing' with exactly this claim_token and
    belongs to session_id. If a worker's claim was stolen (its lease expired, was recovered,
    and the run was reclaimed by another worker) by the time this call runs, the run row no
    longer matches and this returns None with zero side effects: no INSERT, no UPDATE, no
    citation_number consumed. A caller must treat None as "stop collecting immediately", the
    same way every other progressive write in this package (research_runs.py's
    advance_research_run_phase/reserve_deep_fetch, research_chat_requests.py's
    complete_chat_request_with_reply) treats a fenced write returning nothing.

    preserve_existing_full_text=True protects an existing row's full_text (e.g. already
    populated by a prior deep-fetch) from being wiped back to NULL by a later, shallower
    refresh call that only has a fresh snippet/title/url to report: passing full_text=None
    together with preserve_existing_full_text=True keeps whatever full_text is already stored.
    Passing an actual new full_text always overwrites it regardless of this flag -- the flag
    only ever protects against *clearing* deep text, never against refreshing it. It has no
    effect on a brand-new item (there is nothing yet to preserve)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        run_row = conn.execute(
            "SELECT session_id FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if run_row is None or run_row["session_id"] != session_id:
            result = None
        else:
            existing = conn.execute(
                "SELECT id FROM research_evidence_items "
                "WHERE research_source_id = ? AND external_key = ?",
                (research_source_id, external_key)).fetchone()
            raw_metadata_json = json.dumps(raw_metadata or {})
            if existing is not None:
                item_id = existing["id"]
                if preserve_existing_full_text:
                    conn.execute(
                        "UPDATE research_evidence_items SET title = ?, url = ?, snippet = ?, "
                        "full_text = COALESCE(?, full_text), quality = ?, raw_metadata = ? "
                        "WHERE id = ?",
                        (title, url, snippet, full_text, quality.value, raw_metadata_json,
                         item_id))
                else:
                    conn.execute(
                        "UPDATE research_evidence_items SET title = ?, url = ?, snippet = ?, "
                        "full_text = ?, quality = ?, raw_metadata = ? WHERE id = ?",
                        (title, url, snippet, full_text, quality.value, raw_metadata_json,
                         item_id))
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(citation_number), 0) + 1 AS next_number "
                    "FROM research_evidence_items WHERE session_id = ?",
                    (session_id,)).fetchone()
                citation_number = row["next_number"]
                cur = conn.execute(
                    "INSERT INTO research_evidence_items "
                    "(session_id, research_source_id, external_key, title, url, snippet, "
                    "full_text, quality, raw_metadata, citation_number, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, research_source_id, external_key, title, url, snippet,
                     full_text, quality.value, raw_metadata_json, citation_number,
                     now.isoformat()))
                item_id = cur.lastrowid
            result = _row_to_item(_fetch(conn, item_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def get_evidence_item(conn: sqlite3.Connection, item_id: int) -> EvidenceItem | None:
    row = _fetch(conn, item_id)
    return _row_to_item(row) if row else None


def get_evidence_items(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, EvidenceItem]:
    """Batch lookup -- one parameterized IN(...) query rather than N calls to
    get_evidence_item, for the same reason db/deep_reads.py's get_deep_reads_for_items does."""
    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT * FROM research_evidence_items WHERE id IN ({placeholders})",
        list(item_ids)).fetchall()
    return {row["id"]: _row_to_item(row) for row in rows}


def list_evidence_items_for_session(conn: sqlite3.Connection,
                                     session_id: int) -> list[EvidenceItem]:
    rows = conn.execute(
        "SELECT * FROM research_evidence_items WHERE session_id = ? ORDER BY citation_number",
        (session_id,)).fetchall()
    return [_row_to_item(r) for r in rows]
