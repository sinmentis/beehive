import asyncio
import functools
import threading
from datetime import datetime, timedelta, timezone

import pytest

from beehive.ai.response_parser import RankedItem
from beehive.collector.jobs import run_ranking_job
from beehive.collector.jobs_status import load_status
from beehive.collector.jobs_worker import EXIT_STUCK, JobsWorker, JobsWorkerConfig
from beehive.collector.ranking_worker import process_claimed_ranking_request
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
from beehive.db.ranking_requests import (
    claim_next_ranking_request,
    complete_ranking_batch,
    create_ranking_request,
    get_ranking_request,
    prepare_ranking_batch,
    requeue_ranking_request,
)
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
        self.rankings = []
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

    def ranking(self, db_path, claimed):
        self.rankings.append(claimed)
        gate = self.block.get("ranking")
        if gate is not None:
            gate.wait(timeout=10)

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


def _worker(db_path, runners, clock, exits=None, *, ranking_runner=None, **config):
    return JobsWorker(
        JobsWorkerConfig(db_path=db_path, **config),
        clock=clock,
        fetch_runner=runners.fetch,
        deep_read_runner=runners.deep_read,
        ranking_runner=ranking_runner or runners.ranking,
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


def _ranked_item(conn, name="News", *, kind="editorial"):
    channel = create_channel(conn, name, "p", kind=kind)
    source_type, config = {
        "editorial": ("reddit_subreddit", {"subreddit": "nz"}),
        "tracker": ("all_about_auctions", {"url": "https://example.com"}),
    }[kind]
    source = create_source(conn, channel, source_type, config)
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


def _ranking_request(conn, item_id, now=START):
    channel_id = conn.execute(
        "SELECT sources.channel_id FROM sources JOIN items ON items.source_id = sources.id "
        "WHERE items.id = ?", (item_id,),
    ).fetchone()[0]
    return create_ranking_request(conn, channel_id, mode="apply", profile="p", now=now)


@pytest.mark.asyncio
async def test_ranking_excludes_both_fetch_lanes_but_other_channels_keep_running(db_path, conn):
    request = _ranking_request(conn, _ranked_item(conn))
    busy = request["channel_id"]
    other = create_channel(conn, "Other", "p")
    runners, clock = Runners(), Clock(START)
    gate = runners.block["ranking"] = threading.Event()
    worker = _worker(db_path, runners, clock)
    try:
        await worker.poll_once()
        await asyncio.sleep(0.05)
        assert [claim.channel_id for claim in runners.rankings] == [busy]
        assert runners.fetches == [(other, False)]
        request_fetch(conn, [busy, other], START)
        await worker.poll_once()
        await _drain_lane(worker, "manual_fetch")
        assert fetch_request_states(conn, START) == {busy: "queued"}
        assert (other, True) in runners.fetches
        assert all(channel_id != busy for channel_id, _ in runners.fetches)
        status = load_status(conn)
        assert next(lane for lane in status.lanes if lane.name == "ranking").busy_since == START

        gate.set()
        await _drain(worker)
        await worker.poll_once()
        await _drain(worker)
        assert (busy, True) in runners.fetches
    finally:
        gate.set()
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_ranking_waits_for_a_channel_fetch_and_can_process_another_channel(
    db_path, conn, manual,
):
    busy_item = _ranked_item(conn)
    free_item = _ranked_item(conn, name="Free")
    busy = conn.execute("SELECT id FROM channels WHERE name = 'News'").fetchone()[0]
    if manual:
        request_fetch(conn, [busy], START)
    runners, clock = Runners(), Clock(START)
    gate = runners.block[busy] = threading.Event()
    worker = _worker(db_path, runners, clock)
    try:
        await worker.poll_once()
        await asyncio.sleep(0.05)
        waiting = _ranking_request(conn, busy_item)
        free = _ranking_request(conn, free_item)
        await worker.poll_once()
        await _drain_lane(worker, "ranking")
        assert get_ranking_request(conn, waiting["id"])["status"] == "queued"
        assert [claim.request_id for claim in runners.rankings] == [free["id"]]
        gate.set()
        await _drain(worker)
        await worker.poll_once()
        await _drain(worker)
        assert runners.rankings[-1].request_id == waiting["id"]
    finally:
        gate.set()
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
async def test_ranking_defers_rather_than_drops_the_same_channels_scheduled_fetch(db_path, conn):
    request = _ranking_request(conn, _ranked_item(conn))
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)
    try:
        await worker.poll_once()
        await _drain(worker)
        assert runners.fetches == []
        await worker.poll_once()
        await _drain(worker)
        assert runners.fetches == [(request["channel_id"], False)]
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_a_deferred_ranking_channel_cannot_block_later_sweep_boundaries(db_path, conn):
    request = _ranking_request(conn, _ranked_item(conn))
    busy = request["channel_id"]
    others = [create_channel(conn, name, "p") for name in ("Other", "Third")]
    runners, clock, exits = Runners(), Clock(START), []
    gate = runners.block["ranking"] = threading.Event()
    worker = _worker(db_path, runners, clock, exits, ranking_limit_seconds=7200)
    try:
        for minute in range(61):
            clock.now = START + timedelta(minutes=minute)
            await worker.poll_once()
            for name in ("scheduled_fetch", "digest", "reminders"):
                await _drain_lane(worker, name)
        assert exits == []
        assert all(channel_id != busy for channel_id, _ in runners.fetches)
        for channel_id in others:
            assert runners.fetches.count((channel_id, False)) == 5
        assert list(worker._sweep) == [busy]

        gate.set()
        await _drain(worker)
        await worker.poll_once()
        await _drain(worker)
        assert runners.fetches.count((busy, False)) == 1
    finally:
        gate.set()
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["editorial", "tracker"])
async def test_large_apply_yields_to_preview_and_finishes_without_restarts_or_repeat_work(
    db_path, conn, monkeypatch, kind,
):
    first_item = _ranked_item(conn, name="Large apply", kind=kind)
    source_id = conn.execute("SELECT source_id FROM items WHERE id = ?", (first_item,)).fetchone()[0]
    for index in range(79):
        insert_new(
            conn, source_id,
            RawItem(external_id=f"extra-{index}", title=f"Item {index}", url=f"https://example.com/{index}"),
        )
    apply_ids = {row[0] for row in conn.execute("SELECT id FROM items WHERE source_id = ?", (source_id,))}
    request = _ranking_request(conn, first_item)
    preview_item = _ranked_item(conn, name="Preview")
    preview_channel = conn.execute(
        "SELECT sources.channel_id FROM sources JOIN items ON items.source_id = sources.id "
        "WHERE items.id = ?", (preview_item,),
    ).fetchone()[0]
    runners, clock, exits = Runners(), Clock(START), []
    claims = []
    ranked_apply_ids = []
    own_fetches = []
    original_fetch = runners.fetch

    def observe_fetch(path, channel_id, *, force):
        original_fetch(path, channel_id, force=force)
        if channel_id == request["channel_id"]:
            reader = connect(path)
            try:
                progress = get_ranking_request(reader, request["id"])
                own_fetches.append((clock.now, progress["status"], progress["processed_count"]))
            finally:
                reader.close()

    async def rank(**kwargs):
        if kwargs["profile"] == "p":
            ranked_apply_ids.extend(int(candidate.item_key) for candidate in kwargs["candidates"])
        return [
            RankedItem(candidate.item_key, 85, "Updated summary", "Updated reason")
            for candidate in kwargs["candidates"]
        ]

    monkeypatch.setattr("beehive.collector.ranking_worker.rank_channel", rank)
    monkeypatch.setattr("beehive.collector.ranking_worker.rank_monitor_channel", rank)
    monkeypatch.setattr(runners, "fetch", observe_fetch)
    monkeypatch.setattr(
        "beehive.collector.ranking_worker.process_claimed_ranking_request",
        functools.partial(process_claimed_ranking_request, clock=clock),
    )

    def run_slice(path, claim):
        claims.append(claim)
        run_ranking_job(path, claim)

    worker = _worker(db_path, runners, clock, exits, ranking_runner=run_slice)
    try:
        await worker.poll_once()
        await _drain(worker)
        partial = get_ranking_request(conn, request["id"])
        assert partial["status"] == "queued"
        assert 0 < partial["processed_count"] < partial["total_count"]

        clock.advance(minutes=10)
        preview = create_ranking_request(
            conn, preview_channel, mode="preview", profile="Draft preview", now=clock.now,
        )
        await worker.poll_once()
        await _drain(worker)
        assert get_ranking_request(conn, preview["id"])["status"] == "completed"
        assert get_ranking_request(conn, request["id"])["processed_count"] == partial["processed_count"]

        for _ in range(20):
            clock.advance(minutes=10)
            await worker.poll_once()
            await _drain(worker)
            if get_ranking_request(conn, request["id"])["status"] == "completed":
                break
        complete = get_ranking_request(conn, request["id"])
        assert (complete["status"], complete["processed_count"], complete["skipped_count"]) == (
            "completed", len(apply_ids), 0,
        )
        assert len(ranked_apply_ids) == len(set(ranked_apply_ids)) == len(apply_ids)
        assert set(ranked_apply_ids) == apply_ids
        assert all(claim.attempt == 1 for claim in claims)
        assert len(claims) > 3
        assert clock.now - START > timedelta(hours=1)
        partial_fetches = [
            (at, count) for at, status, count in own_fetches
            if status == "queued" and 0 < count < len(apply_ids)
        ]
        assert len({int(at.timestamp() // 900) for at, _ in partial_fetches}) >= 3
        assert len({count for _, count in partial_fetches}) >= 3
        assert all(status != "running" for _, status, _ in own_fetches)
        assert runners.fetches.count((preview_channel, False)) >= 3
        assert exits == []
    finally:
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
async def test_progressed_apply_does_not_wait_for_an_unrelated_busy_scheduled_fetch(db_path, conn):
    busy = create_channel(conn, "A busy fetch", "p")
    item_id = _ranked_item(conn, name="B apply")
    source_id = conn.execute("SELECT source_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    insert_new(conn, source_id, RawItem(external_id="second", title="Second", url="https://example.com/2"))
    runners, clock = Runners(), Clock(START)
    gate = runners.block[busy] = threading.Event()
    worker = _worker(db_path, runners, clock)
    try:
        await worker.poll_once()
        await asyncio.sleep(0.05)
        request = _ranking_request(conn, item_id)
        claim = claim_next_ranking_request(conn, START, lease_seconds=180)
        batch = prepare_ranking_batch(conn, claim, START, limit=1)
        complete_ranking_batch(
            conn, claim,
            [{"item_id": batch[0]["id"], "score": 85, "summary": "Summary", "rationale": "Reason"}],
            START,
        )
        requeue_ranking_request(conn, claim, START, failed=False)
        assert list(worker._sweep) == [request["channel_id"]]

        await worker.poll_once()
        await _drain_lane(worker, "ranking")

        assert [claim.request_id for claim in runners.rankings] == [request["id"]]
        assert "scheduled_fetch" in worker.busy_lanes()
    finally:
        gate.set()
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
async def test_an_abandoned_ranking_request_is_recovered_at_worker_startup(db_path, conn):
    earlier = START - timedelta(minutes=10)
    request = _ranking_request(conn, _ranked_item(conn), now=earlier)
    old_claim = claim_next_ranking_request(conn, earlier, lease_seconds=180)
    runners, clock = Runners(), Clock(START)
    worker = _worker(db_path, runners, clock)
    try:
        await worker.poll_once()
        await _drain(worker)
        assert runners.rankings[0].request_id == request["id"]
        assert runners.rankings[0].attempt == 2
        assert runners.rankings[0].claim_token != old_claim.claim_token
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_worker_renews_a_running_ranking_lease(db_path, conn):
    request = _ranking_request(conn, _ranked_item(conn))
    runners, clock = Runners(), Clock(START)
    gate = runners.block["ranking"] = threading.Event()
    worker = _worker(db_path, runners, clock)
    try:
        await worker.poll_once()
        await asyncio.sleep(0.05)
        clock.advance(seconds=100)
        await worker.poll_once()
        clock.advance(seconds=100)
        await worker.poll_once()
        stored = conn.execute(
            "SELECT status, lease_expires_at, attempts FROM ranking_requests WHERE id = ?",
            (request["id"],),
        ).fetchone()
        assert stored["status"] == "running"
        assert stored["attempts"] == 1
        assert stored["lease_expires_at"] == (clock.now + timedelta(seconds=180)).isoformat()
    finally:
        gate.set()
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
async def test_ranking_hard_limit_requeues_the_claim_before_worker_exit(db_path, conn):
    request = _ranking_request(conn, _ranked_item(conn))
    runners, clock, exits = Runners(), Clock(START), []
    gate = runners.block["ranking"] = threading.Event()
    worker = _worker(db_path, runners, clock, exits, ranking_limit_seconds=60)
    try:
        await worker.poll_once()
        await asyncio.sleep(0.05)
        clock.advance(seconds=61)
        await worker.poll_once()
        assert exits == [EXIT_STUCK]
        assert get_ranking_request(conn, request["id"])["status"] == "queued"
        assert claim_next_ranking_request(conn, clock.now, lease_seconds=180).attempt == 2
    finally:
        gate.set()
        await _drain(worker)
        worker.close()


@pytest.mark.asyncio
async def test_shutdown_hands_back_an_unfinished_ranking_without_consuming_an_attempt(db_path, conn):
    request = _ranking_request(conn, _ranked_item(conn))
    runners, clock = Runners(), Clock(START)
    gate = runners.block["ranking"] = threading.Event()
    worker = _worker(db_path, runners, clock, shutdown_grace_seconds=0.01)

    async def stop_after_start(seconds):
        await asyncio.sleep(0.05)
        worker.request_stop()

    worker._sleep = stop_after_start
    try:
        await worker.run()
        assert get_ranking_request(conn, request["id"])["status"] == "queued"
        assert claim_next_ranking_request(conn, START, lease_seconds=180).attempt == 1
    finally:
        gate.set()
        await _drain(worker)
        worker.close()
