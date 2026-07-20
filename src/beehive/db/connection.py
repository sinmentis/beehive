"""The only module that imports sqlite3 directly (per the layered architecture — see spec
section 5). WAL mode lets the collector and web containers write the same file concurrently
without DuckDB-style single-writer contention (ADR-0004)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns added after the original schema.sql was written, for databases (like the live
# production one) that already had the table before this column existed. schema.sql's own
# CREATE TABLE already defines these for brand-new databases -- this list only matters for
# upgrading an EXISTING database in place, so init_schema() stays safe to call on every
# process start (as it already is) without needing a separate manual migration step.
_COLUMNS_TO_ENSURE = [
    ("items", "opened_at", "TEXT"),
    ("sources", "last_fetch_raw_count", "INTEGER"),
    ("sources", "last_fetch_new_count", "INTEGER"),
    ("items", "best_comment_summary", "TEXT"),
    ("channels", "digest_email", "TEXT"),
    ("channels", "last_digest_sent_at", "TEXT"),
    ("channels", "last_digest_date", "TEXT"),
    (
        "channels",
        "highlight_count",
        "INTEGER NOT NULL DEFAULT 8 CHECK (highlight_count BETWEEN 1 AND 50)",
    ),
    (
        "channels",
        "minimum_score",
        "INTEGER NOT NULL DEFAULT 0 CHECK (minimum_score BETWEEN 0 AND 100)",
    ),
    (
        "channels",
        "kind",
        "TEXT NOT NULL DEFAULT 'editorial' CHECK (kind IN ('editorial', 'monitor'))",
    ),
]

_CHANNEL_DIGEST_MIGRATION_KEY = "digest_channel_watermarks_migrated_v1"

# Runs once, ever (guarded by the app_state marker below), the first time init_schema() is
# called against a database that predates custom email groups (Task N+1). Without this, every
# 'editorial' Channel that used to get the old fixed once-daily digest would silently stop
# receiving any email at all the moment send_email_group_digests replaces send_daily_digest,
# since sending now happens per-group instead of per-Channel. 'monitor' Channels are left out of
# the auto-created group -- they never emailed before this feature existed, so there is nothing
# to preserve for them; an admin opts them into a group manually. Deliberately not folded into
# _migrate_channel_digest_watermarks: that migration only ever touches existing rows in place
# (idempotent even if re-run), whereas this one inserts a brand new email_groups row, so it needs
# its own marker to avoid a duplicate "Default" group if init_schema() ran again.
_DEFAULT_EMAIL_GROUP_MIGRATION_KEY = "default_email_group_migrated_v1"
_DEFAULT_EMAIL_GROUP_NAME = "Default"
_DEFAULT_EMAIL_GROUP_SUBJECT = "Beehive Daily Digest \u00b7 {date}"


def connect(db_path: str) -> sqlite3.Connection:
    # FastAPI may enter, use, and finalize one sync dependency on different worker threads.
    # Each request still owns its connection; this only disables sqlite3's thread-affinity guard.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _migrate_channel_digest_watermarks(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (_CHANNEL_DIGEST_MIGRATION_KEY,)).fetchone()
    if marker is not None:
        return

    legacy = conn.execute(
        "SELECT value FROM app_state WHERE key = 'last_digest_sent_at'").fetchone()
    if legacy is not None and legacy["value"]:
        legacy_timestamp = legacy["value"]
        conn.execute(
            "UPDATE channels SET last_digest_sent_at = ?, last_digest_date = ? "
            "WHERE last_digest_sent_at IS NULL",
            (legacy_timestamp, legacy_timestamp[:10]))

    conn.execute(
        "INSERT OR IGNORE INTO app_state (key, value) VALUES (?, '1')",
        (_CHANNEL_DIGEST_MIGRATION_KEY,))


def _migrate_default_email_group(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (_DEFAULT_EMAIL_GROUP_MIGRATION_KEY,)).fetchone()
    if marker is not None:
        return

    group_id = conn.execute(
        "INSERT INTO email_groups (name, subject_template, send_interval_hours) "
        "VALUES (?, ?, 24)",
        (_DEFAULT_EMAIL_GROUP_NAME, _DEFAULT_EMAIL_GROUP_SUBJECT)).lastrowid
    conn.execute(
        "INSERT INTO email_group_channels (email_group_id, channel_id) "
        "SELECT ?, id FROM channels WHERE kind = 'editorial'",
        (group_id,))

    conn.execute(
        "INSERT OR IGNORE INTO app_state (key, value) VALUES (?, '1')",
        (_DEFAULT_EMAIL_GROUP_MIGRATION_KEY,))


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_PATH.read_text())
    for table, column, column_type in _COLUMNS_TO_ENSURE:
        _ensure_column(conn, table, column, column_type)
    _migrate_channel_digest_watermarks(conn)
    _migrate_default_email_group(conn)
    conn.commit()
