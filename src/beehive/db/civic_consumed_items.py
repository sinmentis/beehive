"""Which source items a Civic Watch has already turned into a Candidate.

`civic_candidates.dedupe_key` answers "have I already published this development?"; this table
answers the different question "have I already *used* this article?". They are not the same
question because clustering is many-to-one: one Candidate is built from several documents, so a
resumed Run that re-clustered the same articles would produce a cluster with a different key and
a second Candidate for one development. UNIQUE(watch_id, item_key) makes that structurally
impossible -- an item belongs to exactly one Candidate per Watch, permanently.

The claim is taken in the same BEGIN IMMEDIATE that creates the Candidate (see
`beehive.civic.pipeline`), so two workers racing on the same morning's documents cannot both
believe they own the item: one inserts, the other sees the conflict and is told which Candidate
already owns it rather than being allowed to build a duplicate.

Claims are never released. An item consumed by a Candidate that was later rejected stays
consumed: re-reading the same article tomorrow would reach the same rejection, and re-running it
would only spend the pipeline's budget to arrive there again.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CivicConsumedItem:
    """One source item, and the Candidate that owns it."""

    id: int | None
    watch_id: int
    run_id: int
    candidate_id: int
    item_key: str
    source_key: str
    external_key: str
    canonical_url: str
    consumed_at: datetime

    def __post_init__(self) -> None:
        if not self.item_key.strip():
            raise ValueError("a consumed Civic item needs an item_key")
        if not self.source_key.strip():
            raise ValueError("a consumed Civic item needs a source_key")
        if not self.external_key.strip():
            raise ValueError("a consumed Civic item needs an external_key")


def _row_to_item(row: sqlite3.Row) -> CivicConsumedItem:
    return CivicConsumedItem(
        id=row["id"],
        watch_id=row["watch_id"],
        run_id=row["run_id"],
        candidate_id=row["candidate_id"],
        item_key=row["item_key"],
        source_key=row["source_key"],
        external_key=row["external_key"],
        canonical_url=row["canonical_url"],
        consumed_at=datetime.fromisoformat(row["consumed_at"]))


def claim_consumed_item(
    conn: sqlite3.Connection,
    *,
    watch_id: int,
    run_id: int,
    candidate_id: int,
    item_key: str,
    source_key: str,
    external_key: str,
    canonical_url: str,
    now: datetime,
) -> CivicConsumedItem:
    """Records that one item now belongs to one Candidate. Caller holds the transaction.

    Deliberately not transactional itself: the claim and the Candidate it belongs to must commit
    together or not at all, so this is only ever called from inside an open BEGIN IMMEDIATE.
    Raises ValueError when the item is already owned by a *different* Candidate -- an explicit
    conflict rather than a second Candidate for the same material -- and returns the existing row
    unchanged when the owner is the same Candidate, which is what makes a retried write a
    no-op."""
    existing = conn.execute(
        "SELECT * FROM civic_consumed_items WHERE watch_id = ? AND item_key = ?",
        (watch_id, item_key)).fetchone()
    if existing is not None:
        if existing["candidate_id"] != candidate_id:
            raise ValueError(
                f"Civic item {item_key!r} is already consumed by Civic Candidate "
                f"{existing['candidate_id']}")
        return _row_to_item(existing)
    cur = conn.execute(
        "INSERT INTO civic_consumed_items (watch_id, run_id, candidate_id, item_key, "
        "source_key, external_key, canonical_url, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (watch_id, run_id, candidate_id, item_key, source_key, external_key, canonical_url,
         now.isoformat()))
    return _row_to_item(conn.execute(
        "SELECT * FROM civic_consumed_items WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_consumed_item_keys(conn: sqlite3.Connection, watch_id: int) -> list[str]:
    """Every item this Watch has already used, for feeding straight into clustering."""
    rows = conn.execute(
        "SELECT item_key FROM civic_consumed_items WHERE watch_id = ? ORDER BY item_key",
        (watch_id,)).fetchall()
    return [row["item_key"] for row in rows]


def list_candidate_items(
    conn: sqlite3.Connection, candidate_id: int,
) -> list[CivicConsumedItem]:
    rows = conn.execute(
        "SELECT * FROM civic_consumed_items WHERE candidate_id = ? ORDER BY id",
        (candidate_id,)).fetchall()
    return [_row_to_item(row) for row in rows]


def list_run_items(conn: sqlite3.Connection, run_id: int) -> list[CivicConsumedItem]:
    rows = conn.execute(
        "SELECT * FROM civic_consumed_items WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [_row_to_item(row) for row in rows]


def get_consuming_candidate_id(
    conn: sqlite3.Connection, watch_id: int, item_key: str,
) -> int | None:
    row = conn.execute(
        "SELECT candidate_id FROM civic_consumed_items WHERE watch_id = ? AND item_key = ?",
        (watch_id, item_key)).fetchone()
    return row["candidate_id"] if row else None


__all__ = [
    "CivicConsumedItem",
    "claim_consumed_item",
    "get_consuming_candidate_id",
    "list_candidate_items",
    "list_consumed_item_keys",
    "list_run_items",
]
