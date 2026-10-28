import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from beehive.ai.prompt_builder import ItemCandidate, ProductCandidate
from beehive.ai.response_parser import RankedItem, ResponseParseError
from beehive.collector.ranking_worker import process_claimed_ranking_request
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.item_events import (
    mark_events_delivered,
    mark_item_events_ready,
    record_or_coalesce_event,
    suppress_item_events,
)
from beehive.db.items import insert_new_returning_id, update_ai_ranking_by_id
from beehive.db.ranking_requests import (
    cancel_ranking_request,
    claim_next_ranking_request,
    create_ranking_request,
    get_ranking_request,
    recover_expired_ranking_requests,
    requeue_ranking_request,
)
from beehive.db.sources import create_source
from beehive.db.votes import upsert_vote
from beehive.localization import localizer_for

NOW = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)
LOCALIZER = localizer_for("ja")


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "ranking.db"))
    init_schema(connection)
    yield connection
    connection.close()


def _setup(conn, *, kind="editorial", count=2):
    channel_id = create_channel(conn, "Channel", "saved rules", kind=kind, minimum_score=50)
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
                    url=f"https://example.com/{index}",
                    raw_metadata={"score": 25, "num_comments": 4, "price": 99,
                                  "available": True, "listing_kind": "auction_lot",
                                  "estimated_cost": 120, "buyer_premium_rate": 0.15}),
        )
        update_ai_ranking_by_id(conn, item_id, 40, "Original summary", "Original reason")
        conn.execute(
            "UPDATE items SET opened_at = ?, best_comment_summary = 'Kept comment', "
            "fetched_at = ?, last_seen_at = ? WHERE id = ?",
            (NOW.isoformat(), (NOW + timedelta(seconds=index)).isoformat(), NOW.isoformat(), item_id),
        )
        conn.commit()
        ids.append(item_id)
    upsert_vote(conn, ids[0], 1, "Useful")
    conn.execute(
        "INSERT INTO auction_watches (item_id, watched_at) VALUES (?, ?)",
        (ids[0], NOW.isoformat()),
    )
    conn.commit()
    return channel_id, ids


def _events(conn, item_id):
    delivered = record_or_coalesce_event(conn, item_id, "discovered", {}, NOW.isoformat())
    mark_item_events_ready(conn, item_id, NOW.isoformat())
    mark_events_delivered(conn, [delivered], NOW.isoformat())
    suppressed = record_or_coalesce_event(conn, item_id, "discovered", {}, NOW.isoformat())
    suppress_item_events(conn, item_id, NOW.isoformat())
    pending = record_or_coalesce_event(conn, item_id, "discovered", {}, NOW.isoformat())
    return delivered, suppressed, pending


def _domain_state(conn):
    return {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in (
            "channels", "sources", "items", "votes", "auction_watches", "item_events",
            "email_groups", "email_group_channels", "deep_reads",
        )
    }


def _request(conn, channel_id, *, mode="apply", profile="saved rules", now=NOW):
    request = create_ranking_request(conn, channel_id, mode=mode, profile=profile, now=now)
    claim = claim_next_ranking_request(conn, now, lease_seconds=180)
    return request, claim


def _ranked(candidates, score=90):
    return [
        RankedItem(item_key=item.item_key, score=score, summary="New summary", rationale="New reason")
        for item in candidates
    ]


def _ranker(monkeypatch, kind="editorial", *, callback=None, score=90):
    fake = AsyncMock(side_effect=callback or (lambda **kwargs: _ranked(kwargs["candidates"], score)))
    entrypoint = "rank_channel" if kind == "editorial" else "rank_monitor_channel"
    monkeypatch.setattr(f"beehive.collector.ranking_worker.{entrypoint}", fake)
    return fake


async def _process(conn, claim, now=NOW, *, max_batches=None):
    await process_claimed_ranking_request(
        conn, claim, localizer=LOCALIZER, model="offline-test-model", clock=lambda: now,
        max_batches=max_batches,
    )


async def test_preview_uses_draft_rules_and_vote_context_without_any_domain_mutations(conn, monkeypatch):
    channel_id, ids = _setup(conn, count=7)
    _events(conn, ids[0])
    before = _domain_state(conn)
    request, claim = _request(conn, channel_id, mode="preview", profile="Draft interests")
    fake = _ranker(monkeypatch)

    await _process(conn, claim)

    assert _domain_state(conn) == before
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["total_count"], stored["processed_count"]) == ("completed", 5, 5)
    assert [row["item_id"] for row in stored["result"]] == list(reversed(ids))[:5]
    assert set(stored["result"][0]) == {
        "item_id", "title", "url", "previous_score", "previous_summary", "score", "summary", "rationale",
    }
    kwargs = fake.await_args.kwargs
    assert kwargs["profile"] == "Draft interests"
    assert kwargs["language"] == LOCALIZER.language
    assert kwargs["model"] == "offline-test-model"
    assert [(vote.title, vote.value, vote.reason) for vote in kwargs["votes"]] == [
        ("Item 0", 1, "Useful"),
    ]
    assert all(isinstance(item, ItemCandidate) for item in kwargs["candidates"])
    assert kwargs["candidates"][0].score == 25
    assert kwargs["candidates"][0].num_comments == 4


@pytest.mark.parametrize("kind", ["monitor", "tracker"])
async def test_listing_preview_uses_the_same_product_inputs_without_editorial_votes(
    conn, monkeypatch, kind,
):
    channel_id, _ = _setup(conn, kind=kind)
    request, claim = _request(conn, channel_id, mode="preview", profile="Draft shopping rules")
    fake = _ranker(monkeypatch, kind)

    await _process(conn, claim)

    assert get_ranking_request(conn, request["id"])["processed_count"] == 2
    kwargs = fake.await_args.kwargs
    assert "votes" not in kwargs
    candidate = kwargs["candidates"][0]
    assert isinstance(candidate, ProductCandidate)
    assert (candidate.price, candidate.estimated_cost, candidate.buyer_premium_rate) == (99, 120, 0.15)


@pytest.mark.parametrize("kind", ["editorial", "monitor", "tracker"])
@pytest.mark.parametrize("score", [20, 90])
async def test_apply_only_changes_ai_fields_and_still_open_events(conn, monkeypatch, kind, score):
    channel_id, ids = _setup(conn, kind=kind, count=3)
    delivered, suppressed, pending = _events(conn, ids[0])
    if kind == "editorial":
        conn.execute("UPDATE items SET is_read = 1 WHERE id = ?", (ids[2],))
    else:
        conn.execute("UPDATE items SET inactive_at = ? WHERE id = ?", (NOW.isoformat(), ids[2]))
    conn.commit()
    before = _domain_state(conn)
    request, claim = _request(conn, channel_id)
    _ranker(monkeypatch, kind, score=score)

    await _process(conn, claim)

    after = _domain_state(conn)
    for table in before.keys() - {"items", "item_events"}:
        assert after[table] == before[table]
    for old, new in zip(before["items"], after["items"], strict=True):
        if old["id"] in ids[:2]:
            expected = {**old, "ai_score": score, "ai_summary": "New summary", "ai_rationale": "New reason"}
        else:
            expected = old
        assert new == expected
    assert len(after["item_events"]) == 3  # No backfill for the other existing item.
    by_id = {event["id"]: event for event in after["item_events"]}
    original = {event["id"]: event for event in before["item_events"]}
    assert by_id[delivered] == original[delivered]
    assert by_id[suppressed] == original[suppressed]
    assert by_id[pending]["delivered_at"] is None
    assert by_id[pending]["ready_at"] == (NOW.isoformat() if score >= 50 else None)
    assert by_id[pending]["suppressed_at"] == (NOW.isoformat() if score < 50 else None)
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"], stored["skipped_count"]) == ("completed", 2, 0)
    assert all(row["previous_score"] == 40 for row in stored["result"])


@pytest.mark.parametrize("change", ["rules", "content", "read", "cancel", "lost_claim"])
async def test_web_changes_during_model_work_cannot_be_overwritten(conn, monkeypatch, change):
    channel_id, _ = _setup(conn, count=1)
    request, claim = _request(conn, channel_id)
    other = connect(conn.execute("PRAGMA database_list").fetchone()["file"])

    def finish_with_intervening_write(**kwargs):
        assert not conn.in_transaction
        if change == "rules":
            other.execute("UPDATE channels SET profile = 'New saved rules'")
        elif change == "content":
            other.execute("UPDATE items SET body = 'Fresh content'")
        elif change == "read":
            other.execute("UPDATE items SET is_read = 1")
        elif change == "cancel":
            cancel_ranking_request(other, request["id"], NOW)
        else:
            requeue_ranking_request(other, claim, NOW, failed=False)
            claim_next_ranking_request(other, NOW, lease_seconds=180)
        other.commit()
        return _ranked(kwargs["candidates"])

    _ranker(monkeypatch, callback=finish_with_intervening_write)
    try:
        await _process(conn, claim)
    finally:
        other.close()

    stored = get_ranking_request(conn, request["id"])
    assert stored["processed_count"] == 0
    assert conn.execute("SELECT ai_score FROM items").fetchone()[0] == 40
    if change == "rules":
        assert (stored["status"], stored["error_code"]) == ("failed", "rules_changed")
    elif change in ("content", "read"):
        assert (stored["status"], stored["skipped_count"]) == ("completed", 1)
    elif change == "cancel":
        assert stored["status"] == "cancelled"
    else:
        assert stored["status"] == "running"


async def test_model_failure_leaves_previous_scores_and_completed_batches_usable(conn, monkeypatch):
    channel_id, _ = _setup(conn, count=12)
    request, claim = _request(conn, channel_id)
    calls = 0

    def partial_failure(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("offline model failure")
        return _ranked(kwargs["candidates"])

    _ranker(monkeypatch, callback=partial_failure)
    with pytest.raises(RuntimeError, match="offline model failure"):
        await _process(conn, claim)

    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["error_code"], stored["processed_count"]) == (
        "failed", "ranking_failed", 10,
    )
    assert calls == 3  # The second batch gets the ordinary ranking retry budget.
    scores = [row[0] for row in conn.execute("SELECT ai_score FROM items")]
    assert scores.count(90) == 10
    assert scores.count(40) == 2


async def test_cancellation_keeps_completed_batches_but_prevents_the_next_write(conn, monkeypatch):
    channel_id, _ = _setup(conn, count=12)
    request, claim = _request(conn, channel_id)
    calls = 0

    def cancel_during_second_batch(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            cancel_ranking_request(conn, request["id"], NOW)
        return _ranked(kwargs["candidates"])

    _ranker(monkeypatch, callback=cancel_during_second_batch)
    await _process(conn, claim)

    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"], stored["total_count"]) == ("cancelled", 10, 12)
    scores = [row[0] for row in conn.execute("SELECT ai_score FROM items")]
    assert scores.count(90) == 10
    assert scores.count(40) == 2


async def test_a_batch_of_stale_candidates_yields_without_ai_or_retry_cost(conn, monkeypatch):
    channel_id, ids = _setup(conn, count=12)
    request, claim = _request(conn, channel_id)
    conn.executemany("UPDATE items SET is_read = 1 WHERE id = ?", [(item_id,) for item_id in ids[2:]])
    conn.commit()
    fake = _ranker(monkeypatch)

    await _process(conn, claim, max_batches=1)

    partial = get_ranking_request(conn, request["id"])
    assert (partial["status"], partial["processed_count"], partial["skipped_count"]) == ("queued", 0, 10)
    fake.assert_not_awaited()
    next_claim = claim_next_ranking_request(conn, NOW, lease_seconds=180)
    assert next_claim.attempt == 1
    await _process(conn, next_claim, max_batches=1)
    complete = get_ranking_request(conn, request["id"])
    assert (complete["status"], complete["processed_count"], complete["skipped_count"]) == (
        "completed", 2, 10,
    )
    assert {int(item.item_key) for item in fake.await_args.kwargs["candidates"]} == set(ids[:2])


async def test_cancelling_between_slices_fences_the_old_claim_and_remaining_work(conn, monkeypatch):
    channel_id, _ = _setup(conn, count=12)
    request, claim = _request(conn, channel_id)
    fake = _ranker(monkeypatch)
    await _process(conn, claim, max_batches=1)
    assert get_ranking_request(conn, request["id"])["status"] == "queued"

    assert cancel_ranking_request(conn, request["id"], NOW)
    await _process(conn, claim, max_batches=1)

    assert claim_next_ranking_request(conn, NOW, lease_seconds=180) is None
    assert fake.await_count == 1
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"]) == ("cancelled", 10)
    scores = [row[0] for row in conn.execute("SELECT ai_score FROM items")]
    assert scores.count(90) == 10
    assert scores.count(40) == 2


async def test_cutoff_changes_do_not_invalidate_rules_and_open_events_use_the_current_cutoff(
    conn, monkeypatch,
):
    channel_id, ids = _setup(conn)
    _, _, pending = _events(conn, ids[0])
    request, claim = _request(conn, channel_id)

    def change_delivery_filter(**kwargs):
        conn.execute("UPDATE channels SET minimum_score = 95")
        conn.commit()
        return _ranked(kwargs["candidates"], score=90)

    _ranker(monkeypatch, callback=change_delivery_filter)
    await _process(conn, claim)

    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["error_code"], stored["processed_count"]) == ("completed", None, 2)
    event = conn.execute("SELECT * FROM item_events WHERE id = ?", (pending,)).fetchone()
    assert (event["suppressed_at"], event["delivered_at"]) == (NOW.isoformat(), None)


async def test_incomplete_or_duplicate_model_output_cannot_partially_apply_a_batch(conn, monkeypatch):
    channel_id, _ = _setup(conn)
    request, claim = _request(conn, channel_id)
    _ranker(monkeypatch, callback=lambda **kwargs: _ranked([kwargs["candidates"][0]]) * 2)

    with pytest.raises(ResponseParseError, match="exactly one"):
        await _process(conn, claim)

    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"]) == ("failed", 0)
    assert {row[0] for row in conn.execute("SELECT ai_score FROM items")} == {40}


async def test_item_updates_events_and_progress_roll_back_together_on_a_batch_write_failure(
    conn, monkeypatch,
):
    channel_id, ids = _setup(conn)
    for item_id in ids:
        _events(conn, item_id)
    before = _domain_state(conn)
    request, claim = _request(conn, channel_id)
    _ranker(monkeypatch)
    calls = 0

    def fail_second_event_write(connection, item_id, ready_at):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("event persistence failed")
        return mark_item_events_ready(connection, item_id, ready_at)

    monkeypatch.setattr(
        "beehive.db.ranking_requests.mark_item_events_ready", fail_second_event_write,
    )
    with pytest.raises(RuntimeError, match="event persistence failed"):
        await _process(conn, claim)

    assert _domain_state(conn) == before
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"], stored["result"]) == ("failed", 0, [])


async def test_crash_recovery_resumes_only_unfinished_snapshot_candidates(conn, monkeypatch):
    channel_id, ids = _setup(conn, count=12)
    request, claim = _request(conn, channel_id)
    calls = []

    def crash_in_second_batch(**kwargs):
        calls.append([int(item.item_key) for item in kwargs["candidates"]])
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return _ranked(kwargs["candidates"])

    _ranker(monkeypatch, callback=crash_in_second_batch)
    with pytest.raises(asyncio.CancelledError):
        await _process(conn, claim)
    assert get_ranking_request(conn, request["id"])["processed_count"] == 10
    later = NOW + timedelta(seconds=181)
    recover_expired_ranking_requests(conn, later)
    next_claim = claim_next_ranking_request(conn, later, lease_seconds=180)
    fake = _ranker(monkeypatch)

    await _process(conn, next_claim, later)

    assert {int(item.item_key) for item in fake.await_args.kwargs["candidates"]} == set(ids[:2])
    stored = get_ranking_request(conn, request["id"])
    assert (stored["status"], stored["processed_count"], len(stored["result"])) == ("completed", 12, 12)
