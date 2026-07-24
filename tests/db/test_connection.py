import sqlite3
from concurrent.futures import ThreadPoolExecutor

from beehive.db.connection import connect, init_schema
from beehive.db.research_notifications import list_pending_completion_notifications


def test_init_schema_creates_all_tables(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"channels", "sources", "items", "votes", "admin_login_attempts", "app_state",
            "sessions", "deep_reads"} <= tables


def test_wal_mode_enabled(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connection_can_cross_fastapi_worker_threads(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            lambda: conn.execute("SELECT 1").fetchone()[0]
        ).result()
    assert result == 1
    conn.close()


def test_foreign_keys_cascade_channel_to_source_to_item(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Test', 'x')")
    channel_id = conn.execute("SELECT id FROM channels").fetchone()[0]
    conn.execute(
        "INSERT INTO sources (channel_id, type, config) VALUES (?, 'reddit_subreddit', '{}')",
        (channel_id,))
    source_id = conn.execute("SELECT id FROM sources").fetchone()[0]
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'T', 'https://x')",
        (source_id,))
    conn.commit()

    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_dedup_unique_constraint(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Test', 'x')")
    channel_id = conn.execute("SELECT id FROM channels").fetchone()[0]
    conn.execute(
        "INSERT INTO sources (channel_id, type, config) VALUES (?, 'reddit_subreddit', '{}')",
        (channel_id,))
    source_id = conn.execute("SELECT id FROM sources").fetchone()[0]
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'T', 'https://x')",
        (source_id,))
    conn.commit()
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'T2', 'https://y')",
        (source_id,))
    conn.commit()
    assert cur.rowcount == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_sessions_table_has_required_columns(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    conn.execute(
        "INSERT INTO sessions (session_id, csrf_token, expires_at) VALUES (?, ?, ?)",
        ("sess1", "csrf1", "2099-01-01T00:00:00"))
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess1'").fetchone()
    assert row["csrf_token"] == "csrf1"
    assert row["expires_at"] == "2099-01-01T00:00:00"
    assert row["created_at"] is not None  # DEFAULT applies without an explicit value


def test_init_schema_adds_missing_columns_to_a_pre_existing_database(tmp_path):
    """Simulates upgrading a production database that predates this migration: create items/
    sources WITHOUT the new columns first (mirroring the pre-migration schema exactly), then
    confirm init_schema adds the columns without dropping the existing row."""
    path = str(tmp_path / "old.db")
    conn = connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, "
        "external_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, "
        "body TEXT NOT NULL DEFAULT '', created_at TEXT, "
        "fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')), "
        "ai_score REAL, ai_summary TEXT, ai_rationale TEXT, is_read INTEGER NOT NULL DEFAULT 0, "
        "raw_metadata TEXT NOT NULL DEFAULT '{}', UNIQUE(source_id, external_id))")
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (1, 't1', 'T', 'https://x')")
    conn.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL, "
        "type TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', last_fetch_at TEXT, "
        "last_fetch_error TEXT)")
    conn.commit()
    conn.close()

    conn = connect(path)
    init_schema(conn)

    row = conn.execute(
        "SELECT opened_at, best_comment_summary FROM items WHERE external_id = 't1'").fetchone()
    assert row["opened_at"] is None  # column added; existing row preserved, not dropped
    assert row["best_comment_summary"] is None
    source_columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    assert "last_fetch_raw_count" in source_columns
    assert "last_fetch_new_count" in source_columns
    assert "name" in source_columns
    assert "paused_at" in source_columns
    assert "last_attempt_at" in source_columns
    assert "last_fetch_status" in source_columns


def test_init_schema_adds_source_lifecycle_columns_and_preserves_rows(tmp_path):
    """A production database whose sources table predates the lifecycle/observability columns:
    init_schema must backfill name/paused_at/last_attempt_at/last_fetch_status (all NULL) without
    dropping the existing Source row."""
    path = str(tmp_path / "old.db")
    conn = connect(path)
    conn.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL, "
        "type TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', last_fetch_at TEXT, "
        "last_fetch_error TEXT, last_fetch_raw_count INTEGER, last_fetch_new_count INTEGER)")
    conn.execute(
        "INSERT INTO sources (channel_id, type, config, last_fetch_at) "
        "VALUES (1, 'reddit_subreddit', '{\"subreddit\": \"x\"}', '2026-07-09T00:00:00')")
    conn.commit()
    conn.close()

    conn = connect(path)
    init_schema(conn)

    row = conn.execute(
        "SELECT name, paused_at, last_attempt_at, last_fetch_status, last_fetch_at "
        "FROM sources WHERE type = 'reddit_subreddit'").fetchone()
    assert row["name"] is None
    assert row["paused_at"] is None
    assert row["last_attempt_at"] is None
    assert row["last_fetch_status"] is None
    assert row["last_fetch_at"] == "2026-07-09T00:00:00"  # existing row preserved


def test_init_schema_is_safe_to_run_twice_on_an_already_migrated_database(tmp_path):
    """The existing convention: init_schema runs on every process start. Running it twice
    against a database that's already fully migrated must not raise (e.g. "duplicate column")."""
    conn = connect(str(tmp_path / "new.db"))
    init_schema(conn)
    init_schema(conn)  # must not raise


def test_fresh_channels_schema_has_email_and_digest_checkpoint_columns(tmp_path):
    conn = connect(str(tmp_path / "fresh.db"))
    init_schema(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channels)")}
    assert {
        "digest_email",
        "last_digest_sent_at",
        "last_digest_date",
        "highlight_count",
        "minimum_score",
    } <= columns


def test_existing_channels_receive_legacy_digest_checkpoint_once(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = connect(path)
    conn.execute(
        "CREATE TABLE channels ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "profile TEXT NOT NULL DEFAULT '', "
        "fetch_interval_hours INTEGER NOT NULL DEFAULT 3, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))"
        ")")
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Existing', 'profile')")
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        ("last_digest_sent_at", "2026-07-12T20:00:00+00:00"))
    conn.commit()

    init_schema(conn)

    channel = conn.execute(
        "SELECT highlight_count, minimum_score FROM channels WHERE name = 'Existing'"
    ).fetchone()
    assert channel["highlight_count"] == 8
    assert channel["minimum_score"] == 0

    row = conn.execute(
        "SELECT last_digest_sent_at, last_digest_date FROM channels WHERE name = 'Existing'"
    ).fetchone()
    assert row["last_digest_sent_at"] == "2026-07-12T20:00:00+00:00"
    assert row["last_digest_date"] == "2026-07-12"
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = 'digest_channel_watermarks_migrated_v1'"
    ).fetchone()
    assert marker["value"] == "1"

    conn.execute(
        "INSERT INTO channels (name, profile) VALUES ('Created Later', 'profile')")
    conn.commit()
    init_schema(conn)
    later = conn.execute(
        "SELECT last_digest_sent_at, last_digest_date FROM channels "
        "WHERE name = 'Created Later'").fetchone()
    assert later["last_digest_sent_at"] is None
    assert later["last_digest_date"] is None


def test_new_database_creates_an_empty_default_email_group(tmp_path):
    conn = connect(str(tmp_path / "fresh.db"))
    init_schema(conn)

    groups = conn.execute("SELECT * FROM email_groups").fetchall()
    assert len(groups) == 1
    assert groups[0]["name"] == "Default"
    assert groups[0]["send_interval_hours"] == 24
    assert conn.execute("SELECT COUNT(*) FROM email_group_channels").fetchone()[0] == 0


def test_existing_editorial_channels_join_default_email_group_once(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = connect(path)
    conn.execute(
        "CREATE TABLE channels ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "profile TEXT NOT NULL DEFAULT '', "
        "fetch_interval_hours INTEGER NOT NULL DEFAULT 3, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))"
        ")")
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Existing Editorial', 'profile')")
    conn.commit()

    init_schema(conn)

    # kind defaults to 'editorial' once _ensure_column backfills the column, so this
    # pre-existing Channel is swept into the auto-created Default group.
    groups = conn.execute("SELECT * FROM email_groups").fetchall()
    assert len(groups) == 1
    default_group = groups[0]
    assert default_group["name"] == "Default"

    def _member_names():
        rows = conn.execute(
            "SELECT channels.name FROM channels "
            "JOIN email_group_channels ON email_group_channels.channel_id = channels.id "
            "WHERE email_group_channels.email_group_id = ?", (default_group["id"],)).fetchall()
        return [row["name"] for row in rows]

    assert _member_names() == ["Existing Editorial"]

    conn.execute(
        "INSERT INTO channels (name, profile, kind) VALUES (?, 'p', 'monitor')",
        ("New Monitor Channel",))
    conn.commit()
    init_schema(conn)

    # Re-running init_schema is a no-op for this migration: no second Default group is created,
    # and a Channel added after the migration already ran is not retroactively enrolled.
    groups_after = conn.execute("SELECT * FROM email_groups").fetchall()
    assert len(groups_after) == 1
    assert _member_names() == ["Existing Editorial"]


def _insert_completed_research_run(
    conn: sqlite3.Connection,
    *,
    question: str,
    completed_at: str,
) -> int:
    session_id = conn.execute(
        """
        INSERT INTO research_sessions (question, created_at, last_activity_at)
        VALUES (?, ?, ?)
        """,
        (question, completed_at, completed_at),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO research_runs (
            session_id,
            status,
            requested_at,
            completed_at
        )
        VALUES (?, 'completed', ?, ?)
        """,
        (session_id, completed_at, completed_at),
    ).lastrowid
    conn.commit()
    return int(run_id)


def test_existing_completed_research_runs_are_baselined_without_emailing(tmp_path):
    conn = connect(str(tmp_path / "legacy.db"))
    init_schema(conn)
    conn.execute(
        "DELETE FROM app_state "
        "WHERE key = 'research_completion_notifications_baselined_v1'"
    )
    conn.execute("DROP TABLE research_completion_notifications")
    completed_at = "2026-07-01T12:00:00+00:00"
    old_run_id = _insert_completed_research_run(
        conn,
        question="Historical research",
        completed_at=completed_at,
    )

    init_schema(conn)

    baseline = conn.execute(
        """
        SELECT attempted_at, sent_at
        FROM research_completion_notifications
        WHERE run_id = ?
        """,
        (old_run_id,),
    ).fetchone()
    assert dict(baseline) == {
        "attempted_at": completed_at,
        "sent_at": completed_at,
    }
    assert list_pending_completion_notifications(conn) == []

    new_run_id = _insert_completed_research_run(
        conn,
        question="New research",
        completed_at="2026-07-02T12:00:00+00:00",
    )
    init_schema(conn)
    assert [
        pending["run_id"] for pending in list_pending_completion_notifications(conn)
    ] == [new_run_id]


def test_research_notification_baseline_preserves_failed_attempts(tmp_path):
    conn = connect(str(tmp_path / "existing.db"))
    init_schema(conn)
    conn.execute(
        "DELETE FROM app_state "
        "WHERE key = 'research_completion_notifications_baselined_v1'"
    )
    run_id = _insert_completed_research_run(
        conn,
        question="Retry research",
        completed_at="2026-07-01T12:00:00+00:00",
    )
    conn.execute(
        """
        INSERT INTO research_completion_notifications (run_id, attempted_at)
        VALUES (?, '2026-07-01T12:05:00+00:00')
        """,
        (run_id,),
    )
    conn.commit()

    init_schema(conn)

    retry = conn.execute(
        """
        SELECT attempted_at, sent_at
        FROM research_completion_notifications
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    assert dict(retry) == {
        "attempted_at": "2026-07-01T12:05:00+00:00",
        "sent_at": None,
    }
    assert [
        pending["run_id"] for pending in list_pending_completion_notifications(conn)
    ] == [run_id]


def _insert_item(conn) -> int:
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Test', 'x')")
    channel_id = conn.execute("SELECT id FROM channels").fetchone()[0]
    conn.execute(
        "INSERT INTO sources (channel_id, type, config) VALUES (?, 'reddit_subreddit', '{}')",
        (channel_id,))
    source_id = conn.execute("SELECT id FROM sources").fetchone()[0]
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'T', 'https://x')",
        (source_id,))
    conn.commit()
    return conn.execute("SELECT id FROM items").fetchone()[0]


def test_deep_reads_defaults_and_cascades_from_item(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    item_id = _insert_item(conn)

    conn.execute(
        "INSERT INTO deep_reads (item_id, requested_at) VALUES (?, '2026-07-15T00:00:00+00:00')",
        (item_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM deep_reads WHERE item_id = ?", (item_id,)).fetchone()
    assert row["status"] == "pending"  # DEFAULT applies without an explicit value
    assert row["request_version"] == 1  # DEFAULT applies without an explicit value

    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM deep_reads").fetchone()[0] == 0


def test_deep_reads_status_check_constraint_rejects_unknown_status(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    item_id = _insert_item(conn)

    try:
        conn.execute(
            "INSERT INTO deep_reads (item_id, status, requested_at) "
            "VALUES (?, 'bogus', '2026-07-15T00:00:00+00:00')",
            (item_id,))
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# channels.kind rebuild + auction->tracker data migration
# ---------------------------------------------------------------------------

# The full pre-tracker channels shape: kind CHECK still only permits editorial/monitor. Written
# out (rather than derived) so the test pins the exact legacy DDL the rebuild must upgrade.
_LEGACY_TWO_KIND_CHANNELS_DDL = (
    "CREATE TABLE channels ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name TEXT NOT NULL UNIQUE, "
    "profile TEXT NOT NULL DEFAULT '', "
    "kind TEXT NOT NULL DEFAULT 'editorial' CHECK (kind IN ('editorial', 'monitor')), "
    "fetch_interval_hours INTEGER NOT NULL DEFAULT 3, "
    "highlight_count INTEGER NOT NULL DEFAULT 8 CHECK (highlight_count BETWEEN 1 AND 50), "
    "minimum_score INTEGER NOT NULL DEFAULT 0 CHECK (minimum_score BETWEEN 0 AND 100), "
    "digest_email TEXT, last_digest_sent_at TEXT, last_digest_date TEXT, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')))"
)


def _build_legacy_two_kind_db(path):
    """A pre-tracker database: a two-kind channels table plus the FK-connected children (email
    group membership, sources, items, an auction watch) whose survival the rebuild must guarantee.
    The two prior migration markers are set so only the new kind migration does any work."""
    conn = connect(path)
    conn.executescript(
        _LEGACY_TWO_KIND_CHANNELS_DDL + ";"
        "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE email_groups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
        "subject_template TEXT NOT NULL DEFAULT '', recipient_email TEXT, "
        "send_interval_hours INTEGER NOT NULL DEFAULT 24, last_sent_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')));"
        "CREATE TABLE email_group_channels ("
        "email_group_id INTEGER NOT NULL REFERENCES email_groups(id) ON DELETE CASCADE, "
        "channel_id INTEGER NOT NULL UNIQUE REFERENCES channels(id) ON DELETE CASCADE, "
        "PRIMARY KEY (email_group_id, channel_id));"
        "CREATE TABLE sources ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, "
        "type TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', last_fetch_at TEXT, "
        "last_fetch_error TEXT);"
        "CREATE TABLE items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE, "
        "external_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, "
        "body TEXT NOT NULL DEFAULT '', created_at TEXT, "
        "fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')), "
        "ai_score REAL, ai_summary TEXT, ai_rationale TEXT, "
        "is_read INTEGER NOT NULL DEFAULT 0, raw_metadata TEXT NOT NULL DEFAULT '{}', "
        "UNIQUE(source_id, external_id));"
        "CREATE TABLE auction_watches ("
        "item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE, "
        "watched_at TEXT NOT NULL, reminder_sent_for_closing_at TEXT, reminder_sent_at TEXT, "
        "claim_token TEXT, claim_closing_at TEXT, claim_expires_at TEXT, last_error TEXT);"
    )
    # editorial (id 1), auction monitor (id 2), shop monitor (id 3)
    conn.execute("INSERT INTO channels (name, profile, kind) VALUES ('News', 'p', 'editorial')")
    conn.execute("INSERT INTO channels (name, profile, kind) VALUES ('Auction', 'p', 'monitor')")
    conn.execute("INSERT INTO channels (name, profile, kind) VALUES ('Shop', 'p', 'monitor')")
    conn.execute("INSERT INTO email_groups (name) VALUES ('Default')")
    conn.execute("INSERT INTO email_group_channels (email_group_id, channel_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO sources (channel_id, type) VALUES (2, 'all_about_auctions')")
    conn.execute(
        "INSERT INTO sources (channel_id, type) VALUES (3, 'shopify_collection')")
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) "
        "VALUES (1, 'lot-1', 'Lot 1', 'https://example.com/lot-1')")
    conn.execute(
        "INSERT INTO auction_watches (item_id, watched_at) "
        "VALUES (1, '2026-07-01T00:00:00+00:00')")
    conn.executemany(
        "INSERT INTO app_state (key, value) VALUES (?, '1')",
        [("digest_channel_watermarks_migrated_v1",), ("default_email_group_migrated_v1",)])
    conn.commit()
    conn.close()


def test_channels_kind_rebuild_widens_check_and_preserves_related_data(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_legacy_two_kind_db(path)

    conn = connect(path)
    init_schema(conn)

    # The auction monitor Channel (it owns an all_about_auctions Source) becomes a tracker; the
    # shop monitor Channel is left alone. Selection is by Source type, never a literal id.
    kinds = {
        row["name"]: row["kind"]
        for row in conn.execute("SELECT name, kind FROM channels")
    }
    assert kinds == {"News": "editorial", "Auction": "tracker", "Shop": "monitor"}

    # Inserting a tracker Channel now succeeds -- the widened CHECK is really in place.
    conn.execute(
        "INSERT INTO channels (name, profile, kind) VALUES ('New Tracker', 'p', 'tracker')")
    conn.commit()

    # Every FK-connected row survived the table rebuild with its parent id intact.
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    assert conn.execute(
        "SELECT channel_id FROM sources WHERE type = 'all_about_auctions'"
    ).fetchone()["channel_id"] == 2
    assert conn.execute("SELECT COUNT(*) FROM email_group_channels").fetchone()[0] == 1
    assert conn.execute(
        "SELECT channel_id FROM email_group_channels"
    ).fetchone()["channel_id"] == 1
    assert conn.execute("SELECT COUNT(*) FROM auction_watches").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_channels_kind_rebuild_preserves_channel_ids_and_settings(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_legacy_two_kind_db(path)
    conn = connect(path)
    conn.execute(
        "UPDATE channels SET highlight_count = 12, minimum_score = 40, "
        "digest_email = 'x@example.com', profile = 'shopping' WHERE name = 'Shop'")
    conn.commit()

    init_schema(conn)

    shop = conn.execute("SELECT * FROM channels WHERE name = 'Shop'").fetchone()
    assert shop["id"] == 3  # id preserved across the rebuild
    assert shop["highlight_count"] == 12
    assert shop["minimum_score"] == 40
    assert shop["digest_email"] == "x@example.com"
    assert shop["profile"] == "shopping"


def test_channels_kind_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_legacy_two_kind_db(path)
    conn = connect(path)
    init_schema(conn)
    first = [
        (r["id"], r["kind"])
        for r in conn.execute("SELECT id, kind FROM channels ORDER BY id")
    ]

    init_schema(conn)  # must be a no-op
    second = [
        (r["id"], r["kind"])
        for r in conn.execute("SELECT id, kind FROM channels ORDER BY id")
    ]

    assert first == second
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = 'channels_kind_tracker_migrated_v1'").fetchone()
    assert marker["value"] == "1"
    assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 3


def test_fresh_database_skips_the_channels_kind_rebuild_but_marks_it_done(tmp_path):
    conn = connect(str(tmp_path / "fresh.db"))
    init_schema(conn)
    # A fresh schema already carries the three-kind CHECK, so a tracker Channel inserts directly.
    conn.execute("INSERT INTO channels (name, profile, kind) VALUES ('T', 'p', 'tracker')")
    conn.commit()
    marker = conn.execute(
        "SELECT value FROM app_state WHERE key = 'channels_kind_tracker_migrated_v1'").fetchone()
    assert marker["value"] == "1"
