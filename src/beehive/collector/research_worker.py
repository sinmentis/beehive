# src/beehive/collector/research_worker.py
"""The durable Research worker (ADR-0009): one long-running asyncio process that drains both
Research Runs (research.orchestrator) and Research Chat replies (research.conversation) through
two independent, bounded pools -- research runs default max 3, chat replies default max 3 -- so
a handful of long research runs can never starve a chat reply, and vice versa.

=== Connection ownership (never shared across tasks or threads) ===
Exactly three kinds of sqlite3.Connection exist here, and each is used by exactly one thing:
  * the coordinator connection -- one per ResearchWorker, opened lazily and closed by close()/
    run()'s own finally -- used ONLY for claim/list/reconcile bookkeeping (list_pending_*,
    claim_*, recover_expired_*, and the global/language reads load_localizer/load_model, which
    are the same kind of quick, synchronous, non-task-owned read). It is never handed to a task
    or a heartbeat.
  * one heartbeat connection per live task, opened right before that task's supervisor spawns
    and closed right after its heartbeat loop stops -- used only by that task's own independent
    heartbeat coroutine, on the main event loop, never touched by the task's own work.
  * one task connection per live task -- for a chat reply, opened on the main loop and passed to
    research.conversation.process_claimed_chat_request; for a research run, opened and closed
    entirely INSIDE the dedicated background thread that runs it (see _default_research_task_
    runner) so the connection object is only ever touched on the one thread that created it.

=== Moving research's still-synchronous connector/fetch work off the event loop ===
research.orchestrator.run_research_orchestration is `async def`, but still calls
research.collector.collect and research.enrichment.reserve_and_deep_fetch directly (not via any
executor) -- both perform blocking connector/HTTP I/O. Awaiting it straight on this worker's own
event loop would therefore block every other live task (heartbeats, chat replies) for as long as
that I/O takes. Per ADR-0009 and without changing orchestrator.py itself, _run_in_thread is the
narrow executor seam that fixes this: it runs a whole claimed research run -- connection open,
`asyncio.run(run_research_orchestration(...))`, connection close -- on its own dedicated daemon
thread with its OWN fresh event loop, and bridges only the final result (or exception) back to
this worker's event loop via a loop-owned asyncio.Future. The main loop's heartbeats and chat
pool keep running the entire time that thread is blocked on connector/HTTP I/O.

=== Claim loss, the deadline watchdog, and graceful shutdown ===
Each live task has its own heartbeat coroutine (a separate connection, per above) that renews
its lease every heartbeat_interval_seconds; if a renewal is refused (the lease already expired
and was recovered/reclaimed elsewhere -- or, for a research run, its own hard deadline_at has
now arrived), the task's own asyncio wrapper is cancelled and the worker simply stops tracking/
waiting for it -- for a chat reply this actually interrupts the in-flight AI call; for a research
run the underlying background thread cannot be forcibly killed and keeps running to its own
natural completion, but every write it could still make is fenced by db.research_runs's own
(run_id, claim_token, status='processing') checks, so it can never clobber whatever a new claim
holder does (persistence fencing is the last line of defense, exactly as db/research_runs.py's
and orchestrator.py's own docstrings describe).

A research run's heartbeat additionally acts as its deadline watchdog: it stores the claimed
run's own deadline_at (from claim_research_run/the pending row's ResearchRunLease) in that task's
live bookkeeping, and sleeps for min(heartbeat_interval_seconds, seconds-until-deadline) instead
of always the full heartbeat interval, so it wakes up (and calls heartbeat_research_run) right at
the deadline rather than up to a whole heartbeat_interval_seconds late. For a run nobody asked to
cancel, heartbeat_research_run is what atomically hard-terminates it at that instant --
transitioning it straight to FAILED (error_code='deadline_exceeded') under its own BEGIN IMMEDIATE
and refusing the renewal -- so by the time this worker cancels/detaches the supervisor the DB row
is already terminal; the background thread may still be physically unwinding (connector/AI calls
remain bounded by their own transport timeouts or remaining-deadline budget, never by an unsafe
thread-kill), but it can no longer extend or persist anything against that claim. For an
Owner-cancelled run, heartbeat_research_run instead grants a short cancellation-finalization
grace past the deadline (Task A) so the background thread can finish sealing whatever evidence it
already collected -- this worker never decides that distinction itself, it only ever reacts to
whatever heartbeat_research_run's own claim/deadline/cancellation fencing reports: a renewal that
keeps succeeding is left alone (the loop polls at a small fixed interval once the deadline has
passed, rather than busy-spinning), and one that is finally refused (the claim reached a genuine
terminal state, by any route) is treated exactly like any other lost claim. This worker's own
heartbeat can therefore never itself "un-cancel" or resurrect a run past a deadline or an expired
lease -- db.research_runs.recover_expired_research_runs and the fenced writes inside
orchestrator.py's own claimed-run calls are what own every actual state transition; the heartbeat
loop here is purely a lease-keepalive/detach signal, never a decision-maker.

On a graceful stop (request_stop(), e.g. from SIGTERM/SIGINT), the worker stops claiming new
work, waits up to shutdown_grace_seconds for live tasks to finish on their own, and only for
those still running afterward: both a chat reply's and a research run's still-held claim is
voluntarily, atomically requeued (db.research_chat_requests.requeue_chat_request /
db.research_runs.requeue_research_run) so it is immediately available again rather than waiting
out the rest of its lease, and its asyncio task is then cancelled/detached. Shutdown is an
operational event, never an Owner action: it never sets cancel_requested or calls
db.research_runs.request_cancel_research_run (that flag/function remain reserved for an actual
Owner-initiated cancellation via the web layer), so no product-visible CANCELLED status and no
stranded staged evidence is ever caused solely by a SIGTERM. The requeued run's next claim
(by this worker or another) resumes the same attempt/budget through the existing snapshot-
resumption path in orchestrator.py -- the stale detached task can persist nothing to race it,
exactly like any other lost claim above.

=== Testability ===
Every external effect is an injectable parameter of ResearchWorker: `connection_factory` (DB
connections), `clock`/`sleep` (time), `research_task_runner`/`chat_processor` (the actual claimed-
task execution), and request_stop()'s internal asyncio.Event (the "stop event"). The public
surface stays small and deep: ResearchWorkerConfig/load_worker_config, ResearchWorker itself
(poll_once/run/wait_idle/close plus request_stop and two read-only pool-size properties), and the
separate reconcile_once/ReconcileResult pair for the --reconcile-once timer entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from beehive.ai.llm_client import tool_free_client
from beehive.ai.model_selection import DEFAULT_MODEL, load_model
from beehive.db.connection import connect
from beehive.db.research_chat_requests import (ChatRequest, claim_chat_request,
                                                fail_chat_request, heartbeat_chat_request,
                                                list_pending_chat_requests,
                                                recover_expired_chat_requests,
                                                requeue_chat_request)
from beehive.db.research_runs import (ResearchRunLease, claim_research_run,
                                       complete_research_run_if_claimed, heartbeat_research_run,
                                       list_pending_research_runs,
                                       recover_expired_research_runs, requeue_research_run)
from beehive.db.research_sessions import get_research_session
from beehive.deep_read.fetch import ArticleFetcher
from beehive.domain.research import ConversationMessage, ResearchRunStatus
from beehive.localization import Localizer, load_localizer, localizer_for
from beehive.research.conversation import (ConversationClaimLostError, ConversationError,
                                            process_claimed_chat_request)
from beehive.research.limits import MAX_ERROR_DETAIL_LENGTH, MAX_RUN_DURATION
from beehive.research.orchestrator import SealedEvidenceOutcome, run_research_orchestration
from beehive.research.synthesis_retry import run_synthesis_retry

_ERROR_DETAIL_CAP = MAX_ERROR_DETAIL_LENGTH
_ENV_PREFIX = "RESEARCH_WORKER_"
# Once a research run's own deadline_at has been reached, _heartbeat_loop's usual
# min(heartbeat_interval, seconds-until-deadline) sleep collapses to zero -- fine for a
# non-cancelled run, whose very next heartbeat_research_run call hard-terminates it and this
# loop returns immediately. For a CANCELLED run, heartbeat_research_run instead grants a
# cancellation-finalization grace (Task A) and keeps returning True while the background thread
# finishes local finalization, which would otherwise make this loop busy-spin heartbeat_research_
# run calls with no delay at all. This is the small, fixed floor used for every post-deadline
# iteration instead of zero -- short enough to notice finalization completing promptly, long
# enough to never hammer the DB connection in a tight loop.
_POST_DEADLINE_GRACE_POLL_SECONDS = 0.1

ConnectionFactory = Callable[[], sqlite3.Connection]
NowFactory = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
ResearchTaskRunner = Callable[..., SealedEvidenceOutcome]
ChatProcessor = Callable[..., Awaitable[object]]
Logger = Callable[[str], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _classify_error(exc: BaseException) -> tuple[str, str]:
    """Turns any unexpected exception into a typed, capped (error_code, error_detail) pair for
    the fenced DB fail functions -- never the Research Question, evidence, or a raw prompt: only
    the exception's own type name (a stable, generic code) and its own message, capped, matching
    collector/deep_read_worker.py's existing `f"{type(exc).__name__}: {exc}"` convention for
    infra-level failures."""
    return type(exc).__name__, str(exc)[:_ERROR_DETAIL_CAP]


# ============================================================================
# Configuration -- product ceilings (MAX_RUN_DURATION, the DB-enforced 3-processing-run cap in
# db/research_runs.py) stay in code/DB and are never overridable here; only the worker's own
# operational knobs are.
# ============================================================================

@dataclass(frozen=True)
class ResearchWorkerConfig:
    db_path: str
    research_pool_size: int = 3
    chat_pool_size: int = 3
    poll_interval_seconds: float = 5.0
    lease_seconds: float = 90.0
    heartbeat_interval_seconds: float = 30.0
    reconcile_interval_seconds: float = 60.0
    shutdown_grace_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.db_path:
            raise ValueError("db_path must be non-empty")
        for name in ("research_pool_size", "chat_pool_size"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        for name in ("poll_interval_seconds", "lease_seconds", "heartbeat_interval_seconds",
                     "reconcile_interval_seconds", "shutdown_grace_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        if self.heartbeat_interval_seconds >= self.lease_seconds:
            raise ValueError(
                "heartbeat_interval_seconds must be smaller than lease_seconds so a heartbeat "
                "can renew the lease before it expires (got heartbeat_interval_seconds="
                f"{self.heartbeat_interval_seconds!r}, lease_seconds={self.lease_seconds!r})")


def _read_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _read_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def load_worker_config(env: Mapping[str, str], db_path: str) -> ResearchWorkerConfig:
    """Builds a validated ResearchWorkerConfig from `RESEARCH_WORKER_*` environment overrides
    (research/chat pool size, poll interval, lease/heartbeat/reconcile intervals, shutdown grace)
    layered onto ResearchWorkerConfig's own defaults. Raises ValueError -- for a malformed
    numeric override here, or for any value ResearchWorkerConfig.__post_init__ itself rejects --
    which scripts/run_research_worker.py treats as a fatal, nonzero-exit startup error."""
    defaults = ResearchWorkerConfig(db_path=db_path)
    return ResearchWorkerConfig(
        db_path=db_path,
        research_pool_size=_read_int(
            env, _ENV_PREFIX + "RESEARCH_POOL_SIZE", defaults.research_pool_size),
        chat_pool_size=_read_int(
            env, _ENV_PREFIX + "CHAT_POOL_SIZE", defaults.chat_pool_size),
        poll_interval_seconds=_read_float(
            env, _ENV_PREFIX + "POLL_INTERVAL_SECONDS", defaults.poll_interval_seconds),
        lease_seconds=_read_float(
            env, _ENV_PREFIX + "LEASE_SECONDS", defaults.lease_seconds),
        heartbeat_interval_seconds=_read_float(
            env, _ENV_PREFIX + "HEARTBEAT_INTERVAL_SECONDS",
            defaults.heartbeat_interval_seconds),
        reconcile_interval_seconds=_read_float(
            env, _ENV_PREFIX + "RECONCILE_INTERVAL_SECONDS", defaults.reconcile_interval_seconds),
        shutdown_grace_seconds=_read_float(
            env, _ENV_PREFIX + "SHUTDOWN_GRACE_SECONDS", defaults.shutdown_grace_seconds))


# ============================================================================
# The narrow executor seam: bridges one blocking callable to a daemon thread + this coroutine's
# event loop, without ever touching concurrent.futures.ThreadPoolExecutor (its worker threads are
# joined by its own atexit hook, which would make a graceful shutdown hang on a still-running
# research task instead of returning promptly once its grace period has elapsed).
# ============================================================================

def _settle_result(fut: asyncio.Future, result: object) -> None:
    if not fut.done():
        fut.set_result(result)


def _settle_exception(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)


async def _run_in_thread(func: Callable[[], object]) -> object:
    """Runs `func` (a synchronous, blocking callable) on a brand-new daemon thread, and awaits
    its result on the CALLING coroutine's own event loop. `daemon=True` is what lets a research
    task straggling past a graceful shutdown's grace period never itself keep the process alive;
    it is simply left to finish (or be stopped by an eventual SIGKILL) in the background."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _runner() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 -- forwarded to the awaiting coroutine
            settle = functools.partial(_settle_exception, fut, exc)
        else:
            settle = functools.partial(_settle_result, fut, result)
        try:
            loop.call_soon_threadsafe(settle)
        except RuntimeError:
            pass  # the event loop is already closed (process shutting down); nothing to notify

    threading.Thread(target=_runner, daemon=True, name="research-run-worker").start()
    return await fut


def _default_research_task_runner(
    *, connection_factory: ConnectionFactory, run_id: int, claim_token: str, session_id: int,
    question: str, language_code: str, model: str, now_fn: NowFactory,
) -> SealedEvidenceOutcome:
    """The default, real research task body -- always run on its own thread via _run_in_thread,
    never called directly on the worker's main event loop. Calls `connection_factory` (the same
    one every other connection this worker opens comes from -- a plain, stateless callable that
    is just as safe to call from this background thread as from the main loop) to get its OWN
    sqlite3 connection, never shared with any other task, heartbeat, or the coordinator; opens
    its own ArticleFetcher and one `tool_free_client()`, reused across every tool-free AI call
    this run makes (plan generation, sufficiency assessment, synthesis) instead of spawning a
    fresh Copilot SDK process per call; runs the existing, unmodified research.orchestrator.
    run_research_orchestration to completion through a fresh asyncio event loop; and always
    closes all three before returning."""
    conn = connection_factory()
    fetcher = ArticleFetcher()
    try:
        async def _run() -> SealedEvidenceOutcome:
            async with tool_free_client() as ai_client:
                row = conn.execute(
                    "SELECT run_kind FROM research_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if row is not None and row["run_kind"] == "synthesis":
                    return await run_synthesis_retry(
                        conn,
                        run_id,
                        claim_token,
                        session_id,
                        question,
                        localizer_for(language_code),
                        model=model,
                        now_fn=now_fn,
                        client=ai_client,
                    )
                return await run_research_orchestration(
                    conn, run_id, claim_token, session_id, question, localizer_for(language_code),
                    now_fn=now_fn, fetcher=fetcher, planner_model=model, sufficiency_model=model,
                    client=ai_client)

        return asyncio.run(_run())
    finally:
        fetcher.close()
        conn.close()



# ============================================================================
# Live task bookkeeping
# ============================================================================

@dataclass
class _LiveResearchTask:
    run_id: int
    claim_token: str
    deadline_at: datetime
    task: asyncio.Task | None = None
    graceful_stop: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _LiveChatTask:
    request_id: int
    claim_token: str
    task: asyncio.Task | None = None
    graceful_stop: asyncio.Event = field(default_factory=asyncio.Event)


# ============================================================================
# Reconciliation: idempotent, recovery-only, no claiming or execution -- shared by the worker's
# own startup/periodic sweep and the standalone --reconcile-once entrypoint.
# ============================================================================

@dataclass(frozen=True)
class ReconcileResult:
    recovered_research_runs: int
    recovered_chat_requests: int


def reconcile_once(
    config: ResearchWorkerConfig, *, connection_factory: ConnectionFactory | None = None,
    clock: NowFactory = _utc_now,
) -> ReconcileResult:
    """One idempotent sweep that recovers only leases whose lease_expires_at has already
    passed -- it never claims or executes any Research Run or chat request, and recovering an
    already-recovered lease is always a no-op, so this is safe to run repeatedly (e.g. from a
    periodic timer unit) alongside the always-on ResearchWorker process."""
    conn = (connection_factory or functools.partial(connect, config.db_path))()
    try:
        now = clock()
        # now_fn=clock is the authoritative clock recover_expired_research_runs samples only
        # AFTER its own BEGIN IMMEDIATE has actually acquired the write lock -- never the
        # possibly-stale `now` above. See recover_expired_research_runs' own docstring.
        run_result = recover_expired_research_runs(conn, now, now_fn=clock)
        chat_recovered = recover_expired_chat_requests(conn, now)
    finally:
        conn.close()
    return ReconcileResult(
        recovered_research_runs=(
            run_result.requeued_count + run_result.deadline_exceeded_count),
        recovered_chat_requests=chat_recovered)


async def _default_chat_processor(
    conn: sqlite3.Connection, request: ChatRequest, localizer: Localizer, now: datetime,
    model: str = DEFAULT_MODEL, timeout: float = 120.0,
) -> ConversationMessage:
    """The default, real chat-reply body: opens one `tool_free_client()` for this chat turn's
    two sequential tool-free AI calls (draft reply, then memory update) so they share one
    Copilot SDK process instead of spawning a fresh one per call, then delegates to the
    existing, unmodified research.conversation.process_claimed_chat_request."""
    async with tool_free_client() as ai_client:
        return await process_claimed_chat_request(
            conn, request, localizer, now, model=model, timeout=timeout, client=ai_client)


# ============================================================================
# The worker
# ============================================================================

class ResearchWorker:
    def __init__(
        self, config: ResearchWorkerConfig, *,
        connection_factory: ConnectionFactory | None = None,
        clock: NowFactory = _utc_now,
        sleep: Sleep = asyncio.sleep,
        research_task_runner: ResearchTaskRunner = _default_research_task_runner,
        chat_processor: ChatProcessor = _default_chat_processor,
        log: Logger = print,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory or functools.partial(connect, config.db_path)
        self._clock = clock
        self._sleep = sleep
        self._research_task_runner = research_task_runner
        self._chat_processor = chat_processor
        self._log = log
        self._research_tasks: dict[int, _LiveResearchTask] = {}
        self._chat_tasks: dict[int, _LiveChatTask] = {}
        self._stopping = asyncio.Event()
        self._coordinator_conn: sqlite3.Connection | None = None
        self._last_reconcile: datetime | None = None

    # -- observability / control -----------------------------------------------------------

    @property
    def active_research_count(self) -> int:
        return len(self._research_tasks)

    @property
    def active_chat_count(self) -> int:
        return len(self._chat_tasks)

    def request_stop(self) -> None:
        """Cooperative stop signal (e.g. a SIGTERM/SIGINT handler calls this) -- run() stops
        claiming new work and begins its graceful shutdown sequence."""
        self._stopping.set()

    def close(self) -> None:
        """Idempotent: closes the coordinator connection if one is open. run() always calls
        this on its way out; tests that call poll_once()/reconcile directly without run() should
        call it too once done."""
        if self._coordinator_conn is not None:
            self._coordinator_conn.close()
            self._coordinator_conn = None

    async def wait_idle(self) -> None:
        """Awaits every currently-tracked live task to finish. A testing/observability seam --
        run()'s own loop never blocks on in-flight work this way."""
        tasks = [item.task for item in
                 (*self._research_tasks.values(), *self._chat_tasks.values())
                 if item.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- main loop ---------------------------------------------------------------------------

    def _ensure_coordinator_conn(self) -> sqlite3.Connection:
        if self._coordinator_conn is None:
            self._coordinator_conn = self._connection_factory()
        return self._coordinator_conn

    async def poll_once(self) -> None:
        """One iteration: reconcile expired leases if due (always true the first time -- this
        is what gives run() its startup reconciliation for free), then top up both pools up to
        their configured capacity. Never sleeps or waits for in-flight work."""
        conn = self._ensure_coordinator_conn()
        now = self._clock()
        if (self._last_reconcile is None or
                (now - self._last_reconcile).total_seconds()
                >= self._config.reconcile_interval_seconds):
            recover_expired_research_runs(conn, now, now_fn=self._clock)
            recover_expired_chat_requests(conn, now)
            self._last_reconcile = now
        self._fill_research_pool(now)
        self._fill_chat_pool(now)

    async def run(self) -> None:
        """Runs until request_stop() is called (or a fatal error propagates), then performs a
        bounded graceful shutdown before returning. Always closes the coordinator connection."""
        self._ensure_coordinator_conn()
        try:
            while not self._stopping.is_set():
                await self.poll_once()
                if self._stopping.is_set():
                    break
                await self._sleep(self._config.poll_interval_seconds)
            await self._shutdown()
        finally:
            self.close()

    # -- pool filling --------------------------------------------------------------------------

    def _fill_research_pool(self, now: datetime) -> None:
        capacity = self._config.research_pool_size - len(self._research_tasks)
        if capacity <= 0:
            return
        conn = self._ensure_coordinator_conn()
        for pending in list_pending_research_runs(conn, limit=capacity):
            if len(self._research_tasks) >= self._config.research_pool_size:
                break
            # now_fn=self._clock is the authoritative clock claim_research_run samples only
            # AFTER its own BEGIN IMMEDIATE has actually acquired the write lock -- never the
            # possibly-stale `now` above, sampled once per poll_once() iteration before this
            # loop (and before this call could even attempt to acquire that lock, let alone
            # block on it behind another writer). See claim_research_run's own docstring.
            lease = claim_research_run(
                conn, pending.id, now, lease_seconds=self._config.lease_seconds,
                deadline_seconds=MAX_RUN_DURATION.total_seconds(), now_fn=self._clock)
            if lease is None:
                continue
            session = get_research_session(conn, lease.run.session_id)
            if session is None:
                # now_fn=self._clock for the same reason as claim_research_run above. A missing
                # Research Session this deep into claiming is a true data-integrity failure --
                # not a race a cancellation should ever be able to hide -- so this passes
                # `honor_cancel=False` explicitly: complete_research_run_if_claimed commits this
                # FAILED request exactly as requested (its own specific error_code) even if
                # cancel_requested happens to be set, never silently superseded by CANCELLED.
                complete_research_run_if_claimed(
                    conn, lease.run.id, lease.run.claim_token, ResearchRunStatus.FAILED, now,
                    now_fn=self._clock, error_code="ResearchSessionMissing",
                    error_detail="Research Session no longer exists", honor_cancel=False)
                self._log(
                    f"[research-worker] research run {lease.run.id} failed: session missing")
                continue
            localizer = load_localizer(conn)
            model = load_model(conn)
            self._spawn_research_task(lease, session.question, localizer.code, model)

    def _fill_chat_pool(self, now: datetime) -> None:
        capacity = self._config.chat_pool_size - len(self._chat_tasks)
        if capacity <= 0:
            return
        conn = self._ensure_coordinator_conn()
        for pending in list_pending_chat_requests(conn, limit=capacity):
            if len(self._chat_tasks) >= self._config.chat_pool_size:
                break
            claimed = claim_chat_request(conn, pending.id, now, lease_seconds=self._config.lease_seconds)
            if claimed is None:
                continue
            localizer = load_localizer(conn)
            model = load_model(conn)
            self._spawn_chat_task(claimed, localizer.code, model)

    def _spawn_research_task(
        self, lease: ResearchRunLease, question: str, language_code: str, model: str,
    ) -> None:
        live = _LiveResearchTask(
            run_id=lease.run.id, claim_token=lease.run.claim_token,
            deadline_at=lease.run.deadline_at)
        live.task = asyncio.create_task(
            self._supervise_research(live, lease.run.session_id, question, language_code, model))
        self._research_tasks[lease.run.id] = live

    def _spawn_chat_task(self, request: ChatRequest, language_code: str, model: str) -> None:
        live = _LiveChatTask(request_id=request.id, claim_token=request.claim_token)
        live.task = asyncio.create_task(self._supervise_chat(live, request, language_code, model))
        self._chat_tasks[request.id] = live

    # -- heartbeat -----------------------------------------------------------------------------

    async def _heartbeat_loop(
        self, *, kind: str, entity_id: int, claim_token: str, conn: sqlite3.Connection,
        on_lost: Callable[[], None], deadline_at: datetime | None = None,
    ) -> None:
        """Independent per-task coroutine on its own connection: extends the claim's lease every
        heartbeat_interval_seconds regardless of whether the task's own work is currently
        blocked. Stops as soon as its task is done (the supervisor cancels it in its `finally`)
        or the moment a renewal is refused, in which case `on_lost` detaches the worker from
        waiting on that task any further -- whether the refusal is an ordinary claim loss (lease
        already expired and recovered/reclaimed elsewhere) or, for a research run, its own fixed
        deadline_at being reached while NOT cancelled: heartbeat_research_run itself is what
        atomically fails the run at the deadline (error_code='deadline_exceeded'), so this loop
        only has to notice the refusal and detach, never decide the deadline itself.

        For a research run (deadline_at given), this loop sleeps for
        min(heartbeat_interval_seconds, seconds-until-deadline) instead of always the full
        heartbeat interval -- so it is woken (and calls heartbeat_research_run, hard-terminating
        a non-cancelled run) right at the deadline rather than up to a whole
        heartbeat_interval_seconds after it. A chat request has no such fixed deadline
        (deadline_at is None) and always sleeps the full heartbeat_interval_seconds.

        A CANCELLED research run reaching its own deadline is different: heartbeat_research_run
        grants a cancellation-finalization grace (Task A) instead of hard-terminating -- it keeps
        returning True, extending the lease PAST deadline_at, for as long as the background
        thread is still sealing already-collected evidence and has not yet reached its own
        terminal write. This loop never has to know which case it is in: once `deadline_at` has
        passed, it simply polls every `_POST_DEADLINE_GRACE_POLL_SECONDS` instead of computing a
        (now permanently zero/negative) `remaining`-based sleep -- short enough to notice this
        exact claim finally going terminal (heartbeat_research_run then returns False, exactly
        like any other lost claim, and this loop detaches) without busy-spinning
        heartbeat_research_run calls on this connection while a legitimate grace is in progress.

        For a research run, `now_fn=self._clock` is passed to heartbeat_research_run so its own
        deadline check samples the authoritative clock only AFTER its BEGIN IMMEDIATE has
        actually acquired the write lock -- never the possibly-stale `self._clock()` sampled
        just above, before this call could even attempt to acquire (or block behind another
        writer holding) that lock. See heartbeat_research_run's own docstring. Chat requests
        have no such seam (out of this module's scope) and keep using heartbeat_chat_request's
        plain pre-sampled-`now` signature."""
        try:
            while True:
                sleep_for = self._config.heartbeat_interval_seconds
                if deadline_at is not None:
                    remaining = (deadline_at - self._clock()).total_seconds()
                    sleep_for = (
                        min(sleep_for, remaining) if remaining > 0
                        else _POST_DEADLINE_GRACE_POLL_SECONDS)
                await self._sleep(sleep_for)
                if kind == "research":
                    ok = heartbeat_research_run(
                        conn, entity_id, claim_token, self._clock(), self._config.lease_seconds,
                        now_fn=self._clock)
                else:
                    ok = heartbeat_chat_request(
                        conn, entity_id, claim_token, self._clock(), self._config.lease_seconds)
                if not ok:
                    self._log(
                        f"[research-worker] {kind} {entity_id} lease lost; stopping heartbeat")
                    on_lost()
                    return
        except asyncio.CancelledError:
            return

    # -- research task supervision -------------------------------------------------------------

    async def _supervise_research(
        self, live: _LiveResearchTask, session_id: int, question: str, language_code: str,
        model: str,
    ) -> None:
        hb_conn = self._connection_factory()
        heartbeat = asyncio.create_task(self._heartbeat_loop(
            kind="research", entity_id=live.run_id, claim_token=live.claim_token, conn=hb_conn,
            on_lost=lambda: live.task.cancel(), deadline_at=live.deadline_at))
        try:
            outcome = await _run_in_thread(functools.partial(
                self._research_task_runner, connection_factory=self._connection_factory,
                run_id=live.run_id, claim_token=live.claim_token, session_id=session_id,
                question=question, language_code=language_code, model=model,
                now_fn=self._clock))
        except asyncio.CancelledError:
            if live.graceful_stop.is_set():
                self._log(
                    f"[research-worker] research run {live.run_id} requeued for shutdown; its "
                    "background thread is now stale and fenced from further writes")
            else:
                self._log(
                    f"[research-worker] research run {live.run_id} claim lost or hard-"
                    f"terminated at its deadline; detached from its still-running background "
                    "thread (writes remain claim-fenced)")
        except Exception as exc:  # noqa: BLE001 -- one run's failure must not crash the worker
            self._fail_research_run(live.run_id, live.claim_token, exc)
        else:
            self._log(
                f"[research-worker] research run {live.run_id} finished: {outcome.status.value} "
                f"(rounds={outcome.rounds_completed})")
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            hb_conn.close()
            self._research_tasks.pop(live.run_id, None)

    def _fail_research_run(self, run_id: int, claim_token: str, exc: BaseException) -> None:
        error_code, detail = _classify_error(exc)
        conn = self._connection_factory()
        try:
            complete_research_run_if_claimed(
                conn, run_id, claim_token, ResearchRunStatus.FAILED, self._clock(),
                now_fn=self._clock, error_code=error_code, error_detail=detail)
        finally:
            conn.close()
        self._log(f"[research-worker] research run {run_id} failed: {error_code}")

    # -- chat task supervision ------------------------------------------------------------------

    async def _supervise_chat(
        self, live: _LiveChatTask, request: ChatRequest, language_code: str, model: str,
    ) -> None:
        hb_conn = self._connection_factory()
        heartbeat = asyncio.create_task(self._heartbeat_loop(
            kind="chat", entity_id=live.request_id, claim_token=live.claim_token, conn=hb_conn,
            on_lost=lambda: live.task.cancel()))
        task_conn = self._connection_factory()
        try:
            localizer = localizer_for(language_code)
            reply = await self._chat_processor(
                task_conn, request, localizer, self._clock(), model=model)
        except asyncio.CancelledError:
            if live.graceful_stop.is_set():
                if requeue_chat_request(task_conn, live.request_id, live.claim_token):
                    self._log(
                        f"[research-worker] chat request {live.request_id} requeued for "
                        "shutdown")
            else:
                self._log(
                    f"[research-worker] chat request {live.request_id} claim lost; stopped")
        except ConversationClaimLostError:
            self._log(
                f"[research-worker] chat request {live.request_id} claim lost mid-reply; no "
                "reply written")
        except ConversationError as exc:
            self._fail_chat_request(task_conn, live.request_id, live.claim_token, exc)
        except Exception as exc:  # noqa: BLE001 -- one reply's failure must not crash the worker
            self._fail_chat_request(task_conn, live.request_id, live.claim_token, exc)
        else:
            self._log(
                f"[research-worker] chat request {live.request_id} completed reply "
                f"{reply.id}")
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            hb_conn.close()
            task_conn.close()
            self._chat_tasks.pop(live.request_id, None)

    def _fail_chat_request(
        self, conn: sqlite3.Connection, request_id: int, claim_token: str, exc: BaseException,
    ) -> None:
        error_code, detail = _classify_error(exc)
        fail_chat_request(conn, request_id, claim_token, error_code, detail, self._clock())
        self._log(f"[research-worker] chat request {request_id} failed: {error_code}")

    # -- graceful shutdown ----------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Operational shutdown, never an Owner cancellation: a still-live claim past its grace
        period is atomically requeued (never marked cancel_requested, never routed through
        db.research_runs.request_cancel_research_run -- that stays reserved for a real Owner
        action via the web layer) so it is immediately available for the next claim, and only
        then is its local asyncio task cancelled/detached. No claim is ever left 'processing',
        no staged evidence is stranded, and no product-visible CANCELLED status is ever caused
        solely by a SIGTERM."""
        self._log("[research-worker] shutdown requested; no longer claiming new work")
        live = (*self._research_tasks.values(), *self._chat_tasks.values())
        tasks = [item.task for item in live if item.task is not None]
        pending: set[asyncio.Task] = set()
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=self._config.shutdown_grace_seconds)
        if pending:
            self._log(
                f"[research-worker] {len(pending)} task(s) still live after "
                f"{self._config.shutdown_grace_seconds}s grace period; requeuing and stopping "
                "them")
            conn = self._ensure_coordinator_conn()
            for item in self._research_tasks.values():
                if item.task is not None and not item.task.done():
                    item.graceful_stop.set()
                    requeue_research_run(conn, item.run_id, item.claim_token)
                    item.task.cancel()
            for item in self._chat_tasks.values():
                if item.task is not None and not item.task.done():
                    item.graceful_stop.set()
                    item.task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._log("[research-worker] shutdown complete")


__all__ = [
    "ResearchWorkerConfig",
    "load_worker_config",
    "ReconcileResult",
    "reconcile_once",
    "ResearchWorker",
]
