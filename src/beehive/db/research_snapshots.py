"""Evidence Snapshot persistence (ADR-0010): one immutable-once-sealed body of source material
per explicit search/refresh action, plus its cumulative Evidence Item membership.

research_snapshot_items is cumulative by construction here, not by query-time aggregation:
create_snapshot's `copy_forward_from` copies the previous snapshot's full membership into the
new snapshot before any new items are added, so "the evidence available as of snapshot N" is
always just a plain filter on snapshot_id -- callers never need to walk earlier snapshots."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from beehive.domain.research import (EvidenceSnapshot, EvidenceSnapshotStatus,
                                      require_snapshot_transition)


def _row_to_snapshot(row: sqlite3.Row) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        id=row["id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        sequence_number=row["sequence_number"],
        status=EvidenceSnapshotStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        sealed_at=datetime.fromisoformat(row["sealed_at"]) if row["sealed_at"] else None)


def _fetch(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM research_snapshots WHERE id = ?", (snapshot_id,)).fetchone()


def create_snapshot(conn: sqlite3.Connection, session_id: int, run_id: int, now: datetime,
                     copy_forward_from: int | None = None) -> EvidenceSnapshot:
    """Creates a new 'building' snapshot with sequence_number allocated as MAX(sequence_number)
    + 1 for the session under BEGIN IMMEDIATE. If copy_forward_from is given, that snapshot's
    full item membership is copied into the new snapshot in the same transaction, giving the
    new snapshot cumulative evidence before any newly-collected items are added on top. Raises
    ValueError if copy_forward_from names a snapshot belonging to a different Research Session
    -- session_id and a snapshot id are never allowed to silently disagree about which session
    they belong to -- or if `run_id` already has an Evidence Snapshot of its own: at most one is
    ever allowed per Research Run (the schema's UNIQUE(run_id) index on research_snapshots
    enforces the same invariant as defense in depth, exactly like
    db.research_runs.enqueue_research_run's explicit active-run check backed by its own partial
    unique index). A run must RESUME its own existing snapshot via `get_snapshot_for_run`
    (building or sealed) rather than ever calling this a second time for the same run_id."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM research_snapshots WHERE run_id = ?", (run_id,)).fetchone()
        if existing is not None:
            raise ValueError(
                f"Research Run {run_id} already has an Evidence Snapshot "
                f"(id={existing['id']}) -- at most one is ever allowed per run")
        if copy_forward_from is not None:
            prev = _fetch(conn, copy_forward_from)
            if prev is None:
                raise ValueError(f"no Evidence Snapshot with id={copy_forward_from}")
            if prev["session_id"] != session_id:
                raise ValueError(
                    f"Evidence Snapshot {copy_forward_from} belongs to Research Session "
                    f"{prev['session_id']}, not {session_id}")
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
            "FROM research_snapshots WHERE session_id = ?",
            (session_id,)).fetchone()
        sequence_number = row["next_seq"]
        now_iso = now.isoformat()
        cur = conn.execute(
            "INSERT INTO research_snapshots (session_id, run_id, sequence_number, status, "
            "created_at) VALUES (?, ?, ?, 'building', ?)",
            (session_id, run_id, sequence_number, now_iso))
        snapshot_id = cur.lastrowid
        if copy_forward_from is not None:
            conn.execute(
                "INSERT INTO research_snapshot_items (snapshot_id, evidence_item_id, added_at) "
                "SELECT ?, evidence_item_id, ? FROM research_snapshot_items "
                "WHERE snapshot_id = ?",
                (snapshot_id, now_iso, copy_forward_from))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_snapshot(_fetch(conn, snapshot_id))


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> EvidenceSnapshot | None:
    row = _fetch(conn, snapshot_id)
    return _row_to_snapshot(row) if row else None


class SnapshotClaimFailureReason(str, Enum):
    """Why `get_or_create_snapshot_if_claimed` resolved (or created) nothing. Both are reported
    purely for observability -- a caller must treat them exactly the same way (stop, make no
    further writes), mirroring `db.research_finalization.FinalizationFailureReason`'s own
    CLAIM_LOST/DEADLINE_EXCEEDED pair for the same class of race. CLAIM_LOST means (run_id,
    claim_token, status='processing') no longer matched anything at all by the time this call's
    own BEGIN IMMEDIATE acquired the write lock -- some other heartbeat/recovery sweep/terminal
    write already transitioned or reclaimed this run earlier. DEADLINE_EXCEEDED means this exact
    claim WAS still active at that lock-held instant, `cancel_requested` was NOT set (Task A's
    cancellation-finalization grace bypasses this -- see the function docstring), and the run's
    own fixed deadline_at had -- by that same instant -- already arrived: this call atomically
    fails the run outright (error_code='deadline_exceeded') instead of resolving or creating
    anything."""
    CLAIM_LOST = "claim_lost"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True)
class SnapshotClaimResult:
    """The single typed outcome of `get_or_create_snapshot_if_claimed`. `failure_reason` is None
    iff `snapshot` is this run's own Evidence Snapshot (an existing one resumed, building or
    sealed, or a brand-new 'building' one this call just created) -- callers must never treat a
    non-None `failure_reason` as "still pending", only as "no snapshot was resolved or created
    this call"."""
    failure_reason: SnapshotClaimFailureReason | None
    snapshot: EvidenceSnapshot | None = None

    @property
    def ok(self) -> bool:
        return self.failure_reason is None


def get_or_create_snapshot_if_claimed(
        conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int, now: datetime,
        *, now_fn: Callable[[], datetime] | None = None) -> SnapshotClaimResult:
    """The claim-fenced, atomic sibling of `create_snapshot` that closes its check-then-create
    race (Task D): `research.orchestrator.py` used to read this run's own Evidence Snapshot with
    a plain, unfenced `get_snapshot_for_run` and only THEN, in a wholly separate transaction,
    call `create_snapshot` if none existed -- leaving a real gap in which a stale worker whose
    claim had already been reclaimed by a new owner (its lease expired, recovered, and reclaimed
    by `claim_research_run` on another connection) could still race that new owner to be the one
    whose `create_snapshot` call wins the row for `run_id`. Because `create_snapshot` raises
    ValueError the instant it sees `run_id` already has a snapshot, whichever side's
    `create_snapshot` call happened to run SECOND -- even the genuinely current owner -- could
    receive a spurious ValueError purely from losing that low-level race, never from anything
    wrong with its own claim.

    This function closes that gap by making the claim check and the get-or-create decision one
    atomic unit, inside a single `BEGIN IMMEDIATE` transaction:

    1. Verifies (run_id, claim_token, status='processing') and that the claimed run belongs to
       `session_id` -- the same fence every other claim-fenced write in this package uses. If the
       row no longer matches (already terminal, reclaimed, or a foreign claim_token), nothing is
       read or written and this returns `SnapshotClaimResult(failure_reason=CLAIM_LOST)` -- a
       stale worker whose claim was reclaimed can never win (or lose) this race with a ValueError
       again: it simply gets a clean, typed "your claim is gone" result, exactly like every other
       claim-fenced repository call in this package.
    2. Verifies the run's fixed deadline_at has not yet arrived (`now < deadline_at`, exactly the
       instant heartbeat_research_run/finalize_snapshot_if_claimed already treat as "arrived") --
       UNLESS `cancel_requested` is set, mirroring finalize_snapshot_if_claimed's own
       cancellation-finalization grace (Task A) exactly: an already-cancelled run whose deadline
       has also passed must still be able to resolve (resume or create) its own snapshot, so
       `_finalize` can go on to seal whatever evidence already exists (or correctly report
       CANCELLED_NO_EVIDENCE for a run that never collected any) instead of being hard-failed
       for deadline_exceeded before it ever gets the chance to finalize locally. If the deadline
       HAS arrived and the run is NOT cancelled, this atomically fails the run right here
       (error_code='deadline_exceeded', phase/claim/lease cleared) and resolves/creates nothing,
       returning `SnapshotClaimResult(failure_reason=DEADLINE_EXCEEDED)`.
    3. Looks for this run's own Evidence Snapshot (any status -- the schema's UNIQUE(run_id)
       index guarantees at most one ever exists). If one already exists -- building (an earlier
       attempt of this exact run staged items before crashing or being reclaimed) or sealed (a
       prior attempt already won `finalize_snapshot_if_claimed`'s atomic write) -- it is returned
       as-is, never recreated or mutated.
    4. Otherwise creates a genuinely new 'building' snapshot for this run: sequence_number is
       allocated as MAX(sequence_number)+1 for `session_id` and, if the session already has a
       SEALED snapshot (never a stray still-'building' one some other run left behind, whose
       membership is not fixed yet and must never be copied), that snapshot's full item
       membership is copied forward into the new one before this call returns -- the exact same
       allocation/copy-forward logic `create_snapshot` performs, just under this call's own claim
       fence instead of a separate, unfenced transaction. The schema's UNIQUE(run_id) index
       remains defense in depth against ever creating a second row for this exact run_id.

    Fence failures do not raise: they return a `SnapshotClaimResult` with `failure_reason` set and
    `snapshot=None`, exactly like `finalize_snapshot_if_claimed`/`add_snapshot_items_if_claimed`
    return for the same class of race -- a caller must treat CLAIM_LOST/DEADLINE_EXCEEDED exactly
    like any other claim/deadline loss (stop, no further writes this round).

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like every other `now_fn`
    seam in this package. A caller blocked behind another writer until after this run's deadline
    sees that arrival, rather than the stale `now` it happened to sample before ever attempting
    to acquire the lock. `now` itself is used verbatim only when `now_fn` is omitted
    (deterministic single-connection tests); every production caller MUST pass `now_fn` -- its
    own live clock callback, never a pre-sampled datetime."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        effective_now = now_fn() if now_fn is not None else now
        failure: SnapshotClaimFailureReason | None = None
        snapshot_id: int | None = None

        run_row = conn.execute(
            "SELECT session_id, deadline_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if run_row is None or run_row["session_id"] != session_id:
            failure = SnapshotClaimFailureReason.CLAIM_LOST

        if failure is None:
            deadline_at = (
                datetime.fromisoformat(run_row["deadline_at"])
                if run_row["deadline_at"] else None)
            deadline_arrived = deadline_at is not None and effective_now >= deadline_at
            if deadline_arrived and not bool(run_row["cancel_requested"]):
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (effective_now.isoformat(), run_id, claim_token))
                failure = SnapshotClaimFailureReason.DEADLINE_EXCEEDED
            # else: either the deadline has not arrived, or it has but cancel_requested is set --
            # cancellation-finalization grace (Task A) -- proceed to resolve/create below.

        if failure is None:
            existing = conn.execute(
                "SELECT id, session_id FROM research_snapshots WHERE run_id = ?",
                (run_id,)).fetchone()
            if existing is not None and existing["session_id"] == session_id:
                snapshot_id = existing["id"]
            elif existing is None:
                prev_sealed = conn.execute(
                    "SELECT id FROM research_snapshots WHERE session_id = ? AND status = ? "
                    "ORDER BY sequence_number DESC LIMIT 1",
                    (session_id, EvidenceSnapshotStatus.SEALED.value)).fetchone()
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq "
                    "FROM research_snapshots WHERE session_id = ?",
                    (session_id,)).fetchone()
                sequence_number = seq_row["next_seq"]
                now_iso = effective_now.isoformat()
                cur = conn.execute(
                    "INSERT INTO research_snapshots (session_id, run_id, sequence_number, "
                    "status, created_at) VALUES (?, ?, ?, 'building', ?)",
                    (session_id, run_id, sequence_number, now_iso))
                snapshot_id = cur.lastrowid
                if prev_sealed is not None:
                    conn.execute(
                        "INSERT INTO research_snapshot_items (snapshot_id, evidence_item_id, "
                        "added_at) SELECT ?, evidence_item_id, ? FROM research_snapshot_items "
                        "WHERE snapshot_id = ?",
                        (snapshot_id, now_iso, prev_sealed["id"]))
            else:
                # Defense in depth: a snapshot exists for this run_id but names a DIFFERENT
                # session -- can only mean a data-integrity violation, since every write path
                # that creates a snapshot ties it to the claimed run's own session. Fail closed
                # exactly like every other ineligible-snapshot case in this package.
                failure = SnapshotClaimFailureReason.CLAIM_LOST
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

    if failure is not None:
        return SnapshotClaimResult(failure_reason=failure)
    return SnapshotClaimResult(failure_reason=None, snapshot=get_snapshot(conn, snapshot_id))


def list_snapshots(conn: sqlite3.Connection, session_id: int) -> list[EvidenceSnapshot]:
    rows = conn.execute(
        "SELECT * FROM research_snapshots WHERE session_id = ? ORDER BY sequence_number",
        (session_id,)).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def get_latest_snapshot(conn: sqlite3.Connection, session_id: int) -> EvidenceSnapshot | None:
    row = conn.execute(
        "SELECT * FROM research_snapshots WHERE session_id = ? "
        "ORDER BY sequence_number DESC LIMIT 1",
        (session_id,)).fetchone()
    return _row_to_snapshot(row) if row else None


def get_building_snapshot_for_run(conn: sqlite3.Connection,
                                   run_id: int) -> EvidenceSnapshot | None:
    """Returns the given Research Run's own still-'building' Evidence Snapshot, if it already
    created one -- e.g. an earlier attempt of this exact run staged items before crashing or
    being reclaimed. research.orchestrator.py uses this to RESUME that snapshot on recovery
    instead of creating a second one for the same run and stranding the first, still-'building'
    one with no path to ever being sealed."""
    row = conn.execute(
        "SELECT * FROM research_snapshots WHERE run_id = ? AND status = 'building' "
        "ORDER BY sequence_number DESC LIMIT 1",
        (run_id,)).fetchone()
    return _row_to_snapshot(row) if row else None


def get_snapshot_for_run(conn: sqlite3.Connection, run_id: int) -> EvidenceSnapshot | None:
    """Returns the given Research Run's own Evidence Snapshot regardless of status (building OR
    sealed) -- the schema's UNIQUE(run_id) index on research_snapshots guarantees at most one row
    ever matches. research.orchestrator.py checks this FIRST on every crash-recovery resume,
    before `get_building_snapshot_for_run`'s narrower building-only view: a run whose own
    snapshot is already 'sealed' means a PRIOR attempt of this exact run already won
    `db.research_finalization.finalize_snapshot_if_claimed`'s atomic clusters+seal+revision write
    before crashing or losing its claim, and must be RESUMED (never re-clustered, re-sealed, or
    given a second snapshot) rather than routed through `get_building_snapshot_for_run`, which
    would see nothing and (incorrectly) let a second snapshot be created for this same run."""
    row = conn.execute(
        "SELECT * FROM research_snapshots WHERE run_id = ? "
        "ORDER BY sequence_number DESC LIMIT 1",
        (run_id,)).fetchone()
    return _row_to_snapshot(row) if row else None


def get_latest_sealed_snapshot(conn: sqlite3.Connection,
                                session_id: int) -> EvidenceSnapshot | None:
    """Returns the Research Session's most recent SEALED Evidence Snapshot, skipping over any
    stray still-'building' one a run left behind without sealing (e.g. one abandoned by a run
    that failed before finalizing). This -- never `get_latest_snapshot`, which does not filter
    on status -- is what a genuinely new run snapshot must copy forward from: a still-'building'
    snapshot's membership is not fixed yet and must never be copied into another snapshot."""
    row = conn.execute(
        "SELECT * FROM research_snapshots WHERE session_id = ? AND status = 'sealed' "
        "ORDER BY sequence_number DESC LIMIT 1",
        (session_id,)).fetchone()
    return _row_to_snapshot(row) if row else None


def add_snapshot_items(conn: sqlite3.Connection, snapshot_id: int, evidence_item_ids: list[int],
                        now: datetime) -> None:
    """Adds newly-collected Evidence Items to a still-'building' snapshot. INSERT OR IGNORE
    makes re-adding an item already in the snapshot (e.g. a retried step) a no-op rather than
    an error, matching this table's cumulative, append-friendly membership model. Runs under
    BEGIN IMMEDIATE and raises ValueError if the snapshot is missing or already 'sealed' -- a
    sealed snapshot's item membership is immutable, so this must never silently succeed (or
    silently no-op) against one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _fetch(conn, snapshot_id)
        if row is None:
            raise ValueError(f"no Evidence Snapshot with id={snapshot_id}")
        if row["status"] != EvidenceSnapshotStatus.BUILDING.value:
            raise ValueError(
                f"cannot add items to Evidence Snapshot {snapshot_id}: status is "
                f"{row['status']!r}, not 'building'")
        now_iso = now.isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO research_snapshot_items (snapshot_id, evidence_item_id, "
            "added_at) VALUES (?, ?, ?)",
            [(snapshot_id, item_id, now_iso) for item_id in evidence_item_ids])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


class SnapshotAppendFailureReason(str, Enum):
    """Why `add_snapshot_items_if_claimed` wrote nothing. CLAIM_LOST and DEADLINE_EXCEEDED mirror
    db.research_finalization's FinalizationFailureReason exactly -- a caller must treat either
    the same way (stop, make no further writes), since it cannot always distinguish "someone
    else's heartbeat/recovery sweep already failed, requeued, or reclaimed this run" from "this
    transaction itself just found the deadline has arrived": both leave the exact same
    zero-new-rows outcome. INVALID_ITEMS is different in kind, not degree: it means every id in
    `evidence_item_ids` was checked against `research_evidence_items` under this same lock and at
    least one is missing or belongs to a foreign session -- a caller/input mismatch, never a
    claim or deadline race. It fails closed the same way (nothing appended), but the run's claim
    remains perfectly live and is never itself failed for it -- exactly like
    db.research_finalization.FinalizationFailureReason.MEMBERSHIP_CHANGED is never treated as a
    claim loss."""
    CLAIM_LOST = "claim_lost"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INVALID_ITEMS = "invalid_items"


@dataclass(frozen=True)
class SnapshotAppendResult:
    """The single typed outcome of `add_snapshot_items_if_claimed`. `failure_reason` is None iff
    every id in `evidence_item_ids` was (idempotently) appended to the snapshot's membership --
    callers must never treat a non-None `failure_reason` as "partially applied", only as
    "nothing was written"."""
    failure_reason: SnapshotAppendFailureReason | None

    @property
    def ok(self) -> bool:
        return self.failure_reason is None


def add_snapshot_items_if_claimed(
        conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
        snapshot_id: int, evidence_item_ids: Sequence[int], now: datetime, *,
        now_fn: Callable[[], datetime] | None = None) -> SnapshotAppendResult:
    """Claim- and deadline-fenced sibling of `add_snapshot_items`, closing the stale-worker
    membership race: a worker whose claim has already been reclaimed -- its lease expired and
    `recover_expired_research_runs` requeued or failed it, or some other heartbeat/recovery
    sweep discovered its deadline had arrived, all on a wholly separate connection -- must never
    be able to append Evidence Items to a snapshot a newer claim holder may already have read
    the membership of and computed clusters/curation against. An application-level claim check
    followed by a separate, unfenced write leaves exactly that gap open: a stale worker can pass
    the check, pause (GC, thread scheduling, a slow network call unwinding), and only then reach
    the write, by which point its claim is long gone. This closes it by making the check and the
    write one atomic unit, exactly like `db.research_finalization.finalize_snapshot_if_claimed`
    and `db.evidence_items.upsert_evidence_item_if_claimed` already do for their own writes --
    under a single `BEGIN IMMEDIATE` transaction, this verifies:

    1. (run_id, claim_token, status='processing') and that the run belongs to `session_id`,
       exactly like every other claim-fenced write in this package.
    2. The run's fixed deadline_at has not yet arrived (`now < deadline_at`, exact-equality
       counts as arrived, mirroring claim_research_run/heartbeat_research_run/
       finalize_snapshot_if_claimed's own `now >= deadline_at` fencing exactly). If it HAS
       arrived, the run is atomically failed right here (error_code='deadline_exceeded',
       phase/claim/lease cleared) and nothing is appended.
    3. The target Evidence Snapshot belongs to this exact run and session and is still
       'building' -- appending to an already-'sealed' snapshot (this run's own finalization
       already won the race, or any other unexpected state) fails closed instead of silently
       mutating membership that is supposed to be immutable once sealed.
    4. Every id in `evidence_item_ids` names a Research Evidence Item that actually belongs to
       `session_id` -- checked as one set-equality comparison against `research_evidence_items`
       under this same lock. A missing id or one belonging to a foreign session fails closed
       with `INVALID_ITEMS` and appends nothing, but -- unlike CLAIM_LOST/DEADLINE_EXCEEDED --
       never fails the run itself: this is a caller/input mismatch (a programming error upstream
       of this call), not evidence the claim or deadline is bad, so the run remains fully able to
       retry with corrected ids while its claim stays live.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like
    finalize_snapshot_if_claimed/claim_research_run's own `now_fn` seam. A caller blocked behind
    another writer until after this run's deadline sees that arrival, rather than the stale
    `now` it happened to sample before ever attempting to acquire the lock. `now` itself is used
    verbatim only when `now_fn` is omitted (deterministic single-connection tests); every
    production caller MUST pass `now_fn` -- its own live clock callback, never a pre-sampled
    datetime.

    Fence failures do not raise: they return a `SnapshotAppendResult` with `failure_reason` set
    and nothing written, exactly like `finalize_snapshot_if_claimed` returns for the same class
    of race -- callers must treat CLAIM_LOST/DEADLINE_EXCEEDED exactly like any other claim/
    deadline loss (stop, no further writes this round); INVALID_ITEMS alone leaves the claim
    itself untouched. `INSERT OR IGNORE` still makes re-adding an item already in the snapshot a
    no-op, not an error, matching `add_snapshot_items`'s own cumulative, append-friendly
    membership model."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        effective_now = now_fn() if now_fn is not None else now
        failure: SnapshotAppendFailureReason | None = None

        run_row = conn.execute(
            "SELECT session_id, deadline_at FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if run_row is None or run_row["session_id"] != session_id:
            failure = SnapshotAppendFailureReason.CLAIM_LOST

        if failure is None:
            deadline_at = (
                datetime.fromisoformat(run_row["deadline_at"])
                if run_row["deadline_at"] else None)
            if deadline_at is not None and effective_now >= deadline_at:
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (effective_now.isoformat(), run_id, claim_token))
                failure = SnapshotAppendFailureReason.DEADLINE_EXCEEDED

        if failure is None:
            snapshot_row = _fetch(conn, snapshot_id)
            eligible = (
                snapshot_row is not None
                and snapshot_row["session_id"] == session_id
                and snapshot_row["run_id"] == run_id
                and snapshot_row["status"] == EvidenceSnapshotStatus.BUILDING.value)
            if not eligible:
                failure = SnapshotAppendFailureReason.CLAIM_LOST

        if failure is None and evidence_item_ids:
            requested_ids = frozenset(evidence_item_ids)
            placeholders = ", ".join("?" for _ in requested_ids)
            owned_ids = frozenset(
                row["id"] for row in conn.execute(
                    f"SELECT id FROM research_evidence_items "
                    f"WHERE session_id = ? AND id IN ({placeholders})",
                    (session_id, *requested_ids)).fetchall())
            if owned_ids != requested_ids:
                failure = SnapshotAppendFailureReason.INVALID_ITEMS

        if failure is None:
            now_iso = effective_now.isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO research_snapshot_items (snapshot_id, evidence_item_id, "
                "added_at) VALUES (?, ?, ?)",
                [(snapshot_id, item_id, now_iso) for item_id in evidence_item_ids])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return SnapshotAppendResult(failure_reason=failure)


def list_snapshot_item_ids(conn: sqlite3.Connection, snapshot_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT evidence_item_id FROM research_snapshot_items WHERE snapshot_id = ? "
        "ORDER BY evidence_item_id",
        (snapshot_id,)).fetchall()
    return [r["evidence_item_id"] for r in rows]


def seal_snapshot(conn: sqlite3.Connection, snapshot_id: int, now: datetime) -> EvidenceSnapshot:
    """Transitions building -> sealed. Raises ValueError (via require_snapshot_transition) if
    the snapshot does not exist or is already sealed -- a sealed snapshot's item membership is
    immutable from this point on; add_snapshot_items is only ever called on a building one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _fetch(conn, snapshot_id)
        if row is None:
            raise ValueError(f"no Evidence Snapshot with id={snapshot_id}")
        require_snapshot_transition(
            EvidenceSnapshotStatus(row["status"]), EvidenceSnapshotStatus.SEALED)
        conn.execute(
            "UPDATE research_snapshots SET status = 'sealed', sealed_at = ? WHERE id = ?",
            (now.isoformat(), snapshot_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_snapshot(_fetch(conn, snapshot_id))
