import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import pytest

from beehive.collector import research_worker as rw
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.research_chat_requests import (count_active_processing_chat_requests,
                                                get_chat_request, submit_chat_request)
from beehive.db.research_runs import (claim_research_run, count_active_processing_runs,
                                       enqueue_research_run, get_research_run,
                                       request_cancel_research_run)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.domain.research import (ClaimProvenance, EvidenceCitation, EvidenceQuality,
                                      ResearchRunStatus, ResearchSourceOrigin,
                                      SufficiencyState, SynthesisClaim, SynthesisSection)
from beehive.research.conversation import ConversationClaimLostError, ConversationError
from beehive.research.orchestrator import RunOutcomeStatus, SealedEvidenceOutcome

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# Fixtures / scenario builders
# ============================================================================

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "worker.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


def _config(db_path, **overrides):
    defaults = dict(
        research_pool_size=3, chat_pool_size=3, poll_interval_seconds=1.0, lease_seconds=90.0,
        heartbeat_interval_seconds=5.0, reconcile_interval_seconds=60.0,
        shutdown_grace_seconds=5.0)
    defaults.update(overrides)
    return rw.ResearchWorkerConfig(db_path=db_path, **defaults)


def _pending_run(conn, question="Why did RBNZ cut rates?", now=T0):
    session_id = create_research_session(conn, question, now).id
    run = enqueue_research_run(conn, session_id, now)
    return session_id, run.id


def _chat_scenario(conn, question="Why did RBNZ cut rates?", now=T0):
    """Builds a Research Session with sealed evidence, a Research Synthesis, and one pending
    chat request -- everything submit_chat_request/process_claimed_chat_request needs."""
    session_id = create_research_session(conn, question, now).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, now).id
    run_id = enqueue_research_run(conn, session_id, now).id
    claim_research_run(conn, run_id, now, lease_seconds=600, deadline_seconds=3600)
    snapshot_id = create_snapshot(conn, session_id, run_id, now).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e0", "Title", "https://x/0", EvidenceQuality.REPORTING,
        now, snippet="snippet")
    add_snapshot_items(conn, snapshot_id, [item.id], now)
    seal_snapshot(conn, snapshot_id, now)
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item.id], now)
    claim = SynthesisClaim(
        text="Bottom line claim", section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(EvidenceCitation(item.id, item.citation_number),))
    create_synthesis(
        conn, session_id, revision.id, SufficiencyState.PARTIAL, (claim,), "gpt-5", "en", now)
    _, chat_request = submit_chat_request(conn, session_id, "What happened?", now)
    return session_id, chat_request.id


def _sealed_outcome(run_id, status=RunOutcomeStatus.SUFFICIENT):
    return SealedEvidenceOutcome(
        status=status, run_id=run_id, snapshot_id=None, evidence_state_revision_id=None,
        sufficiency=None, rounds_completed=1, source_failures=())


@dataclass
class _FakeReply:
    id: int = 1


def _blocking_research_runner(event: threading.Event, *, timeout=5.0):
    """A research_task_runner fake that blocks a REAL OS thread (via _run_in_thread) until
    `event` is set -- used to prove heartbeats/chat keep progressing while research "connector
    I/O" blocks, without needing the real orchestrator or network access. Opens (and closes) its
    own connection via `connection_factory`, exactly like _default_research_task_runner does."""
    def runner(*, connection_factory, run_id, claim_token, session_id, question, language_code,
               model, now_fn):
        conn = connection_factory()
        try:
            event.wait(timeout=timeout)
            return _sealed_outcome(run_id)
        finally:
            conn.close()
    return runner


def _raising_research_runner(exc: BaseException):
    def runner(**_kwargs):
        raise exc
    return runner


async def _async_chat_ok(*_args, **_kwargs):
    return _FakeReply()


def _blocking_chat_processor(event: asyncio.Event):
    async def processor(conn, request, localizer, now, model=None, timeout=None):
        await event.wait()
        return _FakeReply()
    return processor


def _raising_chat_processor(exc: BaseException):
    async def processor(*_args, **_kwargs):
        raise exc
    return processor


# ============================================================================
# Configuration validation
# ============================================================================

@pytest.mark.parametrize("overrides", [
    {"db_path": ""},
    {"research_pool_size": 0},
    {"research_pool_size": -1},
    {"chat_pool_size": 0},
    {"poll_interval_seconds": 0},
    {"poll_interval_seconds": -1},
    {"lease_seconds": 0},
    {"heartbeat_interval_seconds": 0},
    {"reconcile_interval_seconds": 0},
    {"shutdown_grace_seconds": 0},
])
def test_config_rejects_invalid_values(overrides):
    kwargs = dict(db_path="/tmp/x.db")
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        rw.ResearchWorkerConfig(**kwargs)


def test_config_rejects_heartbeat_not_smaller_than_lease():
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        rw.ResearchWorkerConfig(
            db_path="/tmp/x.db", lease_seconds=10.0, heartbeat_interval_seconds=10.0)


def test_load_worker_config_reads_env_overrides():
    env = {
        "RESEARCH_WORKER_RESEARCH_POOL_SIZE": "5",
        "RESEARCH_WORKER_CHAT_POOL_SIZE": "2",
        "RESEARCH_WORKER_POLL_INTERVAL_SECONDS": "2.5",
        "RESEARCH_WORKER_LEASE_SECONDS": "120",
        "RESEARCH_WORKER_HEARTBEAT_INTERVAL_SECONDS": "10",
        "RESEARCH_WORKER_RECONCILE_INTERVAL_SECONDS": "30",
        "RESEARCH_WORKER_SHUTDOWN_GRACE_SECONDS": "15",
    }
    config = rw.load_worker_config(env, "/tmp/x.db")
    assert config.research_pool_size == 5
    assert config.chat_pool_size == 2
    assert config.poll_interval_seconds == 2.5
    assert config.lease_seconds == 120
    assert config.heartbeat_interval_seconds == 10
    assert config.reconcile_interval_seconds == 30
    assert config.shutdown_grace_seconds == 15


def test_load_worker_config_defaults_when_env_missing():
    config = rw.load_worker_config({}, "/tmp/x.db")
    assert config == rw.ResearchWorkerConfig(db_path="/tmp/x.db")


def test_load_worker_config_rejects_malformed_numeric_env_value():
    with pytest.raises(ValueError, match="RESEARCH_WORKER_RESEARCH_POOL_SIZE"):
        rw.load_worker_config({"RESEARCH_WORKER_RESEARCH_POOL_SIZE": "not-a-number"}, "/tmp/x.db")


def test_load_worker_config_rejects_invalid_resulting_config():
    with pytest.raises(ValueError):
        rw.load_worker_config({"RESEARCH_WORKER_RESEARCH_POOL_SIZE": "0"}, "/tmp/x.db")


# ============================================================================
# reconcile_once: idempotent, recovery-only
# ============================================================================

def test_reconcile_once_recovers_expired_leases_and_is_idempotent(db_path, conn):
    _, run_id = _pending_run(conn)
    lease = claim_research_run(conn, run_id, T0, lease_seconds=60, deadline_seconds=3600)
    # _chat_scenario claims its OWN research run internally (lease_seconds=600) -- `later` below
    # must stay strictly short of that so this test's "only the target run's shorter lease
    # expired" setup is not incidentally clobbered by db/research_runs.py's equality-inclusive
    # `lease_expires_at <= now` expiry check (an exact match at 10 minutes would expire both).
    _, request_id = _chat_scenario(conn, question="Other question")

    later = T0 + timedelta(minutes=9)
    config = _config(db_path)
    result = rw.reconcile_once(
        config, connection_factory=lambda: connect(db_path), clock=lambda: later)
    assert result.recovered_research_runs == 1
    assert get_research_run(conn, run_id).status == ResearchRunStatus.PENDING
    assert lease.run.claim_token is not None  # sanity: it really had been claimed

    second = rw.reconcile_once(
        config, connection_factory=lambda: connect(db_path), clock=lambda: later)
    assert second.recovered_research_runs == 0
    assert second.recovered_chat_requests == 0


def test_reconcile_once_never_claims_or_executes_anything(db_path, conn):
    _pending_run(conn)
    config = _config(db_path)
    result = rw.reconcile_once(
        config, connection_factory=lambda: connect(db_path), clock=lambda: T0)
    assert result.recovered_research_runs == 0
    assert result.recovered_chat_requests == 0
    runs = conn.execute("SELECT status FROM research_runs").fetchall()
    assert [r["status"] for r in runs] == ["pending"]


# ============================================================================
# Pool isolation: chat progresses while the research pool is full
# ============================================================================

@pytest.mark.asyncio
async def test_chat_progresses_while_research_pool_is_full(db_path, conn):
    session_a, run_a = _pending_run(conn, question="Q-A")
    session_b, run_b = _pending_run(conn, question="Q-B")
    _, request_id = _chat_scenario(conn, question="Q-chat")

    event = threading.Event()
    config = _config(db_path, research_pool_size=1, chat_pool_size=1)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_blocking_research_runner(event),
        chat_processor=_async_chat_ok, log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        # Only one of the two pending runs was claimed (pool size 1); the other stays pending.
        claimed_ids = {run_a, run_b} & {
            row["id"] for row in conn.execute(
                "SELECT id FROM research_runs WHERE status = 'processing'").fetchall()}
        assert len(claimed_ids) == 1
        pending_ids = {run_a, run_b} - claimed_ids
        assert get_research_run(conn, next(iter(pending_ids))).status == ResearchRunStatus.PENDING

        # Give the (non-blocking) chat coroutine a moment to actually finish -- the fake
        # chat_processor never writes to the DB itself (that is process_claimed_chat_request's
        # own responsibility, not this worker's), so "progressed" here means the worker's local
        # bookkeeping released the slot, not that the DB row transitioned.
        for _ in range(50):
            if worker.active_chat_count == 0:
                break
            await asyncio.sleep(0.02)
        assert worker.active_chat_count == 0
        # The research run is still blocked the entire time.
        assert worker.active_research_count == 1
    finally:
        event.set()
        await worker.wait_idle()
        worker.close()


# ============================================================================
# The global processing caps are DB-enforced, even across separate worker instances.
# ============================================================================

@pytest.mark.asyncio
async def test_max_three_research_claims_enforced_across_worker_instances(db_path, conn):
    for i in range(5):
        _pending_run(conn, question=f"Q-{i}")
    event = threading.Event()

    def make_worker():
        return rw.ResearchWorker(
            _config(db_path, research_pool_size=5), connection_factory=lambda: connect(db_path),
            research_task_runner=_blocking_research_runner(event), log=lambda _msg: None)

    worker_one = make_worker()
    worker_two = make_worker()
    try:
        await worker_one.poll_once()
        await worker_two.poll_once()

        assert worker_one.active_research_count + worker_two.active_research_count == 3
        assert count_active_processing_runs(conn, T0 + timedelta(seconds=1)) == 3
        processing = conn.execute(
            "SELECT COUNT(*) AS n FROM research_runs WHERE status = 'processing'").fetchone()
        assert processing["n"] == 3
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM research_runs WHERE status = 'pending'").fetchone()
        assert pending["n"] == 2
    finally:
        event.set()
        await worker_one.wait_idle()
        await worker_two.wait_idle()
        worker_one.close()
        worker_two.close()


@pytest.mark.asyncio
async def test_max_three_chat_claims_enforced_across_worker_instances(db_path, conn):
    for i in range(5):
        _chat_scenario(conn, question=f"Q-chat-{i}")
    event = asyncio.Event()

    def make_worker():
        return rw.ResearchWorker(
            _config(db_path, chat_pool_size=5), connection_factory=lambda: connect(db_path),
            chat_processor=_blocking_chat_processor(event), log=lambda _msg: None)

    worker_one = make_worker()
    worker_two = make_worker()
    try:
        await worker_one.poll_once()
        await worker_two.poll_once()

        assert worker_one.active_chat_count + worker_two.active_chat_count == 3
        assert count_active_processing_chat_requests(conn, T0 + timedelta(seconds=1)) == 3
        processing = conn.execute(
            "SELECT COUNT(*) AS n FROM research_chat_requests "
            "WHERE status = 'processing'").fetchone()
        assert processing["n"] == 3
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM research_chat_requests "
            "WHERE status = 'pending'").fetchone()
        assert pending["n"] == 2
    finally:
        event.set()
        await worker_one.wait_idle()
        await worker_two.wait_idle()
        worker_one.close()
        worker_two.close()


# ============================================================================
# Per-task connections are distinct objects, never shared across tasks.
# ============================================================================

@pytest.mark.asyncio
async def test_each_task_gets_its_own_distinct_connection(db_path, conn):
    _, run_id = _pending_run(conn)
    _, request_id = _chat_scenario(conn, question="Other question")

    created: list = []

    def factory():
        c = connect(db_path)
        created.append(c)
        return c

    research_event = threading.Event()
    chat_event = asyncio.Event()
    config = _config(db_path, research_pool_size=1, chat_pool_size=1)
    worker = rw.ResearchWorker(
        config, connection_factory=factory,
        research_task_runner=_blocking_research_runner(research_event),
        chat_processor=_blocking_chat_processor(chat_event), log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        assert worker.active_chat_count == 1
        # Give the newly-scheduled supervisor/heartbeat coroutines a chance to actually run --
        # asyncio.create_task() only schedules them; poll_once() itself never awaits them.
        await asyncio.sleep(0.05)
        # coordinator + research heartbeat + research task-conn + chat heartbeat + chat task-conn
        assert len(created) >= 5
        assert len({id(c) for c in created}) == len(created)
    finally:
        research_event.set()
        chat_event.set()
        await worker.wait_idle()
        worker.close()


# ============================================================================
# Heartbeat keeps renewing the lease while the research thread is blocked on I/O.
# ============================================================================

@pytest.mark.asyncio
async def test_heartbeat_continues_while_research_work_blocks(db_path, conn):
    _, run_id = _pending_run(conn)

    def slow_runner(*, connection_factory, run_id, claim_token, session_id, question,
                     language_code, model, now_fn):
        time.sleep(0.35)
        return _sealed_outcome(run_id)

    config = _config(
        db_path, research_pool_size=1, heartbeat_interval_seconds=0.05, lease_seconds=1.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path), research_task_runner=slow_runner,
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        seen_leases = set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and worker.active_research_count:
            row = conn.execute(
                "SELECT lease_expires_at FROM research_runs WHERE id = ?", (run_id,)).fetchone()
            if row["lease_expires_at"] is not None:
                seen_leases.add(row["lease_expires_at"])
            await asyncio.sleep(0.03)
        assert len(seen_leases) >= 2, "expected the lease to be renewed more than once"
    finally:
        await worker.wait_idle()
        worker.close()


# ============================================================================
# Claim-loss cancellation: a stale claim's local task is stopped once its heartbeat notices.
# ============================================================================

@pytest.mark.asyncio
async def test_claim_loss_cancels_the_local_chat_task(db_path, conn):
    _, request_id = _chat_scenario(conn)
    chat_event = asyncio.Event()
    config = _config(db_path, chat_pool_size=1, heartbeat_interval_seconds=0.05, lease_seconds=1.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path), clock=lambda: T0,
        chat_processor=_blocking_chat_processor(chat_event), log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_chat_count == 1
        live = worker._chat_tasks[request_id]
        stale_claim_token = live.claim_token

        # Simulate another worker reclaiming this request once its lease looks expired --
        # force-expire with a far-future "now" regardless of the actual lease value, then
        # re-claim it fresh (a different claim_token) exactly like a second worker would.
        far_future = T0 + timedelta(days=1)
        from beehive.db.research_chat_requests import (claim_chat_request,
                                                         recover_expired_chat_requests)
        recover_expired_chat_requests(conn, far_future)
        reclaimed = claim_chat_request(conn, request_id, far_future, lease_seconds=90)
        assert reclaimed is not None
        assert reclaimed.claim_token != stale_claim_token

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and worker.active_chat_count:
            await asyncio.sleep(0.02)
        assert worker.active_chat_count == 0
    finally:
        chat_event.set()
        await worker.wait_idle()
        worker.close()


@pytest.mark.asyncio
async def test_claim_loss_detaches_the_local_research_task(db_path, conn):
    _, run_id = _pending_run(conn)
    research_event = threading.Event()
    config = _config(
        db_path, research_pool_size=1, heartbeat_interval_seconds=0.05, lease_seconds=1.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path), clock=lambda: T0,
        research_task_runner=_blocking_research_runner(research_event, timeout=3.0),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        live = worker._research_tasks[run_id]
        stale_claim_token = live.claim_token

        # Force-expire with a "now" past the lease but well short of the run's 20-minute
        # deadline (recover_expired_research_runs fails a run outright, rather than requeuing
        # it, once BOTH the lease and the deadline have passed).
        far_future = T0 + timedelta(seconds=5)
        from beehive.db.research_runs import recover_expired_research_runs
        recover_expired_research_runs(conn, far_future)
        lease = claim_research_run(
            conn, run_id, far_future, lease_seconds=90, deadline_seconds=3600)
        assert lease is not None
        assert lease.run.claim_token != stale_claim_token

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and worker.active_research_count:
            await asyncio.sleep(0.02)
        assert worker.active_research_count == 0
        # The new claim holder's row is untouched by the detached (stale) local task.
        assert get_research_run(conn, run_id).claim_token == lease.run.claim_token
    finally:
        research_event.set()
        worker.close()


# ============================================================================
# One task's failure never crashes the worker, and is recorded via the fenced fail path.
# ============================================================================

@pytest.mark.asyncio
async def test_research_task_failure_is_isolated_and_recorded(db_path, conn):
    _, run_id = _pending_run(conn)
    config = _config(db_path, research_pool_size=1)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_raising_research_runner(RuntimeError("boom: secret prompt text")),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        await worker.wait_idle()
        stored = get_research_run(conn, run_id)
        assert stored.status == ResearchRunStatus.FAILED
        # poll_once() (and the worker generally) must still be usable afterward.
        await worker.poll_once()
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_chat_task_failure_is_isolated_and_recorded(db_path, conn):
    _, request_id = _chat_scenario(conn)
    config = _config(db_path, chat_pool_size=1)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        chat_processor=_raising_chat_processor(ConversationError("bad alias in response")),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        await worker.wait_idle()
        stored = get_chat_request(conn, request_id)
        assert stored.status.value == "failed"
        assert stored.error_code == "ConversationError"
        await worker.poll_once()
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_chat_claim_lost_mid_reply_writes_nothing(db_path, conn):
    _, request_id = _chat_scenario(conn)
    config = _config(db_path, chat_pool_size=1)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        chat_processor=_raising_chat_processor(ConversationClaimLostError("lost")),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        await worker.wait_idle()
        stored = get_chat_request(conn, request_id)
        assert stored.status.value == "processing"  # untouched: the claim was already gone
    finally:
        worker.close()


# ============================================================================
# Graceful stop: still-live claims are requeued/cooperatively cancelled after the grace period.
# ============================================================================

@pytest.mark.asyncio
async def test_shutdown_requeues_a_still_running_chat_claim(db_path, conn):
    _, request_id = _chat_scenario(conn)
    chat_event = asyncio.Event()  # never set: the reply never finishes on its own
    config = _config(db_path, chat_pool_size=1, shutdown_grace_seconds=0.05)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        chat_processor=_blocking_chat_processor(chat_event), log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_chat_count == 1
        worker.request_stop()
        await worker._shutdown()
        stored = get_chat_request(conn, request_id)
        assert stored.status.value == "pending"
        assert stored.claim_token is None
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_shutdown_requeues_a_still_running_research_claim_without_cancel_requested(
        db_path, conn):
    _, run_id = _pending_run(conn)
    research_event = threading.Event()  # never set within the test
    config = _config(
        db_path, research_pool_size=1, shutdown_grace_seconds=0.05, lease_seconds=90.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_blocking_research_runner(research_event, timeout=3.0),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        stale_claim_token = worker._research_tasks[run_id].claim_token
        worker.request_stop()
        await worker._shutdown()
        stored = get_research_run(conn, run_id)
        # Requeued immediately -- shutdown is operational, never an Owner cancellation: no
        # cancel_requested, no CANCELLED status, and the claim/lease are cleared so the run is
        # available for the very next claim (by this worker on restart, or another) rather than
        # waiting out the rest of its lease or being force-cancelled.
        assert stored.status == ResearchRunStatus.PENDING
        assert stored.claim_token is None
        assert stored.cancel_requested is False
        # The task slot was released even though the background thread is still blocked.
        assert worker.active_research_count == 0

        # The requeued run resumes the same attempt/budget and can be reclaimed right away --
        # by a fresh poll_once(), exactly like any other recovered/pending run -- even while the
        # old (now stale-and-fenced) background thread is still physically unwinding.
        await worker.poll_once()
        assert worker.active_research_count == 1
        reclaimed = get_research_run(conn, run_id)
        assert reclaimed.status == ResearchRunStatus.PROCESSING
        assert reclaimed.claim_token is not None
        assert reclaimed.claim_token != stale_claim_token
        assert reclaimed.started_at == stored.started_at  # same attempt, not a fresh run
    finally:
        research_event.set()
        await worker.wait_idle()
        worker.close()


# ============================================================================
# Deadline watchdog: a live research task is hard-terminated at its persisted deadline_at, not
# only after a fixed heartbeat interval -- even while its background thread stays blocked.
# ============================================================================

@pytest.mark.asyncio
async def test_deadline_watchdog_fails_the_run_and_releases_the_slot_at_the_deadline(
        db_path, conn, monkeypatch):
    _, run_id = _pending_run(conn)
    # A short, fixed product deadline for this test -- MAX_RUN_DURATION (20 minutes) is a real
    # product ceiling we must not shrink for everyone, so only this test's own claim uses it,
    # via the same module-level name _fill_research_pool reads at claim time.
    monkeypatch.setattr(rw, "MAX_RUN_DURATION", timedelta(seconds=0.2))
    research_event = threading.Event()  # never set: simulates a stuck connector/AI call
    config = _config(
        db_path, research_pool_size=1, heartbeat_interval_seconds=5.0, lease_seconds=60.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_blocking_research_runner(research_event, timeout=10.0),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        live = worker._research_tasks[run_id]
        stale_claim_token = live.claim_token

        # The heartbeat interval (5s) is far longer than the 0.2s deadline: only a deadline-
        # aware wake-up (not a coincidental heartbeat) can catch this in a couple of seconds.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and worker.active_research_count:
            await asyncio.sleep(0.02)

        assert worker.active_research_count == 0, "task slot must be released at the deadline"
        stored = get_research_run(conn, run_id)
        assert stored.status == ResearchRunStatus.FAILED
        assert stored.claim_token is None
        assert stored.phase is None
        row = conn.execute(
            "SELECT error_code FROM research_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["error_code"] == "deadline_exceeded"

        # A later write from the now-stale background thread (still physically blocked/
        # unwinding) must be rejected: its claim_token no longer matches any 'processing' row.
        from beehive.db.research_runs import complete_research_run
        stale_write_ok = complete_research_run(
            conn, run_id, stale_claim_token, ResearchRunStatus.COMPLETED, rw._utc_now())
        assert stale_write_ok is False
        assert get_research_run(conn, run_id).status == ResearchRunStatus.FAILED

        # The recovered run can be reclaimed once terminal only via a fresh enqueue -- but a
        # FAILED run itself is not reclaimable (it is terminal), which is exactly what protects
        # it from the stale thread: there is no path back to 'pending' for this run any more.
    finally:
        research_event.set()
        await worker.wait_idle()
        worker.close()


@pytest.mark.asyncio
async def test_deadline_watchdog_wakes_before_a_longer_heartbeat_interval(
        db_path, conn, monkeypatch):
    """Directly proves the sleep-until-the-earlier-of behavior: with a deadline much shorter
    than heartbeat_interval_seconds, the lease is capped to (and the run fails at) the deadline,
    not the full heartbeat interval."""
    _, run_id = _pending_run(conn)
    monkeypatch.setattr(rw, "MAX_RUN_DURATION", timedelta(seconds=0.15))
    research_event = threading.Event()
    config = _config(
        db_path, research_pool_size=1, heartbeat_interval_seconds=10.0, lease_seconds=60.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_blocking_research_runner(research_event, timeout=10.0),
        log=lambda _msg: None)
    started = time.monotonic()
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        while time.monotonic() - started < 5.0 and worker.active_research_count:
            await asyncio.sleep(0.02)
        elapsed = time.monotonic() - started
        assert worker.active_research_count == 0
        # Must be woken near the 0.15s deadline, nowhere close to the 10s heartbeat interval.
        assert elapsed < 2.0
        assert get_research_run(conn, run_id).status == ResearchRunStatus.FAILED
    finally:
        research_event.set()
        await worker.wait_idle()
        worker.close()


@pytest.mark.asyncio
async def test_deadline_watchdog_grants_grace_and_never_detaches_a_cancelled_run_at_the_deadline(
        db_path, conn, monkeypatch):
    """Task A, at the worker layer: unlike the two sibling tests above (a non-cancelled run is
    hard-terminated -- and its task slot released -- the instant its deadline arrives), a
    CANCELLED research task's heartbeat must NOT detach it at that same instant:
    heartbeat_research_run grants a cancellation-finalization grace and keeps renewing the lease
    PAST deadline_at for as long as the background thread is still finishing local finalization,
    so this worker never prematurely treats a legitimate in-progress cancellation as a lost
    claim. Only once that claim genuinely goes terminal (simulating the real orchestrator's own
    completion write, which the fake research_task_runner used here does not itself perform)
    does the next heartbeat get refused and the task slot released."""
    _, run_id = _pending_run(conn)
    monkeypatch.setattr(rw, "MAX_RUN_DURATION", timedelta(seconds=0.2))
    research_event = threading.Event()  # only set once this test says the "thread" may finish
    config = _config(
        db_path, research_pool_size=1, heartbeat_interval_seconds=0.05, lease_seconds=60.0)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_blocking_research_runner(research_event, timeout=10.0),
        log=lambda _msg: None)
    try:
        await worker.poll_once()
        assert worker.active_research_count == 1
        live = worker._research_tasks[run_id]
        claim_token = live.claim_token
        assert request_cancel_research_run(conn, run_id) is True

        # Wait comfortably past the 0.2s deadline -- long enough that a non-cancelled sibling
        # would already have been hard-terminated and released (per the tests above).
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        # Still alive: the grace kept renewing the lease past its own deadline_at instead of
        # hard-terminating the run or detaching the task.
        assert worker.active_research_count == 1
        stored = get_research_run(conn, run_id)
        assert stored.status == ResearchRunStatus.PROCESSING
        assert stored.claim_token == claim_token
        row = conn.execute(
            "SELECT deadline_at, lease_expires_at, error_code FROM research_runs WHERE id = ?",
            (run_id,)).fetchone()
        assert row["error_code"] is None
        assert (datetime.fromisoformat(row["lease_expires_at"])
                > datetime.fromisoformat(row["deadline_at"]))

        # Simulate the real orchestrator's own terminal write (what _synthesize_and_terminate
        # would do once local finalization actually finishes) -- the fake runner used here never
        # touches the DB itself.
        from beehive.db.research_runs import complete_research_run_if_claimed
        terminal = complete_research_run_if_claimed(
            conn, run_id, claim_token, ResearchRunStatus.COMPLETED, rw._utc_now())
        assert terminal.ok
        assert terminal.committed_status == ResearchRunStatus.CANCELLED

        # The underlying background thread now finishes for real; the next heartbeat -- no
        # longer matching a 'processing' row -- is refused, releasing the slot.
        research_event.set()
        release_deadline = time.monotonic() + 2.0
        while time.monotonic() < release_deadline and worker.active_research_count:
            await asyncio.sleep(0.02)
        assert worker.active_research_count == 0
        assert get_research_run(conn, run_id).status == ResearchRunStatus.CANCELLED
    finally:
        research_event.set()
        await worker.wait_idle()
        worker.close()


@pytest.mark.asyncio
async def test_run_stops_claiming_new_work_once_stop_is_requested(db_path, conn):
    _, run_id = _pending_run(conn)
    config = _config(db_path, poll_interval_seconds=0.01, shutdown_grace_seconds=0.05)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path),
        research_task_runner=_raising_research_runner(RuntimeError("unused")),
        log=lambda _msg: None)
    worker.request_stop()
    await worker.run()
    # run() saw stop already requested and returned without ever claiming the pending run.
    assert get_research_run(conn, run_id).status == ResearchRunStatus.PENDING


# ============================================================================
# Expired-lease recovery inside the worker's own poll loop (startup + periodic).
# ============================================================================

@pytest.mark.asyncio
async def test_poll_once_recovers_and_immediately_reclaims_an_expired_research_lease(
        db_path, conn):
    _, run_id = _pending_run(conn)
    stale = claim_research_run(conn, run_id, T0, lease_seconds=1, deadline_seconds=3600)
    assert stale is not None

    later = T0 + timedelta(minutes=5)
    event = threading.Event()
    config = _config(db_path, research_pool_size=1)
    worker = rw.ResearchWorker(
        config, connection_factory=lambda: connect(db_path), clock=lambda: later,
        research_task_runner=_blocking_research_runner(event), log=lambda _msg: None)
    try:
        await worker.poll_once()
        stored = get_research_run(conn, run_id)
        assert stored.status == ResearchRunStatus.PROCESSING
        assert stored.claim_token != stale.run.claim_token
        assert worker.active_research_count == 1
    finally:
        event.set()
        await worker.wait_idle()
        worker.close()


def test_reconcile_once_matches_worker_startup_recovery(db_path, conn):
    _, run_id = _pending_run(conn)
    claim_research_run(conn, run_id, T0, lease_seconds=1, deadline_seconds=3600)
    later = T0 + timedelta(minutes=5)
    result = rw.reconcile_once(
        _config(db_path), connection_factory=lambda: connect(db_path), clock=lambda: later)
    assert result.recovered_research_runs == 1
    assert get_research_run(conn, run_id).status == ResearchRunStatus.PENDING
