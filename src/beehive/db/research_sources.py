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


def list_research_sources(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    include_inactive: bool = False,
    origin: ResearchSourceOrigin | None = None,
) -> list[ResearchSource]:
    clauses = ["session_id = ?"]
    params: list[object] = [session_id]
    if not include_inactive:
        clauses.append("is_active = 1")
    if origin is not None:
        clauses.append("origin = ?")
        params.append(origin.value)
    rows = conn.execute(
        "SELECT * FROM research_sources WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id",
        tuple(params),
    ).fetchall()
    return [_row_to_source(r) for r in rows]


def upsert_owner_research_source(
    conn: sqlite3.Connection,
    session_id: int,
    connector_type: str,
    config: dict,
    now: datetime,
) -> ResearchSource:
    rows = conn.execute(
        """
        SELECT *
        FROM research_sources
        WHERE session_id = ? AND connector_type = ?
        ORDER BY id
        """,
        (session_id, connector_type),
    ).fetchall()
    for row in rows:
        if json.loads(row["config"]) != config:
            continue
        conn.execute(
            """
            UPDATE research_sources
            SET origin = 'owner', is_active = 1
            WHERE id = ?
            """,
            (row["id"],),
        )
        conn.commit()
        return get_research_source(conn, row["id"])
    return create_research_source(
        conn,
        session_id,
        connector_type,
        config,
        ResearchSourceOrigin.OWNER,
        now,
    )


def update_research_source(
    conn: sqlite3.Connection,
    source_id: int,
    config: dict,
) -> ResearchSource:
    row = conn.execute(
        """
        SELECT session_id, connector_type
        FROM research_sources
        WHERE id = ? AND is_active = 1
        """,
        (source_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Research Source not found")
    duplicates = conn.execute(
        """
        SELECT id, config
        FROM research_sources
        WHERE session_id = ? AND connector_type = ? AND id != ?
        """,
        (row["session_id"], row["connector_type"], source_id),
    ).fetchall()
    if any(json.loads(candidate["config"]) == config for candidate in duplicates):
        raise ValueError("Research Source already exists")
    conn.execute(
        "UPDATE research_sources SET config = ? WHERE id = ? AND is_active = 1",
        (json.dumps(config), source_id),
    )
    conn.commit()
    return get_research_source(conn, source_id)


def deactivate_research_source(conn: sqlite3.Connection, source_id: int) -> bool:
    cursor = conn.execute(
        "UPDATE research_sources SET is_active = 0 WHERE id = ? AND is_active = 1",
        (source_id,),
    )
    conn.commit()
    return cursor.rowcount > 0
