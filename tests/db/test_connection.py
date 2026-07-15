from concurrent.futures import ThreadPoolExecutor

from beehive.db.connection import connect, init_schema


def test_init_schema_creates_all_tables(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"channels", "sources", "items", "votes", "admin_login_attempts", "app_state",
            "sessions"} <= tables


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
    assert {"digest_email", "last_digest_sent_at", "last_digest_date"} <= columns


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
