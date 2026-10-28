from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new_returning_id, update_ai_ranking_by_id
from beehive.db.ranking_requests import (
    MAX_ATTEMPTS,
    RankingRequestError,
    cancel_ranking_request,
    claim_next_ranking_request,
    complete_ranking_batch,
    create_ranking_request,
    get_ranking_request,
    heartbeat_ranking_request,
    list_ranking_requests_for_channel,
    prepare_ranking_batch,
    ranking_candidate_count,
    recover_expired_ranking_requests,
    requeue_ranking_request,
)
from beehive.db.sources import create_source

NOW = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "ranking.db"))
    init_schema(connection)
    yield connection
    connection.close()


def _channel(conn, count=2, *, kind="editorial", name="Channel"):
    channel_id = create_channel(conn, name, "saved rules", kind=kind)
    source_type, config = {
        "editorial": ("reddit_subreddit", {"subreddit": "news"}),
        "monitor": ("shopify_collection", {"collection_url": "https://example.com/collections/all"}),
        "tracker": ("all_about_auctions", {"url": "https://example.com"}),
    }[kind]
    source_id = create_source(conn, channel_id, source_type, config)
    ids = []
    for index in range(count):
        item_id = insert_new_returning_id(
            conn, source_id,
            RawItem(external_id=str(index), title=f"Item {index}", body="Original body",
                    url=f"https://example.com/{index}"),
        )
        update_ai_ranking_by_id(conn, item_id, 40, "Original summary", "Original reason")
        conn.execute("UPDATE items SET fetched_at = ? WHERE id = ?",
                     ((NOW + timedelta(seconds=index)).isoformat(), item_id))
        conn.commit()
        ids.append(item_id)
    return channel_id, ids


def _queue(conn, channel_id, *, mode="apply"):
    return create_ranking_request(conn, channel_id, mode=mode, profile="saved rules", now=NOW)


def _claim(conn, now=NOW):
    return claim_next_ranking_request(conn, now, lease_seconds=180)


def _results(batch):
    return [
        {"item_id": item["id"], "score": 85, "summary": "New summary", "rationale": "New reason"}
        for item in batch
    ]


def test_preview_is_small_recent_and_accepts_draft_rules_without_changing_saved_data(conn):
    channel_id, ids = _channel(conn, count=7)
    before = [tuple(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]

    request = create_ranking_request(
        conn, channel_id, mode="preview", profile="unsaved draft", now=NOW,
    )
    claim = _claim(conn)
    batch = prepare_ranking_batch(conn, claim, NOW, limit=10)

    assert request["status"] == "queued"
    assert request["requested_profile"] == "unsaved draft"
    assert (request["total_count"], request["processed_count"], request["result"]) == (5, 0, [])
    assert "claim_token" not in request
    assert [item["id"] for item in batch] == list(reversed(ids))[:5]
    assert ranking_candidate_count(conn, channel_id) == 7
    assert [tuple(row) for row in conn.execute("SELECT * FROM items ORDER BY id")] == before
    assert conn.execute("SELECT profile FROM channels").fetchone()[0] == "saved rules"


@pytest.mark.parametrize("kind", ["editorial", "monitor", "tracker"])
def test_apply_snapshots_only_eligible_existing_ids(conn, kind):
    channel_id, ids = _channel(conn, count=4, kind=kind)
    conn.execute("UPDATE items SET is_read = 1 WHERE id = ?", (ids[1],))
    conn.execute("UPDATE items SET inactive_at = ? WHERE id = ?", (NOW.isoformat(), ids[2]))
    conn.execute("UPDATE items SET superseded_at = ? WHERE id = ?", (NOW.isoformat(), ids[3]))
    conn.commit()
    request = _queue(conn, channel_id)
    expected = {ids[0], ids[2] if kind == "editorial" else ids[1]}

    assert request["total_count"] == ranking_candidate_count(conn, channel_id) == 2
    source_id = conn.execute("SELECT source_id FROM items WHERE id = ?", (ids[0],)).fetchone()[0]
    insert_new_returning_id(
        conn, source_id, RawItem(external_id="later", title="After request", url="https://example.com/new"),
    )
    batch = prepare_ranking_batch(conn, _claim(conn), NOW, limit=10)
    assert {item["id"] for item in batch} == expected


def test_public_errors_are_stable_and_only_one_active_operation_can_exist(conn):
    empty = create_channel(conn, "Empty", "")
    with pytest.raises(RankingRequestError, match="^no_candidates$"):
        create_ranking_request(conn, empty, mode="preview", profile="draft")
    with pytest.raises(RankingRequestError, match="^channel_not_found$"):
        create_ranking_request(conn, 999, mode="preview", profile="draft")
    channel_id, _ = _channel(conn)
    with pytest.raises(RankingRequestError, match="^rules_changed$"):
        create_ranking_request(conn, channel_id, mode="apply", profile="draft")
    first = _queue(conn, channel_id)
    for claim_first in (False, True):
        if claim_first:
            _claim(conn)
        with pytest.raises(RankingRequestError, match="^request_in_progress$"):
            _queue(conn, channel_id, mode="preview")
    assert cancel_ranking_request(conn, first["id"], NOW)
    assert not cancel_ranking_request(conn, first["id"], NOW)
    second = _queue(conn, channel_id, mode="preview")
    assert [row["id"] for row in list_ranking_requests_for_channel(conn, channel_id, 1)] == [
        second["id"],
    ]


def test_apply_rechecks_saved_rules_at_commit_and_leaves_existing_scores_usable(conn):
    channel_id, _ = _channel(conn)
    request = _queue(conn, channel_id)
    claim = _claim(conn)
    batch = prepare_ranking_batch(conn, claim, NOW, limit=10)
    conn.execute("UPDATE channels SET profile = 'changed while AI was running'")
    conn.commit()

    assert not complete_ranking_batch(conn, claim, _results(batch), NOW)
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["error_code"], stored["processed_count"]) == (
        "failed", "rules_changed", 0,
    )
    assert {row[0] for row in conn.execute("SELECT ai_score FROM items")} == {40}


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE items SET title = 'Changed'",
        "UPDATE items SET body = 'Changed'",
        "UPDATE items SET raw_metadata = '{\"price\": 25}'",
        "UPDATE items SET ai_summary = 'Rewritten elsewhere'",
        "UPDATE items SET is_read = 1",
        "UPDATE items SET inactive_at = '2026-10-28T01:00:00+00:00'",
        "UPDATE items SET superseded_at = '2026-10-28T01:00:00+00:00'",
        "DELETE FROM items",
    ],
)
def test_stale_content_deleted_items_and_changed_interaction_state_are_skipped(conn, change):
    channel_id, _ = _channel(conn, count=1)
    request = _queue(conn, channel_id)
    claim = _claim(conn)
    batch = prepare_ranking_batch(conn, claim, NOW, limit=10)
    conn.execute(change)
    conn.commit()
    changed = [tuple(row) for row in conn.execute("SELECT * FROM items")]

    assert complete_ranking_batch(conn, claim, _results(batch), NOW)
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"], stored["skipped_count"]) == (
        "completed", 0, 1,
    )
    assert stored["result"] == []
    assert [tuple(row) for row in conn.execute("SELECT * FROM items")] == changed


def test_known_stale_inputs_are_skipped_before_requesting_ai_work(conn):
    channel_id, _ = _channel(conn, count=1)
    request = _queue(conn, channel_id)
    claim = _claim(conn)
    conn.execute("UPDATE items SET is_read = 1")
    conn.commit()

    assert prepare_ranking_batch(conn, claim, NOW, limit=10) is None
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["skipped_count"]) == ("completed", 1)


@pytest.mark.parametrize("loss", ["cancelled", "wrong_token", "expired"])
def test_cancelled_and_lost_claims_cannot_write_or_renew(conn, loss):
    channel_id, _ = _channel(conn)
    request = _queue(conn, channel_id)
    claim = _claim(conn)
    batch = prepare_ranking_batch(conn, claim, NOW, limit=10)
    now = NOW
    if loss == "cancelled":
        cancel_ranking_request(conn, request["id"], now)
    elif loss == "wrong_token":
        claim = replace(claim, claim_token="not-the-owner")
    else:
        now += timedelta(seconds=181)

    assert not complete_ranking_batch(conn, claim, _results(batch), now)
    assert not heartbeat_ranking_request(conn, claim, now, lease_seconds=180)
    assert {row[0] for row in conn.execute("SELECT ai_score FROM items")} == {40}
    assert get_ranking_request(conn, request["id"])["processed_count"] == 0


def test_recovery_keeps_atomic_batch_progress_and_fences_old_claims(conn):
    channel_id, _ = _channel(conn)
    request = _queue(conn, channel_id)
    old_claim = _claim(conn)
    first = prepare_ranking_batch(conn, old_claim, NOW, limit=1)
    complete_ranking_batch(conn, old_claim, _results(first), NOW)
    later = NOW + timedelta(seconds=181)

    assert recover_expired_ranking_requests(conn, later) == 1
    new_claim = _claim(conn, later)
    remaining = prepare_ranking_batch(conn, new_claim, later, limit=10)
    assert new_claim.attempt == 2
    assert {item["id"] for item in first}.isdisjoint(item["id"] for item in remaining)
    assert not complete_ranking_batch(conn, old_claim, _results(remaining), later)
    assert not requeue_ranking_request(conn, old_claim, later, failed=False)
    assert complete_ranking_batch(conn, new_claim, _results(remaining), later)
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"], stored["skipped_count"]) == (
        "completed", 2, 0,
    )
    assert len(stored["result"]) == 2


def test_lease_heartbeat_shutdown_and_repeated_interruptions_are_bounded(conn):
    channel_id, _ = _channel(conn)
    request = _queue(conn, channel_id)
    claim = _claim(conn)
    later = NOW + timedelta(seconds=170)
    assert heartbeat_ranking_request(conn, claim, later, lease_seconds=180)
    assert recover_expired_ranking_requests(conn, NOW + timedelta(seconds=200)) == 0
    assert requeue_ranking_request(conn, claim, later, failed=False)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claim = _claim(conn, later)
        assert claim.attempt == attempt
        assert requeue_ranking_request(conn, claim, later, failed=True)
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["error_code"]) == ("failed", "interrupted")
    assert _claim(conn, later) is None


def test_claim_skips_channels_with_other_work_without_blocking_the_queue(conn):
    first, _ = _channel(conn, name="Busy")
    second, _ = _channel(conn, name="Free")
    _queue(conn, first)
    free = _queue(conn, second)

    claim = claim_next_ranking_request(conn, NOW, lease_seconds=180, exclude_channel_ids=[first])
    assert claim.request_id == free["id"]


def test_a_new_preview_takes_priority_over_a_yielded_apply(conn):
    applying, _ = _channel(conn, name="Apply")
    _queue(conn, applying)
    claim = _claim(conn)
    batch = prepare_ranking_batch(conn, claim, NOW, limit=1)
    complete_ranking_batch(conn, claim, _results(batch), NOW)
    requeue_ranking_request(conn, claim, NOW, failed=False)
    previewing, _ = _channel(conn, name="Preview")
    preview = create_ranking_request(
        conn, previewing, mode="preview", profile="draft", now=NOW + timedelta(seconds=1),
    )

    assert _claim(conn, NOW + timedelta(seconds=2)).request_id == preview["id"]


@pytest.mark.parametrize("advance_clock", [False, True])
def test_yielded_applies_take_turns_without_consuming_retry_attempts(conn, advance_clock):
    channels = [_channel(conn, count=3, name=name)[0] for name in ("First", "Second")]
    requests = [_queue(conn, channel_id) for channel_id in channels]
    claimed_ids = []
    for turn in range(4):
        now = NOW + timedelta(seconds=turn if advance_clock else 0)
        claim = _claim(conn, now)
        assert claim.attempt == 1
        claimed_ids.append(claim.request_id)
        batch = prepare_ranking_batch(conn, claim, now, limit=1)
        complete_ranking_batch(conn, claim, _results(batch), now)
        assert requeue_ranking_request(conn, claim, now, failed=False)

    assert claimed_ids == [request["id"] for request in requests] * 2


@pytest.mark.parametrize("skip_first", [False, True])
def test_sweep_deferral_only_holds_progressed_applies_not_previews_or_initial_work(conn, skip_first):
    applying, _ = _channel(conn, count=3, name="Progressed")
    request = _queue(conn, applying)
    claim = _claim(conn)
    if skip_first:
        conn.execute("UPDATE items SET is_read = 1")
        conn.commit()
    batch = prepare_ranking_batch(conn, claim, NOW, limit=1)
    if not skip_first:
        complete_ranking_batch(conn, claim, _results(batch), NOW)
    requeue_ranking_request(conn, claim, NOW, failed=False)
    initial_channel, _ = _channel(conn, name="Initial")
    initial = _queue(conn, initial_channel)
    preview_channel, _ = _channel(conn, name="Preview")
    preview = _queue(conn, preview_channel, mode="preview")
    waiting = [applying, initial_channel, preview_channel]

    assert claim_next_ranking_request(
        conn, NOW, lease_seconds=180, defer_apply_channel_ids=waiting,
    ).request_id == preview["id"]
    assert claim_next_ranking_request(
        conn, NOW, lease_seconds=180, exclude_channel_ids=[initial_channel],
        defer_apply_channel_ids=waiting,
    ) is None
    assert claim_next_ranking_request(
        conn, NOW, lease_seconds=180, defer_apply_channel_ids=waiting,
    ).request_id == initial["id"]
    assert claim_next_ranking_request(
        conn, NOW, lease_seconds=180, defer_apply_channel_ids=waiting,
    ) is None
    resumed = _claim(conn)
    assert resumed.request_id == request["id"]
    assert resumed.attempt == 1
