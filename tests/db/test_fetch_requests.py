from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.fetch_requests import (
    MAX_ATTEMPTS,
    claim_next_fetch_request,
    clear_stale_fetch_requests,
    fetch_request_states,
    finish_fetch_request,
    heartbeat_fetch_request,
    recover_expired_fetch_requests,
    request_fetch,
    requeue_fetch_request,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    yield connection
    connection.close()


def _channels(conn, count):
    return [create_channel(conn, f"Channel {n}", "profile") for n in range(count)]


def test_requests_queue_once_per_channel_in_request_order(conn):
    first, second = _channels(conn, 2)
    request_fetch(conn, [second], NOW)
    request_fetch(conn, [first, second, first], NOW + timedelta(seconds=1))

    assert fetch_request_states(conn, NOW) == {first: "queued", second: "queued"}
    claim = claim_next_fetch_request(conn, NOW, lease_seconds=180)
    assert (claim.channel_id, claim.attempt) == (second, 1)
    assert fetch_request_states(conn, NOW) == {first: "queued", second: "running"}
    assert claim_next_fetch_request(conn, NOW, lease_seconds=180).channel_id == first
    assert claim_next_fetch_request(conn, NOW, lease_seconds=180) is None


def test_a_finished_request_disappears_and_a_stolen_one_cannot_be_finished(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    claim = claim_next_fetch_request(conn, NOW, lease_seconds=180)
    stale = claim.__class__(channel_id=channel, claim_token="someone-else", attempt=1)

    assert finish_fetch_request(conn, stale, NOW) is False
    assert heartbeat_fetch_request(conn, stale, NOW, lease_seconds=180) is False
    assert finish_fetch_request(conn, claim, NOW) is True
    assert fetch_request_states(conn, NOW) == {}


def test_heartbeats_keep_a_long_fetch_running(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    claim = claim_next_fetch_request(conn, NOW, lease_seconds=180)
    later = NOW + timedelta(seconds=170)

    assert heartbeat_fetch_request(conn, claim, later, lease_seconds=180) is True
    assert fetch_request_states(conn, NOW + timedelta(seconds=300)) == {channel: "running"}
    assert recover_expired_fetch_requests(conn, NOW + timedelta(seconds=300)).requeued == ()


def test_an_abandoned_claim_is_retried_then_dropped(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    moment = NOW
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claim = claim_next_fetch_request(conn, moment, lease_seconds=180)
        assert claim.attempt == attempt
        moment += timedelta(seconds=181)
        assert fetch_request_states(conn, moment) == {channel: "stale"}
        recovered = recover_expired_fetch_requests(conn, moment)
        if attempt < MAX_ATTEMPTS:
            assert recovered.requeued == (channel,)
        else:
            assert recovered.dropped == (channel,)
    assert fetch_request_states(conn, moment) == {}


def test_a_requeue_on_shutdown_does_not_use_up_an_attempt(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    claim = claim_next_fetch_request(conn, NOW, lease_seconds=180)

    assert requeue_fetch_request(conn, claim, NOW, failed=False) is True
    assert fetch_request_states(conn, NOW) == {channel: "queued"}
    assert claim_next_fetch_request(conn, NOW, lease_seconds=180).attempt == 1


def test_the_owner_clears_only_stale_requests(conn):
    stale, running, queued = _channels(conn, 3)
    request_fetch(conn, [stale], NOW)
    claim_next_fetch_request(conn, NOW, lease_seconds=60)
    request_fetch(conn, [running], NOW + timedelta(seconds=1))
    claim_next_fetch_request(conn, NOW + timedelta(seconds=100), lease_seconds=180)
    request_fetch(conn, [queued], NOW + timedelta(seconds=2))

    assert clear_stale_fetch_requests(conn, NOW + timedelta(seconds=120)) == 1
    assert fetch_request_states(conn, NOW + timedelta(seconds=120)) == {
        running: "running",
        queued: "queued",
    }


def test_deleting_a_channel_removes_its_request(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    conn.execute("DELETE FROM channels WHERE id = ?", (channel,))
    conn.commit()
    assert fetch_request_states(conn, NOW) == {}


def test_a_channel_already_being_fetched_is_skipped(conn):
    busy, free = _channels(conn, 2)
    request_fetch(conn, [busy], NOW)
    request_fetch(conn, [free], NOW + timedelta(seconds=1))

    claim = claim_next_fetch_request(conn, NOW, lease_seconds=180, exclude_channel_ids=[busy])

    assert claim.channel_id == free
    assert fetch_request_states(conn, NOW)[busy] == "queued"


def test_a_try_that_ran_past_the_limit_counts_and_the_request_is_dropped_in_the_end(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    attempts = []
    while True:
        claim = claim_next_fetch_request(conn, NOW, lease_seconds=180)
        if claim is None:
            break
        attempts.append(claim.attempt)
        assert requeue_fetch_request(conn, claim, NOW, failed=True) is True

    assert attempts == list(range(1, MAX_ATTEMPTS + 1))
    assert fetch_request_states(conn, NOW) == {}


def test_fetch_now_during_a_running_fetch_runs_it_again_afterwards(conn):
    (channel,) = _channels(conn, 1)
    request_fetch(conn, [channel], NOW)
    claim = claim_next_fetch_request(conn, NOW, lease_seconds=180)
    request_fetch(conn, [channel], NOW + timedelta(seconds=5))  # e.g. after editing a Source

    assert fetch_request_states(conn, NOW) == {channel: "running"}
    assert finish_fetch_request(conn, claim, NOW + timedelta(seconds=30)) is True
    assert fetch_request_states(conn, NOW) == {channel: "queued"}
    again = claim_next_fetch_request(conn, NOW + timedelta(seconds=31), lease_seconds=180)
    assert again.attempt == 1
    assert finish_fetch_request(conn, again, NOW + timedelta(seconds=40)) is True
    assert fetch_request_states(conn, NOW) == {}


def test_a_queued_request_keeps_its_place_when_asked_again(conn):
    first, second = _channels(conn, 2)
    request_fetch(conn, [first], NOW)
    request_fetch(conn, [second], NOW + timedelta(seconds=1))
    request_fetch(conn, [first], NOW + timedelta(seconds=2))

    assert claim_next_fetch_request(conn, NOW, lease_seconds=180).channel_id == first
