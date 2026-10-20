# tests/db/test_civic_consumed_items.py
"""Consumed-item fencing: one source item belongs to one Civic Candidate per Watch, forever.

This is the invariant that stops clustering from producing a second Candidate for a development
it has already recorded. `civic_candidates.dedupe_key` cannot do it: clustering is many-to-one, so
a resumed Run that re-grouped the same articles would arrive at a *different* cluster key and a
brand new Candidate that the dedupe_key unique index would happily accept.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_batches import get_or_create_batch_if_claimed
from beehive.db.civic_candidates import create_candidate
from beehive.db.civic_consumed_items import (claim_consumed_item, get_consuming_candidate_id,
                                             list_candidate_items, list_consumed_item_keys,
                                             list_run_items)
from beehive.db.civic_runs import claim_civic_run, enqueue_civic_run
from beehive.db.civic_watches import create_civic_watch, hard_delete_civic_watch
from beehive.db.connection import connect, init_schema

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def context(conn):
    watch_id = create_civic_watch(conn, "NZ election", "Q", T0).id
    run = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    return watch_id, run.id, batch


def _candidate(conn, batch, key="dk-1"):
    return create_candidate(conn, batch.id, key, "Title", "Statement", T0)


def _claim(conn, watch_id, run_id, candidate_id, item_key="src:1", **overrides):
    source_key, _, external_key = item_key.partition(":")
    kwargs = dict(
        source_key=source_key, external_key=external_key or "1",
        canonical_url=f"https://example.test/{item_key}", now=T0)
    kwargs.update(overrides)
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = claim_consumed_item(
            conn, watch_id=watch_id, run_id=run_id, candidate_id=candidate_id,
            item_key=item_key, **kwargs)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return item


# ============================================================================
# Claiming
# ============================================================================


def test_claiming_an_item_records_its_owner(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    item = _claim(conn, watch_id, run_id, candidate.id)
    assert item.candidate_id == candidate.id
    assert item.item_key == "src:1"
    assert item.source_key == "src"
    assert item.external_key == "1"
    assert item.consumed_at == T0


def test_reclaiming_the_same_item_for_the_same_candidate_is_a_no_op(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    first = _claim(conn, watch_id, run_id, candidate.id)
    second = _claim(conn, watch_id, run_id, candidate.id)
    assert first.id == second.id
    assert len(list_candidate_items(conn, candidate.id)) == 1


def test_a_second_candidate_cannot_take_an_item_that_is_already_owned(conn, context):
    watch_id, run_id, batch = context
    first = _candidate(conn, batch, "dk-1")
    second = _candidate(conn, batch, "dk-2")
    _claim(conn, watch_id, run_id, first.id)
    with pytest.raises(ValueError, match="already consumed by Civic Candidate"):
        _claim(conn, watch_id, run_id, second.id)
    assert get_consuming_candidate_id(conn, watch_id, "src:1") == first.id


def test_the_same_item_key_may_be_consumed_by_a_different_watch(conn, context):
    """The fence is per Watch: two Watches monitoring different subjects legitimately read the
    same feed, and neither should be able to starve the other of an article."""
    watch_id, run_id, batch = context
    first = _candidate(conn, batch)
    _claim(conn, watch_id, run_id, first.id)

    other_watch = create_civic_watch(conn, "Local body", "Q", T0).id
    other_run = enqueue_civic_run(conn, other_watch, T0, T0)
    other_lease = claim_civic_run(conn, other_run.id, T0, 600, 3600)
    other_batch = get_or_create_batch_if_claimed(
        conn, other_run.id, other_lease.run.claim_token, T0)
    other_candidate = create_candidate(conn, other_batch.id, "dk-1", "T", "S", T0)
    item = _claim(conn, other_watch, other_run.id, other_candidate.id)
    assert item.candidate_id == other_candidate.id


def test_the_unique_index_holds_against_raw_sql(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    _claim(conn, watch_id, run_id, candidate.id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO civic_consumed_items (watch_id, run_id, candidate_id, item_key, "
            "source_key, external_key, canonical_url, consumed_at) VALUES (?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (watch_id, run_id, candidate.id, "src:1", "src", "1", "https://x.test",
             T0.isoformat()))


# ============================================================================
# Reading
# ============================================================================


def test_consumed_keys_come_back_sorted_for_clustering(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    _claim(conn, watch_id, run_id, candidate.id, item_key="src:9")
    _claim(conn, watch_id, run_id, candidate.id, item_key="src:2")
    assert list_consumed_item_keys(conn, watch_id) == ["src:2", "src:9"]


def test_run_items_are_scoped_to_their_run(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    _claim(conn, watch_id, run_id, candidate.id)
    assert [item.item_key for item in list_run_items(conn, run_id)] == ["src:1"]

    other_watch = create_civic_watch(conn, "Local body", "Q", T0).id
    later = enqueue_civic_run(
        conn, other_watch, T0 + timedelta(days=1), T0 + timedelta(days=1))
    assert list_run_items(conn, later.id) == []


def test_an_unconsumed_item_has_no_owner(conn, context):
    watch_id, _run_id, _batch = context
    assert get_consuming_candidate_id(conn, watch_id, "src:absent") is None


# ============================================================================
# Validation and lifecycle
# ============================================================================


@pytest.mark.parametrize("field", ["source_key", "external_key"])
def test_an_empty_identifier_is_rejected(conn, context, field):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    with pytest.raises(ValueError, match="needs an? "):
        _claim(conn, watch_id, run_id, candidate.id, **{field: "  "})


def test_an_empty_item_key_is_rejected(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    with pytest.raises(ValueError, match="needs an item_key"):
        _claim(conn, watch_id, run_id, candidate.id, item_key="  ", source_key="src",
               external_key="1")


def test_claims_cascade_away_with_the_watch(conn, context):
    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    _claim(conn, watch_id, run_id, candidate.id)
    hard_delete_civic_watch(conn, watch_id)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_consumed_items").fetchone()["n"] == 0


def test_a_claim_survives_the_candidate_being_rejected(conn, context):
    """Consumption is never released. Re-reading the same article tomorrow reaches the same
    rejection, so re-running it would only spend budget to arrive there again."""
    from beehive.db.civic_candidates import reject_candidate

    watch_id, run_id, batch = context
    candidate = _candidate(conn, batch)
    _claim(conn, watch_id, run_id, candidate.id)
    reject_candidate(conn, candidate.id, "off_topic", T0)
    assert list_consumed_item_keys(conn, watch_id) == ["src:1"]
