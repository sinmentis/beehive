import json

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.sources import (create_source, delete_source, find_duplicate_source, get_source,
                                    list_by_channel, record_fetch_error, record_fetch_success,
                                    reset_fetch_state_by_channel, set_source_paused,
                                    update_source)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def channel_id(conn):
    return create_channel(conn, "NZ Finance", "economic news")


def test_create_source_rejects_a_type_incompatible_with_the_channel_kind(conn):
    """The persistence safety seam: an editorial-only Source type cannot be attached to a
    non-editorial Channel, and nothing is inserted when it is refused."""
    monitor_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    with pytest.raises(ValueError, match="not compatible with a 'monitor' Channel"):
        create_source(conn, monitor_id, "reddit_subreddit", {"subreddit": "x"})
    assert list_by_channel(conn, monitor_id) == []


def test_create_source_rejects_a_monitor_type_on_an_editorial_channel(conn, channel_id):
    with pytest.raises(ValueError, match="not compatible with a 'editorial' Channel"):
        create_source(
            conn, channel_id, "shopify_collection",
            {"collection_url": "https://a/collections/x"})
    assert list_by_channel(conn, channel_id) == []


def test_create_source_accepts_a_tracker_type_on_a_tracker_channel(conn):
    tracker_id = create_channel(conn, "Auctions", "lots", kind="tracker")
    create_source(conn, tracker_id, "all_about_auctions", {})
    assert [s["type"] for s in list_by_channel(conn, tracker_id)] == ["all_about_auctions"]


def test_create_source_raises_for_a_missing_channel(conn):
    with pytest.raises(ValueError, match="Channel 9999 does not exist"):
        create_source(conn, 9999, "reddit_subreddit", {"subreddit": "x"})


def test_create_source_raises_for_an_unknown_source_type(conn, channel_id):
    with pytest.raises(ValueError, match="unknown Source type"):
        create_source(conn, channel_id, "bogus_type", {})
    assert list_by_channel(conn, channel_id) == []


def test_create_and_list_source(conn, channel_id):
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    sources = list_by_channel(conn, channel_id)
    assert len(sources) == 1
    assert sources[0]["type"] == "reddit_subreddit"
    assert json.loads(sources[0]["config"])["subreddit"] == "PersonalFinanceNZ"



def test_record_fetch_success_clears_error(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_error(conn, source_id, "boom", "2026-07-09T00:00:00")
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00")
    row = list_by_channel(conn, channel_id)[0]
    assert row["last_fetch_error"] is None
    assert row["last_fetch_at"] == "2026-07-09T03:00:00"


def test_record_fetch_error_keeps_last_fetch_at(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T00:00:00")
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T03:00:00")
    row = list_by_channel(conn, channel_id)[0]
    assert row["last_fetch_error"] == "timeout"
    assert row["last_fetch_at"] == "2026-07-09T00:00:00"


def test_delete_source_removes_it(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    delete_source(conn, source_id)
    assert list_by_channel(conn, channel_id) == []


def test_delete_source_cascades_to_items(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="T", url="https://x"))
    delete_source(conn, source_id)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_get_source_returns_row(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    source = get_source(conn, source_id)
    assert source["id"] == source_id
    assert source["channel_id"] == channel_id
    assert source["type"] == "reddit_subreddit"


def test_get_source_returns_none_for_missing_id(conn):
    assert get_source(conn, 999) is None


def test_record_fetch_success_stores_raw_and_new_counts(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00", raw_count=50, new_count=20)
    row = list_by_channel(conn, channel_id)[0]
    assert row["last_fetch_raw_count"] == 50
    assert row["last_fetch_new_count"] == 20


def test_reset_fetch_state_by_channel_clears_all_fetch_bookkeeping(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00", raw_count=50, new_count=20)
    record_fetch_error(conn, source_id, "boom", "2026-07-09T06:00:00")

    reset_fetch_state_by_channel(conn, channel_id)

    row = list_by_channel(conn, channel_id)[0]
    assert row["last_fetch_at"] is None
    assert row["last_fetch_error"] is None
    assert row["last_fetch_raw_count"] is None
    assert row["last_fetch_new_count"] is None


def test_reset_fetch_state_by_channel_does_not_affect_other_channels(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00", raw_count=50, new_count=20)
    other_channel_id = create_channel(conn, "Other Channel", "profile")
    other_source_id = create_source(conn, other_channel_id, "reddit_subreddit", {"subreddit": "y"})
    record_fetch_success(conn, other_source_id, "2026-07-09T03:00:00", raw_count=5, new_count=1)

    reset_fetch_state_by_channel(conn, channel_id)

    other_row = list_by_channel(conn, other_channel_id)[0]
    assert other_row["last_fetch_at"] == "2026-07-09T03:00:00"
    assert other_row["last_fetch_raw_count"] == 5


def test_create_source_persists_optional_display_name(conn, channel_id):
    source_id = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"}, name="Personal Finance")
    assert get_source(conn, source_id)["name"] == "Personal Finance"


def test_create_source_blank_name_is_stored_as_null(conn, channel_id):
    source_id = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"}, name="   ")
    assert get_source(conn, source_id)["name"] is None


def test_update_source_replaces_type_config_and_name(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "old"})
    update_source(
        conn, source_id, "google_news_query", {"query": "kiwis"}, name="Kiwi news")
    row = get_source(conn, source_id)
    assert row["type"] == "google_news_query"
    assert json.loads(row["config"])["query"] == "kiwis"
    assert row["name"] == "Kiwi news"


def test_update_source_leaves_fetch_bookkeeping_untouched(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "old"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00", raw_count=9, new_count=2)
    update_source(conn, source_id, "reddit_subreddit", {"subreddit": "new"})
    row = get_source(conn, source_id)
    assert row["last_fetch_at"] == "2026-07-09T03:00:00"
    assert row["last_fetch_raw_count"] == 9


def test_update_source_rejects_type_incompatible_with_channel_kind(conn):
    monitor_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    source_id = create_source(
        conn, monitor_id, "shopify_collection", {"collection_url": "https://a/collections/x"})
    with pytest.raises(ValueError, match="not compatible with a 'monitor' Channel"):
        update_source(conn, source_id, "reddit_subreddit", {"subreddit": "x"})
    # The original type is preserved when the update is refused.
    assert get_source(conn, source_id)["type"] == "shopify_collection"


def test_update_source_raises_for_missing_source(conn):
    with pytest.raises(ValueError, match="Source 999 does not exist"):
        update_source(conn, 999, "reddit_subreddit", {"subreddit": "x"})


def test_find_duplicate_source_matches_same_type_and_config(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    assert find_duplicate_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"}) == source_id


def test_find_duplicate_source_is_key_order_independent(conn):
    monitor_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    source_id = create_source(
        conn, monitor_id, "shopify_collection",
        {"collection_url": "https://s/collections/x", "vendors": ["A", "B"]})
    # Same keys, different insertion order -- still the same Source.
    assert find_duplicate_source(
        conn, monitor_id, "shopify_collection",
        {"vendors": ["A", "B"], "collection_url": "https://s/collections/x"}) == source_id


def test_find_duplicate_source_returns_none_for_different_config(conn, channel_id):
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    assert find_duplicate_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "y"}) is None


def test_find_duplicate_source_excludes_the_edited_row(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    # Re-saving the same Source unchanged is not a duplicate of itself.
    assert find_duplicate_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"},
        exclude_source_id=source_id) is None


def test_find_duplicate_source_is_scoped_to_one_channel(conn, channel_id):
    other_channel_id = create_channel(conn, "Other", "profile")
    create_source(conn, other_channel_id, "reddit_subreddit", {"subreddit": "x"})
    assert find_duplicate_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"}) is None


def test_set_source_paused_records_and_clears_the_timestamp(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    set_source_paused(conn, source_id, True, now_iso="2026-07-09T03:00:00")
    assert get_source(conn, source_id)["paused_at"] == "2026-07-09T03:00:00"
    set_source_paused(conn, source_id, False)
    assert get_source(conn, source_id)["paused_at"] is None


def test_set_source_paused_requires_a_timestamp_to_pause(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    with pytest.raises(ValueError, match="needs a timestamp"):
        set_source_paused(conn, source_id, True)


def test_record_fetch_success_stamps_attempt_and_ok_status(conn, channel_id):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00")
    row = get_source(conn, source_id)
    assert row["last_attempt_at"] == "2026-07-09T03:00:00"
    assert row["last_fetch_status"] == "ok"


def test_record_fetch_error_stamps_attempt_and_error_status_without_last_fetch_at(
    conn, channel_id
):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T00:00:00")
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T03:00:00")
    row = get_source(conn, source_id)
    assert row["last_attempt_at"] == "2026-07-09T03:00:00"  # attempt advances
    assert row["last_fetch_status"] == "error"
    assert row["last_fetch_at"] == "2026-07-09T00:00:00"  # last SUCCESS unchanged


def test_reset_fetch_state_clears_attempt_and_status_but_not_pause_or_name(conn, channel_id):
    source_id = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"}, name="Keep me")
    record_fetch_error(conn, source_id, "boom", "2026-07-09T06:00:00")
    set_source_paused(conn, source_id, True, now_iso="2026-07-09T06:00:00")

    reset_fetch_state_by_channel(conn, channel_id)

    row = get_source(conn, source_id)
    assert row["last_attempt_at"] is None
    assert row["last_fetch_status"] is None
    # Clearing fetched data must not resume a paused Source or forget its display name.
    assert row["paused_at"] == "2026-07-09T06:00:00"
    assert row["name"] == "Keep me"
