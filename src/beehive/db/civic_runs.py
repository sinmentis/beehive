"""Civic Run persistence: the durable worker queue, lease, and phase machine for one Civic
Watch's daily slot.

**One Run per slot.** `slot_at` is the Run's identity within its Watch, and
UNIQUE(watch_id, slot_at) is what enforces it in the database rather than in the scheduler.
`enqueue_civic_run` is therefore idempotent by design: asked twice for the same slot it returns
the run it already created, so a restart, an overlapping timer, or two hosts ticking at once can
never mint two Runs for the same slot -- and can never publish the same day twice. Asked for a
*different* slot while the Watch still has an active Run, it raises instead: a Watch works one
slot at a time, so a backlog of missed slots is served in order rather than in parallel (the
partial unique index on civic_runs(watch_id) WHERE status IN ('pending','processing') is the
DB-enforced backstop for that second rule).

**Lease vs. deadline**, exactly as db/research_runs.py documents for Research Runs:
  * claim_token/lease_expires_at is a short worker lease. Every progressive write below is fenced
    on (run_id, claim_token, status='processing'), so a worker that lost its lease can never
    write past it -- a reclaim changes status and/or claim_token first, which makes every later
    call using the old token match nothing at all rather than race the new owner.
  * deadline_at is the Run's fixed total budget, set once via COALESCE at first claim and never
    reset by recovery. A crash-and-retry cycle buys more attempts (attempt_count), never more
    total wall-clock time.
  * The lease can never outlive the deadline: claims and heartbeats both grant
    min(now + lease_seconds, deadline_at). A live worker's own heartbeat is therefore what hard-
    terminates the Run the instant its deadline arrives -- it fails the Run outright
    (error_code='deadline_exceeded') instead of renewing, so every write that worker could still
    attempt while unwinding is already fenced out.

**Phases only move forward** (domain.civic.require_run_phase_advance). Re-entering an earlier
phase after a crash would redo work whose output is already committed -- and re-entering
PUBLISHING could try to mint a second Civic Batch.

`now_fn`, where offered, is the authoritative clock and is sampled ONLY after BEGIN IMMEDIATE has
actually acquired the write lock -- never before. A caller blocked behind another writer until
after this Run's deadline arrives must see that arrival, not the stale `now` it sampled before
ever blocking. `now` is used verbatim only when `now_fn` is omitted (deterministic
single-connection tests); production callers pass their own live clock callback."""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.db.civic_batches import (DEADLINE_RELEASE_REASON, RELEASE_REASON_BY_RUN_STATUS,
                                      release_terminal_run_holds_locked)
from beehive.domain.civic import (CivicRun, CivicRunPhase, CivicRunStatus, CivicVocabularyStatus,
                                   CivicWatchStatus, is_terminal_run_status,
                                   require_run_phase_advance, require_run_transition)

# Global cap on simultaneously-processing Civic Runs, checked under the same BEGIN IMMEDIATE as
# the claim itself so two workers racing to claim different Runs can never both push past it.
_MAX_PROCESSING_RUNS = 2

_TERMINAL_STATUSES = frozenset({
    CivicRunStatus.COMPLETED, CivicRunStatus.CANCELLED, CivicRunStatus.FAILED})


@dataclass(frozen=True)
class CivicRunLease:
    """A CivicRun paired with the worker-only lease bookkeeping that is not part of the pure
    domain contract: lease_expires_at (the short heartbeat lease, distinct from the Run's fixed
    deadline_at) and attempt_count (how many times this Run has been claimed)."""
    run: CivicRun
    lease_expires_at: str
    attempt_count: int


@dataclass(frozen=True)
class CivicRunRecoveryResult:
    """Outcome of one recover_expired_civic_runs sweep."""
    requeued_count: int
    deadline_exceeded_count: int


def _row_to_run(row: sqlite3.Row) -> CivicRun:
    return CivicRun(
        id=row["id"],
        watch_id=row["watch_id"],
        slot_at=datetime.fromisoformat(row["slot_at"]),
        status=CivicRunStatus(row["status"]),
        phase=CivicRunPhase(row["phase"]) if row["phase"] else None,
        requested_at=datetime.fromisoformat(row["requested_at"]),
        vocabulary_version_id=row["vocabulary_version_id"],
        run_mode=row["run_mode"],
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        deadline_at=datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else None,
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
        claim_token=row["claim_token"],
        cancel_requested=bool(row["cancel_requested"]),
        error_code=row["error_code"],
        error_detail=row["error_detail"])


def _fetch(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM civic_runs WHERE id = ?", (run_id,)).fetchone()


def get_civic_run(conn: sqlite3.Connection, run_id: int) -> CivicRun | None:
    row = _fetch(conn, run_id)
    return _row_to_run(row) if row else None


def get_active_civic_run(conn: sqlite3.Connection, watch_id: int) -> CivicRun | None:
    """The Watch's Run currently 'pending' or 'processing', if any -- at most one can ever exist
    per Watch (see the partial unique index on civic_runs)."""
    row = conn.execute(
        "SELECT * FROM civic_runs WHERE watch_id = ? AND status IN ('pending', 'processing')",
        (watch_id,)).fetchone()
    return _row_to_run(row) if row else None


def get_civic_run_for_slot(
    conn: sqlite3.Connection, watch_id: int, slot_at: datetime,
) -> CivicRun | None:
    row = conn.execute(
        "SELECT * FROM civic_runs WHERE watch_id = ? AND slot_at = ?",
        (watch_id, slot_at.isoformat())).fetchone()
    return _row_to_run(row) if row else None


def list_civic_runs(conn: sqlite3.Connection, watch_id: int) -> list[CivicRun]:
    rows = conn.execute(
        "SELECT * FROM civic_runs WHERE watch_id = ? ORDER BY id", (watch_id,)).fetchall()
    return [_row_to_run(row) for row in rows]


def list_pending_civic_runs(conn: sqlite3.Connection, limit: int = 20) -> list[CivicRun]:
    """Oldest-slot-first queue of unclaimed Runs. Does not include expired 'processing' rows --
    call recover_expired_civic_runs first."""
    rows = conn.execute(
        "SELECT * FROM civic_runs WHERE status = 'pending' "
        "ORDER BY slot_at ASC, id ASC LIMIT ?", (limit,)).fetchall()
    return [_row_to_run(row) for row in rows]


def count_active_processing_runs(conn: sqlite3.Connection, now: datetime) -> int:
    """Number of 'processing' Runs whose lease has not yet expired -- the figure the global cap
    is checked against. A Run whose lease has passed is excluded immediately, even before any
    recovery sweep has touched it, so its slot is free the moment it is genuinely abandoned."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM civic_runs "
        "WHERE status = 'processing' AND lease_expires_at > ?", (now.isoformat(),)).fetchone()
    return row["n"]


def _require_pinnable_vocabulary_version(
    conn: sqlite3.Connection, watch_id: int, vocabulary_version_id: int,
) -> None:
    """Gate for every write that pins a Civic Vocabulary version onto a Civic Run, called INSIDE
    the caller's own BEGIN IMMEDIATE so the version cannot be superseded between the check and
    the write.

    Two rules, both hard errors rather than a silent skip, because either one means the caller is
    about to screen a Run against the wrong terms:
      * the version must belong to THIS Watch -- pinning another Watch's vocabulary would let a
        Run publish statements naming entities its own Owner never approved;
      * the version must be PUBLISHED *at this instant* -- a DRAFT is still being edited and has
        no content_hash to quote, and a SUPERSEDED one is no longer the vocabulary in force.

    This is deliberately a check at pin time only. A version pinned while PUBLISHED stays pinned
    after it is later superseded: that is the whole point of pinning, and civic_runs'
    ON DELETE RESTRICT keeps the row readable forever so an already-published Civic Batch stays
    explainable."""
    row = conn.execute(
        "SELECT watch_id, status FROM civic_vocabulary_versions WHERE id = ?",
        (vocabulary_version_id,)).fetchone()
    if row is None:
        raise ValueError(f"no Civic Vocabulary version with id={vocabulary_version_id}")
    if row["watch_id"] != watch_id:
        raise ValueError(
            f"Civic Vocabulary version {vocabulary_version_id} belongs to Civic Watch "
            f"{row['watch_id']}, not {watch_id}")
    if row["status"] != CivicVocabularyStatus.PUBLISHED.value:
        raise ValueError(
            f"Civic Vocabulary version {vocabulary_version_id} is {row['status']!r}, not "
            "'published' -- only the vocabulary currently in force can be pinned to a Civic Run")


def enqueue_civic_run(
    conn: sqlite3.Connection,
    watch_id: int,
    slot_at: datetime,
    now: datetime,
    *,
    vocabulary_version_id: int | None = None,
    run_mode: str = "normal",
) -> CivicRun:
    """Creates (or returns) the single Civic Run for one Watch slot.

    Idempotent per slot: if a Run already exists for (watch_id, slot_at) -- pending, processing,
    or long since terminal -- that Run is returned unchanged and nothing is written. This is the
    property that makes a scheduler safe to re-tick: serving the same slot twice can never
    publish the same day twice. The existing Run keeps whatever vocabulary version it already
    has; `vocabulary_version_id` never overwrites a pin.

    Raises ValueError when the Watch does not exist or is not ACTIVE (a paused or retired Watch
    never accepts new work), or when the Watch already has an active Run for a *different* slot
    -- a Watch serves one slot at a time, so a backlog is worked in order rather than in
    parallel.

    A supplied `vocabulary_version_id` is validated under this call's own BEGIN IMMEDIATE by
    `_require_pinnable_vocabulary_version` -- same Watch, PUBLISHED right now -- and that check
    runs even on the idempotent existing-Run path. A caller whose vocabulary read went stale
    between reading and enqueueing therefore learns about it (ValueError) instead of quietly
    proceeding on the assumption that the terms it read are still in force.

    `run_mode` fixes which publication cap this slot runs under (plan 5.2: six normally, ten in
    an election period) and, like the vocabulary pin, is never overwritten on the idempotent
    existing-Run path -- widening a cap after a Run has already sealed under the narrower one
    would retroactively change what that day was allowed to publish."""
    if run_mode not in ("normal", "election"):
        raise ValueError(f"unknown Civic Run mode: {run_mode!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        watch = conn.execute(
            "SELECT status FROM civic_watches WHERE id = ?", (watch_id,)).fetchone()
        if watch is None:
            raise ValueError(f"no Civic Watch with id={watch_id}")
        if vocabulary_version_id is not None:
            _require_pinnable_vocabulary_version(conn, watch_id, vocabulary_version_id)
        slot_iso = slot_at.isoformat()
        existing = conn.execute(
            "SELECT * FROM civic_runs WHERE watch_id = ? AND slot_at = ?",
            (watch_id, slot_iso)).fetchone()
        if existing is not None:
            run = _row_to_run(existing)
        else:
            if watch["status"] != CivicWatchStatus.ACTIVE.value:
                raise ValueError(
                    f"cannot enqueue a Civic Run for a {watch['status']} Civic Watch {watch_id}")
            active = get_active_civic_run(conn, watch_id)
            if active is not None:
                raise ValueError(
                    f"Civic Watch {watch_id} already has an active Civic Run for slot "
                    f"{active.slot_at.isoformat()}")
            cur = conn.execute(
                "INSERT INTO civic_runs (watch_id, vocabulary_version_id, slot_at, run_mode, "
                "status, requested_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (watch_id, vocabulary_version_id, slot_iso, run_mode, now.isoformat()))
            run = _row_to_run(_fetch(conn, cur.lastrowid))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return run


def claim_civic_run(
    conn: sqlite3.Connection,
    run_id: int,
    now: datetime,
    lease_seconds: int,
    deadline_seconds: int,
    phase: CivicRunPhase = CivicRunPhase.GATHERING,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicRunLease | None:
    """Transactionally claims one pending Run for exclusive processing.

    Returns None -- claiming nothing -- if the Run is no longer 'pending' by the time the UPDATE
    runs, or if the global cap of simultaneously-processing Runs is already reached.

    Equality-safe deadline fencing: a *requeued* pending Run already carries a fixed deadline_at
    (recovery never resets it). If that deadline has already arrived (`now >= deadline_at`) this
    is checked FIRST, before the cap, and the Run is atomically failed outright
    (error_code='deadline_exceeded') rather than granted a lease that would be zero-length or
    already expired the instant it was issued. A first claim (no persisted deadline_at) is never
    affected. deadline_at and started_at are COALESCEd so a reclaim keeps the Run's original,
    already-fixed budget; attempt_count is incremented here -- and ONLY here -- so it counts
    exactly how many times this Run has been claimed, whether or not a recovery sweep ever saw
    it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT deadline_at FROM civic_runs WHERE id = ? AND status = 'pending'",
            (run_id,)).fetchone()
        if row is None:
            result = None
        else:
            existing_deadline_at = (
                datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else None)
            if existing_deadline_at is not None and now >= existing_deadline_at:
                expired = conn.execute(
                    "UPDATE civic_runs SET status = 'failed', phase = NULL, claim_token = NULL, "
                    "lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' WHERE id = ? AND status = 'pending'",
                    (now.isoformat(), run_id))
                if expired.rowcount:
                    # A *requeued* Run can already hold carryovers it adopted on its first
                    # attempt, so failing it here strands them like any other terminal path.
                    release_terminal_run_holds_locked(
                        conn, run_id=run_id, reason=DEADLINE_RELEASE_REASON, now=now)
                result = None
            elif count_active_processing_runs(conn, now) >= _MAX_PROCESSING_RUNS:
                result = None
            else:
                claim_token = secrets.token_urlsafe(32)
                now_iso = now.isoformat()
                computed_deadline_at = now + timedelta(seconds=deadline_seconds)
                deadline_at = existing_deadline_at or computed_deadline_at
                lease_expires_at = min(now + timedelta(seconds=lease_seconds), deadline_at)
                cur = conn.execute(
                    "UPDATE civic_runs SET status = 'processing', phase = ?, claim_token = ?, "
                    "lease_expires_at = ?, attempt_count = attempt_count + 1, "
                    "started_at = COALESCE(started_at, ?), "
                    "deadline_at = COALESCE(deadline_at, ?) "
                    "WHERE id = ? AND status = 'pending'",
                    (phase.value, claim_token, lease_expires_at.isoformat(), now_iso,
                     computed_deadline_at.isoformat(), run_id))
                if cur.rowcount == 0:
                    result = None
                else:
                    claimed = _fetch(conn, run_id)
                    result = CivicRunLease(
                        run=_row_to_run(claimed),
                        lease_expires_at=claimed["lease_expires_at"],
                        attempt_count=claimed["attempt_count"])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def heartbeat_civic_run(
    conn: sqlite3.Connection,
    run_id: int,
    claim_token: str,
    now: datetime,
    lease_seconds: int,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> bool:
    """Extends a live claim's lease -- never past the Run's own fixed deadline_at, and never by
    reviving a lease that has already independently expired.

    * A stale or foreign claim (wrong claim_token, already-terminal/reclaimed Run) matches no row
      at all: returns False, mutates nothing.
    * If `now` has reached or passed deadline_at, this exact claimed Run is transitioned
      immediately to FAILED with error_code='deadline_exceeded' (phase/claim_token/
      lease_expires_at cleared, completed_at set) and False is returned. This is what makes the
      deadline actually hard: the worker can never renew again, so every further write it
      attempts is fenced out even while its own thread is still unwinding.
    * Otherwise, if the CURRENT stored lease_expires_at is already <= `now`, this call refuses to
      renew at all -- returns False and mutates nothing. count_active_processing_runs already
      excludes an expired-lease row, so a replacement Run may already have been claimed into what
      looked like a freed slot; resurrecting this one on top of it could push past the global
      cap. recover_expired_civic_runs, not this call, owns deciding requeue vs. fail from here.
    * Otherwise the lease is renewed to min(now + lease_seconds, deadline_at) and True returned.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT deadline_at, lease_expires_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if row is None or row["deadline_at"] is None:
            ok = False
        else:
            deadline_at = datetime.fromisoformat(row["deadline_at"])
            lease_expired = datetime.fromisoformat(row["lease_expires_at"]) <= now
            if now >= deadline_at:
                expired = conn.execute(
                    "UPDATE civic_runs SET status = 'failed', phase = NULL, claim_token = NULL, "
                    "lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (now.isoformat(), run_id, claim_token))
                if expired.rowcount:
                    release_terminal_run_holds_locked(
                        conn, run_id=run_id, reason=DEADLINE_RELEASE_REASON, now=now)
                ok = False
            elif lease_expired:
                ok = False
            else:
                lease_expires_at = min(now + timedelta(seconds=lease_seconds), deadline_at)
                conn.execute(
                    "UPDATE civic_runs SET lease_expires_at = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (lease_expires_at.isoformat(), run_id, claim_token))
                ok = True
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ok


def advance_civic_run_phase(
    conn: sqlite3.Connection,
    run_id: int,
    claim_token: str,
    phase: CivicRunPhase,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> bool:
    """Moves a claimed Run to a later phase. Fenced on (run_id, claim_token,
    status='processing') like every progressive write here, and validated through
    domain.civic.require_run_phase_advance under the same BEGIN IMMEDIATE that performs the
    write -- so a backwards or repeated phase raises rather than silently redoing committed
    work. Returns False (writing nothing) when the claim no longer matches."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else None
        row = conn.execute(
            "SELECT phase, deadline_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if (row is None or (
                now is not None and row["deadline_at"] is not None
                and now >= datetime.fromisoformat(row["deadline_at"]))):
            advanced = False
        else:
            require_run_phase_advance(CivicRunPhase(row["phase"]), phase)
            cur = conn.execute(
                "UPDATE civic_runs SET phase = ? "
                "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                (phase.value, run_id, claim_token))
            advanced = cur.rowcount > 0
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return advanced


def pin_vocabulary_version(
    conn: sqlite3.Connection, run_id: int, claim_token: str, vocabulary_version_id: int,
) -> bool:
    """Records which Civic Vocabulary version this Run is screening against, once.

    Everything happens under one BEGIN IMMEDIATE, and the two failure modes are deliberately
    reported differently, matching the convention every other write in the civic_* family uses:

    * **False, writing nothing** -- the claim fence did not match. Either (run_id, claim_token,
      status='processing') is stale (a recovery sweep reclaimed the Run) or the Run already has a
      version pinned. Both are races a worker must simply tolerate: a vocabulary published
      mid-run must never change what the run in flight is allowed to say, so an existing pin is
      never overwritten.
    * **ValueError** -- the version itself is not pinnable: it does not exist, belongs to a
      different Civic Watch, or is not PUBLISHED at this exact instant
      (`_require_pinnable_vocabulary_version`). That is not a race to tolerate, it means the
      caller is about to screen against the wrong terms and must re-read.

    The fence is checked first, so a worker that has lost its claim always gets the same
    uniform False from every fenced write rather than a different error depending on what it
    happened to be pinning. Reading the Run's own watch_id inside the transaction is what ties
    the version to the right Watch -- the caller never supplies it, so it cannot be spoofed."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT watch_id FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing' "
            "AND vocabulary_version_id IS NULL",
            (run_id, claim_token)).fetchone()
        if row is None:
            pinned = False
        else:
            _require_pinnable_vocabulary_version(
                conn, row["watch_id"], vocabulary_version_id)
            cur = conn.execute(
                "UPDATE civic_runs SET vocabulary_version_id = ? "
                "WHERE id = ? AND claim_token = ? AND status = 'processing' "
                "AND vocabulary_version_id IS NULL",
                (vocabulary_version_id, run_id, claim_token))
            pinned = cur.rowcount > 0
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return pinned


def request_cancel_civic_run(conn: sqlite3.Connection, run_id: int) -> bool:
    """Sets the cooperative cancel_requested flag. A worker observes it between phases and is
    responsible for actually transitioning to CANCELLED -- setting the flag never forces a
    transition itself. Returns False if the Run is already terminal."""
    cur = conn.execute(
        "UPDATE civic_runs SET cancel_requested = 1 "
        "WHERE id = ? AND status IN ('pending', 'processing')", (run_id,))
    conn.commit()
    return cur.rowcount > 0


def complete_civic_run(
    conn: sqlite3.Connection,
    run_id: int,
    claim_token: str,
    status: CivicRunStatus,
    now: datetime,
    *,
    now_fn: Callable[[], datetime] | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> bool:
    """Terminal write (COMPLETED/CANCELLED/FAILED), fenced on (run_id, claim_token,
    status='processing').

    Cancellation-first precedence: `cancel_requested` is reread under the same BEGIN IMMEDIATE,
    and if set this call commits CANCELLED (clearing error_code/error_detail) regardless of what
    was requested -- an Owner's cancellation that committed a moment before this write must never
    be silently overwritten. Likewise a requested COMPLETED whose deadline has already arrived by
    that same lock-held instant commits FAILED('deadline_exceeded') instead, mirroring
    heartbeat_civic_run's own fencing so both agree on exactly the same instant.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after BEGIN IMMEDIATE
    has actually acquired the write lock -- never before this call even attempts to acquire it --
    exactly like claim_civic_run's and heartbeat_civic_run's own `now_fn` seams. A caller blocked
    behind another writer until after this Run's deadline arrives must see that arrival, not the
    stale `now` it happened to sample before ever blocking; without the seam a slow terminal
    write could commit COMPLETED for a Run whose budget had already expired by the time the write
    actually landed. That lock-held instant is used for the deadline comparison AND for the
    `completed_at` timestamp, so the recorded terminal time is when the transition really
    committed rather than when the caller started trying. `now` is used verbatim only when
    `now_fn` is omitted (deterministic single-connection tests); every production caller MUST
    pass `now_fn` -- its own live clock callback, never a pre-sampled datetime.

    Returns False, writing nothing, when the claim no longer matches."""
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"complete_civic_run status must be terminal, got {status!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT status, cancel_requested, deadline_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if row is None:
            ok = False
        else:
            require_run_transition(CivicRunStatus(row["status"]), status)
            committed_status = status
            committed_error_code = error_code
            committed_error_detail = error_detail
            if row["cancel_requested"]:
                committed_status = CivicRunStatus.CANCELLED
                committed_error_code = None
                committed_error_detail = None
            elif (status is CivicRunStatus.COMPLETED and row["deadline_at"]
                    and now >= datetime.fromisoformat(row["deadline_at"])):
                committed_status = CivicRunStatus.FAILED
                committed_error_code = "deadline_exceeded"
                committed_error_detail = None
            cur = conn.execute(
                "UPDATE civic_runs SET status = ?, phase = NULL, claim_token = NULL, "
                "lease_expires_at = NULL, completed_at = ?, error_code = ?, error_detail = ? "
                "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                (committed_status.value, now.isoformat(), committed_error_code,
                 committed_error_detail, run_id, claim_token))
            ok = cur.rowcount > 0
            if ok:
                # Same transaction as the terminal transition, so a Run can never be observed
                # ended while still holding Candidates no later Run can reach.
                release_terminal_run_holds_locked(
                    conn, run_id=run_id,
                    reason=(DEADLINE_RELEASE_REASON
                            if committed_error_code == "deadline_exceeded"
                            else RELEASE_REASON_BY_RUN_STATUS[committed_status]),
                    now=now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ok


def fail_pending_civic_run(
    conn: sqlite3.Connection, run_id: int, now: datetime, error_code: str,
    error_detail: str | None = None,
) -> bool:
    """Fails a Run that was never claimed (e.g. its Watch was retired, or its Vocabulary was
    withdrawn, before any worker picked it up). Only ever touches a 'pending' row -- validated
    and written under the same BEGIN IMMEDIATE -- so it can never steal a Run out from under a
    live claim."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_runs WHERE id = ? AND status = 'pending'",
            (run_id,)).fetchone()
        if row is None:
            failed = False
        else:
            require_run_transition(CivicRunStatus(row["status"]), CivicRunStatus.FAILED)
            cur = conn.execute(
                "UPDATE civic_runs SET status = 'failed', completed_at = ?, error_code = ?, "
                "error_detail = ? WHERE id = ? AND status = 'pending'",
                (now.isoformat(), error_code, error_detail, run_id))
            failed = cur.rowcount > 0
            if failed:
                # A never-claimed Run has no Batch and so nothing to hand back, but a *requeued*
                # one does, and both arrive here.
                release_terminal_run_holds_locked(
                    conn, run_id=run_id,
                    reason=RELEASE_REASON_BY_RUN_STATUS[CivicRunStatus.FAILED], now=now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return failed


def recover_expired_civic_runs(
    conn: sqlite3.Connection, now: datetime, *, now_fn: Callable[[], datetime] | None = None,
) -> CivicRunRecoveryResult:
    """Reconciliation sweep over 'processing' Runs whose lease has expired (lease_expires_at
    <= now -- an equality match IS expired, exactly like the `now >= deadline_at` fencing
    elsewhere in this module treats the exact deadline instant as already arrived).

    A Run whose fixed deadline_at has ALSO arrived (deadline_at <= now) is failed outright: no
    further attempt fits inside its budget. A Run whose deadline is still strictly in the future
    is requeued to 'pending' with its lease/claim/phase cleared -- but deadline_at, started_at and
    attempt_count untouched, because this is a retry of the same Run rather than a fresh budget,
    and the attempt was already counted when it was claimed.

    Both branches are one UPDATE each under a single BEGIN IMMEDIATE, so a Run can never be seen
    half-recovered and can never be both failed and requeued by two racing sweeps."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        now_iso = now.isoformat()
        expiring = [
            row["id"] for row in conn.execute(
                "SELECT id FROM civic_runs WHERE status = 'processing' "
                "AND lease_expires_at <= ? AND deadline_at <= ? ORDER BY id",
                (now_iso, now_iso)).fetchall()]
        failed = conn.execute(
            "UPDATE civic_runs SET status = 'failed', phase = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, completed_at = ?, error_code = 'deadline_exceeded' "
            "WHERE status = 'processing' AND lease_expires_at <= ? AND deadline_at <= ?",
            (now_iso, now_iso, now_iso))
        for expired_run_id in expiring:
            # The deadline branch only. A requeued Run keeps everything it was holding, because
            # it is coming back to finish it.
            release_terminal_run_holds_locked(
                conn, run_id=expired_run_id, reason=DEADLINE_RELEASE_REASON, now=now)
        requeued = conn.execute(
            "UPDATE civic_runs SET status = 'pending', phase = NULL, claim_token = NULL, "
            "lease_expires_at = NULL "
            "WHERE status = 'processing' AND lease_expires_at <= ? AND deadline_at > ?",
            (now_iso, now_iso))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return CivicRunRecoveryResult(
        requeued_count=requeued.rowcount, deadline_exceeded_count=failed.rowcount)


def is_terminal(run: CivicRun) -> bool:
    return is_terminal_run_status(run.status)
