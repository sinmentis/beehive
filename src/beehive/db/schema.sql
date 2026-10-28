-- All TEXT timestamp DEFAULTs use strftime(..., 'now') with an explicit 'T' separator (not
-- SQLite's datetime('now'), which emits a space separator) so they are lexicographically
-- comparable against Python's datetime.isoformat() strings used everywhere else in the app
-- (e.g. list_new_since's since_iso parameter). Mixing the two formats made same-UTC-day
-- string comparisons silently invert, permanently dropping same-day items from the digest.
-- kind discriminates three Channel behaviors: 'editorial' (fetched items are AI-ranked against
-- `profile`, a news-interest profile, and rolled into Home/the Channel page), 'monitor'
-- (deterministic state-change watches, e.g. a retail page's price -- items are fetched and
-- deduped exactly the same way, still AI-ranked but against a shopping profile via
-- rank_monitor_channel, and never accrue votes), and 'tracker' (time-bounded auction lots that
-- are refreshed in place as their live state changes; ranked like a monitor). See
-- run_channel_cycle and beehive.channels for the per-kind behavior, which is data-driven from
-- the ChannelDefinition registry rather than these SQL comments. Either kind's items can be
-- included in a periodic digest once its Channel is assigned to an email_groups group (see
-- below) -- kind does not gate digest inclusion, only how ranking is done.
-- Set once at creation and treated as immutable afterwards -- the kinds imply different
-- meanings for highlight_count/minimum_score/profile, so converting an existing channel in
-- place would leave those fields in a confusing state.
-- digest_email only overrides the recipient for this channel's own fetch/AI-ranking failure
-- alert emails (see run_channel_cycle); periodic digest recipients are controlled entirely by
-- email_groups.recipient_email instead (see below), independent of this column.
-- fetch_schedule_mode picks how this Channel's Sources become due (beehive.scheduling):
-- 'interval' keeps the historical elapsed-duration cadence (last successful fetch +
-- fetch_interval_hours) and is the default so an upgraded database behaves exactly as before;
-- 'calendar' means "every day at fetch_schedule_time in fetch_schedule_timezone", anchored on the
-- intended wall-clock slot rather than on the previous completion, so latency cannot make the
-- daily fetch drift later day after day. fetch_interval_hours stays populated in either mode --
-- switching back to 'interval' restores the cadence the Owner last chose.
-- setup_stage is NULL for normal Channels, including every legacy row. A non-NULL stage marks
-- unfinished setup: even a forced collection cycle does nothing until review clears it.
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'editorial' CHECK (kind IN ('editorial', 'monitor', 'tracker')),
    fetch_interval_hours INTEGER NOT NULL DEFAULT 3,
    fetch_schedule_mode TEXT NOT NULL DEFAULT 'interval'
        CHECK (fetch_schedule_mode IN ('interval', 'calendar')),
    fetch_schedule_timezone TEXT NOT NULL DEFAULT 'Pacific/Auckland',
    fetch_schedule_time TEXT NOT NULL DEFAULT '05:00',
    highlight_count      INTEGER NOT NULL DEFAULT 8 CHECK (highlight_count BETWEEN 1 AND 50),
    minimum_score        INTEGER NOT NULL DEFAULT 0 CHECK (minimum_score BETWEEN 0 AND 100),
    digest_email          TEXT,
    last_digest_sent_at   TEXT,
    last_digest_date      TEXT,
    setup_stage TEXT CHECK (setup_stage IN ('sources', 'rules', 'delivery', 'review')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- A channel's periodic digest email is sent as part of at most one email_groups "group" (see
-- email_group_channels below), not per-channel -- this replaced the old fixed once-daily digest.
-- subject_template is formatted with .format(date=...) at send time (see digest/compose.py);
-- send_interval_hours + last_sent_at drive scheduling.email_group_is_due exactly like
-- sources.last_fetch_at (or last_scheduled_slot_at in calendar mode) drives source_is_due.
-- recipient_email is optional: a blank/NULL value falls back to the same global-default resolver
-- channels already use
-- (email_routing.resolve_default_email) -- see digest/send.py. last_checked_at is a separate
-- watermark for the mutable-item event path (item_events): it records when this group last
-- scanned its Channels for deliverable events, independent of last_sent_at (a scan can find
-- nothing ready and send no email), so the editorial per-Channel digest watermark is never reused
-- to mean two different things.
CREATE TABLE IF NOT EXISTS email_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    subject_template TEXT NOT NULL DEFAULT '',
    recipient_email TEXT,
    send_interval_hours INTEGER NOT NULL DEFAULT 24,
    schedule_mode TEXT NOT NULL DEFAULT 'interval'
        CHECK (schedule_mode IN ('interval', 'calendar')),
    schedule_timezone TEXT NOT NULL DEFAULT 'Pacific/Auckland',
    schedule_time TEXT NOT NULL DEFAULT '09:00',
    schedule_weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
    last_sent_at TEXT,
    last_checked_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- channel_id is UNIQUE so a channel can only ever belong to one email group at a time --
-- db/email_groups.py's assign_channel() enforces "moving" a channel between groups by deleting
-- any existing membership row before inserting the new one, rather than a true many-to-many
-- join. This table has no fetch/digest state of its own -- send_email_group_digests derives
-- "what's new" per channel from channels.last_digest_sent_at exactly as it did before groups
-- existed, and only the group's own last_sent_at tracks when *this group's* email last went out.
CREATE TABLE IF NOT EXISTS email_group_channels (
    email_group_id INTEGER NOT NULL REFERENCES email_groups(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL UNIQUE REFERENCES channels(id) ON DELETE CASCADE,
    PRIMARY KEY (email_group_id, channel_id)
);

-- name is an optional Owner-supplied display label; when blank the admin UI derives one from the
-- config (see web/admin.py::_admin_source_label). paused_at, when set, takes the Source out of
-- every collector cycle entirely (run_channel_cycle skips it before the due-check and before any
-- snapshot reconciliation) and out of every operational/digest warning summary, without deleting
-- its config or history -- clearing it resumes the Source. last_attempt_at / last_fetch_status
-- record the outcome of the most recent fetch attempt regardless of success (status 'ok' on a
-- successful cycle, 'error' on a failure that left last_fetch_at untouched), so the Channel editor
-- can show "attempted Nm ago, last succeeded Nh ago" even while a Source is failing.
-- last_scheduled_slot_at is the schedule anchor for a Channel in calendar fetch mode: the intended
-- wall-clock slot (not the moment work finished) that this Source last completed successfully as
-- part of an automatic run. It is deliberately separate from last_fetch_at, which stays the
-- freshness watermark -- a manual "fetch now" writes last_fetch_at and never this column, so
-- running a Channel by hand cannot move its future automatic schedule.
-- last_scheduled_error_at records only failed automatic attempts. Retry backoff reads this field,
-- so a failed manual fetch remains visible without postponing the next calendar slot.
-- consecutive_failures counts automatic failures since the last success (any success resets it);
-- the retry backoff doubles with it (see source_health.py). last_fetch_error_kind is the stable
-- classification of the latest failure (for example 'transient', 'access_denied', 'truncated').
-- retire_hold_at / retire_hold_count record a held mass retirement: a monitor snapshot that would
-- have retired most active listings at once, kept back until the next snapshot confirms it.
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    name TEXT,
    last_fetch_at TEXT,
    last_fetch_error TEXT,
    last_fetch_raw_count INTEGER,
    last_fetch_new_count INTEGER,
    paused_at TEXT,
    last_attempt_at TEXT,
    last_fetch_status TEXT,
    last_scheduled_slot_at TEXT,
    last_scheduled_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_fetch_error_kind TEXT,
    retire_hold_at TEXT,
    retire_hold_count INTEGER
);

-- external_id is the connector's stable identity for a listing. For an editorial (APPEND) feed
-- every fetched item is either new or an already-seen duplicate, so a row is inserted once and
-- never mutated. For a monitor/tracker (MUTABLE_SNAPSHOT) Source the same external_id is refetched
-- every cycle and its current row is refreshed in place (see db/items.py's upsert_mutable_item):
--   last_seen_at  -- last successful snapshot that still contained this listing.
--   inactive_at   -- set when a complete snapshot no longer lists it (out of stock / delisted /
--                    auction ended). Cleared again if it reappears. Never deletes the row.
--   superseded_at -- set only by the stable-id compaction migration when an older duplicate row is
--                    collapsed into a surviving current row. A superseded row is history: it keeps
--                    its id (so dependent rows like votes/deep_reads/auction_watches never cascade
--                    away) but is excluded from every normal current-item query by default.
-- All three are NULL for a plain editorial item, so the APPEND path is unchanged.
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
    last_seen_at TEXT,
    inactive_at TEXT,
    superseded_at TEXT,
    UNIQUE(source_id, external_id)
);

-- Current-item lookups (Channel page, collector ranking backlog, digest "new since") and the
-- snapshot-reconciliation scan all filter to superseded_at IS NULL within one Source, and the
-- active/history split (tracker) further filters on inactive_at. The composite index that covers
-- both -- idx_items_source_lifecycle(source_id, superseded_at, inactive_at) -- is created by
-- db/connection.py AFTER init_schema backfills these columns, not here: an existing database that
-- predates the columns runs this file's CREATE statements before the column backfill, so building
-- the index inline would fail on the not-yet-added columns. It never disturbs the
-- UNIQUE(source_id, external_id) dedup index above.

CREATE TABLE IF NOT EXISTS votes (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    value INTEGER NOT NULL CHECK (value IN (-1, 1)),
    reason TEXT,
    voted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- Deliverable state changes on a monitor/tracker Item, staged for a regular Email Group (the
-- APPEND/editorial digest never uses this table -- it derives "what's new" straight from
-- channels.last_digest_sent_at). One row is the pending or delivered record of a single
-- observed event on one Item:
--   event_type   -- 'discovered' (first time we surfaced this listing), 'price_drop' (current
--                   price fell below what we last recorded), 'back_in_stock' (a listing that had
--                   gone inactive is available again). Mirrors domain/channels.py EmailEventType.
--   payload      -- JSON snapshot of the numbers that make the event legible in an email (e.g.
--                   old_price/new_price), decoded by db/item_events.py, never parsed in SQL.
--   observed_at  -- when the collector detected this event (caller-supplied, like deep_reads).
--   ready_at     -- set once AI scoring keeps the Item; a NULL ready_at event is still pending
--                   and must not be delivered. suppressed_at is the opposite verdict.
--   suppressed_at-- set when AI scoring or a delivery policy drops the Item, so the pending event
--                   is never delivered.
--   delivered_at -- set when the event has actually gone out in a group email.
-- The partial unique index below allows at most ONE undelivered, unsuppressed event per
-- (item_id, event_type). db/item_events.py coalesces a fresh observation into that single open
-- row (refreshing its payload) instead of inserting a duplicate, so a listing whose price ticks
-- down three times between two emails still delivers one up-to-date price_drop, and a delivered
-- event never blocks a later, genuinely new one of the same type.
CREATE TABLE IF NOT EXISTS item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('discovered', 'price_drop', 'back_in_stock')),
    payload TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    ready_at TEXT,
    suppressed_at TEXT,
    delivered_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_item_events_open_unique
    ON item_events(item_id, event_type)
    WHERE delivered_at IS NULL AND suppressed_at IS NULL;

-- Delivery scan: "ready, not yet suppressed, not yet delivered" events for a set of Channels.
CREATE INDEX IF NOT EXISTS idx_item_events_deliverable
    ON item_events(item_id, ready_at, suppressed_at, delivered_at);


-- Legacy physical name retained for a zero-copy production upgrade. The table now backs generic
-- Tracker watches; connector-specific lifecycle and reminder rules live behind TrackerAdapter.
CREATE TABLE IF NOT EXISTS auction_watches (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    watched_at TEXT NOT NULL,
    reminder_sent_for_closing_at TEXT,
    reminder_sent_at TEXT,
    claim_token TEXT,
    claim_closing_at TEXT,
    claim_expires_at TEXT,
    last_error TEXT,
    CHECK (
        (
            claim_token IS NULL
            AND claim_closing_at IS NULL
            AND claim_expires_at IS NULL
        )
        OR (
            claim_token IS NOT NULL
            AND claim_closing_at IS NOT NULL
            AND claim_expires_at IS NOT NULL
        )
    ),
    CHECK (
        (
            reminder_sent_for_closing_at IS NULL
            AND reminder_sent_at IS NULL
        )
        OR (
            reminder_sent_for_closing_at IS NOT NULL
            AND reminder_sent_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_auction_watches_claim
    ON auction_watches(claim_token, claim_expires_at);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    ip TEXT,
    country TEXT,
    success INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER,
    target_label TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    undo_payload BLOB,
    undo_expires_at TEXT,
    undone_at TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_admin_actions_created
    ON admin_actions(created_at DESC);

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

-- Research Session persistence (ADR-0006..0010). A dedicated, prefixed table family kept
-- entirely separate from the feed data model above: Research Sessions never become
-- Channels/Sources/Items, so none of these tables reuse or reference the feed tables.
--
-- Every TEXT timestamp column in this family is written explicitly by its db/research_*.py /
-- db/evidence_*.py module from a caller-supplied `now: datetime` -- exactly the convention
-- documented at the top of db/deep_reads.py -- rather than a SQL-side DEFAULT. research_runs
-- and research_chat_requests carry worker leases (claim_token/lease_expires_at) and a fixed
-- deadline_at that must be compared against wall-clock time the caller already captured, and
-- letting SQLite stamp its own "now" would make that untestable with frozen time. The same
-- explicit-now convention is used uniformly across every table in this family (not only the
-- leased ones) so ordering columns (sequence_number/version) and audit timestamps stay
-- deterministic under the same frozen-time tests.

-- Immutable question; only status/last_activity_at/archived_at ever change after insert.
CREATE TABLE IF NOT EXISTS research_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    last_viewed_at TEXT,
    archived_at TEXT,
    CHECK (
        (status = 'archived' AND archived_at IS NOT NULL)
        OR (status = 'active' AND archived_at IS NULL)
    )
);

-- A Research Source is scoped to exactly one Research Session (never shared/recurring like
-- feed `sources`), added either by the Owner or by the Research Plan (origin).
CREATE TABLE IF NOT EXISTS research_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    connector_type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL CHECK (origin IN ('owner', 'plan')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_sources_session ON research_sources(session_id);

-- One row per attempt lineage of a Research Session's search/refresh action. status/phase
-- mirror domain/research.py's ResearchRunStatus/ResearchRunPhase exactly -- a row must satisfy
-- these CHECKs for domain.research.ResearchRun's own __post_init__ invariants to ever be
-- satisfiable when a row is loaded back into that frozen dataclass.
--
-- claim_token/lease_expires_at are a short worker lease (same shape as deep_reads' lease):
-- recover_expired_research_runs reclaims a run whose lease expired, clears the lease, and
-- bumps attempt_count -- but deadline_at (the run's fixed overall time budget, set once via
-- COALESCE at first claim) and started_at are never reset by recovery, so a crash-and-retry
-- cycle can't quietly extend a run's total allowed running time. cancel_requested is a
-- best-effort flag a worker observes cooperatively between phases; it does not itself force a
-- transition (require_run_transition still gates PROCESSING -> CANCELLED).
--
-- deep_fetch_count is a per-run budget of expensive deep-fetch operations (ADR-0010), capped
-- at 30 and only ever incremented via a transactional reservation taken before the I/O runs
-- (reserve_deep_fetch), never after -- so a crash mid-fetch leaks at most the reservation, and
-- never lets the run silently exceed the cap.
--
-- The partial unique index below is what enforces "at most one active (pending/processing)
-- Research Run per Research Session" -- db/research_runs.py's enqueue_research_run also checks
-- this explicitly under BEGIN IMMEDIATE before inserting so the caller gets a clear ValueError
-- instead of relying solely on the index to reject the INSERT, exactly like
-- research_chat_requests' own one-active-per-session invariant below. Terminal runs
-- (completed/cancelled/failed) are never touched by this constraint, so a session's full run
-- history is preserved and a fresh refresh is always allowed once the active run ends.
CREATE TABLE IF NOT EXISTS research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    run_kind TEXT NOT NULL DEFAULT 'full' CHECK (run_kind IN ('full', 'synthesis')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'cancelled', 'failed')),
    phase TEXT
        CHECK (phase IS NULL OR phase IN (
            'planning', 'collecting', 'enriching', 'clustering', 'assessing', 'synthesizing'
        )),
    claim_token TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    deep_fetch_count INTEGER NOT NULL DEFAULT 0 CHECK (deep_fetch_count BETWEEN 0 AND 30),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    deadline_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_detail TEXT,
    CHECK (
        (
            status = 'processing' AND phase IS NOT NULL AND claim_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL
            AND deadline_at IS NOT NULL
        )
        OR (status != 'processing' AND phase IS NULL)
    ),
    CHECK (
        (status IN ('completed', 'cancelled', 'failed') AND completed_at IS NOT NULL)
        OR (status IN ('pending', 'processing') AND completed_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_research_runs_session ON research_runs(session_id);
-- Backs both list_pending_research_runs (oldest-first queue) and the global three-processing-
-- run cap check (COUNT WHERE status='processing' AND lease_expires_at > now), both taken
-- under BEGIN IMMEDIATE in db/research_runs.py.
CREATE INDEX IF NOT EXISTS idx_research_runs_status
    ON research_runs(status, requested_at, lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_runs_one_active_per_session
    ON research_runs(session_id) WHERE status IN ('pending', 'processing');

CREATE TABLE IF NOT EXISTS research_completion_notifications (
    run_id INTEGER PRIMARY KEY REFERENCES research_runs(id) ON DELETE CASCADE,
    attempted_at TEXT,
    sent_at TEXT
);

-- Visible, persisted Research Plan revisions for a run. Append-only: a revision is never
-- edited or deleted once written, so the Owner can always see exactly what the AI proposed at
-- each step. version is allocated as MAX(version)+1 per run_id under BEGIN IMMEDIATE.
CREATE TABLE IF NOT EXISTS research_plan_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    plan_json TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    is_validated INTEGER NOT NULL DEFAULT 0 CHECK (is_validated IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, version)
);

-- Canonical, session-scoped Evidence Items (ADR-0010). "Canonical" means one row per distinct
-- piece of source material for the life of the Research Session: re-collecting the same item
-- in a later run/snapshot upserts this same row (matched on research_source_id + external_key)
-- rather than inserting a duplicate, which is what lets citation_number stay stable and
-- session-wide -- once assigned it is never reassigned or reused, even if the item is later
-- excluded via research_evidence_curation. citation_number is allocated as
-- MAX(citation_number)+1 per session_id under BEGIN IMMEDIATE.
CREATE TABLE IF NOT EXISTS research_evidence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    research_source_id INTEGER NOT NULL REFERENCES research_sources(id) ON DELETE CASCADE,
    external_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    full_text TEXT,
    quality TEXT NOT NULL
        CHECK (quality IN ('primary', 'reporting', 'analysis', 'community', 'aggregator')),
    raw_metadata TEXT NOT NULL DEFAULT '{}',
    citation_number INTEGER NOT NULL CHECK (citation_number > 0),
    created_at TEXT NOT NULL,
    UNIQUE(research_source_id, external_key),
    UNIQUE(session_id, citation_number)
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_items_session ON research_evidence_items(session_id);

-- Exactly one Evidence Snapshot per Research Run (its own explicit search/refresh action) --
-- the UNIQUE(run_id) index below is what makes this a DB-enforced invariant, not merely an
-- application convention: a run may only ever RESUME its own existing snapshot (building or,
-- after finalize_snapshot_if_claimed's atomic clusters+seal+revision write, sealed), never mint
-- a second one, even across a crash-and-reclaim (db/research_snapshots.py's
-- get_snapshot_for_run / research.orchestrator.py's crash-recovery resume path).
CREATE TABLE IF NOT EXISTS research_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    status TEXT NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'sealed')),
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    UNIQUE(session_id, sequence_number),
    CHECK (
        (status = 'sealed' AND sealed_at IS NOT NULL)
        OR (status = 'building' AND sealed_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_research_snapshots_session ON research_snapshots(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_snapshots_one_per_run
    ON research_snapshots(run_id);

-- Cumulative membership: unlike a delta/diff table, each later snapshot's item set is a
-- superset of the previous one (db/research_snapshots.py's copy-forward helper carries prior
-- membership into a new snapshot before new items are added), so "the evidence available as of
-- snapshot N" is always just this table filtered by snapshot_id -- callers never need to walk
-- earlier snapshots to reconstruct the cumulative view.
CREATE TABLE IF NOT EXISTS research_snapshot_items (
    snapshot_id INTEGER NOT NULL REFERENCES research_snapshots(id) ON DELETE CASCADE,
    evidence_item_id INTEGER NOT NULL REFERENCES research_evidence_items(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, evidence_item_id)
);

CREATE INDEX IF NOT EXISTS idx_research_snapshot_items_evidence
    ON research_snapshot_items(evidence_item_id);

-- Mutable Owner curation of one canonical Evidence Item: exactly one row per evidence item,
-- upserted in place (this is deliberately NOT append-only/versioned -- research_evidence_
-- state_revisions below is what turns a moment of curation into an immutable, citable fact).
CREATE TABLE IF NOT EXISTS research_evidence_curation (
    evidence_item_id INTEGER PRIMARY KEY
        REFERENCES research_evidence_items(id) ON DELETE CASCADE,
    is_excluded INTEGER NOT NULL DEFAULT 0 CHECK (is_excluded IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Immutable, versioned snapshot of "which Evidence Items are part of the Research Session's
-- active evidence" at one moment (i.e. curation decisions baked into a citable fact). A
-- Research Synthesis or chat reply pins one of these by id so it stays reproducible even after
-- later curation changes -- never a live join over research_evidence_curation. version is
-- allocated as MAX(version)+1 per session_id under BEGIN IMMEDIATE.
CREATE TABLE IF NOT EXISTS research_evidence_state_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    snapshot_id INTEGER NOT NULL REFERENCES research_snapshots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS research_evidence_state_revision_items (
    revision_id INTEGER NOT NULL
        REFERENCES research_evidence_state_revisions(id) ON DELETE CASCADE,
    evidence_item_id INTEGER NOT NULL REFERENCES research_evidence_items(id) ON DELETE CASCADE,
    PRIMARY KEY (revision_id, evidence_item_id)
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_state_revision_items_evidence
    ON research_evidence_state_revision_items(evidence_item_id);

-- Evidence Clusters are scoped to one Evidence Snapshot, not to research_evidence_items, and
-- research_evidence_items has no column pointing back at a cluster -- membership is expressed
-- one-directionally through research_evidence_cluster_items only, so this pair can never form
-- a circular FK with the canonical evidence table.
CREATE TABLE IF NOT EXISTS research_evidence_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES research_snapshots(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_clusters_snapshot
    ON research_evidence_clusters(snapshot_id);

CREATE TABLE IF NOT EXISTS research_evidence_cluster_items (
    cluster_id INTEGER NOT NULL REFERENCES research_evidence_clusters(id) ON DELETE CASCADE,
    evidence_item_id INTEGER NOT NULL REFERENCES research_evidence_items(id) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, evidence_item_id)
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_cluster_items_evidence
    ON research_evidence_cluster_items(evidence_item_id);

-- Append-only, versioned Research Synthesis. claims_json holds each claim's text and
-- provenance only (never a citation, and never a raw evidence_item_id) -- citations live
-- exclusively in research_synthesis_citations below, keyed by (synthesis_id, claim_index), so
-- a citation can be FK-validated against research_evidence_items and queried ("which
-- syntheses cite evidence item X") without ever parsing JSON. version is allocated as
-- MAX(version)+1 per session_id under BEGIN IMMEDIATE.
CREATE TABLE IF NOT EXISTS research_syntheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    evidence_state_revision_id INTEGER NOT NULL
        REFERENCES research_evidence_state_revisions(id) ON DELETE RESTRICT,
    sufficiency_state TEXT NOT NULL
        CHECK (sufficiency_state IN ('sufficient', 'partial', 'insufficient')),
    claims_json TEXT NOT NULL,
    model TEXT NOT NULL,
    language_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);

-- Deliberately its own concrete table (not a shared/polymorphic "citations" table with a
-- parent_type discriminator) -- see research_message_citations below for the sibling table
-- for Conversation Messages. A single polymorphic table would need an application-enforced
-- (not FK-enforced) parent_type/parent_id pair, which is exactly the kind of un-checkable
-- reference this schema otherwise avoids everywhere else.
CREATE TABLE IF NOT EXISTS research_synthesis_citations (
    synthesis_id INTEGER NOT NULL REFERENCES research_syntheses(id) ON DELETE CASCADE,
    claim_index INTEGER NOT NULL CHECK (claim_index >= 0),
    evidence_item_id INTEGER NOT NULL REFERENCES research_evidence_items(id) ON DELETE RESTRICT,
    citation_number INTEGER NOT NULL CHECK (citation_number > 0),
    PRIMARY KEY (synthesis_id, claim_index, evidence_item_id)
);

CREATE INDEX IF NOT EXISTS idx_research_synthesis_citations_evidence
    ON research_synthesis_citations(evidence_item_id);

-- Append-only Conversation Messages. sequence_number is allocated as MAX(sequence_number)+1
-- per session_id under BEGIN IMMEDIATE, giving a stable, gapless-per-success ordering
-- independent of id (id is never exposed as the ordering key to callers).
CREATE TABLE IF NOT EXISTS research_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    role TEXT NOT NULL CHECK (role IN ('owner', 'assistant')),
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('pending', 'ready', 'failed')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence_number)
);

-- Separate, concrete sibling of research_synthesis_citations -- see the comment there for why
-- citations are never polymorphic.
CREATE TABLE IF NOT EXISTS research_message_citations (
    message_id INTEGER NOT NULL REFERENCES research_messages(id) ON DELETE CASCADE,
    evidence_item_id INTEGER NOT NULL REFERENCES research_evidence_items(id) ON DELETE RESTRICT,
    citation_number INTEGER NOT NULL CHECK (citation_number > 0),
    PRIMARY KEY (message_id, evidence_item_id)
);

CREATE INDEX IF NOT EXISTS idx_research_message_citations_evidence
    ON research_message_citations(evidence_item_id);

-- A pending owner message's in-flight reply-generation task. Mirrors deep_reads' claim_token +
-- lease_expires_at lease shape. pinned_evidence_state_revision_id/pinned_synthesis_id/
-- pinned_memory_version freeze exactly which evidence state, synthesis, and conversation
-- memory version the reply is generated against, so a reply started before a later curation
-- change or new synthesis remains reproducible and never silently reads a moving target.
-- pinned_synthesis_id is nullable because a chat reply can be requested before any synthesis
-- exists yet. The partial unique index below is what enforces "at most one pending/processing
-- chat request per session" -- db/research_chat_requests.py also checks this under BEGIN
-- IMMEDIATE before inserting so the caller gets a clear error instead of relying solely on the
-- index to reject the INSERT.
CREATE TABLE IF NOT EXISTS research_chat_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    owner_message_id INTEGER NOT NULL REFERENCES research_messages(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    claim_token TEXT,
    lease_expires_at TEXT,
    pinned_evidence_state_revision_id INTEGER NOT NULL
        REFERENCES research_evidence_state_revisions(id) ON DELETE RESTRICT,
    pinned_synthesis_id INTEGER REFERENCES research_syntheses(id) ON DELETE RESTRICT,
    pinned_memory_version INTEGER NOT NULL DEFAULT 0 CHECK (pinned_memory_version >= 0),
    reply_message_id INTEGER REFERENCES research_messages(id) ON DELETE SET NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_detail TEXT,
    CHECK (
        (status = 'processing' AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status != 'processing' AND claim_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
        OR (status IN ('pending', 'processing') AND completed_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_research_chat_requests_session ON research_chat_requests(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_chat_requests_one_active_per_session
    ON research_chat_requests(session_id) WHERE status IN ('pending', 'processing');

-- Mutable, versioned in place (unlike research_syntheses/research_messages): exactly one row
-- per Research Session, and each update bumps version rather than inserting a new row --
-- there is no history of past memory contents to preserve, only "the current compression" and
-- the version number a chat request can pin.
CREATE TABLE IF NOT EXISTS research_conversation_memory (
    session_id INTEGER PRIMARY KEY REFERENCES research_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    content TEXT NOT NULL DEFAULT '',
    covers_through_message_id INTEGER REFERENCES research_messages(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

-- "Fetch now" requests waiting for, or held by, the worker's fetch lane: at most one per Channel.
-- No claim means queued; a claim with a live lease means running; a claim whose lease ran out
-- means the worker holding it stopped, and the next poll hands it out again. `rerun` records a
-- request made while the fetch was already running, so it runs once more when this one ends.
CREATE TABLE IF NOT EXISTS fetch_requests (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    requested_at TEXT NOT NULL,
    claim_token TEXT,
    started_at TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    rerun INTEGER NOT NULL DEFAULT 0 CHECK (rerun IN (0, 1)),
    CHECK ((claim_token IS NULL) = (lease_expires_at IS NULL))
);

-- Owner-requested previews and explicit, non-destructive reprocessing share one worker lane.
-- A fixed candidate snapshot and per-candidate progress let an interrupted apply resume without
-- rescoring completed rows. Item ids deliberately have no foreign key: deletion becomes a skip,
-- rather than silently removing work from the request's original count.
CREATE TABLE IF NOT EXISTS ranking_requests (
    id TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('preview', 'apply')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    requested_profile TEXT NOT NULL,
    saved_profile TEXT NOT NULL,
    channel_kind TEXT NOT NULL CHECK (channel_kind IN ('editorial', 'monitor', 'tracker')),
    total_count INTEGER NOT NULL CHECK (total_count > 0),
    processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    error TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    claim_token TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    CHECK (processed_count + skipped_count <= total_count),
    CHECK (
        (status = 'running' AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status != 'running' AND claim_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ranking_requests_active_channel
    ON ranking_requests(channel_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_ranking_requests_channel_history
    ON ranking_requests(channel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ranking_requests_queue
    ON ranking_requests(status, created_at);

CREATE TABLE IF NOT EXISTS ranking_request_items (
    request_id TEXT NOT NULL REFERENCES ranking_requests(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    item_id INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processed', 'skipped')),
    result TEXT,
    skip_reason TEXT,
    PRIMARY KEY (request_id, position),
    UNIQUE (request_id, item_id)
);

-- Beehive Civic persistence. A third, additive table family alongside the feed tables and the
-- Research tables: a Civic Watch never becomes a Channel/Source/Item or a Research Session, so
-- none of these tables reference either family and nothing above this line changes because they
-- exist. An existing database first picked the family up from these CREATE TABLE IF NOT EXISTS
-- statements; later additive columns (currently the two payload_hash fields) are backfilled by
-- db/connection.py so upgraded and fresh databases remain identical.
--
-- Same explicit-now convention as the Research family: every TEXT timestamp is written by
-- db/civic_*.py from a caller-supplied `now: datetime`, never a SQL-side DEFAULT, because
-- worker leases and fixed deadlines have to be compared against wall-clock time the caller
-- already captured and letting SQLite stamp its own "now" makes that untestable under frozen
-- time. Every status/phase CHECK mirrors domain/civic.py exactly, so a row that satisfies these
-- CHECKs is always loadable into the corresponding frozen dataclass without tripping its
-- __post_init__.

-- One row per Civic Watch. subject is the Watch's identity (UNIQUE) so the same civic subject
-- can never be monitored twice in parallel under two competing schedules.
CREATE TABLE IF NOT EXISTS civic_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL UNIQUE,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'retired')),
    schedule_timezone TEXT NOT NULL DEFAULT 'Pacific/Auckland',
    schedule_time TEXT NOT NULL DEFAULT '06:30',
    created_at TEXT NOT NULL,
    paused_at TEXT,
    retired_at TEXT,
    CHECK (
        (status = 'paused' AND paused_at IS NOT NULL)
        OR (status != 'paused' AND paused_at IS NULL)
    ),
    CHECK (
        (status = 'retired' AND retired_at IS NOT NULL)
        OR (status != 'retired' AND retired_at IS NULL)
    )
);

-- Immutable once published: a Civic Vocabulary version is numbered per Watch, sealed with a
-- content_hash over its own terms, and thereafter only ever superseded by a later version. An
-- already-delivered Civic Batch names the version it was screened against, so editing a
-- published version in place would retroactively change what a published statement was allowed
-- to say. The partial unique index below enforces at most one 'published' version per Watch --
-- "the vocabulary currently in force" is a single, unambiguous row, never a set.
CREATE TABLE IF NOT EXISTS civic_vocabulary_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'superseded')),
    content_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    published_at TEXT,
    superseded_at TEXT,
    UNIQUE(watch_id, version),
    CHECK (
        (status = 'draft' AND content_hash = '' AND published_at IS NULL)
        OR (status != 'draft' AND content_hash != '' AND published_at IS NOT NULL)
    ),
    CHECK (
        (status = 'superseded' AND superseded_at IS NOT NULL)
        OR (status != 'superseded' AND superseded_at IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_civic_vocabulary_one_published_per_watch
    ON civic_vocabulary_versions(watch_id) WHERE status = 'published';

-- Terms belong to a version, never to the Watch directly: that is what makes "which terms were
-- in force" answerable for any past Civic Batch. normalized_term is the comparison form
-- (domain.civic.normalize_term) and carries the uniqueness constraint, so "Green Party" and
-- "green  party" cannot both be added to the same version.
CREATE TABLE IF NOT EXISTS civic_vocabulary_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vocabulary_version_id INTEGER NOT NULL
        REFERENCES civic_vocabulary_versions(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    normalized_term TEXT NOT NULL,
    category TEXT NOT NULL
        CHECK (category IN ('party', 'person', 'topic', 'place', 'organisation')),
    role TEXT NOT NULL CHECK (role IN ('required', 'optional', 'excluded')),
    created_at TEXT NOT NULL,
    UNIQUE(vocabulary_version_id, normalized_term)
);

-- One Civic Run per Watch per slot -- UNIQUE(watch_id, slot_at) is the DB-enforced form of
-- "a slot is executed at most once", so a scheduler that ticks twice inside the same slot (a
-- restart, an overlapping timer, two hosts) can never mint a second run for it; db/civic_runs.py
-- returns the existing row instead. slot_at is the Watch's own calendar slot, not the moment the
-- scheduler happened to notice it, so a late tick still serves the slot it was late for.
--
-- Lease/deadline shape is deliberately identical to research_runs: claim_token/lease_expires_at
-- is a short worker lease every progressive write is fenced on, while deadline_at is the run's
-- fixed total budget, set once via COALESCE at first claim and never reset by recovery. The
-- partial unique index enforces at most one active (pending/processing) run per Watch, so a
-- backlog of missed slots can never be worked in parallel against the same Watch.
CREATE TABLE IF NOT EXISTS civic_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    vocabulary_version_id INTEGER
        REFERENCES civic_vocabulary_versions(id) ON DELETE RESTRICT,
    slot_at TEXT NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'normal' CHECK (run_mode IN ('normal', 'election')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'cancelled', 'failed')),
    phase TEXT
        CHECK (phase IS NULL OR phase IN (
            'gathering', 'screening', 'researching', 'verifying', 'publishing'
        )),
    claim_token TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    deadline_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_detail TEXT,
    UNIQUE(watch_id, slot_at),
    CHECK (
        (
            status = 'processing' AND phase IS NOT NULL AND claim_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL
            AND deadline_at IS NOT NULL
        )
        OR (status != 'processing' AND phase IS NULL AND claim_token IS NULL)
    ),
    CHECK (
        (status IN ('completed', 'cancelled', 'failed') AND completed_at IS NOT NULL)
        OR (status IN ('pending', 'processing') AND completed_at IS NULL)
    ),
    CHECK (error_code IS NULL OR status = 'failed')
);

CREATE INDEX IF NOT EXISTS idx_civic_runs_watch ON civic_runs(watch_id);
-- Backs both the oldest-first pending queue and the global processing-run cap check
-- (COUNT WHERE status='processing' AND lease_expires_at > now), both taken under BEGIN IMMEDIATE.
CREATE INDEX IF NOT EXISTS idx_civic_runs_status
    ON civic_runs(status, requested_at, lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_civic_runs_one_active_per_watch
    ON civic_runs(watch_id) WHERE status IN ('pending', 'processing');

-- The publishable envelope of one Civic Run. Exactly one Batch per Run (UNIQUE(run_id)), so a
-- crash-and-reclaim resumes the same envelope rather than minting a second one -- the same
-- invariant research_snapshots enforces for Evidence Snapshots.
--
-- content_hash is NULL while 'building' and written exactly once at seal, over the Batch's own
-- Candidates. Every state after that reads those same bytes, which is what makes a retried
-- delivery provably the same publication rather than a similar one. idempotency_key is
-- generated at insert (never derived from mutable content) and is what a Civic Delivery Receipt
-- is keyed on, so a replayed delivery is recognisable even before the envelope is compared.
-- payload_hash is separate: new PartyMap seals bind it atomically with their frozen v1 bytes.
-- Legacy and generic non-publication Batches are not backfilled.
--
-- next_attempt_at is retry backoff for a delivery that failed retryably: the Batch returns to
-- 'sealed' and is not offered again until that moment. 'quarantined' is deliberately terminal
-- and never auto-retried -- it means an integrity problem an Owner has to look at.
CREATE TABLE IF NOT EXISTS civic_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN (
            'building', 'sealed', 'delivering', 'delivered', 'quarantined', 'failed'
        )),
    idempotency_key TEXT NOT NULL UNIQUE,
    content_hash TEXT,
    payload_hash TEXT,
    publication_schema_version INTEGER
        CHECK (publication_schema_version IS NULL OR publication_schema_version = 1),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    claim_token TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    settled_at TEXT,
    next_attempt_at TEXT,
    error_code TEXT,
    error_detail TEXT,
    UNIQUE(run_id),
    CHECK (
        (status = 'building' AND content_hash IS NULL AND sealed_at IS NULL)
        OR (status != 'building' AND content_hash IS NOT NULL AND sealed_at IS NOT NULL)
    ),
    CHECK (
        (status = 'delivering' AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status != 'delivering' AND claim_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status IN ('delivered', 'quarantined', 'failed') AND settled_at IS NOT NULL)
        OR (status IN ('building', 'sealed', 'delivering') AND settled_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_civic_batches_delivery_queue
    ON civic_batches(status, next_attempt_at, id);
-- The parent key civic_capacity_deferrals' composite (run, batch) foreign keys resolve against.
-- Redundant as a constraint -- UNIQUE(run_id) already implies it -- but SQLite requires a unique
-- index over exactly the referenced columns, in that order, for a composite reference to be
-- accepted at all.
CREATE UNIQUE INDEX IF NOT EXISTS idx_civic_batches_run_identity
    ON civic_batches(run_id, id);

-- Present only for new publication-scoped seals. Historical Batches are never backfilled.
-- Bytes, payload hash and membership are committed by one seal transaction.
CREATE TABLE IF NOT EXISTS civic_batch_publications (
    batch_id INTEGER PRIMARY KEY REFERENCES civic_batches(id) ON DELETE CASCADE,
    envelope_bytes BLOB NOT NULL CHECK (typeof(envelope_bytes) = 'blob'),
    frozen_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS civic_batch_publications_building_only
BEFORE INSERT ON civic_batch_publications
WHEN NOT EXISTS (
    SELECT 1 FROM civic_batches WHERE id = NEW.batch_id AND status = 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'a Civic publication must be frozen before sealing');
END;

CREATE TRIGGER IF NOT EXISTS civic_batch_publications_no_update
BEFORE UPDATE ON civic_batch_publications
BEGIN
    SELECT RAISE(ABORT, 'a frozen Civic publication cannot be updated');
END;

-- One proposed civic statement. dedupe_key is scoped to the Watch, not the Run
-- (UNIQUE(watch_id, dedupe_key)), which is what makes cross-day deduplication a database
-- invariant instead of a screening heuristic: the same underlying development can only ever
-- enter one Civic Batch, however many days it keeps being reported.
--
-- superseded_by_candidate_id is how a correction is published: the earlier statement is marked
-- superseded and points at its replacement rather than being edited or deleted, so the published
-- record stays append-only. ON DELETE RESTRICT, never CASCADE or SET NULL -- deleting a
-- correction must neither delete the statement it corrected nor quietly orphan it into a
-- "superseded by nothing" state. Deleting the whole Civic Watch still works: that cascade
-- removes every Candidate together, so no surviving row is left referencing a deleted one.
--
-- sealed_into_batch_at is what makes a Civic Batch's envelope membership immutable. It is
-- written exactly once, by db/civic_batches.py's seal, onto the Candidates that were VERIFIED at
-- that instant, and never cleared. The content_hash is computed over "sealed_into_batch_at IS
-- NOT NULL" rather than over "status = 'verified'" precisely because status keeps moving after
-- the seal: publishing a correction tomorrow marks this Candidate SUPERSEDED, which would
-- otherwise silently change the recomputed hash of an envelope that was already delivered and
-- receipted. Only a Candidate that reached VERIFIED can carry it, so the stamp survives that
-- later supersession and nothing else can ever acquire it.
CREATE TABLE IF NOT EXISTS civic_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES civic_batches(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'drafted'
        CHECK (status IN (
            'drafted', 'researching', 'verifying', 'verified', 'deferred_capacity',
            'deferred_ai', 'rejected', 'superseded'
        )),
    research_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (research_attempt_count >= 0),
    ai_revision INTEGER NOT NULL DEFAULT 0 CHECK (ai_revision >= 0),
    ai_refresh_reason TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    sealed_into_batch_at TEXT,
    rejection_code TEXT,
    superseded_by_candidate_id INTEGER
        REFERENCES civic_candidates(id) ON DELETE RESTRICT,
    UNIQUE(watch_id, dedupe_key),
    CHECK (
        (status IN ('verified', 'deferred_capacity', 'deferred_ai', 'rejected', 'superseded')
            AND settled_at IS NOT NULL)
        OR (status IN ('drafted', 'researching', 'verifying') AND settled_at IS NULL)
    ),
    CHECK (
        (status = 'rejected' AND rejection_code IS NOT NULL)
        OR (status != 'rejected' AND rejection_code IS NULL)
    ),
    CHECK (
        (status = 'superseded' AND superseded_by_candidate_id IS NOT NULL)
        OR (status != 'superseded' AND superseded_by_candidate_id IS NULL)
    ),
    CHECK (superseded_by_candidate_id IS NULL OR superseded_by_candidate_id != id),
    CHECK (
        sealed_into_batch_at IS NULL OR status IN ('verified', 'superseded')
    )
);

CREATE INDEX IF NOT EXISTS idx_civic_candidates_batch ON civic_candidates(batch_id, id);
CREATE INDEX IF NOT EXISTS idx_civic_candidates_run ON civic_candidates(run_id, status);
-- The seal-time envelope read: "the Candidates this Batch actually published, in id order".
CREATE INDEX IF NOT EXISTS idx_civic_candidates_envelope
    ON civic_candidates(batch_id, sealed_into_batch_at, id);
-- The adoption read: "this Watch's Candidates waiting for a slot with room", oldest first. A
-- partial index, so it holds only the handful of carried-over rows rather than every Candidate
-- the Watch has ever produced.
CREATE INDEX IF NOT EXISTS idx_civic_candidates_capacity_queue
    ON civic_candidates(watch_id, id) WHERE status = 'deferred_capacity';

CREATE TABLE IF NOT EXISTS civic_publication_rejections (
    candidate_id INTEGER PRIMARY KEY REFERENCES civic_candidates(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES civic_batches(id) ON DELETE CASCADE,
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    detail TEXT NOT NULL CHECK (length(detail) > 0),
    rejected_at TEXT NOT NULL
);

-- Source records for one Candidate. UNIQUE(candidate_id, external_key) makes re-capturing the
-- same source idempotent, so a resumed run cannot inflate a Candidate's corroboration count by
-- recording the same article twice. tier/stance are what domain.civic.evidence_supports_
-- publication reads -- they are real validated fields, never conventions inside `excerpt`.
--
-- publisher_id through anonymous_source_dependency are the beehive.civic.evidence policy inputs,
-- stored as real validated columns for exactly the same reason: the qualification engine reads
-- them, so they must be queryable and constrained rather than conventions buried in `excerpt`.
-- They are written exactly once -- add_candidate_evidence returns the existing row rather than
-- updating it -- which is what makes evidence membership and ownership immutable: the basis of a
-- decision cannot be edited after a decision has been recorded against it. A NULL source_kind
-- means discovery-only material, which can never become a citation.
CREATE TABLE IF NOT EXISTS civic_candidate_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    source_ref TEXT NOT NULL,
    external_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL
        CHECK (tier IN (
            'official', 'party_primary', 'reputable_media', 'other_media', 'aggregator'
        )),
    stance TEXT NOT NULL
        CHECK (stance IN ('supporting', 'corroborating', 'contradicting', 'context')),
    published_at TEXT,
    captured_at TEXT NOT NULL,
    publisher_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT,
    party_slug TEXT,
    subject_id TEXT NOT NULL DEFAULT '',
    claim_category TEXT,
    event_date TEXT,
    canonical_url TEXT NOT NULL DEFAULT '',
    accessible_full_text INTEGER NOT NULL DEFAULT 0
        CHECK (accessible_full_text IN (0, 1)),
    paywalled INTEGER NOT NULL DEFAULT 0 CHECK (paywalled IN (0, 1)),
    anonymous_source_dependency INTEGER NOT NULL DEFAULT 0
        CHECK (anonymous_source_dependency IN (0, 1)),
    UNIQUE(candidate_id, external_key)
);

CREATE INDEX IF NOT EXISTS idx_civic_candidate_evidence_candidate
    ON civic_candidate_evidence(candidate_id, stance);

-- Append-only record of the bounded extra research passes spent on one Candidate. The attempt
-- number is allocated MAX+1 per candidate under BEGIN IMMEDIATE and is what enforces the budget
-- across crashes: the spend is durable before the work starts, so a worker that dies mid-pass
-- can never silently buy itself another one.
CREATE TABLE IF NOT EXISTS civic_research_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('resolved', 'unresolved', 'error')),
    evidence_added INTEGER NOT NULL DEFAULT 0 CHECK (evidence_added >= 0),
    notes TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error_code TEXT,
    UNIQUE(candidate_id, attempt_number),
    CHECK (
        (outcome = 'error' AND error_code IS NOT NULL)
        OR (outcome != 'error' AND error_code IS NULL)
    ),
    CHECK (outcome != 'resolved' OR evidence_added > 0)
);

-- Append-only verifier records, numbered per Candidate. A later verification never overwrites
-- an earlier one, so the full history behind a published statement stays readable -- and a
-- 'supported' verdict is structurally impossible without supporting evidence and structurally
-- impossible alongside contradicting evidence, mirroring domain.civic.CivicVerification.
CREATE TABLE IF NOT EXISTS civic_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    verdict TEXT NOT NULL
        CHECK (verdict IN ('supported', 'contradicted', 'insufficient', 'error')),
    supporting_evidence_count INTEGER NOT NULL DEFAULT 0
        CHECK (supporting_evidence_count >= 0),
    contradicting_evidence_count INTEGER NOT NULL DEFAULT 0
        CHECK (contradicting_evidence_count >= 0),
    rationale TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    error_code TEXT,
    UNIQUE(candidate_id, attempt_number),
    CHECK (
        (verdict = 'error' AND error_code IS NOT NULL)
        OR (verdict != 'error' AND error_code IS NULL AND model != '')
    ),
    CHECK (
        verdict != 'supported'
        OR (supporting_evidence_count > 0 AND contradicting_evidence_count = 0)
    ),
    CHECK (verdict != 'contradicted' OR contradicting_evidence_count > 0)
);

-- Owner-facing operational messages, on their own send lease and retry budget so a failing mail
-- provider can never stall or duplicate a Civic Batch. Same lease shape as civic_batches:
-- claim_token/lease_expires_at while 'sending', next_attempt_at for backoff after a retryable
-- failure, attempt_count against max_attempts.
CREATE TABLE IF NOT EXISTS civic_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES civic_runs(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'suppressed')),
    claim_token TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    queued_at TEXT NOT NULL,
    settled_at TEXT,
    next_attempt_at TEXT,
    error_code TEXT,
    error_detail TEXT,
    UNIQUE(watch_id, dedupe_key),
    CHECK (
        (status = 'sending' AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status != 'sending' AND claim_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status IN ('sent', 'failed', 'suppressed') AND settled_at IS NOT NULL)
        OR (status IN ('pending', 'sending') AND settled_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_civic_notifications_queue
    ON civic_notifications(status, next_attempt_at, id);

-- Immutable proof that one Civic Batch was accepted downstream. UNIQUE(idempotency_key) is what
-- makes a replayed delivery a no-op: the key -- not the row id, not the batch id -- is the
-- delivery's identity, so a retry presenting the same key finds this row and returns it instead
-- of publishing again. content_hash is recorded alongside it so a replay whose envelope has
-- somehow changed is detectable rather than silently accepted. payload_hash binds the exact
-- canonical publication-v1 bytes as well; NULL is accepted only on legacy rows written before
-- the column existed. There is deliberately no UPDATE path in db/civic_receipts.py: a receipt is
-- written once and never revised.
CREATE TABLE IF NOT EXISTS civic_delivery_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES civic_batches(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    payload_hash TEXT,
    channel TEXT NOT NULL CHECK (channel IN ('spool', 'email')),
    target TEXT NOT NULL,
    provider_reference TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_civic_delivery_receipts_batch
    ON civic_delivery_receipts(batch_id);

-- Versioned receipts for the Civic AI stages. There are exactly two, because there are exactly
-- two model calls per Candidate: `generate` screens the material and, when it passes, writes the
-- canonical Chinese entry together with its English, Hindi and Tagalog overlays in one strict
-- response; `verification` is an independent, differently-modelled call that judges the result.
-- One row per attempt, holding what was sent (hashes only), what came back (normalized, parsed
-- JSON), and which provider, model, prompt and schema produced it. Nothing here stores a
-- credential, a URL or the prompt text itself.
--
-- Two calls rather than six is what makes the plan's fixed ceiling of 30 AI calls per Run
-- compatible with both publication caps: 12 calls for a normal slot of six Candidates, 20 for an
-- election slot of ten, leaving the remaining budget for the single bounded transient retry each
-- stage is allowed. A stage row is never split to make one call look like several.
--
-- The reservation is the fence. A worker inserts a 'reserved' row inside BEGIN IMMEDIATE, commits
-- it, and only then makes the remote call -- so a model call never holds a write lock, and the
-- two partial unique indexes below mean a second worker can neither reserve the same stage
-- concurrently nor record a second success for one that already succeeded. A crash between the
-- reservation and the response leaves exactly one 'reserved' row, which recovery can see and
-- which costs the Candidate one of its bounded attempts rather than being silently refunded.
--
-- input_hash covers the immutable candidate/evidence snapshot the stage was given, and the
-- prompt/schema/model columns cover everything else that could change the answer. A replay whose
-- values all match returns the stored output; a replay whose values differ is drift and raises,
-- because regenerating a statement other rows already depend on must never be silent.
CREATE TABLE IF NOT EXISTS civic_ai_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('generate', 'verification')),
    ai_revision INTEGER NOT NULL DEFAULT 0 CHECK (ai_revision >= 0),
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'succeeded', 'failed')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    output_hash TEXT,
    output_json TEXT,
    error_code TEXT,
    error_detail TEXT NOT NULL DEFAULT '',
    reserved_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(candidate_id, stage, attempt),
    CHECK (
        (status = 'succeeded' AND output_hash IS NOT NULL AND output_json IS NOT NULL
            AND error_code IS NULL AND completed_at IS NOT NULL)
        OR (status = 'failed' AND error_code IS NOT NULL AND output_hash IS NULL
            AND output_json IS NULL AND completed_at IS NOT NULL)
        OR (status = 'reserved' AND output_hash IS NULL AND output_json IS NULL
            AND error_code IS NULL AND completed_at IS NULL)
    )
);

-- Revision-scoped reservation/success indexes are installed by connection.py after additive
-- column migrations, so a legacy table can gain ai_revision before those indexes reference it.
-- The per-run AI call budget read (plan 7.4: 30 calls per run).
CREATE INDEX IF NOT EXISTS idx_civic_ai_stages_run ON civic_ai_stages(run_id, id);

-- Which source items a Civic Run has already turned into a Candidate. Clustering is many-to-one
-- -- several documents about the same development become one Candidate -- so "have I seen this
-- item?" cannot be answered from civic_candidates.dedupe_key alone, and without this table a
-- resumed Run would re-cluster the same articles into a second Candidate under a different key.
-- UNIQUE(watch_id, item_key) is the fence: an item belongs to exactly one Candidate per Watch,
-- forever, and the claim is taken in the same BEGIN IMMEDIATE that creates the Candidate.
CREATE TABLE IF NOT EXISTS civic_consumed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    source_key TEXT NOT NULL,
    external_key TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    UNIQUE(watch_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_civic_consumed_items_candidate
    ON civic_consumed_items(candidate_id);
CREATE INDEX IF NOT EXISTS idx_civic_consumed_items_run
    ON civic_consumed_items(run_id, id);

-- The durable receipt of one pure `beehive.civic.evidence` decision: what went in, what came
-- out, and which build of the engine decided it.
--
-- input_hash covers the claim, the exact evidence rows, the ownership registry and the dedup
-- membership the engine saw -- deliberately NOT the wall clock, so a resumed worker replaying
-- the same decision matches instead of drifting on the passage of time alone. A replay whose
-- inputs hash the same returns the stored decision untouched; a replay whose inputs hash
-- differently raises, because a Run coming back to a Candidate it has already qualified on
-- material that has since changed is exactly the silent rewrite this table exists to prevent.
--
-- research_attempts_used (0 or 1) is what distinguishes the first decision from the one taken
-- after the single bounded research pass, so the extra evidence writes a NEW row rather than
-- overwriting the decision that justified spending the attempt. Both remain readable.
CREATE TABLE IF NOT EXISTS civic_qualifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    research_attempts_used INTEGER NOT NULL DEFAULT 0
        CHECK (research_attempts_used BETWEEN 0 AND 1),
    ai_revision INTEGER NOT NULL DEFAULT 0 CHECK (ai_revision >= 0),
    refresh_reason TEXT,
    engine_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    outcome TEXT NOT NULL
        CHECK (outcome IN (
            'qualified', 'screened_out', 'duplicate', 'deferred_evidence',
            'deferred_unsupported', 'deferred_capacity'
        )),
    disposition TEXT NOT NULL
        CHECK (disposition IN (
            'publishable', 'deferred', 'deferred_unsupported', 'excluded'
        )),
    reasons TEXT NOT NULL,
    evidence_used TEXT NOT NULL DEFAULT '',
    evidence_excluded TEXT NOT NULL DEFAULT '',
    ownership_groups TEXT NOT NULL DEFAULT '',
    dedup_reason TEXT,
    matched_candidate_id INTEGER REFERENCES civic_candidates(id) ON DELETE SET NULL,
    matched_batch_id INTEGER REFERENCES civic_batches(id) ON DELETE SET NULL,
    record_kind TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE(candidate_id, ai_revision, research_attempts_used),
    CHECK (reasons != ''),
    CHECK (
        (outcome = 'qualified' AND disposition = 'publishable')
        OR (outcome IN ('screened_out', 'duplicate') AND disposition = 'excluded')
        OR (outcome IN ('deferred_evidence', 'deferred_capacity') AND disposition = 'deferred')
        OR (outcome = 'deferred_unsupported' AND disposition = 'deferred_unsupported')
    ),
    CHECK (outcome = 'duplicate' OR (matched_candidate_id IS NULL AND matched_batch_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_civic_qualifications_run
    ON civic_qualifications(run_id, candidate_id);

-- Why a VERIFIED Candidate that was ready to publish did not go out in the Batch it was built
-- in, and where it went instead.
--
-- The daily cap (plan 5.2: six normally, ten in an election period) is a capacity limit, not an
-- evidence one, so overflow must not look like a failure anywhere in the record: the Candidate
-- keeps its evidence, its verification and its qualification receipt, keeps owning the source
-- items it consumed, and is simply not in today's envelope. This table is the audit trail of
-- that -- one row per deferral, written by the seal that made the decision.
--
-- from_batch_id is fixed at the moment of deferral; to_batch_id is filled in later, once a Run
-- with room adopts the Candidate. That deliberately does not require the receiving Run to exist
-- when the seal happens: a Batch that has to defer cannot be made to wait for tomorrow. A
-- Candidate deferred twice has two rows, so "carried from Monday to Wednesday" stays readable
-- instead of collapsing into a single moving pointer.
--
-- UNIQUE(candidate_id, from_batch_id) makes the write idempotent: a replayed seal records the
-- same deferral once. The partial index on the open rows is the "still waiting" read.
--
-- `reason` distinguishes the two ways a ready Candidate leaves a Batch without being published:
-- the day was full ('batch_cap_reached'), or the Run itself died before it could seal, in which
-- case the Candidate was adopted or verified but never stamped and has to go back in the queue
-- ('run_failed' / 'run_cancelled' / 'run_deadline_exceeded' / 'run_completed_unsealed').
--
-- Both (run, batch) pairs are COMPOSITE foreign keys into civic_batches(run_id, id) rather than
-- two independent ones, and that is load bearing: with separate keys, deleting just the
-- destination Batch would null to_batch_id and leave to_run_id pointing at a Batch that no
-- longer exists, which is both untrue and unrepresentable under the "either both or neither"
-- CHECK. A composite ON DELETE SET NULL nulls every column of the key together, so the
-- destination is always a whole answer or no answer at all. adopted_at deliberately survives
-- that: the hop really did happen, and forgetting when would be the lie.
CREATE TABLE IF NOT EXISTS civic_capacity_deferrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES civic_watches(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    from_run_id INTEGER NOT NULL,
    from_batch_id INTEGER NOT NULL,
    to_run_id INTEGER,
    to_batch_id INTEGER,
    published_cap INTEGER NOT NULL CHECK (published_cap > 0),
    run_mode TEXT NOT NULL CHECK (run_mode IN ('normal', 'election')),
    -- Why this Watch is still holding the Candidate. The batch_cap_* and run_* reasons belong to
    -- a Candidate that was already VERIFIED; ai_budget_exhausted and ai_transient_failed belong
    -- to one whose generation never finished, which is why they resume at VERIFYING rather than
    -- being adopted straight back to VERIFIED. Keeping both in one table is deliberate: "what is
    -- this Watch holding, since when, and why" is one question, and answering it from two tables
    -- would let the two drift.
    reason TEXT NOT NULL DEFAULT 'batch_cap_reached'
        CHECK (reason IN (
            'batch_cap_reached', 'run_failed', 'run_cancelled', 'run_deadline_exceeded',
            'run_completed_unsealed', 'ai_budget_exhausted', 'ai_transient_failed',
            'ai_provenance_stale', 'source_refresh_required'
        )),
    deferred_at TEXT NOT NULL,
    adopted_at TEXT,
    UNIQUE(candidate_id, from_batch_id),
    CHECK (from_batch_id != to_batch_id),
    -- An open deferral has gone nowhere yet, so it can never name a destination.
    CHECK (adopted_at IS NOT NULL OR (to_run_id IS NULL AND to_batch_id IS NULL)),
    -- The destination is one fact, not two.
    CHECK ((to_run_id IS NULL) = (to_batch_id IS NULL)),
    FOREIGN KEY (from_run_id, from_batch_id)
        REFERENCES civic_batches(run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (to_run_id, to_batch_id)
        REFERENCES civic_batches(run_id, id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_civic_capacity_deferrals_candidate
    ON civic_capacity_deferrals(candidate_id, id);
-- "Which carryovers is this Watch still holding?" -- the read the next Run's adoption takes.
CREATE INDEX IF NOT EXISTS idx_civic_capacity_deferrals_open
    ON civic_capacity_deferrals(watch_id, candidate_id) WHERE adopted_at IS NULL;

-- Base evidence identities and original values remain immutable. Captures are append-only;
-- current readers select the latest capture, including an explicitly unknown publication.
CREATE TABLE IF NOT EXISTS civic_evidence_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES civic_candidate_evidence(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES civic_candidates(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id),
    ai_revision INTEGER NOT NULL CHECK (ai_revision >= 0),
    capture_index INTEGER NOT NULL CHECK (capture_index >= 0),
    predecessor_id INTEGER REFERENCES civic_evidence_captures(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at TEXT,
    event_date TEXT,
    captured_at TEXT NOT NULL,
    accessible_full_text INTEGER NOT NULL CHECK (accessible_full_text IN (0, 1)),
    paywalled INTEGER NOT NULL DEFAULT 0 CHECK (paywalled IN (0, 1)),
    source_metadata_json TEXT NOT NULL CHECK (json_valid(source_metadata_json)),
    content_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(evidence_id, capture_index)
);

CREATE INDEX IF NOT EXISTS idx_civic_evidence_captures_current
    ON civic_evidence_captures(evidence_id, capture_index DESC);
CREATE TRIGGER IF NOT EXISTS civic_evidence_captures_no_update
BEFORE UPDATE ON civic_evidence_captures
BEGIN
    SELECT RAISE(ABORT, 'Civic evidence captures are immutable');
END;
CREATE TRIGGER IF NOT EXISTS civic_evidence_captures_no_standalone_delete
BEFORE DELETE ON civic_evidence_captures
WHEN EXISTS (SELECT 1 FROM civic_candidate_evidence WHERE id = OLD.evidence_id)
 AND EXISTS (SELECT 1 FROM civic_candidates WHERE id = OLD.candidate_id)
 AND EXISTS (SELECT 1 FROM civic_runs WHERE id = OLD.run_id)
BEGIN
    SELECT RAISE(ABORT, 'Civic evidence capture history cannot be deleted independently');
END;
CREATE TRIGGER IF NOT EXISTS civic_evidence_captures_unsealed_only
BEFORE INSERT ON civic_evidence_captures
WHEN NOT EXISTS (
    SELECT 1 FROM civic_candidates c
    JOIN civic_batches b ON b.id = c.batch_id AND b.run_id = c.run_id
    JOIN civic_candidate_evidence e ON e.candidate_id = c.id
    WHERE e.id = NEW.evidence_id AND c.id = NEW.candidate_id
      AND c.run_id = NEW.run_id AND c.ai_revision = NEW.ai_revision
      AND c.status IN ('drafted', 'researching', 'verifying')
      AND c.sealed_into_batch_at IS NULL AND b.status = 'building'
)
OR NEW.capture_index != (
    SELECT COALESCE(MAX(capture_index), -1) + 1
    FROM civic_evidence_captures WHERE evidence_id = NEW.evidence_id
)
OR NEW.predecessor_id IS NOT (
    SELECT id FROM civic_evidence_captures WHERE evidence_id = NEW.evidence_id
    ORDER BY capture_index DESC LIMIT 1
)
BEGIN
    SELECT RAISE(ABORT, 'Civic evidence capture requires current unsealed membership and predecessor');
END;

-- Every authorized detail fetch spends the shared Run budget before network I/O. Failed and
-- abandoned reservations remain charged; completed results can be replayed after restart.
CREATE TABLE IF NOT EXISTS civic_hydration_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES civic_runs(id) ON DELETE CASCADE,
    candidate_id INTEGER REFERENCES civic_candidates(id),
    work_key TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units BETWEEN 0 AND 25),
    purpose TEXT NOT NULL CHECK (purpose IN ('fresh', 'refresh')),
    expected_ai_revision INTEGER,
    capture_fingerprint TEXT,
    plan_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'completed', 'failed', 'blocked')),
    reserved_at TEXT NOT NULL,
    completed_at TEXT,
    result_json TEXT,
    result_hash TEXT,
    error_code TEXT,
    CHECK (
        (status = 'reserved' AND units > 0 AND completed_at IS NULL AND error_code IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL
            AND result_json IS NOT NULL AND result_hash IS NOT NULL AND error_code IS NULL)
        OR (status IN ('failed', 'blocked') AND completed_at IS NOT NULL AND error_code IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_civic_hydration_one_reserved
    ON civic_hydration_attempts(run_id, work_key) WHERE status = 'reserved';
CREATE UNIQUE INDEX IF NOT EXISTS idx_civic_hydration_one_completed
    ON civic_hydration_attempts(run_id, work_key) WHERE status = 'completed';
CREATE INDEX IF NOT EXISTS idx_civic_hydration_run_budget
    ON civic_hydration_attempts(run_id, id);
CREATE INDEX IF NOT EXISTS idx_civic_hydration_candidate
    ON civic_hydration_attempts(candidate_id, id);
CREATE TRIGGER IF NOT EXISTS civic_hydration_spend_immutable
BEFORE UPDATE OF run_id, candidate_id, work_key, claim_token, units, purpose,
    expected_ai_revision, capture_fingerprint, plan_hash, reserved_at ON civic_hydration_attempts
BEGIN
    SELECT RAISE(ABORT, 'Civic hydration authorization cannot be rewritten');
END;
CREATE TRIGGER IF NOT EXISTS civic_hydration_settled_immutable
BEFORE UPDATE ON civic_hydration_attempts
WHEN OLD.status != 'reserved'
BEGIN
    SELECT RAISE(ABORT, 'Settled Civic hydration work is immutable');
END;
CREATE TRIGGER IF NOT EXISTS civic_hydration_budget_guard
BEFORE INSERT ON civic_hydration_attempts
WHEN NEW.units + (
    SELECT COALESCE(SUM(units), 0) FROM civic_hydration_attempts WHERE run_id = NEW.run_id
) > 25
BEGIN
    SELECT RAISE(ABORT, 'Civic Run hydration budget exceeded');
END;
CREATE TRIGGER IF NOT EXISTS civic_hydration_no_refund
BEFORE DELETE ON civic_hydration_attempts
WHEN EXISTS (SELECT 1 FROM civic_runs WHERE id = OLD.run_id)
BEGIN
    SELECT RAISE(ABORT, 'Civic hydration spend cannot be refunded');
END;

-- One row per Civic source with an open whole-site outage episode: its listing and its verified
-- health URL both answered with an HTTP error. The row is the hold's escalation clock; see
-- beehive.db.civic_source_outages for how an episode starts, survives flapping, and ends.
CREATE TABLE IF NOT EXISTS civic_source_outages (
    source_key TEXT PRIMARY KEY,
    unavailable_since TEXT NOT NULL,
    last_unavailable_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
