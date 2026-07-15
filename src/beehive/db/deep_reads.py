"""Deep-read persistence: exactly one row per item (schema.sql's deep_reads.item_id is the
PK), reused in place across attempts rather than inserted-per-attempt -- a regenerate keeps
the same row and only bumps request_version, clearing the previous attempt's terminal data.
claim_token + lease_expires_at implement a single-worker-at-a-time lease: claim_deep_read
hands out a fresh token, and heartbeat_deep_read/complete_deep_read_success/fail_deep_read
all gate their write on status = 'processing' plus an exact (item_id, request_version,
claim_token) match, so a crashed or merely slow worker that is still finishing a stale
attempt can never clobber a newer regenerate (bumped request_version) or a different
worker's claim (different claim_token) -- it just loses the UPDATE (rowcount == 0) and the
caller can tell it was pre-empted. Every function here returns (or accepts) only an
immutable, validated DeepRead -- never a raw sqlite3.Row or a mutable dict -- so an invalid
status can't silently leak out of this module.

Every `now: datetime` parameter is expected to be tz-aware UTC (datetime.now(timezone.utc),
the convention already used by auth/rate_limit.py and web/admin.py) and is stored via
.isoformat(), so lease/requested_at string comparisons stay lexicographically valid. This
module never reads the wall clock itself, which keeps request/claim/heartbeat/complete
sequencing deterministic under a frozen `now` in tests."""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

_STATUSES = frozenset({"pending", "processing", "ready", "failed"})

_DEFAULT_CLAIM_LIST_LIMIT = 20


@dataclass(frozen=True)
class DeepRead:
    """Full current state of one item's deep-read row. Validated on construction so every
    value returned by this module's functions is guaranteed to have a recognized status and
    a sane request_version -- callers never need to re-check those invariants themselves."""
    item_id: int
    status: str
    request_version: int
    claim_token: str | None
    lease_expires_at: str | None
    result_json: str | None
    language_code: str | None
    warning_code: str | None
    error_code: str | None
    error_detail: str | None
    requested_at: str
    started_at: str | None
    completed_at: str | None

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"invalid deep_read status: {self.status!r}")
        if self.request_version < 1:
            raise ValueError(f"request_version must be >= 1, got {self.request_version!r}")


def _row_to_deep_read(row: sqlite3.Row) -> DeepRead:
    return DeepRead(
        item_id=row["item_id"],
        status=row["status"],
        request_version=row["request_version"],
        claim_token=row["claim_token"],
        lease_expires_at=row["lease_expires_at"],
        result_json=row["result_json"],
        language_code=row["language_code"],
        warning_code=row["warning_code"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"])


def _fetch(conn: sqlite3.Connection, item_id: int) -> DeepRead | None:
    row = conn.execute(
        "SELECT * FROM deep_reads WHERE item_id = ?", (item_id,)).fetchone()
    return _row_to_deep_read(row) if row else None


def get_deep_read(conn: sqlite3.Connection, item_id: int) -> DeepRead | None:
    """Plain cache lookup -- read-only, never claims or mutates anything."""
    return _fetch(conn, item_id)


def get_deep_reads_for_items(conn: sqlite3.Connection,
                              item_ids: list[int]) -> dict[int, DeepRead]:
    """Batch cache lookup for the Dashboard/Channel/Archive list views: rendering N items'
    deep-read state as N calls to get_deep_read would be exactly the N+1 pattern those views
    need to avoid, so this issues one parameterized `IN (...)` query for the whole batch
    instead. An item_id with no deep_reads row simply has no key in the returned dict --
    callers should treat a missing key the same way get_deep_read's None return is treated
    (never requested yet)."""
    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT * FROM deep_reads WHERE item_id IN ({placeholders})",
        list(item_ids)).fetchall()
    return {row["item_id"]: _row_to_deep_read(row) for row in rows}


def request_deep_read(conn: sqlite3.Connection, item_id: int, now: datetime,
                       regenerate: bool = False) -> DeepRead:
    """First call for an item creates a fresh pending row. A pending/processing row is
    always reused as-is (an in-flight generation can't be redirected mid-attempt, so
    `regenerate` is ignored while one is running). A ready row is reused as a cache hit
    unless `regenerate` is set. A failed row likewise just reports the failure back unless
    `regenerate` is set -- there is no silent auto-retry. Only from ready/failed with
    `regenerate=True` does this bump request_version and clear the previous attempt's
    terminal data (result/language/warning/error/started/completed), moving the row back to
    pending for a worker to pick up.

    The read-then-decide-then-write sequence below runs inside a single BEGIN IMMEDIATE
    transaction, not the sqlite3 module's default deferred one: a deferred transaction takes
    no lock until its first write, which would leave the SELECT and the following
    INSERT/UPDATE open to another connection's writes landing in between. BEGIN IMMEDIATE
    grabs SQLite's write lock up front (any concurrent writer -- another request_deep_read,
    a claim, a heartbeat, a completion -- blocks on ITS OWN BEGIN IMMEDIATE/first-write until
    we commit), which is what makes two concrete races impossible: two connections racing to
    create the same item's first row (only one can be in this critical section at a time, so
    the loser's fresh SELECT already sees the winner's committed row and never reaches
    INSERT), and a "stale" regenerate clobbering a row another caller already moved to
    pending/processing (that move can only have happened before we entered the critical
    section or after we leave it, never during). The regenerate UPDATE's WHERE clause also
    re-checks status/request_version as defense in depth -- if it ever matches zero rows
    despite the lock, that means the on-disk state no longer matches what we read, so we
    re-fetch and hand back the current row instead of assuming our stale decision landed.
    INSERT OR IGNORE is the same kind of belt-and-suspenders: it should never need to ignore
    anything given the lock, but never raising a UNIQUE error here is a hard requirement even
    if that invariant is ever weakened."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _fetch(conn, item_id)
        if existing is None:
            conn.execute(
                "INSERT OR IGNORE INTO deep_reads (item_id, status, request_version, "
                "requested_at) VALUES (?, 'pending', 1, ?)",
                (item_id, now.isoformat()))
            result = _fetch(conn, item_id)
        elif existing.status in ("pending", "processing") or not regenerate:
            result = existing
        else:
            conn.execute(
                "UPDATE deep_reads SET status = 'pending', "
                "request_version = request_version + 1, claim_token = NULL, "
                "lease_expires_at = NULL, result_json = NULL, language_code = NULL, "
                "warning_code = NULL, error_code = NULL, error_detail = NULL, "
                "requested_at = ?, started_at = NULL, completed_at = NULL "
                "WHERE item_id = ? AND request_version = ? AND status IN ('ready', 'failed')",
                (now.isoformat(), item_id, existing.request_version))
            result = _fetch(conn, item_id)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result



def list_pending_deep_reads(conn: sqlite3.Connection,
                             limit: int = _DEFAULT_CLAIM_LIST_LIMIT) -> list[DeepRead]:
    """Oldest-first queue of unclaimed work. Does not include expired `processing` rows --
    call recover_expired_deep_reads first so a reconciliation pass can requeue those, then
    they show up here on the next call."""
    rows = conn.execute(
        "SELECT * FROM deep_reads WHERE status = 'pending' "
        "ORDER BY requested_at ASC, item_id ASC LIMIT ?",
        (limit,)).fetchall()
    return [_row_to_deep_read(r) for r in rows]


def claim_deep_read(conn: sqlite3.Connection, item_id: int, now: datetime,
                     lease_seconds: int) -> DeepRead | None:
    """Transactionally claim one pending item for exclusive processing. Only succeeds
    (rowcount > 0) if the row is still 'pending' at the moment of the UPDATE, so two workers
    racing to claim the same item_id can never both win. started_at is set only the first
    time a request_version is claimed (COALESCE) -- a later reclaim after
    recover_expired_deep_reads keeps reporting when the *current* attempt truly started."""
    claim_token = secrets.token_urlsafe(32)
    now_iso = now.isoformat()
    lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
    cur = conn.execute(
        "UPDATE deep_reads SET status = 'processing', claim_token = ?, "
        "lease_expires_at = ?, started_at = COALESCE(started_at, ?) "
        "WHERE item_id = ? AND status = 'pending'",
        (claim_token, lease_expires_at, now_iso, item_id))
    conn.commit()
    if cur.rowcount == 0:
        return None
    return _fetch(conn, item_id)


def heartbeat_deep_read(conn: sqlite3.Connection, item_id: int, request_version: int,
                         claim_token: str, now: datetime, lease_seconds: int) -> bool:
    """Extend a live claim's lease. Matches on item_id + request_version + claim_token +
    status = 'processing', so a worker whose claim was already reclaimed (lease expired and
    recovered/re-claimed by someone else) or superseded by a regenerate gets back False
    instead of silently reviving a claim that is no longer its own."""
    lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
    cur = conn.execute(
        "UPDATE deep_reads SET lease_expires_at = ? "
        "WHERE item_id = ? AND request_version = ? AND claim_token = ? "
        "AND status = 'processing'",
        (lease_expires_at, item_id, request_version, claim_token))
    conn.commit()
    return cur.rowcount > 0


def complete_deep_read_success(conn: sqlite3.Connection, item_id: int, request_version: int,
                                claim_token: str, result_json: str, language_code: str,
                                now: datetime, warning_code: str | None = None) -> bool:
    """Terminal success write. Same (item_id, request_version, claim_token,
    status='processing') guard as heartbeat_deep_read: a stale worker finishing a superseded
    attempt loses this UPDATE (rowcount == 0) instead of overwriting a newer regenerate or
    another worker's in-flight claim."""
    cur = conn.execute(
        "UPDATE deep_reads SET status = 'ready', result_json = ?, language_code = ?, "
        "warning_code = ?, error_code = NULL, error_detail = NULL, claim_token = NULL, "
        "lease_expires_at = NULL, completed_at = ? "
        "WHERE item_id = ? AND request_version = ? AND claim_token = ? "
        "AND status = 'processing'",
        (result_json, language_code, warning_code, now.isoformat(), item_id,
         request_version, claim_token))
    conn.commit()
    return cur.rowcount > 0


def fail_deep_read(conn: sqlite3.Connection, item_id: int, request_version: int,
                    claim_token: str, error_code: str, error_detail: str | None,
                    now: datetime) -> bool:
    """Terminal failure write, guarded the same way as complete_deep_read_success."""
    cur = conn.execute(
        "UPDATE deep_reads SET status = 'failed', error_code = ?, error_detail = ?, "
        "claim_token = NULL, lease_expires_at = NULL, completed_at = ? "
        "WHERE item_id = ? AND request_version = ? AND claim_token = ? "
        "AND status = 'processing'",
        (error_code, error_detail, now.isoformat(), item_id, request_version, claim_token))
    conn.commit()
    return cur.rowcount > 0


def recover_expired_deep_reads(conn: sqlite3.Connection, now: datetime) -> int:
    """Reconciliation sweep: any 'processing' row whose lease has expired (its worker
    presumably crashed or was killed without a chance to requeue_deep_read) goes back to
    'pending' with its claim cleared, so list_pending_deep_reads/claim_deep_read can hand it
    to another worker. request_version, started_at and any prior result are left untouched --
    this is a retry of the same attempt, not a regenerate. Returns the number of rows
    recovered."""
    cur = conn.execute(
        "UPDATE deep_reads SET status = 'pending', claim_token = NULL, lease_expires_at = NULL "
        "WHERE status = 'processing' AND lease_expires_at < ?",
        (now.isoformat(),))
    conn.commit()
    return cur.rowcount


def requeue_deep_read(conn: sqlite3.Connection, item_id: int, request_version: int,
                       claim_token: str) -> bool:
    """Voluntarily give back an active claim before its lease expires (e.g. a worker
    shutting down cleanly rearms its in-flight items instead of making them wait out the
    full lease). Guarded the same way as heartbeat_deep_read/complete_deep_read_success, so
    it can only requeue the claim it actually still holds."""
    cur = conn.execute(
        "UPDATE deep_reads SET status = 'pending', claim_token = NULL, lease_expires_at = NULL "
        "WHERE item_id = ? AND request_version = ? AND claim_token = ? "
        "AND status = 'processing'",
        (item_id, request_version, claim_token))
    conn.commit()
    return cur.rowcount > 0
