"""Tiny key-value bookkeeping table for singleton state that doesn't belong to any one
Channel/Source/Item — currently just the digest send watermark (Task 16)."""
from __future__ import annotations

import sqlite3


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value))
    conn.commit()


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
    conn.commit()
