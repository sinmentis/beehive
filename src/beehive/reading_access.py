"""Whether the reading pages need the Owner's sign-in (ADR-0011). They are public by default."""
from __future__ import annotations

import sqlite3

from beehive.db import app_state

_READING_PRIVATE_KEY = "reading_private"


def reading_is_private(conn: sqlite3.Connection) -> bool:
    return app_state.get(conn, _READING_PRIVATE_KEY) == "1"


def set_reading_private(conn: sqlite3.Connection, private: bool) -> None:
    app_state.set(conn, _READING_PRIVATE_KEY, "1" if private else "0")
