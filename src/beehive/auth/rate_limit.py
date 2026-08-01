"""Login rate-limiting: lock out after repeated failures rather than relying on the login-attempt
log being forensic-only. Every threshold is a named constant here, not a scattered magic number.

Two ceilings, because they defend against two different attacks:

- **Per-IP** (`MAX_FAILED_ATTEMPTS`): the ordinary case, one source guessing repeatedly.
- **Global** (`MAX_GLOBAL_FAILED_ATTEMPTS`): a per-IP limit alone is defeated by rotating source
  addresses, which costs an attacker with a botnet or a proxy pool essentially nothing. Beehive is
  a single-admin app, so legitimate failures are rare and a global ceiling is affordable: past it
  every login is refused until the window passes. That is a deliberate self-lockout -- for one
  admin, a short window of refused logins beats an open brute-force channel -- and it clears on
  its own after the same 15 minutes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from beehive.db.admin_login_attempts import count_recent_failures, count_recent_failures_all

MAX_FAILED_ATTEMPTS = 5
MAX_GLOBAL_FAILED_ATTEMPTS = 50
LOCKOUT_WINDOW_MINUTES = 15


def is_locked_out(conn: sqlite3.Connection, ip: str, now: datetime) -> bool:
    since = (now - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    if count_recent_failures(conn, ip, since) >= MAX_FAILED_ATTEMPTS:
        return True
    return count_recent_failures_all(conn, since) >= MAX_GLOBAL_FAILED_ATTEMPTS
