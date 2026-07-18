"""Research Plan Revision persistence: the visible, append-only history of what a Research
Run's plan looked like at each version. Never edited or deleted -- a revision is a fact about
what the AI proposed, not a mutable draft."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.domain.research import ResearchPlanRevision


def _row_to_revision(row: sqlite3.Row) -> ResearchPlanRevision:
    return ResearchPlanRevision(
        id=row["id"],
        run_id=row["run_id"],
        version=row["version"],
        plan_json=row["plan_json"],
        rationale=row["rationale"],
        is_validated=bool(row["is_validated"]),
        created_at=datetime.fromisoformat(row["created_at"]))


def create_plan_revision(conn: sqlite3.Connection, run_id: int, plan_json: str, rationale: str,
                          is_validated: bool, now: datetime) -> ResearchPlanRevision:
    """Allocates version as MAX(version)+1 for the run under BEGIN IMMEDIATE, so two
    concurrent revision writes for the same run_id can never collide on the same version
    (UNIQUE(run_id, version) is defense in depth if that were ever violated)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
            "FROM research_plan_revisions WHERE run_id = ?",
            (run_id,)).fetchone()
        version = row["next_version"]
        cur = conn.execute(
            "INSERT INTO research_plan_revisions "
            "(run_id, version, plan_json, rationale, is_validated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, version, plan_json, rationale, int(is_validated), now.isoformat()))
        revision_id = cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_plan_revision(conn, revision_id)


def get_plan_revision(conn: sqlite3.Connection, revision_id: int) -> ResearchPlanRevision | None:
    row = conn.execute(
        "SELECT * FROM research_plan_revisions WHERE id = ?", (revision_id,)).fetchone()
    return _row_to_revision(row) if row else None


def list_plan_revisions(conn: sqlite3.Connection, run_id: int) -> list[ResearchPlanRevision]:
    rows = conn.execute(
        "SELECT * FROM research_plan_revisions WHERE run_id = ? ORDER BY version",
        (run_id,)).fetchall()
    return [_row_to_revision(r) for r in rows]


def get_latest_plan_revision(conn: sqlite3.Connection,
                              run_id: int) -> ResearchPlanRevision | None:
    row = conn.execute(
        "SELECT * FROM research_plan_revisions WHERE run_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (run_id,)).fetchone()
    return _row_to_revision(row) if row else None
