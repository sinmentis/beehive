"""Research Run persistence (ADR-0009): the durable worker's queue, lease, and phase machine
for one Research Session's search/refresh action.

Lease vs. deadline -- two distinct, deliberately separate time budgets, with the deadline as the
hard, never-negotiable ceiling on the other:
  * claim_token/lease_expires_at is a short worker lease, exactly like db/deep_reads.py's --
    claim_research_run/heartbeat_research_run/recover_expired_research_runs manage it, and a
    stale worker that lost its lease can never write past it (every progressive write below is
    fenced on (run_id, claim_token, status='processing')).
  * deadline_at is the run's fixed overall time budget, set once via COALESCE the first time a
    run is claimed and never reset by recover_expired_research_runs. A crash-and-retry cycle
    can extend how many *attempts* a run gets (attempt_count) but never how much total wall-
    clock time it is allowed across all of them -- recovery that finds the deadline already
    passed fails the run outright instead of requeuing it for yet another attempt.
  * The lease can never outlive the deadline: claim_research_run grants lease_expires_at =
    min(now + lease_seconds, deadline_at), and heartbeat_research_run renews it the same way --
    capped, never extended past deadline_at -- so a live worker's own heartbeat is what
    physically hard-terminates a run the instant its deadline arrives (heartbeat_research_run
    atomically checks `now >= deadline_at` under BEGIN IMMEDIATE and fails the run outright,
    typed error_code='deadline_exceeded', rather than renewing). A worker's background thread
    that cannot be forcibly killed may still be unwinding after that point, but its claim is
    already terminal and every write it could still attempt is fenced the same way as any other
    stale claim -- it can never extend or resurrect the run.

Cancellation-first precedence at terminal completion: an Owner's cooperative cancel_requested
flag, once committed, must never be silently overwritten by a same-instant deadline arrival, OR
by a terminal write already on its way to request some OTHER status -- FAILED included -- before
that cancellation committed. complete_research_run_if_claimed's `honor_cancel` parameter (default
True) rereads cancel_requested BEFORE its own deadline check and BEFORE honoring WHATEVER
requested_status was actually passed in -- if it is set, this call commits CANCELLED (clearing
error_code/error_detail) regardless of whether the deadline has ALSO since arrived, and
regardless of whether the caller requested COMPLETED or an explicit FAILED (e.g. `_finalize`'s
'no_evidence_collected' or `_synthesize_and_terminate`'s 'synthesis_failed'). Only a requested
COMPLETED that is neither cancelled nor past-deadline commits as COMPLETED; a requested COMPLETED
that is past-deadline and NOT cancelled still fails outright, unchanged. A caller performing a
true integrity/operational failure that must never be silently hidden behind a cancellation that
merely happened to race it -- e.g. `research.orchestrator._resume_sealed_run`'s sealed-snapshot-
with-no-revision check, or the research worker's Research-Session-no-longer-exists check -- passes
`honor_cancel=False` explicitly at that one call site instead, so its own requested FAILED (with
its own specific error_code) always commits exactly as requested, cancellation or not.

Cancellation-finalization grace, never a work-budget extension: a run whose cancel_requested is
set is still allowed to seal already-collected evidence (db.research_finalization.
finalize_snapshot_if_claimed) and reach a terminal CANCELLED status EVEN AFTER its own fixed
deadline_at has arrived -- but it may never start any NEW connector/fetch/LLM work past that
instant (research.orchestrator.py's round loop and `_remaining_seconds` already enforce that
independently). To make that possible, three of the functions below carry a narrow, explicitly
documented exception to the "deadline is a hard, never-negotiable ceiling" rule above, gated
strictly on cancel_requested=1:
  * claim_research_run may reclaim a *pending* cancel-requested run whose persisted deadline_at
    has already arrived, granting a normal bounded lease based on `now` (never a zero-length or
    already-expired one) instead of failing it outright -- a non-cancelled pending run past its
    deadline is unaffected and still fails exactly as before.
  * heartbeat_research_run may extend a cancel-requested claim's lease PAST its own deadline_at
    instead of hard-terminating it, the instant that deadline is discovered to have arrived --
    a short, finalize-only grace so the current claim holder can finish local finalization; a
    non-cancelled claim past its deadline is unaffected and still hard-terminated exactly as
    before.
  * recover_expired_research_runs requeues (never fails) an expired-lease cancel-requested
    'processing' run even once its deadline_at has also passed, so it can be reclaimed for
    cancellation finalization by the rule above; a non-cancelled expired run at/past its
    deadline is unaffected and still fails exactly as before.
None of this ever hands the SAME slot to two different claims at once: every one of the three
exceptions above still requires (run_id, claim_token, status='processing') to match exactly
under its own BEGIN IMMEDIATE, so it can only ever act on a claim nobody else has reclaimed in
the meantime -- a reclaim (recover_expired_research_runs' requeue followed by a fresh
claim_research_run) always changes status and/or claim_token first, which makes every later call
using the OLD claim_token simply match nothing at all (CLAIM_LOST/False), never a stale renewal
racing the new owner. Separately, and unconditionally (deadline grace or not),
heartbeat_research_run's ORDINARY (non-grace) renewal path additionally requires the current
stored lease_expires_at to be strictly greater than the lock-held effective `now` before it will
renew anything at all (Task B) -- seeing `lease_expires_at <= now` there, with the run's fixed
deadline_at still strictly in the future, means this claim's own short lease already elapsed
independently of the deadline (e.g. this heartbeat call itself arrived late), and count_active_
processing_runs may already be treating this run's slot as free; renewing regardless would risk
more than three simultaneously "active" processing runs. That case returns False and writes
nothing at all, leaving the next recover_expired_research_runs sweep to decide requeue vs. fail,
exactly like any other already-expired lease. (The grace path never re-checks this the same way,
because by construction lease_expires_at is always <= deadline_at -- once deadline_at itself has
arrived, the stored lease has therefore also already elapsed by definition, not independently of
it; requiring it there would make the grace unreachable.)

Every dataclass returned here is domain.research.ResearchRun (frozen, self-validating) except
where lease bookkeeping (lease_expires_at, attempt_count) is not part of that domain contract;
those cases return ResearchRunLease, a local pairing of the domain object with the extra
DB-only fields a worker needs to operate its own lease.

The global cap of three concurrently processing runs (ADR-0009) is enforced in
claim_research_run by counting unexpired-lease 'processing' rows under the same BEGIN IMMEDIATE
transaction as the claim itself -- so two workers racing to claim different pending runs can
never both push the count past three.

enqueue_research_run enforces a second, per-session invariant: at most one active
(pending/processing) Research Run per Research Session, checked explicitly under its own BEGIN
IMMEDIATE before inserting -- mirroring db/research_chat_requests.py's one-active-chat-request-
per-session check -- with the partial unique index on research_runs(session_id) WHERE status IN
('pending','processing') as defense in depth."""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from beehive.db.research_sessions import _is_session_active
from beehive.domain.research import ResearchRun, ResearchRunPhase, ResearchRunStatus

_MAX_PROCESSING_RUNS = 3
_MAX_DEEP_FETCH_COUNT = 30

_TERMINAL_STATUSES = frozenset({
    ResearchRunStatus.COMPLETED, ResearchRunStatus.CANCELLED, ResearchRunStatus.FAILED,
})


@dataclass(frozen=True)
class ResearchRunLease:
    """A ResearchRun paired with the worker-only lease bookkeeping that is not part of the
    pure domain contract: lease_expires_at (the short heartbeat lease, distinct from the run's
    fixed deadline_at) and attempt_count (how many times this run has been claimed)."""
    run: ResearchRun
    lease_expires_at: str
    attempt_count: int


@dataclass(frozen=True)
class ResearchRunRecoveryResult:
    """Outcome of one recover_expired_research_runs sweep."""
    requeued_count: int
    deadline_exceeded_count: int


class TerminalCompletionFailureReason(str, Enum):
    """Why `complete_research_run_if_claimed`'s requested terminal status did not commit
    exactly as requested. STALE_CLAIM means (run_id, claim_token, status='processing') no
    longer matched anything at all by the time this call's own BEGIN IMMEDIATE acquired the
    write lock -- some other heartbeat/recovery sweep/terminal write already transitioned or
    reclaimed this run earlier, and NOTHING is written by this call. DEADLINE_EXCEEDED means
    this exact claim WAS still active at that lock-held instant, `requested_status` was
    COMPLETED, EITHER `honor_cancel` was False OR `cancel_requested` was NOT set (cancellation
    takes precedence over this check whenever both are true -- see `ResearchRunTerminalResult`),
    and the run's own fixed deadline_at had -- by that same instant -- already arrived: this call
    atomically fails the run outright (error_code='deadline_exceeded') INSTEAD OF COMPLETED,
    exactly mirroring heartbeat_research_run/finalize_snapshot_if_claimed's own
    `now >= deadline_at` fencing. Neither is ever a success -- a caller must never read a result
    with either reason set as the requested status having silently gone through."""
    STALE_CLAIM = "stale_claim"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True)
class ResearchRunTerminalResult:
    """The single typed outcome of `complete_research_run_if_claimed`. `failure_reason` is None
    iff this call itself committed the run to a terminal state -- in that case `committed_status`
    is the run's ACTUAL final status, which -- whenever `honor_cancel` is True (the default) --
    may differ from whatever `requested_status` actually was: `cancel_requested` is reread fresh,
    under the very same lock, and CANCELLED is committed instead the instant it is set -- closing
    the race where an Owner's cancellation commits a moment before this run's own terminal write
    would otherwise have landed. This cancellation-first precedence applies EVEN IF the run's
    deadline has ALSO since arrived by this same lock-held instant (Task A): a cancellation that
    already committed must never be overwritten by a deadline watchdog (DEADLINE_EXCEEDED is only
    ever reported for a requested COMPLETED that is genuinely past-deadline and NOT cancelled --
    see below). A requested CANCELLED status is never materially affected by this precedence
    either way (committing CANCELLED is already what it would choose); a requested FAILED status,
    by contrast, IS overwritten by it the same way COMPLETED is -- e.g. `_finalize`'s
    'no_evidence_collected' or `_synthesize_and_terminate`'s 'synthesis_failed' commits CANCELLED
    (with error_code/error_detail cleared to None) instead, the instant cancel_requested is found
    set. `honor_cancel=False` is the explicit, caller-chosen opt-out reserved for true integrity/
    operational failures that must commit exactly as requested regardless of cancellation (see
    `complete_research_run_if_claimed`'s own docstring). Whenever `failure_reason` is set,
    `committed_status` is None -- callers must never treat that as "still pending", only as
    "this call made no progress toward the requested status"."""
    failure_reason: TerminalCompletionFailureReason | None
    committed_status: ResearchRunStatus | None = None

    @property
    def ok(self) -> bool:
        return self.failure_reason is None


def _row_to_run(row: sqlite3.Row) -> ResearchRun:
    return ResearchRun(
        id=row["id"],
        session_id=row["session_id"],
        status=ResearchRunStatus(row["status"]),
        phase=ResearchRunPhase(row["phase"]) if row["phase"] else None,
        requested_at=datetime.fromisoformat(row["requested_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        deadline_at=datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else None,
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
        claim_token=row["claim_token"],
        cancel_requested=bool(row["cancel_requested"]),
        deep_fetch_count=row["deep_fetch_count"])


def _fetch(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()


def get_active_research_run(conn: sqlite3.Connection, session_id: int) -> ResearchRun | None:
    """The session's run currently in 'pending' or 'processing', if any -- at most one can ever
    exist per session (see the partial unique index on research_runs)."""
    row = conn.execute(
        "SELECT * FROM research_runs WHERE session_id = ? "
        "AND status IN ('pending', 'processing')",
        (session_id,)).fetchone()
    return _row_to_run(row) if row else None


def enqueue_research_run(conn: sqlite3.Connection, session_id: int,
                          now: datetime) -> ResearchRun:
    """Creates a fresh pending Research Run. Uses BEGIN IMMEDIATE (per the module-wide
    convention of taking the write lock up front for every enqueue/claim/cap/version/citation
    allocation) so the active-run check below and the INSERT happen as one atomic unit.
    Raises ValueError if the Research Session is not 'active' (archived or nonexistent) --
    an archived session can never have new work enqueued against it -- or if the session
    already has an active (pending/processing) Research Run, checked explicitly here so the
    caller gets a clear error, with the partial unique index on (session_id) WHERE status IN
    ('pending','processing') as defense in depth if this check were ever bypassed. A session's
    terminal runs (completed/cancelled/failed) are never touched by this check, so a fresh
    refresh is always allowed once the active run reaches a terminal state."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _is_session_active(conn, session_id):
            raise ValueError(
                f"cannot enqueue a Research Run for a non-active Research Session {session_id}")
        if get_active_research_run(conn, session_id) is not None:
            raise ValueError(
                f"Research Session {session_id} already has an active Research Run")
        cur = conn.execute(
            "INSERT INTO research_runs (session_id, status, requested_at) "
            "VALUES (?, 'pending', ?)",
            (session_id, now.isoformat()))
        run_id = cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_run(_fetch(conn, run_id))


def get_research_run(conn: sqlite3.Connection, run_id: int) -> ResearchRun | None:
    row = _fetch(conn, run_id)
    return _row_to_run(row) if row else None


def list_research_runs(conn: sqlite3.Connection, session_id: int) -> list[ResearchRun]:
    rows = conn.execute(
        "SELECT * FROM research_runs WHERE session_id = ? ORDER BY id",
        (session_id,)).fetchall()
    return [_row_to_run(r) for r in rows]


def list_pending_research_runs(conn: sqlite3.Connection, limit: int = 20) -> list[ResearchRun]:
    """Oldest-first queue of unclaimed runs. Does not include expired 'processing' rows --
    call recover_expired_research_runs first."""
    rows = conn.execute(
        "SELECT * FROM research_runs WHERE status = 'pending' "
        "ORDER BY requested_at ASC, id ASC LIMIT ?",
        (limit,)).fetchall()
    return [_row_to_run(r) for r in rows]


def count_active_processing_runs(conn: sqlite3.Connection, now: datetime) -> int:
    """Number of 'processing' runs whose lease has not yet expired -- the figure the global
    three-run cap is checked against."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM research_runs "
        "WHERE status = 'processing' AND lease_expires_at > ?",
        (now.isoformat(),)).fetchone()
    return row["n"]


def claim_research_run(conn: sqlite3.Connection, run_id: int, now: datetime,
                        lease_seconds: int, deadline_seconds: int,
                        phase: ResearchRunPhase = ResearchRunPhase.PLANNING, *,
                        now_fn: Callable[[], datetime] | None = None,
                        ) -> ResearchRunLease | None:
    """Transactionally claims one pending run for exclusive processing, refusing the claim if
    three other runs are already actively processing (ADR-0009's global cap) or if the run is
    no longer 'pending' by the time the UPDATE runs. deadline_at is set only the first time a
    run is claimed (COALESCE) so a later reclaim after recover_expired_research_runs keeps the
    original fixed budget; started_at is COALESCEd the same way.

    The lease is never allowed to outlive the run's own fixed deadline: lease_expires_at is set
    to min(now + lease_seconds, deadline_at), using whichever deadline_at actually applies -- the
    already-fixed one on a reclaim, or the freshly-computed one on a first claim -- so a lease
    can never be granted (or, via heartbeat_research_run, renewed) past the point recover_
    expired_research_runs/heartbeat_research_run would fail the run outright for exceeding its
    hard deadline.

    Equality-safe deadline fencing: a *requeued* pending run already carries a persisted, fixed
    deadline_at (recover_expired_research_runs never resets it). If that deadline has already
    arrived (`now >= deadline_at`) by the time anyone tries to reclaim it -- e.g. nobody polled
    it in time after it was requeued -- this is checked FIRST, before the global cap, and the run
    is atomically failed outright (error_code='deadline_exceeded', terminal, lease/claim/phase
    cleared) rather than granted a lease that would be zero-length or already expired the instant
    it was issued. This mirrors heartbeat_research_run's and recover_expired_research_runs' own
    `now >= deadline_at` fencing so all three call sites agree on exactly the same instant. A
    first claim (no persisted deadline_at yet) is never affected by this check.

    Cancellation-finalization grace (Task A): the one exception to the paragraph above. If the
    pending row's own `cancel_requested` is set, a past-arrived deadline_at no longer fails the
    run outright -- it is instead reclaimed normally (still subject to the global cap below),
    with `lease_expires_at` computed as a normal `now + lease_seconds` window instead of being
    capped (and thereby zeroed or already expired) against the stale, already-past deadline_at.
    `deadline_at` itself is still never reset -- this grants a bounded operational window to run
    orchestration straight into `_finalize`'s local-only clusters+seal+revision write and
    terminalize CANCELLED, never a new work budget: the round loop is never entered for an
    already-cancelled run, and every AI-call timeout this package computes
    (`orchestrator._remaining_seconds`) is already <= 0. A *non*-cancelled pending run past its
    deadline is unaffected and still fails outright exactly as before.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before this call even attempts to acquire it.
    A caller blocked behind another writer holding that lock until after this run's persisted
    deadline_at arrives must see that arrival, not the stale `now` it happened to sample before
    ever blocking -- using the pre-lock `now` here could otherwise grant (or reclaim) a
    zero-length or already-expired lease, or let a since-arrived deadline slip past unnoticed.
    Every deadline comparison, the lease/deadline computation, and every timestamp this call
    writes (`started_at`, `completed_at` on a deadline failure) uses that lock-held instant.
    `now` itself is used verbatim only when `now_fn` is omitted (deterministic single-connection
    tests); every production caller MUST pass `now_fn` -- its own live clock callback, never a
    pre-sampled datetime."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT deadline_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND status = 'pending'",
            (run_id,)).fetchone()
        if row is None:
            result = None
        else:
            existing_deadline_at = (
                datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else None)
            deadline_arrived = existing_deadline_at is not None and now >= existing_deadline_at
            cancelled = bool(row["cancel_requested"])
            if deadline_arrived and not cancelled:
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND status = 'pending'",
                    (now.isoformat(), run_id))
                result = None
            elif count_active_processing_runs(conn, now) >= _MAX_PROCESSING_RUNS:
                result = None
            else:
                claim_token = secrets.token_urlsafe(32)
                now_iso = now.isoformat()
                computed_deadline_at = now + timedelta(seconds=deadline_seconds)
                deadline_at = existing_deadline_at or computed_deadline_at
                if deadline_arrived:
                    # Cancellation-finalization grace: deadline_at has already arrived, but
                    # cancel_requested is set -- grant a normal bounded lease based on `now`
                    # rather than min(..., deadline_at), which would zero (or already expire)
                    # it against the stale, past deadline.
                    lease_expires_at = now + timedelta(seconds=lease_seconds)
                else:
                    lease_expires_at = min(now + timedelta(seconds=lease_seconds), deadline_at)
                cur = conn.execute(
                    "UPDATE research_runs SET status = 'processing', phase = ?, "
                    "claim_token = ?, lease_expires_at = ?, "
                    "started_at = COALESCE(started_at, ?), "
                    "deadline_at = COALESCE(deadline_at, ?) "
                    "WHERE id = ? AND status = 'pending'",
                    (phase.value, claim_token, lease_expires_at.isoformat(), now_iso,
                     computed_deadline_at.isoformat(), run_id))
                if cur.rowcount == 0:
                    result = None
                else:
                    result_row = _fetch(conn, run_id)
                    result = ResearchRunLease(
                        run=_row_to_run(result_row),
                        lease_expires_at=result_row["lease_expires_at"],
                        attempt_count=result_row["attempt_count"])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def heartbeat_research_run(conn: sqlite3.Connection, run_id: int, claim_token: str,
                            now: datetime, lease_seconds: int, *,
                            now_fn: Callable[[], datetime] | None = None) -> bool:
    """Extends a live claim's lease -- but never past the run's own fixed deadline_at (unless the
    Task A cancellation-finalization grace below applies), and never by reviving a lease that has
    already independently expired. Reads the exact claimed row (run_id, claim_token,
    status='processing') and the decision it makes is atomic with the write under its own BEGIN
    IMMEDIATE, so no other transaction can observe or act on a half-updated state:

    * A stale or foreign claim (wrong claim_token, already-terminal/reclaimed run) matches no
      row at all -- returns False and mutates nothing.
    * If `now` has reached or passed deadline_at:
        - and `cancel_requested` is NOT set, this exact claimed run is transitioned immediately
          to FAILED with typed error_code='deadline_exceeded', phase/claim_token/
          lease_expires_at cleared and completed_at set -- the same terminal shape complete_
          research_run leaves -- and False is returned. This is what makes the run's hard
          deadline actually hard: a live worker that calls this past the deadline can never
          renew its lease again, so every further write it attempts is fenced out by (run_id,
          claim_token, status='processing') no longer matching anything, even while its own
          background thread (which cannot be forcibly killed) is still unwinding.
        - and `cancel_requested` IS set (Task A's cancellation-finalization grace), the lease is
          instead renewed to `now + lease_seconds` -- deliberately UNCAPPED by deadline_at, since
          the whole point is to extend past it -- and True is returned. This is a short,
          finalize-only grace for the CURRENT claim holder to seal already-collected evidence
          (db.research_finalization.finalize_snapshot_if_claimed carries the same cancel_
          requested-gated bypass) and reach a terminal CANCELLED status; it is never a new work
          budget -- deadline_at itself is untouched, the round loop is never (re-)entered for an
          already-cancelled run, and every AI-call timeout this package computes
          (orchestrator._remaining_seconds) is already <= 0, so no further connector/fetch/LLM
          call can start regardless of how long this lease now runs.
    * Otherwise (deadline not yet arrived): if the CURRENT stored `lease_expires_at` is already
      <= `now`, this call refuses to renew it at all -- returns False and mutates nothing. This
      closes the "expired lease revival" race (Task B): count_active_processing_runs already
      excludes a 'processing' row the instant its lease_expires_at passes, even before any
      reconciliation sweep has touched it, so a replacement run may already have been claimed
      into what looked like a freed slot. A heartbeat call that itself arrived late enough for
      its OWN prior lease to have elapsed independently of the deadline (a GC pause, thread-
      scheduling delay, a slow write queue, ...) must never "resurrect" this run on top of that
      replacement -- doing so could push more than three simultaneously-active processing runs.
      recover_expired_research_runs, not this call, owns deciding requeue vs. fail from here.
    * Otherwise, the lease is renewed to min(now + lease_seconds, deadline_at) -- capped so a
      generous lease_seconds can never itself grant time past the fixed deadline -- and True is
      returned.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like claim_research_run's/
    finalize_snapshot_if_claimed's own `now_fn` seam. A caller blocked behind another writer
    until after this run's deadline sees that arrival, rather than the stale `now` it happened
    to sample before ever attempting to acquire the lock. Every deadline comparison and every
    timestamp this call writes (`completed_at` on a deadline failure, the renewed
    `lease_expires_at` otherwise) uses that lock-held instant. `now` itself is used verbatim
    only when `now_fn` is omitted (deterministic single-connection tests); every production
    caller MUST pass `now_fn` -- its own live clock callback, never a pre-sampled datetime."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT deadline_at, lease_expires_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if row is None or row["deadline_at"] is None:
            ok = False
        else:
            deadline_at = datetime.fromisoformat(row["deadline_at"])
            lease_expired = datetime.fromisoformat(row["lease_expires_at"]) <= now
            cancelled = bool(row["cancel_requested"])
            if now >= deadline_at and not cancelled:
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (now.isoformat(), run_id, claim_token))
                ok = False
            elif lease_expired:
                # Task B: never revive an expired lease -- see the function docstring's third
                # bullet. Write nothing; recover_expired_research_runs owns the next transition.
                ok = False
            elif cancelled:
                # Cancellation-finalization grace: once cancellation is observed while the
                # current lease is still live, stop capping renewals at the work deadline. If
                # this heartbeat arrives too late, the expired-lease branch above wins and
                # recovery/reclaim owns the finalize-only continuation.
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                conn.execute(
                    "UPDATE research_runs SET lease_expires_at = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (lease_expires_at.isoformat(), run_id, claim_token))
                ok = True
            else:
                lease_expires_at = min(now + timedelta(seconds=lease_seconds), deadline_at)
                conn.execute(
                    "UPDATE research_runs SET lease_expires_at = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (lease_expires_at.isoformat(), run_id, claim_token))
                ok = True
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ok


def advance_research_run_phase(conn: sqlite3.Connection, run_id: int, claim_token: str,
                                phase: ResearchRunPhase) -> bool:
    """Moves a claimed run to its next processing phase. Fenced the same way as every
    progressive write in this module: (run_id, claim_token, status='processing')."""
    cur = conn.execute(
        "UPDATE research_runs SET phase = ? "
        "WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (phase.value, run_id, claim_token))
    conn.commit()
    return cur.rowcount > 0


def reserve_deep_fetch(conn: sqlite3.Connection, run_id: int, claim_token: str,
                        count: int = 1) -> bool:
    """Reserves `count` deep-fetch slots against the run's 30-fetch cap (ADR-0010) *before*
    the corresponding I/O runs, inside its own BEGIN IMMEDIATE transaction. Returns False (no
    reservation made) if the run is no longer this worker's active claim or the cap would be
    exceeded -- callers must check the return value and skip the fetch entirely on False."""
    if count <= 0:
        raise ValueError("count must be positive")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT deep_fetch_count FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if row is None or row["deep_fetch_count"] + count > _MAX_DEEP_FETCH_COUNT:
            success = False
        else:
            cur = conn.execute(
                "UPDATE research_runs SET deep_fetch_count = deep_fetch_count + ? "
                "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                (count, run_id, claim_token))
            success = cur.rowcount > 0
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return success


def request_cancel_research_run(conn: sqlite3.Connection, run_id: int) -> bool:
    """Sets the cooperative cancel_requested flag. A worker observes this between phases and
    is responsible for actually transitioning to CANCELLED via complete_research_run -- setting
    the flag never forces a transition itself. Returns False if the run is already terminal."""
    cur = conn.execute(
        "UPDATE research_runs SET cancel_requested = 1 "
        "WHERE id = ? AND status IN ('pending', 'processing')",
        (run_id,))
    conn.commit()
    return cur.rowcount > 0


def complete_research_run(conn: sqlite3.Connection, run_id: int, claim_token: str,
                           status: ResearchRunStatus, now: datetime,
                           error_code: str | None = None,
                           error_detail: str | None = None) -> bool:
    """Terminal write (COMPLETED/CANCELLED/FAILED), fenced on (run_id, claim_token,
    status='processing') like every progressive write in this module. Clears phase/claim_token/
    lease_expires_at -- domain.research.ResearchRun requires phase to be None outside
    PROCESSING, and clearing the lease means a stale heartbeat can never appear to still hold
    an active claim on a finished run.

    This is a single, un-fenced-against-the-deadline UPDATE using whatever `now` the caller
    happened to sample: it cannot detect a run whose deadline_at has arrived by the time this
    write actually lands (it would happily write COMPLETED/CANCELLED past the deadline), nor
    can it detect a cancellation that committed a moment before this call, since it never rereads
    `cancel_requested`. Kept only for callers that do not need either guarantee (e.g. a worker's
    own catch-all `error_code` write, where the requested status IS the final word regardless).
    Every orchestrator terminal path uses `complete_research_run_if_claimed` instead -- see its
    own docstring for exactly what this plain version cannot do."""
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"complete_research_run status must be terminal, got {status!r}")
    cur = conn.execute(
        "UPDATE research_runs SET status = ?, phase = NULL, claim_token = NULL, "
        "lease_expires_at = NULL, completed_at = ?, error_code = ?, error_detail = ? "
        "WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (status.value, now.isoformat(), error_code, error_detail, run_id, claim_token))
    conn.commit()
    return cur.rowcount > 0


def complete_research_run_if_claimed(
        conn: sqlite3.Connection, run_id: int, claim_token: str,
        requested_status: ResearchRunStatus, now: datetime, *,
        now_fn: Callable[[], datetime] | None = None, error_code: str | None = None,
        error_detail: str | None = None, honor_cancel: bool = True) -> ResearchRunTerminalResult:
    """The deep, lock-held-clock sibling of `complete_research_run`, closing the races a plain
    pre-sampled-`now` UPDATE cannot: a run's own fixed deadline_at arriving concurrently with a
    requested SUCCESS write, and an Owner's cancellation committing a moment before ANY requested
    terminal write -- COMPLETED or an explicit FAILED alike. Inside a single `BEGIN IMMEDIATE`
    transaction:

    1. Verifies (run_id, claim_token, status='processing') -- exactly like every other claim-
       fenced write in this package. If the row no longer matches (already terminal, reclaimed,
       or a foreign claim_token), nothing is written and this returns
       `ResearchRunTerminalResult(failure_reason=STALE_CLAIM)`.
    2. Cancellation-first precedence (Task A, extended to every requested status): when
       `honor_cancel` is True (the default), `cancel_requested` is reread fresh under this same
       lock BEFORE the deadline check in point 3 and BEFORE honoring whatever `requested_status`
       actually was. If it is set, this call commits CANCELLED right here -- error_code and
       error_detail are both cleared to None regardless of what was passed in, phase/claim/lease
       cleared -- and returns `ResearchRunTerminalResult(failure_reason=None,
       committed_status=CANCELLED)`, closing the race where an Owner's cancellation commits on a
       wholly separate connection a moment before this run's own terminal write -- requesting
       COMPLETED, or an explicit FAILED such as `_finalize`'s 'no_evidence_collected' or
       `_synthesize_and_terminate`'s 'synthesis_failed' -- would otherwise have landed. This
       takes effect EVEN IF the run's deadline has ALSO since arrived by this exact lock-held
       instant: a deadline watchdog (heartbeat_research_run, another concurrent
       complete_research_run_if_claimed call, recover_expired_research_runs, ...) must never be
       able to overwrite a cancellation that already preserved completed evidence with a false
       FAILED/deadline_exceeded instead. `honor_cancel=False` is the explicit, caller-chosen
       opt-out for a true integrity/operational failure that must never be silently hidden
       behind a cancellation that merely happened to race it -- e.g.
       `research.orchestrator._resume_sealed_run`'s sealed-snapshot-with-no-revision check
       (`error_code='sealed_snapshot_missing_revision'`) or the research worker's Research-
       Session-no-longer-exists check (`error_code='ResearchSessionMissing'`): with
       `honor_cancel=False`, `cancel_requested` is never consulted at all by this call, and
       control always proceeds to point 3 as if it were never set.
    3. Only for `requested_status=COMPLETED` (the one "success terminal" this module reasons
       about specially), and only once point 2 has NOT already committed CANCELLED, re-verifies
       the run's fixed deadline_at has not yet arrived (`now >= deadline_at`, the exact instant
       claim_research_run/heartbeat_research_run/finalize_snapshot_if_claimed already treat as
       "arrived"). If it HAS arrived, this atomically fails the run right here -- FAILED,
       error_code='deadline_exceeded', phase/claim/lease cleared -- INSTEAD OF COMPLETED, and
       returns `ResearchRunTerminalResult(failure_reason=DEADLINE_EXCEEDED)`: a false COMPLETED
       success is never reported once the deadline has genuinely arrived by this call's own
       lock-held instant for a run point 2 did not already decide to cancel.
    4. Otherwise commits `requested_status` exactly as given, together with `error_code`/
       `error_detail` exactly as given -- a requested CANCELLED or an explicit FAILED (e.g.
       `error_code='synthesis_failed'`, a missing sealed revision, etc.) that point 2 did not
       already supersede is never second-guessed by the deadline check, mirroring
       `_synthesize_and_terminate`'s own design: a cooperative stop-and-finalize is deliberately
       allowed to complete past the run's own deadline (it only stops making new AI calls, per
       this module's docstring), and a claim that has already reached an explicit failure is not
       itself a race a since-arrived deadline can or should paper over. Returns
       `ResearchRunTerminalResult(failure_reason=None,
       committed_status=<the actual status just committed>)`.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like claim_research_run's/
    finalize_snapshot_if_claimed's own `now_fn` seam. `now` itself is used verbatim only when
    `now_fn` is omitted (deterministic single-connection tests); every production caller MUST
    pass `now_fn` -- its own live clock callback, never a pre-sampled datetime."""
    if requested_status not in _TERMINAL_STATUSES:
        raise ValueError(
            f"complete_research_run_if_claimed status must be terminal, got {requested_status!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT deadline_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if row is None:
            failure: TerminalCompletionFailureReason | None = (
                TerminalCompletionFailureReason.STALE_CLAIM)
            committed_status: ResearchRunStatus | None = None
        elif honor_cancel and bool(row["cancel_requested"]):
            # Cancellation-first precedence (Task A, extended to every requested_status, not
            # just COMPLETED): wins over BOTH the deadline check below AND whatever status was
            # actually requested -- error_code/error_detail are cleared so an explicit FAILED
            # request's own code (e.g. 'no_evidence_collected', 'synthesis_failed') never
            # persists once a real cancellation has already committed.
            conn.execute(
                "UPDATE research_runs SET status = 'cancelled', phase = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                "error_code = NULL, error_detail = NULL "
                "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                (now.isoformat(), run_id, claim_token))
            failure = None
            committed_status = ResearchRunStatus.CANCELLED
        else:
            deadline_at = (
                datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else None)
            deadline_arrived = deadline_at is not None and now >= deadline_at
            if requested_status is ResearchRunStatus.COMPLETED and deadline_arrived:
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (now.isoformat(), run_id, claim_token))
                failure = TerminalCompletionFailureReason.DEADLINE_EXCEEDED
                committed_status = None
            else:
                conn.execute(
                    "UPDATE research_runs SET status = ?, phase = NULL, claim_token = NULL, "
                    "lease_expires_at = NULL, completed_at = ?, error_code = ?, "
                    "error_detail = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (requested_status.value, now.isoformat(), error_code, error_detail, run_id,
                     claim_token))
                failure = None
                committed_status = requested_status
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ResearchRunTerminalResult(failure_reason=failure, committed_status=committed_status)


def requeue_research_run(conn: sqlite3.Connection, run_id: int, claim_token: str) -> bool:
    """Voluntarily gives back an active claim before its lease expires -- e.g. the durable
    worker (a later todo) rearms an in-flight run on a graceful shutdown instead of making it
    wait out the full lease. Mirrors db/deep_reads.py's requeue_deep_read: guarded on (run_id,
    claim_token, status='processing') exactly like heartbeat_research_run/complete_research_run,
    so it can only requeue the claim it actually still holds. Clears phase/claim_token/
    lease_expires_at like recover_expired_research_runs' requeue path; deadline_at, started_at,
    and attempt_count are left untouched -- this is a continuation of the same run/attempt
    budget, not a fresh one."""
    cur = conn.execute(
        "UPDATE research_runs SET status = 'pending', phase = NULL, claim_token = NULL, "
        "lease_expires_at = NULL WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (run_id, claim_token))
    conn.commit()
    return cur.rowcount > 0


def recover_expired_research_runs(conn: sqlite3.Connection, now: datetime, *,
                                   now_fn: Callable[[], datetime] | None = None,
                                   ) -> ResearchRunRecoveryResult:
    """Reconciliation sweep over 'processing' runs whose lease has expired (lease_expires_at
    <= now -- an equality match IS expired, exactly like heartbeat_research_run and
    claim_research_run's own `now >= deadline_at` fencing treat the exact deadline instant as
    already-arrived, never as still-valid). A run whose fixed deadline_at has ALSO already
    arrived (deadline_at <= now -- equality fails too, never requeues) is failed outright (no
    further attempt is possible within its budget) UNLESS `cancel_requested` is set; a run is
    requeued to 'pending' instead -- with attempt_count incremented and its lease/claim/phase
    cleared, but deadline_at and started_at left untouched (this is a retry of the same run, not
    a fresh budget) -- whenever its deadline_at is still strictly in the future (deadline_at >
    now, regardless of cancel_requested), OR its deadline_at has already arrived AND
    cancel_requested is set.

    That second requeue condition is the cancellation-finalization grace (Task A): an expired,
    already-cancelled run whose deadline has ALSO passed is never simply failed outright the way
    a non-cancelled one is -- it is requeued so a future claim_research_run call (which carries
    the matching cancel_requested-gated exception to its own past-deadline check) can reclaim it
    and run orchestration straight into `_finalize`'s local-only clusters+seal+revision write,
    preserving whatever evidence this run already collected before terminalizing CANCELLED,
    rather than discarding it via a bare deadline_exceeded failure. A *non*-cancelled expired run
    at/past its deadline is unaffected and still failed outright exactly as before.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like claim_research_run's/
    heartbeat_research_run's own `now_fn` seam. A caller blocked behind another writer until
    after some run's deadline arrives sees that arrival, rather than the stale `now` it happened
    to sample before ever attempting to acquire the lock -- every comparison and every timestamp
    this sweep writes (`completed_at` on a deadline failure) uses that lock-held instant. `now`
    itself is used verbatim only when `now_fn` is omitted (deterministic single-connection
    tests); every production caller MUST pass `now_fn` -- its own live clock callback, never a
    pre-sampled datetime."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        now_iso = now.isoformat()
        failed = conn.execute(
            "UPDATE research_runs SET status = 'failed', phase = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, completed_at = ?, error_code = 'deadline_exceeded' "
            "WHERE status = 'processing' AND lease_expires_at <= ? AND deadline_at <= ? "
            "AND cancel_requested = 0",
            (now_iso, now_iso, now_iso))
        requeued = conn.execute(
            "UPDATE research_runs SET status = 'pending', phase = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, attempt_count = attempt_count + 1 "
            "WHERE status = 'processing' AND lease_expires_at <= ? "
            "AND (deadline_at > ? OR cancel_requested = 1)",
            (now_iso, now_iso))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ResearchRunRecoveryResult(
        requeued_count=requeued.rowcount, deadline_exceeded_count=failed.rowcount)
