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
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'editorial' CHECK (kind IN ('editorial', 'monitor', 'tracker')),
    fetch_interval_hours INTEGER NOT NULL DEFAULT 3,
    highlight_count      INTEGER NOT NULL DEFAULT 8 CHECK (highlight_count BETWEEN 1 AND 50),
    minimum_score        INTEGER NOT NULL DEFAULT 0 CHECK (minimum_score BETWEEN 0 AND 100),
    digest_email          TEXT,
    last_digest_sent_at   TEXT,
    last_digest_date      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- A channel's periodic digest email is sent as part of at most one email_groups "group" (see
-- email_group_channels below), not per-channel -- this replaced the old fixed once-daily digest.
-- subject_template is formatted with .format(date=...) at send time (see digest/compose.py);
-- send_interval_hours + last_sent_at drive scheduling.email_group_is_due exactly like
-- sources.last_fetch_at drives source_is_due. recipient_email is optional: a blank/NULL value
-- falls back to the same global-default resolver channels already use
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
    last_sent_at TEXT,
    last_checked_at TEXT,
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
--   suppressed_at-- set when AI scoring drops the Item, so the pending event is never delivered.
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
