"""The jobs worker (ADR-0012): one always-on process that runs every background job except
research, which keeps its own worker (ADR-0009). It replaced the fetch, digest, reminder and
deep-read timers, the "Fetch now" and deep-read marker files, and their oneshot containers.

=== Lanes ===
Five lanes, each running at most one job at a time on its own thread (collector/threads.py), each
job with its own SQLite connection:
  * manual_fetch: "Fetch now" requests (db/fetch_requests.py), oldest first. The worker holds a
    lease on the request and renews it while the fetch runs.
  * scheduled_fetch: a sweep through every Channel at start-up and on every quarter hour, one
    Channel per job, each running run_channel_cycle without force, so every Source's own schedule
    decides whether it is due. A tick that arrives mid-sweep is kept: the next sweep starts as
    soon as this one ends.
  * deep_read: the oldest pending deep read (db/deep_reads.py), claimed here and processed on the
    thread; its lease is renewed like a fetch request's.
  * digest: research completion emails and due Email group digests, every 15 minutes.
  * reminders: due Tracker reminders, every 5 minutes.
Interval lanes run at start-up too, which catches up after downtime: what is due is database
state, not the clock. The two fetch lanes never work on the same Channel at once.

=== One process at a time ===
The worker only runs its lanes while it holds the jobs lock (collector/jobs_lock.py), which it
keeps until its process ends; a second worker waits, and one-shot job commands refuse to run.
With one process, and the two fetch lanes never sharing a Channel, no Channel is ever fetched
twice at the same time, which is what keeps collection writes consistent: they carry no claim
token of their own. The worker renews its own leases before it recovers expired ones, so it never
takes work back from itself after a stall.

=== Failures and limits ===
A job's own failure is logged and its lane moves on. A job that runs past its lane's hard limit
(the timeouts the old oneshot containers had) cannot be stopped from outside its thread, so the
worker hands back its claims and exits with EXIT_STUCK, and systemd restarts it. The stuck job's
own claim counts as a failed try: a "Fetch now" request is dropped after three, and a deep read is
marked failed so the Owner can retry it, so one hopeless job cannot restart the worker forever.
Expired leases from a worker that died are recovered at start-up and every minute.

=== Shutdown ===
request_stop() stops new work. run() then waits up to shutdown_grace_seconds and hands back any
claim still held (a "Fetch now" request, a deep read, the reminders being sent), so the next
worker takes it at once instead of waiting out its lease. The entry point exits the process right
after, without waiting for a job thread that is still unwinding.

=== Status ===
The worker saves its status (collector/jobs_status.py) whenever a lane changes and at least every
status_interval_seconds, and the admin's System chapter shows it.
"""
from __future__ import annotations

import asyncio
import functools
import os
import sqlite3
import traceback
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from beehive.collector.deep_read_worker import LEASE_SECONDS as DEEP_READ_LEASE_SECONDS
from beehive.collector.deep_read_worker import claim_next_deep_read
from beehive.collector.jobs import run_deep_read_job, run_digest_job, run_fetch_job, run_reminder_job
from beehive.collector.jobs_lock import JobsLock
from beehive.collector.jobs_status import LaneStatus, save_status
from beehive.collector.threads import run_in_thread
from beehive.db.channels import list_channels
from beehive.db.connection import connect
from beehive.db.deep_reads import (
    DeepRead,
    fail_deep_read,
    heartbeat_deep_read,
    recover_expired_deep_reads,
    requeue_deep_read,
)
from beehive.db.fetch_requests import (
    FetchClaim,
    claim_next_fetch_request,
    finish_fetch_request,
    heartbeat_fetch_request,
    recover_expired_fetch_requests,
    requeue_fetch_request,
)
from beehive.db.tracker_watches import release_tracker_reminder_claim

EXIT_STUCK = 75

ConnectionFactory = Callable[[], sqlite3.Connection]
ThreadRunner = Callable[..., Awaitable[object]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class JobsWorkerConfig:
    db_path: str
    poll_interval_seconds: float = 5.0
    sweep_minutes: int = 15
    digest_minutes: int = 15
    reminder_minutes: int = 5
    recover_interval_seconds: float = 60.0
    lease_heartbeat_seconds: float = 60.0
    fetch_lease_seconds: float = 180.0
    status_interval_seconds: float = 30.0
    shutdown_grace_seconds: float = 30.0
    fetch_limit_seconds: float = 3600.0
    deep_read_limit_seconds: float = 1800.0
    digest_limit_seconds: float = 1800.0
    reminder_limit_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not self.db_path:
            raise ValueError("db_path must be non-empty")
        if self.lease_heartbeat_seconds >= self.fetch_lease_seconds:
            raise ValueError("lease_heartbeat_seconds must be shorter than fetch_lease_seconds")


class _Lane:
    def __init__(self, name: str, limit_seconds: float) -> None:
        self.name = name
        self.limit_seconds = limit_seconds
        self.task: asyncio.Task | None = None
        self.started_at: datetime | None = None
        self.renewed_at: datetime | None = None
        self.doing: str | None = None
        self.channel_id: int | None = None
        self.claim: FetchClaim | DeepRead | None = None
        # Set from the job's thread once the reminders it is sending are claimed.
        self.reminder_token: str | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def reset(self) -> None:
        self.task = None
        self.started_at = self.renewed_at = None
        self.doing = None
        self.channel_id = None
        self.claim = None
        self.reminder_token = None

    def status(self) -> LaneStatus:
        return LaneStatus(
            name=self.name,
            busy_since=self.started_at if self.busy else None,
            doing=self.doing if self.busy else None,
            last_finished_at=self.last_finished_at,
            last_error=self.last_error,
        )


def _slot(now: datetime, minutes: int) -> int:
    return int(now.timestamp() // (minutes * 60))


class JobsWorker:
    def __init__(
        self,
        config: JobsWorkerConfig,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        thread_runner: ThreadRunner = run_in_thread,
        fetch_runner: Callable[..., None] = run_fetch_job,
        deep_read_runner: Callable[..., object] = run_deep_read_job,
        digest_runner: Callable[[str], None] = run_digest_job,
        reminder_runner: Callable[..., None] = run_reminder_job,
        exit_process: Callable[[int], None] = os._exit,
        log: Callable[[str], None] = print,
        lock: JobsLock | None = None,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory or functools.partial(
            connect, config.db_path)
        self._clock = clock
        self._sleep = sleep
        self._thread_runner = thread_runner
        self._fetch_runner = fetch_runner
        self._deep_read_runner = deep_read_runner
        self._digest_runner = digest_runner
        self._reminder_runner = reminder_runner
        self._exit_process = exit_process
        self._log = log
        self._lanes = {
            "manual_fetch": _Lane("manual_fetch", config.fetch_limit_seconds),
            "scheduled_fetch": _Lane("scheduled_fetch", config.fetch_limit_seconds),
            "deep_read": _Lane("deep_read", config.deep_read_limit_seconds),
            "digest": _Lane("digest", config.digest_limit_seconds),
            "reminders": _Lane("reminders", config.reminder_limit_seconds),
        }
        self._sweep: deque[int] = deque()
        self._sweep_again = False
        self._last_sweep_started_at: datetime | None = None
        self._last_slots: dict[str, int | None] = {
            "scheduled_fetch": None, "digest": None, "reminders": None}
        self._last_recover: datetime | None = None
        self._last_status_write: datetime | None = None
        self._status_dirty = True
        self._stopping = asyncio.Event()
        self._conn: sqlite3.Connection | None = None
        self._lock = lock or JobsLock(config.db_path)
        self._told_waiting = False

    # -- control -----------------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stopping.set()

    def busy_lanes(self) -> tuple[str, ...]:
        return tuple(name for name, lane in self._lanes.items() if lane.busy)

    async def wait_idle(self) -> None:
        """Awaits every running job. For tests; run() never blocks on in-flight work this way."""
        tasks = [lane.task for lane in self._lanes.values() if lane.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        """Closes the database connection, and frees the jobs lock unless a job thread is still
        running: such a thread keeps it until the process exits and takes the thread with it."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if not self.busy_lanes():
            self._lock.release()

    def _coordinator(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connection_factory()
        return self._conn

    # -- main loop ---------------------------------------------------------------------------

    async def run(self) -> None:
        try:
            while not self._stopping.is_set():
                await self.poll_once()
                if self._stopping.is_set():
                    break
                await self._sleep(self._config.poll_interval_seconds)
            await self._shutdown()
        finally:
            self.close()

    async def poll_once(self) -> None:
        """One pass: hold the jobs lock, enforce limits, renew held leases, recover expired
        claims if due, start any idle lane that has work, and save the status. Never waits for a
        job."""
        if not self._hold_lock():
            return
        conn = self._coordinator()
        now = self._clock()
        if self._enforce_limits(conn, now):
            return
        # Renew before recovering, so a worker back from a stall never recovers its own claims.
        self._renew_leases(conn, now)
        if (self._last_recover is None
                or (now - self._last_recover).total_seconds()
                >= self._config.recover_interval_seconds):
            self._recover(conn, now)
        if not self._stopping.is_set():
            self._start_manual_fetch(conn, now)
            self._start_scheduled_fetch(conn, now)
            self._start_deep_read(conn, now)
            self._start_interval_job(
                "digest", self._config.digest_minutes, self._digest_runner, now)
            self._start_reminders(now)
        self._save_status(conn, now)

    def _hold_lock(self) -> bool:
        """True while this worker holds the jobs lock; False while another process does."""
        if self._lock.held:
            return True
        if self._lock.try_acquire():
            if self._told_waiting:
                self._log("[jobs] the other process running jobs has stopped; this worker takes over")
                self._told_waiting = False
            return True
        if not self._told_waiting:
            self._log(f"[jobs] another process holds {self._lock.path}; waiting for it to stop")
            self._told_waiting = True
        return False

    # -- lanes -------------------------------------------------------------------------------

    def _start_manual_fetch(self, conn: sqlite3.Connection, now: datetime) -> None:
        lane = self._lanes["manual_fetch"]
        if lane.busy:
            return
        scheduled = self._lanes["scheduled_fetch"]
        claim = claim_next_fetch_request(
            conn,
            now,
            lease_seconds=self._config.fetch_lease_seconds,
            exclude_channel_ids=(scheduled.channel_id,) if scheduled.busy else (),
        )
        if claim is None:
            return
        self._spawn(
            lane,
            now,
            functools.partial(self._fetch_runner, self._config.db_path, claim.channel_id,
                              force=True),
            doing=f"Channel {claim.channel_id} (Fetch now, try {claim.attempt})",
            channel_id=claim.channel_id,
            claim=claim,
            on_done=lambda: finish_fetch_request(self._coordinator(), claim, self._clock()),
        )

    def _start_scheduled_fetch(self, conn: sqlite3.Connection, now: datetime) -> None:
        lane = self._lanes["scheduled_fetch"]
        slot = _slot(now, self._config.sweep_minutes)
        if slot != self._last_slots["scheduled_fetch"]:
            self._last_slots["scheduled_fetch"] = slot
            self._sweep_again = True
        if lane.busy:
            return
        if not self._sweep and self._sweep_again:
            self._sweep_again = False
            self._sweep.extend(channel["id"] for channel in list_channels(conn))
            self._last_sweep_started_at = now
            self._status_dirty = True
        manual = self._lanes["manual_fetch"]
        while self._sweep:
            channel_id = self._sweep.popleft()
            if manual.busy and manual.channel_id == channel_id:
                continue  # "Fetch now" is fetching it this minute
            self._spawn(
                lane,
                now,
                functools.partial(self._fetch_runner, self._config.db_path, channel_id,
                                  force=False),
                doing=f"Channel {channel_id}",
                channel_id=channel_id,
            )
            return

    def _start_deep_read(self, conn: sqlite3.Connection, now: datetime) -> None:
        lane = self._lanes["deep_read"]
        if lane.busy:
            return
        claimed = claim_next_deep_read(conn, now)
        if claimed is None:
            return
        self._spawn(
            lane,
            now,
            functools.partial(self._deep_read_runner, self._config.db_path, claimed),
            doing=f"item {claimed.item_id}",
            claim=claimed,
        )

    def _start_reminders(self, now: datetime) -> None:
        lane = self._lanes["reminders"]

        def remember(token: str) -> None:
            lane.reminder_token = token

        self._start_interval_job(
            "reminders",
            self._config.reminder_minutes,
            functools.partial(self._reminder_runner, on_claim=remember),
            now,
        )

    def _start_interval_job(
        self, name: str, minutes: int, runner: Callable[[str], None], now: datetime
    ) -> None:
        lane = self._lanes[name]
        slot = _slot(now, minutes)
        # A busy lane leaves the slot unmarked, so the job runs again as soon as it is free.
        if lane.busy or slot == self._last_slots[name]:
            return
        self._last_slots[name] = slot
        self._spawn(lane, now, functools.partial(runner, self._config.db_path))

    def _spawn(
        self,
        lane: _Lane,
        now: datetime,
        func: Callable[[], object],
        *,
        doing: str | None = None,
        channel_id: int | None = None,
        claim: FetchClaim | DeepRead | None = None,
        on_done: Callable[[], object] | None = None,
    ) -> None:
        lane.started_at = lane.renewed_at = now
        lane.doing = doing
        lane.channel_id = channel_id
        lane.claim = claim
        lane.task = asyncio.create_task(self._run_job(lane, func, on_done))
        self._status_dirty = True

    async def _run_job(
        self, lane: _Lane, func: Callable[[], object], on_done: Callable[[], object] | None
    ) -> None:
        error = None
        try:
            await self._thread_runner(func, name=f"jobs-{lane.name}")
        except asyncio.CancelledError:
            lane.reset()
            raise
        except Exception as exc:  # noqa: BLE001 -- one job's failure must not stop the lane
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"[jobs] {lane.name} job failed ({lane.doing or 'no target'}): {error}")
            traceback.print_exception(exc)
        if on_done is not None:
            try:
                on_done()
            except Exception as exc:  # noqa: BLE001 -- the next recovery pass cleans up after it
                self._log(f"[jobs] could not close out the {lane.name} job: {exc}")
        lane.last_finished_at = self._clock()
        lane.last_error = error
        lane.reset()
        self._status_dirty = True

    # -- leases, recovery and limits ---------------------------------------------------------

    def _renew_leases(self, conn: sqlite3.Connection, now: datetime) -> None:
        for lane in (self._lanes["manual_fetch"], self._lanes["deep_read"]):
            if not lane.busy or lane.claim is None or lane.renewed_at is None:
                continue
            if (now - lane.renewed_at).total_seconds() < self._config.lease_heartbeat_seconds:
                continue
            claim = lane.claim
            if isinstance(claim, FetchClaim):
                kept = heartbeat_fetch_request(
                    conn, claim, now, lease_seconds=self._config.fetch_lease_seconds)
            else:
                kept = heartbeat_deep_read(
                    conn, claim.item_id, claim.request_version, claim.claim_token, now,
                    lease_seconds=DEEP_READ_LEASE_SECONDS)
            lane.renewed_at = now
            if not kept:
                # Only the Owner can take it away now (clearing a stuck fetch, or regenerating
                # a deep read); a regenerated deep read's old result is not saved.
                self._log(f"[jobs] {lane.name} no longer holds its claim on {lane.doing}; "
                          "the job runs to its end")

    def _recover(self, conn: sqlite3.Connection, now: datetime) -> None:
        recovered = recover_expired_fetch_requests(conn, now)
        if recovered.requeued:
            self._log(f"[jobs] requeued Fetch now for Channels {list(recovered.requeued)} "
                      "after their worker stopped")
        if recovered.dropped:
            self._log(f"[jobs] gave up on Fetch now for Channels {list(recovered.dropped)} "
                      "after repeated interrupted tries; the next sweep fetches them")
        deep_reads = recover_expired_deep_reads(conn, now)
        if deep_reads:
            self._log(f"[jobs] requeued {deep_reads} deep read(s) after their worker stopped")
        self._last_recover = now

    def _release_claims(
        self, conn: sqlite3.Connection, now: datetime, *, stuck: _Lane | None = None
    ) -> None:
        """Hands back every claim a still-running job holds, so the next worker can take it at
        once. The claim of the `stuck` job, which ran past its limit, counts as a failed try."""
        manual = self._lanes["manual_fetch"]
        if manual.busy and isinstance(manual.claim, FetchClaim):
            requeue_fetch_request(conn, manual.claim, now, failed=manual is stuck)
        deep = self._lanes["deep_read"]
        if deep.busy and isinstance(deep.claim, DeepRead):
            claim = deep.claim
            if deep is stuck:
                fail_deep_read(
                    conn, claim.item_id, claim.request_version, claim.claim_token, "llm",
                    f"Stopped after running past the {deep.limit_seconds / 60:.0f}-minute limit",
                    now)
            else:
                requeue_deep_read(
                    conn, claim.item_id, claim.request_version, claim.claim_token)
        reminders = self._lanes["reminders"]
        if reminders.busy and reminders.reminder_token is not None:
            release_tracker_reminder_claim(conn, reminders.reminder_token)

    def _enforce_limits(self, conn: sqlite3.Connection, now: datetime) -> bool:
        for lane in self._lanes.values():
            if not lane.busy or lane.started_at is None:
                continue
            elapsed = (now - lane.started_at).total_seconds()
            if elapsed <= lane.limit_seconds:
                continue
            self._log(
                f"[jobs] {lane.name} job ({lane.doing or 'no target'}) has run for "
                f"{elapsed / 60:.0f} min, past its {lane.limit_seconds / 60:.0f} min limit; "
                "restarting the worker")
            try:
                self._release_claims(conn, now, stuck=lane)
                self._save_status(conn, now, force=True)
            finally:
                # The jobs lock goes with the process, so the stuck thread can never overlap
                # the worker systemd starts next.
                self._exit_process(EXIT_STUCK)
            return True
        return False

    async def _shutdown(self) -> None:
        running = [lane.task for lane in self._lanes.values() if lane.busy]
        if running:
            await asyncio.wait(running, timeout=self._config.shutdown_grace_seconds)
        conn = self._coordinator()
        still_running = self.busy_lanes()
        if still_running:
            self._log(f"[jobs] stopping with {', '.join(still_running)} still running; "
                      "handing their claims back")
        now = self._clock()
        if self._lock.held:
            self._release_claims(conn, now)
            self._save_status(conn, now, force=True)

    def _save_status(self, conn: sqlite3.Connection, now: datetime, *, force: bool = False) -> None:
        due = (self._last_status_write is None
               or (now - self._last_status_write).total_seconds()
               >= self._config.status_interval_seconds)
        if not (force or due or self._status_dirty):
            return
        try:
            save_status(
                conn,
                seen_at=now,
                last_sweep_started_at=self._last_sweep_started_at,
                lanes=tuple(lane.status() for lane in self._lanes.values()),
            )
        except sqlite3.Error as exc:
            self._log(f"[jobs] could not save the worker status: {exc}")
            return
        self._last_status_write = now
        self._status_dirty = False
