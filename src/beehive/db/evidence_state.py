"""Evidence State Revision persistence: an immutable, versioned snapshot of "which canonical
Evidence Items are part of the Research Session's active evidence" at one moment -- curation
decisions baked into a citable fact. A Research Synthesis or chat reply pins one of these by id
so it stays reproducible even after later curation changes, never a live join over
evidence_curation.py's mutable table."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.domain.research import EvidenceSnapshotStatus, EvidenceStateRevision


def _row_to_revision(conn: sqlite3.Connection, row: sqlite3.Row) -> EvidenceStateRevision:
    item_rows = conn.execute(
        "SELECT evidence_item_id FROM research_evidence_state_revision_items "
        "WHERE revision_id = ? ORDER BY evidence_item_id",
        (row["id"],)).fetchall()
    return EvidenceStateRevision(
        id=row["id"],
        session_id=row["session_id"],
        version=row["version"],
        snapshot_id=row["snapshot_id"],
        evidence_item_ids=tuple(r["evidence_item_id"] for r in item_rows),
        created_at=datetime.fromisoformat(row["created_at"]))


def create_evidence_state_revision(conn: sqlite3.Connection, session_id: int, snapshot_id: int,
                                    evidence_item_ids: list[int],
                                    now: datetime) -> EvidenceStateRevision:
    """Freezes the given set of Evidence Item ids as a new immutable revision. version is
    allocated as MAX(version)+1 for the session under BEGIN IMMEDIATE, and the revision plus
    its item membership are written in the same transaction so a reader can never observe a
    revision row with no items yet (or vice versa). Raises ValueError if the referenced
    Evidence Snapshot is missing, belongs to a different Research Session than session_id (the
    two ids must never silently disagree), or is not yet 'sealed' -- a revision must be built
    from a snapshot whose membership can no longer change underneath it, never a still-
    'building' one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        snapshot_row = conn.execute(
            "SELECT session_id, status FROM research_snapshots WHERE id = ?",
            (snapshot_id,)).fetchone()
        if snapshot_row is None:
            raise ValueError(f"no Evidence Snapshot with id={snapshot_id}")
        if snapshot_row["session_id"] != session_id:
            raise ValueError(
                f"Evidence Snapshot {snapshot_id} belongs to Research Session "
                f"{snapshot_row['session_id']}, not {session_id}")
        if snapshot_row["status"] != EvidenceSnapshotStatus.SEALED.value:
            raise ValueError(
                f"cannot create an Evidence State Revision from Evidence Snapshot "
                f"{snapshot_id}: status is {snapshot_row['status']!r}, not 'sealed'")
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
            "FROM research_evidence_state_revisions WHERE session_id = ?",
            (session_id,)).fetchone()
        version = row["next_version"]
        cur = conn.execute(
            "INSERT INTO research_evidence_state_revisions "
            "(session_id, version, snapshot_id, created_at) VALUES (?, ?, ?, ?)",
            (session_id, version, snapshot_id, now.isoformat()))
        revision_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO research_evidence_state_revision_items "
            "(revision_id, evidence_item_id) VALUES (?, ?)",
            [(revision_id, item_id) for item_id in evidence_item_ids])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_evidence_state_revision(conn, revision_id)


def set_curation_and_create_evidence_state_revision(
        conn: sqlite3.Connection, session_id: int, evidence_item_id: int, is_excluded: bool,
        note: str, now: datetime) -> EvidenceStateRevision:
    """Atomically changes one curation decision and freezes the resulting active evidence.

    The latest sealed snapshot, its membership, the complete live curation overlay, version
    allocation, and revision membership are all read or written under one BEGIN IMMEDIATE lock.
    A concurrent snapshot finalization or opposite curation change therefore serializes entirely
    before or after this transition. No later revision can be built from stale snapshot membership
    or from a curation decision that another writer already replaced.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        item_row = conn.execute(
            "SELECT session_id FROM research_evidence_items WHERE id = ?",
            (evidence_item_id,)).fetchone()
        if item_row is None:
            raise ValueError(f"no Evidence Item with id={evidence_item_id}")
        if item_row["session_id"] != session_id:
            raise ValueError(
                f"Evidence Item {evidence_item_id} belongs to Research Session "
                f"{item_row['session_id']}, not {session_id} (foreign-session)")

        snapshot_row = conn.execute(
            "SELECT id FROM research_snapshots "
            "WHERE session_id = ? AND status = ? "
            "ORDER BY sequence_number DESC LIMIT 1",
            (session_id, EvidenceSnapshotStatus.SEALED.value)).fetchone()
        if snapshot_row is None:
            raise ValueError(
                f"Research Session {session_id} has no sealed Evidence Snapshot yet; "
                "curation requires at least one completed Research Run first")
        snapshot_id = snapshot_row["id"]

        now_iso = now.isoformat()
        conn.execute(
            "INSERT INTO research_evidence_curation "
            "(evidence_item_id, is_excluded, note, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(evidence_item_id) DO UPDATE SET "
            "is_excluded = excluded.is_excluded, note = excluded.note, "
            "updated_at = excluded.updated_at",
            (evidence_item_id, int(is_excluded), note, now_iso))

        member_rows = conn.execute(
            "SELECT si.evidence_item_id, ei.session_id AS item_session_id, "
            "COALESCE(cur.is_excluded, 0) AS is_excluded "
            "FROM research_snapshot_items si "
            "JOIN research_evidence_items ei ON ei.id = si.evidence_item_id "
            "LEFT JOIN research_evidence_curation cur "
            "ON cur.evidence_item_id = si.evidence_item_id "
            "WHERE si.snapshot_id = ? ORDER BY si.evidence_item_id",
            (snapshot_id,)).fetchall()
        if any(row["item_session_id"] != session_id for row in member_rows):
            raise ValueError(
                f"Evidence Snapshot {snapshot_id} contains evidence from a foreign session")
        active_ids = [
            row["evidence_item_id"] for row in member_rows if not row["is_excluded"]]

        version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
            "FROM research_evidence_state_revisions WHERE session_id = ?",
            (session_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO research_evidence_state_revisions "
            "(session_id, version, snapshot_id, created_at) VALUES (?, ?, ?, ?)",
            (session_id, version_row["next_version"], snapshot_id, now_iso))
        revision_id = cur.lastrowid
        if active_ids:
            conn.executemany(
                "INSERT INTO research_evidence_state_revision_items "
                "(revision_id, evidence_item_id) VALUES (?, ?)",
                [(revision_id, item_id) for item_id in active_ids])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_evidence_state_revision(conn, revision_id)


def get_evidence_state_revision(conn: sqlite3.Connection,
                                 revision_id: int) -> EvidenceStateRevision | None:
    row = conn.execute(
        "SELECT * FROM research_evidence_state_revisions WHERE id = ?",
        (revision_id,)).fetchone()
    return _row_to_revision(conn, row) if row else None


def get_latest_evidence_state_revision(conn: sqlite3.Connection,
                                        session_id: int) -> EvidenceStateRevision | None:
    row = conn.execute(
        "SELECT * FROM research_evidence_state_revisions WHERE session_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (session_id,)).fetchone()
    return _row_to_revision(conn, row) if row else None


def get_evidence_state_revision_for_snapshot(
        conn: sqlite3.Connection, snapshot_id: int) -> EvidenceStateRevision | None:
    """Returns the EARLIEST (lowest-version) Evidence State Revision built from `snapshot_id`,
    if any -- used by research.orchestrator.py's crash-recovery resume path (ADR-0009/0010) to
    find the exact revision `db.research_finalization.finalize_snapshot_if_claimed` created
    atomically together with sealing this snapshot, after a run whose prior attempt already won
    that atomic write crashed or lost its claim before ever generating a synthesis. `ORDER BY
    version LIMIT 1` (not DESC) matters here: a later Owner curation change
    (`research.synthesis._rebuild_evidence_state_revision`) may have since built ADDITIONAL,
    newer revisions pointing at this same snapshot_id (a sealed snapshot's membership never
    changes, but which of its members are "active" can be re-derived at any time), and only the
    FIRST one -- created in the same transaction as the seal itself -- is the one this run's own
    finalization actually produced. It is safe to resume synthesis against this exact revision
    only if a synthesis was already persisted for it (the crashed attempt's original output);
    otherwise -- see `get_latest_evidence_state_revision_for_snapshot` -- a later curation change
    may have since made it stale (`research.synthesis.pin_evidence_for_synthesis` requires the
    session's own latest revision), and resume must target that later revision instead."""
    row = conn.execute(
        "SELECT * FROM research_evidence_state_revisions WHERE snapshot_id = ? "
        "ORDER BY version LIMIT 1",
        (snapshot_id,)).fetchone()
    return _row_to_revision(conn, row) if row else None


def get_latest_evidence_state_revision_for_snapshot(
        conn: sqlite3.Connection, snapshot_id: int) -> EvidenceStateRevision | None:
    """Returns the LATEST (highest-version) Evidence State Revision built from `snapshot_id`, if
    any -- the sibling of `get_evidence_state_revision_for_snapshot`'s EARLIEST-revision lookup,
    used by research.orchestrator.py's crash-recovery resume path (ADR-0009/0010) when the
    earliest (atomic-finalization) revision for a resumed run's sealed snapshot has NO synthesis
    yet: an Owner may have curated evidence (`research.synthesis.exclude_evidence_item`/
    `restore_evidence_item`) while the run was crashed or pending reclaim, building one or more
    newer revisions against this SAME still-sealed snapshot (a sealed snapshot's own item
    membership never changes; only which of its members are "active" is re-derived). Resuming
    synthesis against the stale earliest revision would be rejected by
    `research.synthesis.pin_evidence_for_synthesis` (it requires the session's own current
    revision) -- this lookup is scoped to `snapshot_id` alone, so it can never return a revision
    for a foreign snapshot or session: since exactly one Research Session ever owns a given
    sealed snapshot (and ADR-0009's one-active-run-per-session rule means no other run could have
    sealed a newer snapshot for this same session while this run was still active), the revision
    this returns is, in ordinary operation, also that session's own latest revision overall."""
    row = conn.execute(
        "SELECT * FROM research_evidence_state_revisions WHERE snapshot_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (snapshot_id,)).fetchone()
    return _row_to_revision(conn, row) if row else None


def list_evidence_state_revisions(conn: sqlite3.Connection,
                                   session_id: int) -> list[EvidenceStateRevision]:
    rows = conn.execute(
        "SELECT * FROM research_evidence_state_revisions WHERE session_id = ? "
        "ORDER BY version",
        (session_id,)).fetchall()
    return [_row_to_revision(conn, r) for r in rows]
