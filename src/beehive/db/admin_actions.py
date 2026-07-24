from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any

_UNDO_WINDOW = timedelta(days=7)
_ITEM_TABLES = (
    "votes",
    "deep_reads",
    "auction_watches",
    "item_events",
    "summary_rewrite_log",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(f"SELECT * FROM {table} WHERE {where}", params)
    ]


def _item_snapshot(
    conn: sqlite3.Connection,
    source_ids: list[int],
) -> dict[str, list[dict[str, Any]]]:
    if not source_ids:
        return {"items": [], **{table: [] for table in _ITEM_TABLES}}
    placeholders = ", ".join("?" for _ in source_ids)
    items = _rows(
        conn,
        "items",
        f"source_id IN ({placeholders})",
        tuple(source_ids),
    )
    item_ids = [int(item["id"]) for item in items]
    snapshot: dict[str, list[dict[str, Any]]] = {"items": items}
    if not item_ids:
        snapshot.update({table: [] for table in _ITEM_TABLES})
        return snapshot
    item_placeholders = ", ".join("?" for _ in item_ids)
    for table in _ITEM_TABLES:
        snapshot[table] = _rows(
            conn,
            table,
            f"item_id IN ({item_placeholders})",
            tuple(item_ids),
        )
    return snapshot


def _channel_snapshot(conn: sqlite3.Connection, channel_id: int) -> dict[str, Any]:
    channels = _rows(conn, "channels", "id = ?", (channel_id,))
    if not channels:
        raise ValueError("Channel not found")
    sources = _rows(conn, "sources", "channel_id = ?", (channel_id,))
    source_ids = [int(source["id"]) for source in sources]
    return {
        "channels": channels,
        "sources": sources,
        "email_group_channels": _rows(
            conn,
            "email_group_channels",
            "channel_id = ?",
            (channel_id,),
        ),
        **_item_snapshot(conn, source_ids),
    }


def _source_snapshot(conn: sqlite3.Connection, source_id: int) -> dict[str, Any]:
    sources = _rows(conn, "sources", "id = ?", (source_id,))
    if not sources:
        raise ValueError("Source not found")
    return {
        "sources": sources,
        **_item_snapshot(conn, [source_id]),
    }


def _pack(payload: dict[str, Any]) -> bytes:
    return zlib.compress(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(),
        level=6,
    )


def _unpack(payload: bytes) -> dict[str, Any]:
    value = json.loads(zlib.decompress(payload))
    if not isinstance(value, dict):
        raise ValueError("Invalid admin recovery payload")
    return value


def _record_action(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    target_type: str,
    target_id: int | None,
    target_label: str,
    detail: dict[str, Any],
    undo_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> int:
    created_at = (now or _utc_now()).astimezone(timezone.utc)
    cursor = conn.execute(
        """
        INSERT INTO admin_actions (
            action_type, target_type, target_id, target_label, detail_json,
            undo_payload, undo_expires_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_type,
            target_type,
            target_id,
            target_label,
            json.dumps(detail, separators=(",", ":"), ensure_ascii=True),
            _pack(undo_payload) if undo_payload is not None else None,
            (
                (created_at + _UNDO_WINDOW).isoformat()
                if undo_payload is not None
                else None
            ),
            created_at.isoformat(),
        ),
    )
    return int(cursor.lastrowid)


def record_admin_action(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    target_type: str,
    target_id: int | None,
    target_label: str,
    detail: dict[str, Any] | None = None,
) -> int:
    action_id = _record_action(
        conn,
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        detail=detail or {},
    )
    conn.commit()
    return action_id


def delete_channel_with_undo(
    conn: sqlite3.Connection,
    channel_id: int,
    *,
    target_label: str,
) -> int:
    snapshot = _channel_snapshot(conn, channel_id)
    item_count = len(snapshot["items"])
    action_id = _record_action(
        conn,
        action_type="channel_deleted",
        target_type="channel",
        target_id=channel_id,
        target_label=target_label,
        detail={
            "sources": len(snapshot["sources"]),
            "items": item_count,
            "votes": len(snapshot["votes"]),
            "deep_reads": len(snapshot["deep_reads"]),
            "watches": len(snapshot["auction_watches"]),
            "events": len(snapshot["item_events"]),
        },
        undo_payload=snapshot,
    )
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    return action_id


def delete_source_with_undo(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    target_label: str,
) -> int:
    snapshot = _source_snapshot(conn, source_id)
    action_id = _record_action(
        conn,
        action_type="source_deleted",
        target_type="source",
        target_id=source_id,
        target_label=target_label,
        detail={
            "items": len(snapshot["items"]),
            "votes": len(snapshot["votes"]),
            "deep_reads": len(snapshot["deep_reads"]),
            "watches": len(snapshot["auction_watches"]),
            "events": len(snapshot["item_events"]),
        },
        undo_payload=snapshot,
    )
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    return action_id


def clear_channel_with_undo(
    conn: sqlite3.Connection,
    channel_id: int,
    *,
    target_label: str,
) -> tuple[int, int]:
    snapshot = _channel_snapshot(conn, channel_id)
    payload = {
        key: value
        for key, value in snapshot.items()
        if key not in {"channels", "email_group_channels"}
    }
    item_count = len(payload["items"])
    action_id = _record_action(
        conn,
        action_type="channel_data_cleared",
        target_type="channel",
        target_id=channel_id,
        target_label=target_label,
        detail={
            "items": item_count,
            "votes": len(payload["votes"]),
            "deep_reads": len(payload["deep_reads"]),
            "watches": len(payload["auction_watches"]),
            "events": len(payload["item_events"]),
        },
        undo_payload=payload,
    )
    source_ids = [int(source["id"]) for source in payload["sources"]]
    if source_ids:
        placeholders = ", ".join("?" for _ in source_ids)
        conn.execute(
            f"DELETE FROM items WHERE source_id IN ({placeholders})",
            source_ids,
        )
    conn.execute(
        """
        UPDATE sources
        SET last_fetch_at = NULL,
            last_fetch_error = NULL,
            last_fetch_raw_count = NULL,
            last_fetch_new_count = NULL,
            last_attempt_at = NULL,
            last_fetch_status = NULL
        WHERE channel_id = ?
        """,
        (channel_id,),
    )
    conn.commit()
    return action_id, item_count


def delete_email_group_with_undo(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    target_label: str,
) -> int:
    groups = _rows(conn, "email_groups", "id = ?", (group_id,))
    if not groups:
        raise ValueError("Email group not found")
    memberships = _rows(
        conn,
        "email_group_channels",
        "email_group_id = ?",
        (group_id,),
    )
    action_id = _record_action(
        conn,
        action_type="email_group_deleted",
        target_type="email_group",
        target_id=group_id,
        target_label=target_label,
        detail={"channels": len(memberships)},
        undo_payload={
            "email_groups": groups,
            "email_group_channels": memberships,
        },
    )
    conn.execute("DELETE FROM email_groups WHERE id = ?", (group_id,))
    conn.commit()
    return action_id


def list_admin_actions(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = conn.execute(
        """
        SELECT id, action_type, target_type, target_id, target_label, detail_json,
               undo_payload IS NOT NULL AS has_undo, undo_expires_at, undone_at,
               created_at
        FROM admin_actions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    now_iso = _utc_now().isoformat()
    actions: list[dict[str, Any]] = []
    for row in rows:
        action = dict(row)
        action["detail"] = json.loads(action.pop("detail_json"))
        action["can_undo"] = bool(
            action.pop("has_undo")
            and action["undone_at"] is None
            and action["undo_expires_at"]
            and action["undo_expires_at"] > now_iso
        )
        actions.append(action)
    return actions


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )


def _restore_sources(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
) -> None:
    for source in sources:
        source_id = int(source["id"])
        exists = conn.execute(
            "SELECT 1 FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if exists is None:
            _insert_rows(conn, "sources", [source])
            continue
        columns = [column for column in source if column != "id"]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        conn.execute(
            f"UPDATE sources SET {assignments} WHERE id = ?",
            (*[source[column] for column in columns], source_id),
        )


def undo_admin_action(conn: sqlite3.Connection, action_id: int) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM admin_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Admin action not found")
        if row["undo_payload"] is None:
            raise ValueError("Admin action cannot be undone")
        if row["undone_at"] is not None:
            raise ValueError("Admin action was already undone")
        if row["undo_expires_at"] <= _utc_now().isoformat():
            raise ValueError("Admin action undo window has expired")

        payload = _unpack(row["undo_payload"])
        action_type = row["action_type"]
        if action_type == "channel_deleted":
            _insert_rows(conn, "channels", payload.get("channels", []))
            _insert_rows(conn, "sources", payload.get("sources", []))
        elif action_type == "source_deleted":
            _insert_rows(conn, "sources", payload.get("sources", []))
        elif action_type == "channel_data_cleared":
            _restore_sources(conn, payload.get("sources", []))
        elif action_type == "email_group_deleted":
            _insert_rows(conn, "email_groups", payload.get("email_groups", []))
        else:
            raise ValueError("Unsupported admin action recovery")

        _insert_rows(conn, "items", payload.get("items", []))
        for table in _ITEM_TABLES:
            _insert_rows(conn, table, payload.get(table, []))
        if action_type in {"channel_deleted", "email_group_deleted"}:
            _insert_rows(
                conn,
                "email_group_channels",
                payload.get("email_group_channels", []),
            )

        undone_at = _utc_now().isoformat()
        conn.execute(
            "UPDATE admin_actions SET undone_at = ? WHERE id = ?",
            (undone_at, action_id),
        )
        conn.commit()
        return {
            "id": action_id,
            "action_type": action_type,
            "target_label": row["target_label"],
            "undone_at": undone_at,
        }
    except BaseException:
        conn.rollback()
        raise
