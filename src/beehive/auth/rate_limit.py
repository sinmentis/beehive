"""Login rate-limiting: lock out an IP after repeated failures rather than
relying on the login-attempt log being forensic-only. Both thresholds are named constants here,
not scattered magic numbers, so they're easy to tune later."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from beehive.db.admin_login_attempts import count_recent_failures

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15


def is_locked_out(conn: sqlite3.Connection, ip: str, now: datetime) -> bool:
    since = (now - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    return count_recent_failures(conn, ip, since) >= MAX_FAILED_ATTEMPTS
