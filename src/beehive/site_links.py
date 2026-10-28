"""The explicitly configured website address used in outbound email links."""
from __future__ import annotations

import sqlite3

from pydantic import HttpUrl

from beehive.db import app_state

SITE_URL_KEY = "site_url"


def normalize_site_url(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Website address contains control characters")
    parsed = HttpUrl(value)
    if (
        parsed.username is not None or parsed.password is not None
        or parsed.query is not None or parsed.fragment is not None
    ):
        raise ValueError("Website address must not contain credentials, a query or a fragment")
    return str(parsed).rstrip("/")


def load_site_url(conn: sqlite3.Connection) -> str | None:
    try:
        return normalize_site_url(app_state.get(conn, SITE_URL_KEY))
    except ValueError:
        return None


def save_site_url(conn: sqlite3.Connection, value: str | None) -> None:
    normalized = normalize_site_url(value)
    if normalized is None:
        app_state.delete(conn, SITE_URL_KEY)
    else:
        app_state.set(conn, SITE_URL_KEY, normalized)
