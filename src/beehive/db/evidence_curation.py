"""Mutable Owner curation of canonical Evidence Items: exactly one row per evidence item,
upserted in place. This is deliberately NOT append-only or versioned -- evidence_state.py's
Evidence State Revisions are what turn a moment of curation into an immutable, citable fact
that a Research Synthesis or chat reply can pin."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EvidenceCuration:
    evidence_item_id: int
    is_excluded: bool
    note: str
    updated_at: datetime


def _row_to_curation(row: sqlite3.Row) -> EvidenceCuration:
    return EvidenceCuration(
        evidence_item_id=row["evidence_item_id"],
        is_excluded=bool(row["is_excluded"]),
        note=row["note"],
        updated_at=datetime.fromisoformat(row["updated_at"]))


def set_evidence_curation(conn: sqlite3.Connection, evidence_item_id: int, is_excluded: bool,
                           note: str, now: datetime) -> EvidenceCuration:
    """Upserts the single curation row for one Evidence Item. There is no history here by
    design -- only the current decision is kept; evidence_state.py freezes decisions that
    matter into an immutable revision."""
    conn.execute(
        "INSERT INTO research_evidence_curation (evidence_item_id, is_excluded, note, "
        "updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(evidence_item_id) DO UPDATE SET "
        "is_excluded = excluded.is_excluded, note = excluded.note, "
        "updated_at = excluded.updated_at",
        (evidence_item_id, int(is_excluded), note, now.isoformat()))
    conn.commit()
    return get_evidence_curation(conn, evidence_item_id)


def get_evidence_curation(conn: sqlite3.Connection,
                           evidence_item_id: int) -> EvidenceCuration | None:
    row = conn.execute(
        "SELECT * FROM research_evidence_curation WHERE evidence_item_id = ?",
        (evidence_item_id,)).fetchone()
    return _row_to_curation(row) if row else None


def list_evidence_curation(conn: sqlite3.Connection,
                            evidence_item_ids: list[int]) -> dict[int, EvidenceCuration]:
    if not evidence_item_ids:
        return {}
    placeholders = ", ".join("?" for _ in evidence_item_ids)
    rows = conn.execute(
        f"SELECT * FROM research_evidence_curation WHERE evidence_item_id IN ({placeholders})",
        list(evidence_item_ids)).fetchall()
    return {row["evidence_item_id"]: _row_to_curation(row) for row in rows}
