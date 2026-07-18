"""Evidence Cluster persistence: snapshot-scoped groupings of Evidence Items that describe the
same underlying event. Clusters reference their snapshot and members reference their cluster,
but research_evidence_items itself has no column pointing back at a cluster -- membership is
expressed one-directionally through research_evidence_cluster_items only, so this pair can
never form a circular FK with the canonical evidence table."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.domain.research import EvidenceCluster, EvidenceSnapshotStatus


def _row_to_cluster(conn: sqlite3.Connection, row: sqlite3.Row) -> EvidenceCluster:
    item_rows = conn.execute(
        "SELECT evidence_item_id FROM research_evidence_cluster_items "
        "WHERE cluster_id = ? ORDER BY evidence_item_id",
        (row["id"],)).fetchall()
    return EvidenceCluster(
        id=row["id"],
        snapshot_id=row["snapshot_id"],
        evidence_item_ids=tuple(r["evidence_item_id"] for r in item_rows))


def create_evidence_cluster(conn: sqlite3.Connection, snapshot_id: int,
                             evidence_item_ids: list[int], now: datetime) -> EvidenceCluster:
    """Creates one cluster and its member rows in a single transaction, so a reader never
    observes a cluster with no members (domain.research.EvidenceCluster itself requires at
    least one item). Raises ValueError if the target Evidence Snapshot is missing or already
    'sealed' -- clustering only ever happens while a snapshot is still being assembled, never
    retroactively against a sealed (immutable) one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        snapshot_row = conn.execute(
            "SELECT status FROM research_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if snapshot_row is None:
            raise ValueError(f"no Evidence Snapshot with id={snapshot_id}")
        if snapshot_row["status"] != EvidenceSnapshotStatus.BUILDING.value:
            raise ValueError(
                f"cannot create an Evidence Cluster in Evidence Snapshot {snapshot_id}: "
                f"status is {snapshot_row['status']!r}, not 'building'")
        cur = conn.execute(
            "INSERT INTO research_evidence_clusters (snapshot_id, created_at) VALUES (?, ?)",
            (snapshot_id, now.isoformat()))
        cluster_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO research_evidence_cluster_items (cluster_id, evidence_item_id) "
            "VALUES (?, ?)",
            [(cluster_id, item_id) for item_id in evidence_item_ids])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_evidence_cluster(conn, cluster_id)


def get_evidence_cluster(conn: sqlite3.Connection, cluster_id: int) -> EvidenceCluster | None:
    row = conn.execute(
        "SELECT * FROM research_evidence_clusters WHERE id = ?", (cluster_id,)).fetchone()
    return _row_to_cluster(conn, row) if row else None


def list_evidence_clusters(conn: sqlite3.Connection, snapshot_id: int) -> list[EvidenceCluster]:
    rows = conn.execute(
        "SELECT * FROM research_evidence_clusters WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,)).fetchall()
    return [_row_to_cluster(conn, r) for r in rows]
