import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from beehive.collector.jobs_status import load_status
from beehive.collector.jobs_worker import EXIT_STUCK, JobsWorker, JobsWorkerConfig
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import get_deep_read, request_deep_read
from beehive.db.fetch_requests import (
    claim_next_fetch_request,
    fetch_request_states,
    request_fetch,
)
from beehive.db.items import insert_new, update_ai_ranking_by_id
from beehive.db.sources import create_source

START = datetime(2026, 9, 30, 8, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class Runners:
    """Records every job; a Channel listed in `block` waits until released."""

    def __init__(self):
        self.fetches = []
        self.deep_reads = []
        self.digests = 0
        self.reminders = 0
        self.block = {}
        self.fail = set()
        self.reminder_claim = None

    def fetch(self, db_path, channel_id, *, force):
        self.fetches.append((channel_id, force))
        gate = self.block.get(channel_id)
        if gate is not None:
            gate.wait(timeout=10)
        if channel_id in self.fail:
            raise RuntimeError("boom")

    def deep_read(self, db_path, claimed):
        self.deep_reads.append(claimed.item_id)
        gate = self.block.get("deep_read")
        if gate is not None:
            gate.wait(timeout=10)

    def digest(self, db_path):
        self.digests += 1

    def reminder(self, db_path, on_claim=None):
        self.reminders += 1
        if self.reminder_claim is not None:
            on_claim(self.reminder_claim)
            gate = self.block.get("reminders")
            if gate is not None:
                gate.wait(timeout=10)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jobs.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    connection = connect(db_path)
    yield connection
    connection.close()


def _worker(db_path, runners, clock, exits=None, **config):
    return JobsWorker(
        JobsWorkerConfig(db_path=db_path, **config),
        clock=clock,
        fetch_runner=runners.fetch,
        deep_read_runner=runners.deep_read,
        digest_runner=runners.digest,
        reminder_runner=runners.reminder,
        exit_process=(exits.append if exits is not None else lambda code: None),
        log=lambda message: None,
    )


async def _drain(worker):
    await worker.wait_idle()
    await asyncio.sleep(0)


async def _drain_lane(worker, name):
    for _ in range(100):
        if name not in worker.busy_lanes():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_start_up_sweeps_every_channel_and_sends_due_email_once(db_path, conn):
    first = create_channel(conn, "First", "p")
    second = create_channel(conn, "Second", "p")
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)

    for _ in range(4):
        await worker.poll_once()
        await _drain(worker)

    assert runners.fetches == [(first, False), (second, False)]
    assert (runners.digests, runners.reminders) == (1, 1)
    worker.close()


@pytest.mark.asyncio
async def test_interval_jobs_run_once_per_slot(db_path, conn):
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)

    for minutes in (0, 3, 4, 9, 14, 15):
        clock.now = START.replace(minute=0) + timedelta(minutes=1 + minutes)
        await worker.poll_once()
        await _drain(worker)

    # Minutes 1, 4, 5, 10, 15 and 16 fall in reminder slots :00, :00, :05, :10, :15, :15.
    assert runners.reminders == 4
    # Digest slots :00 and :15.
    assert runners.digests == 2
    worker.close()


@pytest.mark.asyncio
async def test_fetch_now_runs_forced_and_clears_its_request(db_path, conn):
    channel = create_channel(conn, "News", "p")
    request_fetch(conn, [channel], START)
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)

    await worker.poll_once()
    await _drain(worker)

    assert (channel, True) in runners.fetches
    assert fetch_request_states(conn, START) == {}
    worker.close()


@pytest.mark.asyncio
async def test_the_two_fetch_lanes_never_share_a_channel(db_path, conn):
    busy = create_channel(conn, "Busy", "p")
    other = create_channel(conn, "Other", "p")
    runners, clock = Runners(), Clock(START)
    runners.block[busy] = threading.Event()
    worker = _worker(db_path, runners, clock)

    await worker.poll_once()  # the sweep starts on `busy` and blocks
    await asyncio.sleep(0.05)
    request_fetch(conn, [busy, other], START)
    await worker.poll_once()
    await asyncio.sleep(0.05)

    await _drain_lane(worker, "manual_fetch")
    assert fetch_request_states(conn, START) == {busy: "queued"}
    assert (other, True) in runners.fetches
    runners.block[busy].set()
    await _drain(worker)
    worker.close()


@pytest.mark.asyncio
async def test_a_quarter_hour_tick_during_a_sweep_starts_another_after_it(db_path, conn):
    channel = create_channel(conn, "Slow", "p")
    runners, clock = Runners(), Clock(START)
    runners.block[channel] = threading.Event()
    worker = _worker(db_path, runners, clock)

    await worker.poll_once()
    clock.advance(minutes=15)
    await worker.poll_once()  # still busy: the tick is kept
    runners.block[channel].set()
    await _drain(worker)
    await worker.poll_once()
    await _drain(worker)
    await worker.poll_once()
    await _drain(worker)

    assert runners.fetches == [(channel, False), (channel, False)]
    worker.close()


@pytest.mark.asyncio
async def test_a_failing_job_is_recorded_and_the_lane_carries_on(db_path, conn):
    broken = create_channel(conn, "Broken", "p")
    fine = create_channel(conn, "Fine", "p")
    runners, clock = Runners(), Clock(START)
    runners.fail.add(broken)
    worker = _worker(db_path, runners, clock)

    for _ in range(3):
        await worker.poll_once()
        await _drain(worker)

    assert [channel for channel, _ in runners.fetches] == [broken, fine]
    status = load_status(conn)
    scheduled = next(lane for lane in status.lanes if lane.name == "scheduled_fetch")
    assert scheduled.last_error is None  # the last job, for `fine`, succeeded
    worker.close()


@pytest.mark.asyncio
async def test_a_pending_deep_read_is_claimed_and_processed(db_path, conn):
    channel = create_channel(conn, "News", "p")
    source = create_source(conn, channel, "reddit_subreddit", {"subreddit": "nz"})
    insert_new(conn, source, RawItem(external_id="a", title="A", url="https://example.com/a"))
    item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    update_ai_ranking_by_id(conn, item_id, 80, "Summary", "Why")
    request_deep_read(conn, item_id, START)
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)

    await worker.poll_once()
    await _drain(worker)

    assert runners.deep_reads == [item_id]
    assert get_deep_read(conn, item_id).status == "processing"  # the fake never completed it
    worker.close()


@pytest.mark.asyncio
async def test_an_abandoned_fetch_request_is_recovered_at_start_up(db_path, conn):
    channel = create_channel(conn, "News", "p")
    request_fetch(conn, [channel], START - timedelta(minutes=10))
    claim_next_fetch_request(conn, START - timedelta(minutes=10), lease_seconds=180)
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)

    await worker.poll_once()
    await _drain(worker)

    assert (channel, True) in runners.fetches
    assert fetch_request_states(conn, START) == {}
    worker.close()


@pytest.mark.asyncio
async def test_a_job_past_its_limit_hands_back_its_claim_and_restarts_the_worker(db_path, conn):
    channel = create_channel(conn, "Stuck", "p")
    request_fetch(conn, [channel], START)
    runners, clock, exits = Runners(), Clock(START), []
    runners.block[channel] = threading.Event()
    worker = _worker(db_path, runners, clock, exits)

    await worker.poll_once()
    await asyncio.sleep(0.05)
    clock.advance(minutes=61)
    await worker.poll_once()

    assert exits == [EXIT_STUCK]
    assert fetch_request_states(conn, clock.now) == {channel: "queued"}
    # The stuck try counts, so a request that always hangs is dropped after three.
    assert claim_next_fetch_request(conn, clock.now, lease_seconds=180).attempt == 2
    runners.block[channel].set()
    await _drain(worker)
    worker.close()


@pytest.mark.asyncio
async def test_stopping_hands_back_a_fetch_that_is_still_running(db_path, conn):
    channel = create_channel(conn, "Slow", "p")
    request_fetch(conn, [channel], START)
    runners, clock = Runners(), Clock(START)
    runners.block[channel] = threading.Event()
    worker = _worker(db_path, runners, clock, shutdown_grace_seconds=0.05)

    async def fake_sleep(seconds):
        worker.request_stop()

    worker._sleep = fake_sleep
    await worker.run()

    assert fetch_request_states(conn, START) == {channel: "queued"}
    runners.block[channel].set()


@pytest.mark.asyncio
async def test_the_status_shows_what_each_lane_is_doing(db_path, conn):
    channel = create_channel(conn, "Slow", "p")
    runners, clock = Runners(), Clock(START)
    runners.block[channel] = threading.Event()
    worker = _worker(db_path, runners, clock)

    await worker.poll_once()
    await asyncio.sleep(0.05)
    await worker.poll_once()
    status = load_status(conn)

    assert status.seen_at == START
    lanes = {lane.name: lane for lane in status.lanes}
    assert lanes["scheduled_fetch"].busy_since == START
    assert lanes["scheduled_fetch"].doing == f"Channel {channel}"
    assert not status.is_down(START + timedelta(minutes=2))
    assert status.is_down(START + timedelta(minutes=4))
    runners.block[channel].set()
    await _drain(worker)
    worker.close()


def _ranked_item(conn):
    channel = create_channel(conn, "News", "p")
    source = create_source(conn, channel, "reddit_subreddit", {"subreddit": "nz"})
    insert_new(conn, source, RawItem(external_id="a", title="A", url="https://example.com/a"))
    item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    update_ai_ranking_by_id(conn, item_id, 80, "Summary", "Why")
    return item_id


@pytest.mark.asyncio
async def test_a_deep_read_past_its_limit_is_failed_so_it_cannot_loop(db_path, conn):
    item_id = _ranked_item(conn)
    request_deep_read(conn, item_id, START)
    runners, clock, exits = Runners(), Clock(START), []
    runners.block["deep_read"] = threading.Event()
    worker = _worker(db_path, runners, clock, exits)

    await worker.poll_once()
    await asyncio.sleep(0.05)
    clock.advance(minutes=31)
    await worker.poll_once()

    assert exits == [EXIT_STUCK]
    stored = get_deep_read(conn, item_id)
    assert (stored.status, stored.error_code) == ("failed", "llm")
    runners.block["deep_read"].set()
    await _drain(worker)
    worker.close()


@pytest.mark.asyncio
async def test_stopping_hands_back_the_reminders_being_sent(db_path, monkeypatch):
    released = []
    monkeypatch.setattr(
        "beehive.collector.jobs_worker.release_tracker_reminder_claim",
        lambda conn, token: released.append(token),
    )
    runners, clock = Runners(), Clock(START)
    runners.reminder_claim = "reminder-token"
    runners.block["reminders"] = threading.Event()
    worker = _worker(db_path, runners, clock, shutdown_grace_seconds=0.05)

    async def stop_after_first_poll(seconds):
        await asyncio.sleep(0.05)
        worker.request_stop()

    worker._sleep = stop_after_first_poll
    await worker.run()

    assert released == ["reminder-token"]
    runners.block["reminders"].set()


@pytest.mark.asyncio
async def test_a_second_worker_waits_until_the_first_stops(db_path, conn):
    create_channel(conn, "News", "p")
    first_runners, second_runners, clock = Runners(), Runners(), Clock(START)
    first = _worker(db_path, first_runners, clock)
    second = _worker(db_path, second_runners, clock)

    await first.poll_once()
    await _drain(first)
    await second.poll_once()
    await _drain(second)
    assert first_runners.digests == 1
    assert (second_runners.digests, second_runners.fetches) == (0, [])

    first.request_stop()
    await first.run()  # already stopping: shuts down and frees the lease
    await second.poll_once()
    await _drain(second)
    assert second_runners.digests == 1
    second.close()
