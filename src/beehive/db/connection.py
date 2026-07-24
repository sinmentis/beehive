"""The only module that imports sqlite3 directly (per the layered architecture — see spec
section 5). WAL mode lets the collector and web containers write the same file concurrently
without DuckDB-style single-writer contention (ADR-0004)."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# The legacy "<product-id>:<price>" external_id suffix is a two-decimal price (connectors wrote
# f"{price:.2f}"). Matching a decimal (not a bare integer) keeps a genuine numeric-only provider
# id -- were one ever to contain a colon -- from being mistaken for a price and truncated.
_LEGACY_PRICE_SUFFIX_RE = re.compile(r"\d+\.\d{1,2}")

# Columns added after the original schema.sql was written, for databases (like the live
# production one) that already had the table before this column existed. schema.sql's own
# CREATE TABLE already defines these for brand-new databases -- this list only matters for
# upgrading an EXISTING database in place, so init_schema() stays safe to call on every
# process start (as it already is) without needing a separate manual migration step.
_COLUMNS_TO_ENSURE = [
    ("items", "opened_at", "TEXT"),
    ("sources", "last_fetch_raw_count", "INTEGER"),
    ("sources", "last_fetch_new_count", "INTEGER"),
    # Source lifecycle + per-Source observability, added after the original sources table. All
    # NULL on a legacy row, so an un-upgraded Source stays active, unnamed, and without recorded
    # attempt history until its first post-upgrade fetch, exactly as before.
    ("sources", "name", "TEXT"),
    ("sources", "paused_at", "TEXT"),
    ("sources", "last_attempt_at", "TEXT"),
    ("sources", "last_fetch_status", "TEXT"),
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
        "TEXT NOT NULL DEFAULT 'editorial' CHECK (kind IN ('editorial', 'monitor', 'tracker'))",
    ),
    # Mutable-snapshot lifecycle (monitor/tracker Channels). All NULL for editorial items, so the
    # APPEND path is unchanged; see items table + db/items.py for the semantics.
    ("items", "last_seen_at", "TEXT"),
    ("items", "inactive_at", "TEXT"),
    ("items", "superseded_at", "TEXT"),
    # Regular Email Group event-scan watermark (item_events path), distinct from last_sent_at.
    ("email_groups", "last_checked_at", "TEXT"),
    ("email_groups", "schedule_mode", "TEXT NOT NULL DEFAULT 'interval'"),
    (
        "email_groups",
        "schedule_timezone",
        "TEXT NOT NULL DEFAULT 'Pacific/Auckland'",
    ),
    ("email_groups", "schedule_time", "TEXT NOT NULL DEFAULT '09:00'"),
    (
        "email_groups",
        "schedule_weekdays",
        "TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6'",
    ),
    ("email_groups", "last_error", "TEXT"),
    ("email_groups", "last_error_at", "TEXT"),
    ("research_sessions", "last_viewed_at", "TEXT"),
    ("research_sources", "is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("research_runs", "run_kind", "TEXT NOT NULL DEFAULT 'full'"),
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
_RESEARCH_COMPLETION_BASELINE_MIGRATION_KEY = (
    "research_completion_notifications_baselined_v1"
)

# Indexes whose columns are added by _COLUMNS_TO_ENSURE above rather than existing on a legacy
# table, so they cannot live in schema.sql: init_schema() runs schema.sql's CREATE statements
# BEFORE the column backfill, and building an index on a not-yet-added column would fail on an
# existing database. Created here (IF NOT EXISTS) after the backfill, which also covers fresh
# databases since init_schema() always runs this step.
_INDEXES_TO_ENSURE = [
    (
        "idx_items_source_lifecycle",
        "items(source_id, superseded_at, inactive_at)",
    ),
]

# Once-ever (app_state-guarded) rebuild of an existing `channels` table whose kind CHECK still
# only permits ('editorial', 'monitor'). SQLite cannot ALTER a CHECK constraint, so the documented
# 12-step table rebuild is used to widen it to include 'tracker'. The immediately-following data
# migration then reassigns any pre-tracker 'monitor' Channel that owns an all_about_auctions Source
# (an auction tracker mis-stored as a monitor before the kind existed) to 'tracker'. Selection is
# by Source type, never a literal Source id.
_CHANNELS_KIND_TRACKER_MIGRATION_KEY = "channels_kind_tracker_migrated_v1"

# The exact current `channels` shape, kept in lockstep with schema.sql. The rebuild copies every
# existing row into a table with this definition (identical but for the widened kind CHECK). The
# _ensure_column backfill runs first, so a legacy table is guaranteed to have all of these columns
# by the time the rebuild reads them.
_CHANNELS_REBUILD_TABLE_SQL = """
CREATE TABLE channels_kind_migration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'editorial'
        CHECK (kind IN ('editorial', 'monitor', 'tracker')),
    fetch_interval_hours INTEGER NOT NULL DEFAULT 3,
    highlight_count INTEGER NOT NULL DEFAULT 8 CHECK (highlight_count BETWEEN 1 AND 50),
    minimum_score INTEGER NOT NULL DEFAULT 0 CHECK (minimum_score BETWEEN 0 AND 100),
    digest_email TEXT,
    last_digest_sent_at TEXT,
    last_digest_date TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)
"""
_CHANNELS_REBUILD_COLUMNS = (
    "id, name, profile, kind, fetch_interval_hours, highlight_count, minimum_score, "
    "digest_email, last_digest_sent_at, last_digest_date, created_at"
)
# Same column order as above, but defensively fills a NULL created_at (a shape older than the
# NOT NULL DEFAULT) so the copy into the rebuilt NOT NULL column can never fail.
_CHANNELS_REBUILD_SELECT = (
    "id, name, profile, kind, fetch_interval_hours, highlight_count, minimum_score, "
    "digest_email, last_digest_sent_at, last_digest_date, "
    "COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%S', 'now'))"
)

# Once-ever (app_state-guarded) compaction of the legacy shopify_collection / land_sea_collection
# external_id scheme, which encoded a product's current price ("<product-id>:<price>") so a price
# move looked like a brand-new row. Both connectors now use the bare stable product id, so this
# collapses each Source's historical "<id>:<price>" rows for one stable id down to a single current
# survivor (newest by fetched_at then id, keeping its id / AI ranking / interactions), marks the
# rest superseded_at instead of deleting them, and frees any exact-stable-id collision. Only these
# two monitor connectors are touched -- editorial and auction (all_about_auctions) rows are left
# exactly as they are.
_STABLE_SHOPPING_ID_MIGRATION_KEY = "stable_shopping_external_id_migrated_v1"


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


def _ensure_index(conn: sqlite3.Connection, name: str, definition: str) -> None:
    conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")


def _channels_kind_check_allows_tracker(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'channels'").fetchone()
    # A brand-new database (schema.sql) already carries the three-kind CHECK; only a legacy
    # two-kind table needs the rebuild. Detecting on the stored DDL keeps this a no-op for any
    # database already at the current shape, independent of the app_state marker.
    return row is not None and "'tracker'" in row["sql"]


def _rebuild_channels_with_tracker_check(conn: sqlite3.Connection) -> None:
    """Widen the legacy channels.kind CHECK to include 'tracker' via SQLite's documented table
    rebuild (CREATE new, copy, DROP old, RENAME). Foreign keys are disabled for the swap so that
    dropping the old `channels` does not cascade-delete its Sources / email_group_channels, then a
    PRAGMA foreign_key_check verifies every preserved id still resolves before the commit; any
    violation aborts loudly instead of leaving dangling references."""
    conn.commit()  # close any implicit transaction so the foreign_keys pragma takes effect
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        try:
            conn.execute(_CHANNELS_REBUILD_TABLE_SQL)
            conn.execute(
                f"INSERT INTO channels_kind_migration ({_CHANNELS_REBUILD_COLUMNS}) "
                f"SELECT {_CHANNELS_REBUILD_SELECT} FROM channels")
            conn.execute("DROP TABLE channels")
            conn.execute("ALTER TABLE channels_kind_migration RENAME TO channels")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "channels kind migration would break foreign keys: "
                    f"{[tuple(v) for v in violations]!r}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_channels_kind_to_tracker(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (_CHANNELS_KIND_TRACKER_MIGRATION_KEY,)).fetchone()
    if marker is not None:
        return

    if not _channels_kind_check_allows_tracker(conn):
        _rebuild_channels_with_tracker_check(conn)

    # Reassign any Channel that predates the 'tracker' kind but already owns an auction Source:
    # selected by Source type, never a literal id, and safe to run even on a fresh database (it
    # simply matches nothing). Runs only after the CHECK permits 'tracker'.
    conn.execute(
        "UPDATE channels SET kind = 'tracker' "
        "WHERE kind = 'monitor' AND id IN "
        "(SELECT channel_id FROM sources WHERE type = 'all_about_auctions')")

    conn.execute(
        "INSERT OR IGNORE INTO app_state (key, value) VALUES (?, '1')",
        (_CHANNELS_KIND_TRACKER_MIGRATION_KEY,))


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


def _migrate_research_completion_notification_baseline(
    conn: sqlite3.Connection,
) -> None:
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (_RESEARCH_COMPLETION_BASELINE_MIGRATION_KEY,),
    ).fetchone()
    if marker is not None:
        return

    # Completed runs that predate this notification feature must not look newly pending after
    # upgrade. Existing rows, including failed attempts awaiting retry, remain untouched.
    conn.execute(
        """
        INSERT OR IGNORE INTO research_completion_notifications (
            run_id,
            attempted_at,
            sent_at
        )
        SELECT id, completed_at, completed_at
        FROM research_runs
        WHERE status = 'completed'
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_state (key, value) VALUES (?, '1')",
        (_RESEARCH_COMPLETION_BASELINE_MIGRATION_KEY,),
    )


def _derive_stable_shopping_id(external_id: str) -> str | None:
    """The stable product id embedded in a legacy "<product-id>:<price>" external_id, or None if
    the trailing colon-delimited suffix is not a numeric price (so the value is already stable, or
    was never in the legacy form and must be left untouched)."""
    prefix, separator, suffix = external_id.rpartition(":")
    if not separator or not prefix:
        return None
    if _LEGACY_PRICE_SUFFIX_RE.fullmatch(suffix) is None:
        return None
    return prefix


def _migrate_stable_shopping_external_ids(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (_STABLE_SHOPPING_ID_MIGRATION_KEY,)).fetchone()
    if marker is not None:
        return

    rows = conn.execute(
        "SELECT items.id AS id, items.source_id AS source_id, "
        "items.external_id AS external_id, items.fetched_at AS fetched_at "
        "FROM items JOIN sources ON sources.id = items.source_id "
        "WHERE sources.type IN ('shopify_collection', 'land_sea_collection') "
        "ORDER BY items.id").fetchall()

    groups: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        stable_id = _derive_stable_shopping_id(row["external_id"])
        if stable_id is None:
            continue
        groups.setdefault((row["source_id"], stable_id), []).append(
            {
                "id": row["id"],
                "fetched_at": row["fetched_at"],
                "external_id": row["external_id"],
            })

    for (source_id, stable_id), members in groups.items():
        # A row that already holds the bare stable id (e.g. from a partially-run migration or a
        # post-switch fetch) is part of the same logical listing and must be considered for
        # survivorship AND freed if it loses, so the survivor can claim that exact id.
        collision = conn.execute(
            "SELECT id, fetched_at, external_id FROM items "
            "WHERE source_id = ? AND external_id = ?",
            (source_id, stable_id)).fetchone()
        candidates = list(members)
        if collision is not None:
            candidates.append(
                {
                    "id": collision["id"],
                    "fetched_at": collision["fetched_at"],
                    "external_id": collision["external_id"],
                })

        survivor = max(candidates, key=lambda row: ((row["fetched_at"] or ""), row["id"]))

        for candidate in candidates:
            if candidate["id"] == survivor["id"]:
                continue
            if candidate["external_id"] == stable_id:
                # Free the exact-stable-id slot before the survivor claims it. The sentinel has no
                # colon-delimited price, so a rerun never re-derives it back into this group.
                conn.execute(
                    "UPDATE items SET external_id = ?, "
                    "superseded_at = COALESCE(superseded_at, "
                    "strftime('%Y-%m-%dT%H:%M:%S', 'now')), "
                    "last_seen_at = COALESCE(last_seen_at, fetched_at) WHERE id = ?",
                    (f"{stable_id}#superseded-{candidate['id']}", candidate["id"]))
            else:
                conn.execute(
                    "UPDATE items SET superseded_at = COALESCE(superseded_at, "
                    "strftime('%Y-%m-%dT%H:%M:%S', 'now')), "
                    "last_seen_at = COALESCE(last_seen_at, fetched_at) WHERE id = ?",
                    (candidate["id"],))

        conn.execute(
            "UPDATE items SET external_id = ?, "
            "last_seen_at = COALESCE(last_seen_at, fetched_at) WHERE id = ?",
            (stable_id, survivor["id"]))

    conn.execute(
        "INSERT OR IGNORE INTO app_state (key, value) VALUES (?, '1')",
        (_STABLE_SHOPPING_ID_MIGRATION_KEY,))


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_PATH.read_text())
    for table, column, column_type in _COLUMNS_TO_ENSURE:
        _ensure_column(conn, table, column, column_type)
    for index_name, index_definition in _INDEXES_TO_ENSURE:
        _ensure_index(conn, index_name, index_definition)
    _migrate_channels_kind_to_tracker(conn)
    _migrate_channel_digest_watermarks(conn)
    _migrate_default_email_group(conn)
    _migrate_research_completion_notification_baseline(conn)
    _migrate_stable_shopping_external_ids(conn)
    conn.commit()
