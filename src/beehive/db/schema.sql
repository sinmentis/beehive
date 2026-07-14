-- All TEXT timestamp DEFAULTs use strftime(..., 'now') with an explicit 'T' separator (not
-- SQLite's datetime('now'), which emits a space separator) so they are lexicographically
-- comparable against Python's datetime.isoformat() strings used everywhere else in the app
-- (e.g. list_new_since's since_iso parameter). Mixing the two formats made same-UTC-day
-- string comparisons silently invert, permanently dropping same-day items from the digest.
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL DEFAULT '',
    fetch_interval_hours INTEGER NOT NULL DEFAULT 3,
    digest_email TEXT,
    last_digest_sent_at TEXT,
    last_digest_date TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    last_fetch_at TEXT,
    last_fetch_error TEXT,
    last_fetch_raw_count INTEGER,
    last_fetch_new_count INTEGER
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    ai_score REAL,
    ai_summary TEXT,
    ai_rationale TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    raw_metadata TEXT NOT NULL DEFAULT '{}',
    opened_at TEXT,
    best_comment_summary TEXT,
    UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS votes (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    value INTEGER NOT NULL CHECK (value IN (-1, 1)),
    reason TEXT,
    voted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    ip TEXT,
    country TEXT,
    success INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    expires_at TEXT NOT NULL
);
