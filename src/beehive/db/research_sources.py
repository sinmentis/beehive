"""Research Source persistence: rows scoped to exactly one Research Session (never shared or
recurring like feed `sources`). origin records whether the Owner added the source directly or
the Research Plan added it automatically (ADR-0007) -- application-controlled tool execution
still validates connector_type/config before use; this module only persists what was already
validated."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from beehive.domain.research import ResearchSource, ResearchSourceOrigin


def _row_to_source(row: sqlite3.Row) -> ResearchSource:
    return ResearchSource(
        id=row["id"],
        session_id=row["session_id"],
        connector_type=row["connector_type"],
        config=json.loads(row["config"]),
        origin=ResearchSourceOrigin(row["origin"]))


def create_research_source(conn: sqlite3.Connection, session_id: int, connector_type: str,
                            config: dict, origin: ResearchSourceOrigin,
                            now: datetime) -> ResearchSource:
    cur = conn.execute(
        "INSERT INTO research_sources (session_id, connector_type, config, origin, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, connector_type, json.dumps(config), origin.value, now.isoformat()))
    conn.commit()
    return get_research_source(conn, cur.lastrowid)


def get_research_source(conn: sqlite3.Connection, source_id: int) -> ResearchSource | None:
    row = conn.execute(
        "SELECT * FROM research_sources WHERE id = ?", (source_id,)).fetchone()
    return _row_to_source(row) if row else None


def list_research_sources(conn: sqlite3.Connection, session_id: int) -> list[ResearchSource]:
    rows = conn.execute(
        "SELECT * FROM research_sources WHERE session_id = ? ORDER BY id",
        (session_id,)).fetchall()
    return [_row_to_source(r) for r in rows]
