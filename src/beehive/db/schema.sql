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
    highlight_count      INTEGER NOT NULL DEFAULT 8 CHECK (highlight_count BETWEEN 1 AND 50),
    minimum_score        INTEGER NOT NULL DEFAULT 0 CHECK (minimum_score BETWEEN 0 AND 100),
    digest_email          TEXT,
    last_digest_sent_at   TEXT,
    last_digest_date      TEXT,
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

-- One row per item's latest deep-read generation attempt (never a history table -- a fresh
-- regenerate reuses the same row and bumps request_version instead of inserting a new one).
-- All *_at columns here are written explicitly by db/deep_reads.py from a caller-supplied
-- `now: datetime`, never left to a SQL-side DEFAULT/strftime('now'): a worker lease has to be
-- compared against wall-clock time the caller already captured, and letting SQLite stamp its
-- own "now" instead would make the request/claim/heartbeat/complete sequence untestable with
-- frozen time and, worse, could race against the app server's clock. request_version +
-- claim_token together are what let a completion/failure write assert "I am still the
-- worker attempt this row is currently waiting on" (matched alongside status = 'processing'
-- in complete_deep_read_success/fail_deep_read) so a stale worker that is still finishing a
-- previous attempt can never overwrite a newer regenerate or a different worker's claim.
CREATE TABLE IF NOT EXISTS deep_reads (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    request_version INTEGER NOT NULL DEFAULT 1,
    claim_token TEXT,
    lease_expires_at TEXT,
    result_json TEXT,
    language_code TEXT,
    warning_code TEXT,
    error_code TEXT,
    error_detail TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

-- Append-only audit trail for db/summary_rewrites.py + collector/summary_rewrite.py's
-- unread-summary rewrite tool: one row per item actually rewritten by a given run_id, written
-- in the same commit as the items.ai_summary UPDATE so the log and the live value can never
-- drift apart. UNIQUE(run_id, item_id) is what makes reruns of the same run_id idempotent --
-- the orchestrator checks this table before spending an LLM call on an item, so a resumed or
-- re-invoked run skips anything it (or a previous attempt with the same run_id) already
-- rewrote, and INSERT OR IGNORE makes the write itself race-safe too. previous_summary is the
-- exact value ai_summary held immediately before this run overwrote it, which is what a
-- rollback of this run_id restores -- and a rollback deletes the run's rows once processed, so
-- the same run_id (or a fresh one covering the same items) can cleanly reprocess them afterward.
CREATE TABLE IF NOT EXISTS summary_rewrite_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    previous_summary TEXT NOT NULL,
    replacement_summary TEXT NOT NULL,
    migrated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(run_id, item_id)
);
