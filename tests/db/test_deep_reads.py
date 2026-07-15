import threading
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import (DeepRead, claim_deep_read, complete_deep_read_success,
                                    fail_deep_read, get_deep_read, get_deep_reads_for_items,
                                    heartbeat_deep_read, list_pending_deep_reads,
                                    recover_expired_deep_reads, request_deep_read,
                                    requeue_deep_read)
from beehive.db.sources import create_source

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def item_id(conn):
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) "
        "VALUES (?, 't1', 'Title', 'https://x')",
        (source_id,))
    conn.commit()
    return conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]


def _another_item_id(conn, external_id="t2"):
    channel_id = create_channel(conn, f"Channel {external_id}", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "y"})
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) "
        f"VALUES (?, '{external_id}', 'Title', 'https://x')",
        (source_id,))
    conn.commit()
    return conn.execute(
        "SELECT id FROM items WHERE external_id=?", (external_id,)).fetchone()[0]


# -- request_deep_read: first request, reuse, cache, regenerate ------------------------

def test_request_deep_read_creates_pending_row_on_first_call(conn, item_id):
    deep_read = request_deep_read(conn, item_id, T0)
    assert deep_read.item_id == item_id
    assert deep_read.status == "pending"
    assert deep_read.request_version == 1
    assert deep_read.requested_at == T0.isoformat()
    assert deep_read.claim_token is None
    assert deep_read.result_json is None


def test_request_deep_read_reuses_pending_without_changing_request_version(conn, item_id):
    first = request_deep_read(conn, item_id, T0)
    second = request_deep_read(conn, item_id, T0 + timedelta(minutes=1))
    assert second.status == "pending"
    assert second.request_version == 1
    assert second.requested_at == first.requested_at  # untouched by the repeat request


def test_request_deep_read_reuses_processing_ignoring_regenerate(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claim_deep_read(conn, item_id, T0, lease_seconds=60)

    reused = request_deep_read(conn, item_id, T0 + timedelta(minutes=1), regenerate=True)

    assert reused.status == "processing"
    assert reused.request_version == 1  # regenerate on an in-flight attempt is a no-op
    assert reused.claim_token is not None


def test_request_deep_read_reuses_ready_result_as_cache_hit(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    complete_deep_read_success(
        conn, item_id, claimed.request_version, claimed.claim_token,
        result_json='{"summary": "x"}', language_code="en", now=T0)

    cached = request_deep_read(conn, item_id, T0 + timedelta(hours=1))

    assert cached.status == "ready"
    assert cached.result_json == '{"summary": "x"}'
    assert cached.request_version == 1  # not bumped: no regenerate requested


def test_request_deep_read_reuses_failed_without_auto_retry(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    fail_deep_read(conn, item_id, claimed.request_version, claimed.claim_token,
                    error_code="llm_timeout", error_detail="timed out", now=T0)

    reused = request_deep_read(conn, item_id, T0 + timedelta(hours=1))

    assert reused.status == "failed"
    assert reused.error_code == "llm_timeout"
    assert reused.request_version == 1  # no auto-retry without explicit regenerate=True


def test_request_deep_read_regenerate_from_ready_bumps_version_and_clears_terminal_data(
        conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    complete_deep_read_success(
        conn, item_id, claimed.request_version, claimed.claim_token,
        result_json='{"summary": "x"}', language_code="en", warning_code="short_source",
        now=T0)

    regenerated = request_deep_read(
        conn, item_id, T0 + timedelta(hours=1), regenerate=True)

    assert regenerated.status == "pending"
    assert regenerated.request_version == 2
    assert regenerated.result_json is None
    assert regenerated.language_code is None
    assert regenerated.warning_code is None
    assert regenerated.started_at is None
    assert regenerated.completed_at is None
    assert regenerated.requested_at == (T0 + timedelta(hours=1)).isoformat()


def test_request_deep_read_regenerate_from_failed_bumps_version_and_clears_error(
        conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    fail_deep_read(conn, item_id, claimed.request_version, claimed.claim_token,
                    error_code="llm_timeout", error_detail="timed out", now=T0)

    regenerated = request_deep_read(
        conn, item_id, T0 + timedelta(hours=1), regenerate=True)

    assert regenerated.status == "pending"
    assert regenerated.request_version == 2
    assert regenerated.error_code is None
    assert regenerated.error_detail is None


# -- request_deep_read: two-connection concurrency (BEGIN IMMEDIATE race prevention) -----

def _second_connection(db_path):
    other = connect(db_path)
    init_schema(other)
    return other


def test_request_deep_read_concurrent_first_requests_do_not_raise_unique_error(
        tmp_path, conn, item_id):
    """Two connections racing to be the first to request a deep read for the same brand-new
    item must not raise sqlite3.IntegrityError -- whichever loses the BEGIN IMMEDIATE race
    must see the winner's already-committed row instead of attempting a conflicting INSERT."""
    other_conn = _second_connection(str(tmp_path / "test.db"))
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def call(label, connection):
        barrier.wait(timeout=5)
        try:
            results[label] = request_deep_read(connection, item_id, T0)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    t1 = threading.Thread(target=call, args=("a", conn))
    t2 = threading.Thread(target=call, args=("b", other_conn))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    other_conn.close()

    assert errors == []
    assert results["a"].item_id == item_id
    assert results["b"].item_id == item_id
    assert results["a"].request_version == 1
    assert results["b"].request_version == 1
    row_count = conn.execute(
        "SELECT COUNT(*) FROM deep_reads WHERE item_id = ?", (item_id,)).fetchone()[0]
    assert row_count == 1


def test_request_deep_read_concurrent_regenerates_bump_version_exactly_once(
        tmp_path, conn, item_id):
    """Two connections racing to regenerate the same failed row must not double-bump
    request_version (one landing on the pre-fix's now-stale ready/failed read and blindly
    overwriting the other's already-pending row): only one regenerate may actually apply,
    and the loser must see -- and report back -- the winner's fresh pending row instead of
    forcing its own now-outdated decision through."""
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    fail_deep_read(conn, item_id, claimed.request_version, claimed.claim_token,
                    error_code="llm_timeout", error_detail="timed out", now=T0)

    other_conn = _second_connection(str(tmp_path / "test.db"))
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def call(label, connection, now):
        barrier.wait(timeout=5)
        try:
            results[label] = request_deep_read(connection, item_id, now, regenerate=True)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    t1 = threading.Thread(
        target=call, args=("a", conn, T0 + timedelta(hours=1)))
    t2 = threading.Thread(
        target=call, args=("b", other_conn, T0 + timedelta(hours=1, minutes=1)))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    other_conn.close()

    assert errors == []
    # Both callers must agree on the single outcome: exactly one regenerate applied.
    assert results["a"].request_version == 2
    assert results["b"].request_version == 2
    assert results["a"].status == "pending"
    assert results["b"].status == "pending"
    final = get_deep_read(conn, item_id)
    assert final.request_version == 2  # not 3 -- the second caller reused, never re-bumped
    assert final.error_code is None
    assert final.error_detail is None


def test_request_deep_read_regenerate_never_claims_a_row_another_worker_now_owns(
        tmp_path, conn, item_id):
    """Regenerating a failed row and concurrently claiming it (as a worker polling the queue
    would) must never let the claim observe or lock in stale pre-regenerate data: whichever
    order the two threads actually interleave in, a successful claim must always be for the
    fresh post-regenerate request_version, and the row must never end up back in a
    ready/failed state with a claim silently lost."""
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    fail_deep_read(conn, item_id, claimed.request_version, claimed.claim_token,
                    error_code="llm_timeout", error_detail="timed out", now=T0)

    other_conn = _second_connection(str(tmp_path / "test.db"))
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def regenerate():
        barrier.wait(timeout=5)
        try:
            results["regenerate"] = request_deep_read(
                conn, item_id, T0 + timedelta(hours=1), regenerate=True)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    def claim():
        barrier.wait(timeout=5)
        try:
            results["claim"] = claim_deep_read(
                other_conn, item_id, T0 + timedelta(hours=1), lease_seconds=60)
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    t1 = threading.Thread(target=regenerate)
    t2 = threading.Thread(target=claim)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    other_conn.close()

    assert errors == []
    assert results["regenerate"].request_version == 2

    claimed_row = results["claim"]
    if claimed_row is not None:
        # The claim only ever won a race against the *post*-regenerate pending row --
        # never the stale failed row that existed before the regenerate committed.
        assert claimed_row.request_version == 2
        assert claimed_row.status == "processing"

    final = get_deep_read(conn, item_id)
    assert final.request_version == 2
    # Never resets back to a terminal state and never silently drops the claim: either still
    # pending (claim lost the race and hasn't run yet at the time of this assertion is
    # impossible since both threads have joined) or processing (claim won).
    assert final.status in ("pending", "processing")
    if claimed_row is not None:
        assert final.status == "processing"
        assert final.claim_token == claimed_row.claim_token


# -- claim_deep_read: transactional claim with unique token + lease --------------------

def test_claim_deep_read_succeeds_on_pending_and_sets_lease(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=120)
    assert claimed.status == "processing"
    assert claimed.claim_token is not None
    assert claimed.lease_expires_at == (T0 + timedelta(seconds=120)).isoformat()
    assert claimed.started_at == T0.isoformat()


def test_claim_deep_read_returns_none_when_already_claimed(conn, item_id):
    request_deep_read(conn, item_id, T0)
    first = claim_deep_read(conn, item_id, T0, lease_seconds=120)
    assert first is not None

    second = claim_deep_read(conn, item_id, T0 + timedelta(seconds=1), lease_seconds=120)
    assert second is None


def test_claim_deep_read_returns_none_for_missing_row(conn, item_id):
    assert claim_deep_read(conn, item_id, T0, lease_seconds=60) is None


def test_claim_deep_read_tokens_are_unique_across_claims(conn, item_id):
    request_deep_read(conn, item_id, T0)
    first_token = claim_deep_read(conn, item_id, T0, lease_seconds=60).claim_token
    requeue_deep_read(conn, item_id, 1, first_token)
    second_token = claim_deep_read(
        conn, item_id, T0 + timedelta(seconds=1), lease_seconds=60).claim_token
    assert first_token != second_token


def test_claim_deep_read_preserves_started_at_across_a_reclaim(conn, item_id):
    request_deep_read(conn, item_id, T0)
    first = claim_deep_read(conn, item_id, T0, lease_seconds=1)
    recover_expired_deep_reads(conn, T0 + timedelta(seconds=5))
    second = claim_deep_read(conn, item_id, T0 + timedelta(seconds=6), lease_seconds=60)
    assert second.started_at == first.started_at == T0.isoformat()


# -- heartbeat_deep_read: lease extension ------------------------------------------------

def test_heartbeat_extends_lease_for_matching_claim(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=30)

    ok = heartbeat_deep_read(
        conn, item_id, claimed.request_version, claimed.claim_token,
        now=T0 + timedelta(seconds=20), lease_seconds=30)

    assert ok is True
    refreshed = get_deep_read(conn, item_id)
    assert refreshed.lease_expires_at == (T0 + timedelta(seconds=50)).isoformat()


def test_heartbeat_fails_for_wrong_claim_token(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=30)

    ok = heartbeat_deep_read(
        conn, item_id, claimed.request_version, "not-the-real-token",
        now=T0 + timedelta(seconds=5), lease_seconds=30)

    assert ok is False


def test_heartbeat_fails_once_row_is_no_longer_processing(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=30)
    complete_deep_read_success(
        conn, item_id, claimed.request_version, claimed.claim_token,
        result_json="{}", language_code="en", now=T0)

    ok = heartbeat_deep_read(
        conn, item_id, claimed.request_version, claimed.claim_token,
        now=T0 + timedelta(seconds=5), lease_seconds=30)

    assert ok is False


# -- complete_deep_read_success / fail_deep_read: terminal writes guarded by claim ------

def test_complete_success_matches_item_version_and_token(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)

    ok = complete_deep_read_success(
        conn, item_id, claimed.request_version, claimed.claim_token,
        result_json='{"summary": "done"}', language_code="en", warning_code=None, now=T0)

    assert ok is True
    result = get_deep_read(conn, item_id)
    assert result.status == "ready"
    assert result.result_json == '{"summary": "done"}'
    assert result.language_code == "en"
    assert result.claim_token is None
    assert result.lease_expires_at is None
    assert result.completed_at == T0.isoformat()


def test_complete_success_fails_on_claim_token_mismatch(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)

    ok = complete_deep_read_success(
        conn, item_id, claimed.request_version, "wrong-token",
        result_json="{}", language_code="en", now=T0)

    assert ok is False
    assert get_deep_read(conn, item_id).status == "processing"


def test_complete_success_prevents_stale_worker_overwrite_after_regenerate(conn, item_id):
    """A worker claims v1, then loses its lease (recovered), the row is regenerated to v2 and
    reclaimed, and only then does the stale v1 worker finally finish. Its terminal write must
    lose, because it is guarded by request_version -- not just item_id + claim_token."""
    request_deep_read(conn, item_id, T0)
    stale = claim_deep_read(conn, item_id, T0, lease_seconds=1)

    recover_expired_deep_reads(conn, T0 + timedelta(seconds=5))
    fresh_claim = claim_deep_read(conn, item_id, T0 + timedelta(seconds=6), lease_seconds=60)
    complete_deep_read_success(
        conn, item_id, fresh_claim.request_version, fresh_claim.claim_token,
        result_json='{"summary": "fresh"}', language_code="en", now=T0 + timedelta(seconds=6))
    regenerated = request_deep_read(
        conn, item_id, T0 + timedelta(hours=1), regenerate=True)
    reclaimed = claim_deep_read(conn, item_id, T0 + timedelta(hours=1), lease_seconds=60)

    stale_write_ok = complete_deep_read_success(
        conn, item_id, stale.request_version, stale.claim_token,
        result_json='{"summary": "stale"}', language_code="en", now=T0 + timedelta(hours=2))

    assert stale_write_ok is False
    current = get_deep_read(conn, item_id)
    assert current.request_version == regenerated.request_version == 2
    assert current.claim_token == reclaimed.claim_token
    assert current.result_json is None  # stale write never landed
    assert current.status == "processing"


def test_fail_deep_read_records_error_and_matches_claim(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)

    ok = fail_deep_read(
        conn, item_id, claimed.request_version, claimed.claim_token,
        error_code="llm_timeout", error_detail="upstream timed out", now=T0)

    assert ok is True
    result = get_deep_read(conn, item_id)
    assert result.status == "failed"
    assert result.error_code == "llm_timeout"
    assert result.error_detail == "upstream timed out"
    assert result.claim_token is None
    assert result.lease_expires_at is None


def test_fail_deep_read_fails_on_claim_token_mismatch(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=60)

    ok = fail_deep_read(
        conn, item_id, claimed.request_version, "wrong-token",
        error_code="llm_timeout", error_detail="x", now=T0)

    assert ok is False
    assert get_deep_read(conn, item_id).status == "processing"


# -- retry: regenerate after a failure re-enters the pending -> processing -> terminal cycle

def test_retry_after_failure_can_succeed_on_the_next_attempt(conn, item_id):
    request_deep_read(conn, item_id, T0)
    first_claim = claim_deep_read(conn, item_id, T0, lease_seconds=60)
    fail_deep_read(conn, item_id, first_claim.request_version, first_claim.claim_token,
                    error_code="llm_timeout", error_detail="x", now=T0)

    retried = request_deep_read(conn, item_id, T0 + timedelta(minutes=1), regenerate=True)
    assert retried.status == "pending"
    assert retried.request_version == 2

    second_claim = claim_deep_read(
        conn, item_id, T0 + timedelta(minutes=1), lease_seconds=60)
    ok = complete_deep_read_success(
        conn, item_id, second_claim.request_version, second_claim.claim_token,
        result_json='{"summary": "recovered"}', language_code="en",
        now=T0 + timedelta(minutes=2))

    assert ok is True
    final = get_deep_read(conn, item_id)
    assert final.status == "ready"
    assert final.result_json == '{"summary": "recovered"}'


# -- expired processing recovery ---------------------------------------------------------

def test_recover_expired_resets_processing_row_past_its_lease(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claim_deep_read(conn, item_id, T0, lease_seconds=30)

    recovered_count = recover_expired_deep_reads(conn, T0 + timedelta(seconds=31))

    assert recovered_count == 1
    recovered = get_deep_read(conn, item_id)
    assert recovered.status == "pending"
    assert recovered.claim_token is None
    assert recovered.lease_expires_at is None


def test_recover_expired_leaves_live_lease_untouched(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claim_deep_read(conn, item_id, T0, lease_seconds=30)

    recovered_count = recover_expired_deep_reads(conn, T0 + timedelta(seconds=10))

    assert recovered_count == 0
    assert get_deep_read(conn, item_id).status == "processing"


def test_recover_expired_only_touches_processing_rows(conn, item_id):
    request_deep_read(conn, item_id, T0)  # still pending, no lease at all

    recovered_count = recover_expired_deep_reads(conn, T0 + timedelta(days=1))

    assert recovered_count == 0
    assert get_deep_read(conn, item_id).status == "pending"


# -- get_deep_read: cache lookup ---------------------------------------------------------

def test_get_deep_read_returns_none_when_never_requested(conn, item_id):
    assert get_deep_read(conn, item_id) is None


def test_get_deep_read_reflects_current_state(conn, item_id):
    request_deep_read(conn, item_id, T0)
    assert get_deep_read(conn, item_id).status == "pending"


# -- get_deep_reads_for_items: batch cache lookup for list views ------------------------

def test_get_deep_reads_for_items_returns_empty_dict_for_empty_input(conn, item_id):
    request_deep_read(conn, item_id, T0)
    assert get_deep_reads_for_items(conn, []) == {}


def test_get_deep_reads_for_items_issues_no_query_for_empty_input(conn, item_id):
    statements = []
    conn.set_trace_callback(statements.append)

    get_deep_reads_for_items(conn, [])

    conn.set_trace_callback(None)
    assert statements == []


def test_get_deep_reads_for_items_maps_each_id_to_its_deep_read(conn, item_id):
    other_item_id = _another_item_id(conn, "t2")
    request_deep_read(conn, item_id, T0)
    request_deep_read(conn, other_item_id, T0 + timedelta(minutes=1))
    claim_deep_read(conn, other_item_id, T0, lease_seconds=60)

    by_item = get_deep_reads_for_items(conn, [item_id, other_item_id])

    assert set(by_item) == {item_id, other_item_id}
    assert by_item[item_id].status == "pending"
    assert by_item[other_item_id].status == "processing"


def test_get_deep_reads_for_items_omits_ids_with_no_row(conn, item_id):
    other_item_id = _another_item_id(conn, "t2")  # never requested
    request_deep_read(conn, item_id, T0)

    by_item = get_deep_reads_for_items(conn, [item_id, other_item_id])

    assert set(by_item) == {item_id}


def test_get_deep_reads_for_items_returns_validated_deep_read_instances(conn, item_id):
    request_deep_read(conn, item_id, T0)
    by_item = get_deep_reads_for_items(conn, [item_id])
    assert isinstance(by_item[item_id], DeepRead)


def test_get_deep_reads_for_items_issues_exactly_one_query_for_a_batch(conn, item_id):
    other_item_id = _another_item_id(conn, "t2")
    request_deep_read(conn, item_id, T0)
    request_deep_read(conn, other_item_id, T0)

    statements = []
    conn.set_trace_callback(statements.append)

    get_deep_reads_for_items(conn, [item_id, other_item_id])

    conn.set_trace_callback(None)
    assert len(statements) == 1
    assert "deep_reads" in statements[0]
    assert f"IN ({item_id}, {other_item_id})" in statements[0]


# -- list_pending_deep_reads / claim: worker queue ---------------------------------------

def test_list_pending_deep_reads_returns_oldest_first(conn, item_id):
    other_item_id = _another_item_id(conn, "t2")
    request_deep_read(conn, other_item_id, T0 + timedelta(minutes=1))
    request_deep_read(conn, item_id, T0)

    pending = list_pending_deep_reads(conn)

    assert [p.item_id for p in pending] == [item_id, other_item_id]


def test_list_pending_deep_reads_excludes_processing_and_terminal_rows(conn, item_id):
    other_item_id = _another_item_id(conn, "t2")
    request_deep_read(conn, item_id, T0)
    claim_deep_read(conn, item_id, T0, lease_seconds=60)  # now processing
    request_deep_read(conn, other_item_id, T0)

    pending = list_pending_deep_reads(conn)

    assert [p.item_id for p in pending] == [other_item_id]


def test_list_pending_deep_reads_excludes_expired_processing_until_recovered(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claim_deep_read(conn, item_id, T0, lease_seconds=1)

    still_hidden = list_pending_deep_reads(conn)
    assert still_hidden == []

    recover_expired_deep_reads(conn, T0 + timedelta(seconds=5))
    now_visible = list_pending_deep_reads(conn)
    assert [p.item_id for p in now_visible] == [item_id]


def test_list_pending_deep_reads_respects_limit(conn, item_id):
    other_item_id = _another_item_id(conn, "t2")
    request_deep_read(conn, item_id, T0)
    request_deep_read(conn, other_item_id, T0 + timedelta(minutes=1))

    assert len(list_pending_deep_reads(conn, limit=1)) == 1


# -- requeue_deep_read: voluntarily give back an active claim ----------------------------

def test_requeue_deep_read_returns_active_claim_to_pending(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=120)

    ok = requeue_deep_read(conn, item_id, claimed.request_version, claimed.claim_token)

    assert ok is True
    requeued = get_deep_read(conn, item_id)
    assert requeued.status == "pending"
    assert requeued.claim_token is None
    assert requeued.lease_expires_at is None
    assert [p.item_id for p in list_pending_deep_reads(conn)] == [item_id]


def test_requeue_deep_read_fails_on_claim_token_mismatch(conn, item_id):
    request_deep_read(conn, item_id, T0)
    claimed = claim_deep_read(conn, item_id, T0, lease_seconds=120)

    ok = requeue_deep_read(conn, item_id, claimed.request_version, "wrong-token")

    assert ok is False
    assert get_deep_read(conn, item_id).status == "processing"


def test_requeue_deep_read_fails_when_not_processing(conn, item_id):
    request_deep_read(conn, item_id, T0)  # still pending, never claimed

    ok = requeue_deep_read(conn, item_id, 1, "any-token")

    assert ok is False


# -- DeepRead validation -------------------------------------------------------------------

def test_deep_read_rejects_an_unrecognized_status():
    with pytest.raises(ValueError):
        DeepRead(item_id=1, status="bogus", request_version=1, claim_token=None,
                  lease_expires_at=None, result_json=None, language_code=None,
                  warning_code=None, error_code=None, error_detail=None,
                  requested_at="2026-01-01T00:00:00+00:00", started_at=None,
                  completed_at=None)


def test_deep_read_rejects_a_non_positive_request_version():
    with pytest.raises(ValueError):
        DeepRead(item_id=1, status="pending", request_version=0, claim_token=None,
                  lease_expires_at=None, result_json=None, language_code=None,
                  warning_code=None, error_code=None, error_detail=None,
                  requested_at="2026-01-01T00:00:00+00:00", started_at=None,
                  completed_at=None)
