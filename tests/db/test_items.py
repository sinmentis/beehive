from datetime import datetime, timezone

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import (
    MutableUpsertOutcome,
    count_dashboard_signals,
    delete_by_channel,
    get_item,
    insert_new,
    list_archive,
    list_by_channel,
    list_dashboard_highlights,
    list_new_since,
    list_unread_rewrite_candidates,
    mark_absent_items_inactive,
    mark_channel_read,
    mark_item_opened,
    mark_read,
    revert_ai_summary_if_unchanged,
    update_ai_ranking,
    update_ai_ranking_by_id,
    update_best_comment,
    upsert_mutable_item,
    upsert_refreshable_item,
)
from beehive.db.sources import create_source
from beehive.db.votes import upsert_vote


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def source_id(conn):
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    return create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"}
    )


def _raw_item(external_id="t3_abc123", title="Rates fall"):
    return RawItem(
        external_id=external_id,
        title=title,
        url="https://reddit.com/r/x/comments/abc",
        body="body text",
        created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        raw_metadata={"score": 100, "num_comments": 20, "author": "someone"},
    )


def test_insert_new_returns_true_for_first_insert(conn, source_id):
    assert insert_new(conn, source_id, _raw_item()) is True


def test_insert_new_returns_false_for_duplicate(conn, source_id):
    insert_new(conn, source_id, _raw_item())
    assert insert_new(conn, source_id, _raw_item()) is False
    assert (
        len(
            list_by_channel(
                conn,
                conn.execute(
                    "SELECT channel_id FROM sources WHERE id=?", (source_id,)
                ).fetchone()[0],
            )
        )
        == 1
    )


def test_upsert_refreshable_item_updates_one_stable_row_and_resets_ranking(
    conn, source_id
):
    insert_new(conn, source_id, _raw_item())
    update_ai_ranking(
        conn,
        source_id,
        "t3_abc123",
        score=91,
        summary="Old summary",
        rationale="Old rationale",
    )
    item_id = conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, "t3_abc123"),
    ).fetchone()["id"]
    mark_read(conn, item_id)
    refreshed = RawItem(
        external_id="t3_abc123",
        title="Rates fall further",
        url="https://reddit.com/r/x/comments/abc",
        body="new body",
        created_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        raw_metadata={"price": 500.0, "current_bid": 500.0},
    )

    assert upsert_refreshable_item(conn, source_id, refreshed) is True

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert row["title"] == "Rates fall further"
    assert row["body"] == "new body"
    assert row["created_at"] == "2026-07-09T00:00:00+00:00"
    assert row["ai_score"] is None
    assert row["ai_summary"] is None
    assert row["ai_rationale"] is None
    assert row["is_read"] == 0


def test_upsert_refreshable_item_does_not_reset_unchanged_items(conn, source_id):
    item = _raw_item()
    insert_new(conn, source_id, item)
    update_ai_ranking(
        conn,
        source_id,
        item.external_id,
        score=91,
        summary="Current summary",
        rationale="Current rationale",
    )

    assert upsert_refreshable_item(conn, source_id, item) is False

    row = conn.execute(
        "SELECT ai_score, ai_summary FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, item.external_id),
    ).fetchone()
    assert row["ai_score"] == 91
    assert row["ai_summary"] == "Current summary"


def test_update_ai_ranking_and_list_by_channel(conn, source_id):
    insert_new(conn, source_id, _raw_item())
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]
    update_ai_ranking(
        conn,
        source_id,
        "t3_abc123",
        score=91.0,
        summary="RBNZ hints at cuts",
        rationale="matches interest rates",
    )
    items = list_by_channel(conn, channel_id)
    assert items[0]["ai_score"] == 91.0
    assert items[0]["ai_summary"] == "RBNZ hints at cuts"
    assert items[0]["source_config"]  # joined source config present for the "📍" badge


def test_update_ai_ranking_by_id_updates_only_the_target_row(conn):
    channel_id = create_channel(conn, "Shared IDs", "profile")
    first_source = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "one"}
    )
    second_source = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "two"}
    )
    insert_new(conn, first_source, _raw_item("shared", "First"))
    insert_new(conn, second_source, _raw_item("shared", "Second"))
    rows = conn.execute(
        "SELECT id, source_id FROM items WHERE external_id = 'shared' ORDER BY source_id"
    ).fetchall()

    update_ai_ranking_by_id(
        conn,
        rows[1]["id"],
        score=88,
        summary="second summary",
        rationale="second source",
    )

    stored = conn.execute(
        "SELECT source_id, ai_score, ai_summary FROM items "
        "WHERE external_id = 'shared' ORDER BY source_id"
    ).fetchall()
    assert stored[0]["ai_score"] is None
    assert stored[1]["ai_score"] == 88
    assert stored[1]["ai_summary"] == "second summary"


def test_list_by_channel_orders_by_score_desc_unscored_last(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", "low"))
    insert_new(conn, source_id, _raw_item("t2", "high"))
    insert_new(conn, source_id, _raw_item("t3", "unscored"))
    update_ai_ranking(conn, source_id, "t1", score=10.0, summary="s", rationale="r")
    update_ai_ranking(conn, source_id, "t2", score=90.0, summary="s", rationale="r")
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]
    items = list_by_channel(conn, channel_id)
    assert [i["external_id"] for i in items] == ["t2", "t1", "t3"]


def test_list_new_since_filters_by_fetched_at(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]
    future = "2999-01-01T00:00:00"
    assert list_new_since(conn, channel_id, future) == []
    past = "2000-01-01T00:00:00"
    assert len(list_new_since(conn, channel_id, past)) == 1


def test_list_by_channel_includes_vote_state(conn, source_id):
    from beehive.db.votes import upsert_vote

    insert_new(conn, source_id, _raw_item())
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]
    item_id = conn.execute(
        "SELECT id FROM items WHERE external_id='t3_abc123'"
    ).fetchone()[0]
    upsert_vote(conn, item_id, -1, "too niche")

    items = list_by_channel(conn, channel_id)
    assert items[0]["vote_value"] == -1
    assert items[0]["vote_reason"] == "too niche"


def test_list_by_channel_vote_fields_none_when_unvoted(conn, source_id):
    insert_new(conn, source_id, _raw_item())
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]
    items = list_by_channel(conn, channel_id)
    assert items[0]["vote_value"] is None
    assert items[0]["vote_reason"] is None


def test_get_item_returns_single_item_with_vote_state(conn, source_id):
    from beehive.db.votes import upsert_vote

    insert_new(conn, source_id, _raw_item())
    item_id = conn.execute(
        "SELECT id FROM items WHERE external_id='t3_abc123'"
    ).fetchone()[0]
    upsert_vote(conn, item_id, 1)

    item = get_item(conn, item_id)
    assert item["external_id"] == "t3_abc123"
    assert item["vote_value"] == 1
    assert item["source_config"]  # joined, present


def test_get_item_returns_none_for_missing_id(conn, source_id):
    assert get_item(conn, 999) is None


def test_mark_read_sets_is_read(conn, source_id):
    insert_new(conn, source_id, _raw_item())
    item_id = conn.execute(
        "SELECT id FROM items WHERE external_id='t3_abc123'"
    ).fetchone()[0]

    mark_read(conn, item_id)

    row = conn.execute("SELECT is_read FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["is_read"] == 1


def test_mark_channel_read_marks_every_unread_item_in_channel(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, source_id, _raw_item("t2"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    mark_channel_read(conn, channel_id)

    rows = conn.execute("SELECT is_read FROM items").fetchall()
    assert all(r["is_read"] == 1 for r in rows)


def test_mark_channel_read_does_not_affect_other_channels(conn, source_id):
    other_channel_id = create_channel(conn, "Other Channel", "profile")
    other_source_id = create_source(
        conn, other_channel_id, "reddit_subreddit", {"subreddit": "y"}
    )
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, other_source_id, _raw_item("t2"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    mark_channel_read(conn, channel_id)

    other_item = conn.execute(
        "SELECT is_read FROM items WHERE external_id='t2'"
    ).fetchone()
    assert other_item["is_read"] == 0


def test_list_archive_returns_all_items_newest_first(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, source_id, _raw_item("t2"))
    conn.execute(
        "UPDATE items SET fetched_at = '2026-07-01T00:00:00' WHERE external_id = 't1'"
    )
    conn.execute(
        "UPDATE items SET fetched_at = '2026-07-05T00:00:00' WHERE external_id = 't2'"
    )
    conn.commit()

    items, total = list_archive(conn)
    assert total == 2
    assert [i["external_id"] for i in items] == ["t2", "t1"]


def test_list_archive_filters_by_channel(conn, source_id):
    other_channel_id = create_channel(conn, "Other", "profile")
    other_source_id = create_source(
        conn, other_channel_id, "reddit_subreddit", {"subreddit": "y"}
    )
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, other_source_id, _raw_item("t2"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    items, total = list_archive(conn, channel_id=channel_id)
    assert total == 1
    assert items[0]["external_id"] == "t1"


def test_list_archive_filters_by_date_range(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, source_id, _raw_item("t2"))
    insert_new(conn, source_id, _raw_item("t3"))
    conn.execute(
        "UPDATE items SET fetched_at = '2026-07-01T00:00:00' WHERE external_id = 't1'"
    )
    conn.execute(
        "UPDATE items SET fetched_at = '2026-07-05T00:00:00' WHERE external_id = 't2'"
    )
    conn.execute(
        "UPDATE items SET fetched_at = '2026-07-09T00:00:00' WHERE external_id = 't3'"
    )
    conn.commit()

    items, total = list_archive(conn, date_from="2026-07-04", date_to="2026-07-06")
    assert total == 1
    assert items[0]["external_id"] == "t2"


def test_list_archive_date_to_includes_the_whole_end_day(conn, source_id):
    # Regression guard: comparing raw ISO-T timestamp strings directly (instead of truncating
    # to just the date part) would make date_to="2026-07-05" wrongly EXCLUDE an item fetched
    # later that same day, since "2026-07-05T23:59:59" > "2026-07-05" lexicographically.
    insert_new(conn, source_id, _raw_item("t1"))
    conn.execute(
        "UPDATE items SET fetched_at = '2026-07-05T23:59:59' WHERE external_id = 't1'"
    )
    conn.commit()

    items, total = list_archive(conn, date_to="2026-07-05")
    assert total == 1
    assert items[0]["external_id"] == "t1"


def test_list_archive_filters_by_read_state(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, source_id, _raw_item("t2"))
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    mark_read(conn, item_id)

    unread_items, unread_total = list_archive(conn, read_state="unread")
    read_items, read_total = list_archive(conn, read_state="read")
    assert unread_total == 1 and unread_items[0]["external_id"] == "t2"
    assert read_total == 1 and read_items[0]["external_id"] == "t1"


def test_list_archive_paginates(conn, source_id):
    for i in range(5):
        insert_new(conn, source_id, _raw_item(f"t{i}"))
        conn.execute(
            "UPDATE items SET fetched_at = ? WHERE external_id = ?",
            (f"2026-07-{i + 1:02d}T00:00:00", f"t{i}"),
        )
    conn.commit()

    page1, total = list_archive(conn, page=1, page_size=2)
    page2, _ = list_archive(conn, page=2, page_size=2)
    assert total == 5
    assert [i["external_id"] for i in page1] == ["t4", "t3"]
    assert [i["external_id"] for i in page2] == ["t2", "t1"]


def test_list_archive_pagination_stable_across_identical_fetched_at(conn, source_id):
    # Regression guard: fetched_at is only second-granular, so items from one fetch cycle share
    # an identical value. Without a unique secondary sort key, SQLite gives no guaranteed order
    # among tied rows across the SEPARATE paginated queries this issues (one per page), so an
    # item can be duplicated across two pages or skipped at the boundary. The ", items.id DESC"
    # tiebreaker makes the order total and stable: highest id (newest item) first. Insertion
    # assigns ids in order, so t0/t1/t2 get ascending ids and must come back t2, t1, t0.
    for i in range(3):
        insert_new(conn, source_id, _raw_item(f"t{i}"))
    conn.execute("UPDATE items SET fetched_at = '2026-07-05T12:00:00'")
    conn.commit()

    page1, total = list_archive(conn, page=1, page_size=2)
    page2, _ = list_archive(conn, page=2, page_size=2)
    assert total == 3
    page1_ids = [i["external_id"] for i in page1]
    page2_ids = [i["external_id"] for i in page2]
    assert set(page1_ids).isdisjoint(
        page2_ids
    )  # no item duplicated across the page boundary
    assert sorted(page1_ids + page2_ids) == [
        "t0",
        "t1",
        "t2",
    ]  # every item present exactly once
    # Exact order pins the tiebreaker down: without ", items.id DESC" SQLite returns the tied
    # rows in its own undefined order (empirically ["t0", "t1"] then ["t2"]), so these fail.
    assert page1_ids == ["t2", "t1"]
    assert page2_ids == ["t0"]


def test_list_archive_includes_channel_name_and_vote_state(conn, source_id):
    from beehive.db.votes import upsert_vote

    insert_new(conn, source_id, _raw_item("t1"))
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(conn, item_id, 1)

    items, _ = list_archive(conn)
    assert items[0]["channel_name"] == "NZ Finance"
    assert items[0]["vote_value"] == 1


def test_list_archive_filters_by_search_matching_title(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", title="Interest rates rise"))
    insert_new(conn, source_id, _raw_item("t2", title="Housing market update"))

    items, total = list_archive(conn, search="rates")
    assert total == 1
    assert items[0]["external_id"] == "t1"


def test_list_archive_filters_by_search_matching_ai_summary(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", title="Some title"))
    insert_new(conn, source_id, _raw_item("t2", title="Other title"))
    update_ai_ranking(
        conn, source_id, "t1", score=90, summary="RBNZ 宣布降息", rationale="r"
    )

    items, total = list_archive(conn, search="降息")
    assert total == 1
    assert items[0]["external_id"] == "t1"


def test_list_archive_filters_by_search_matching_body(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", title="Some title"))
    insert_new(conn, source_id, _raw_item("t2", title="Other title"))
    conn.execute(
        "UPDATE items SET body = 'mentions the OCR decision' WHERE external_id = 't1'"
    )
    conn.commit()

    items, total = list_archive(conn, search="OCR")
    assert total == 1
    assert items[0]["external_id"] == "t1"


def test_list_archive_search_finds_nothing_returns_empty(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", title="Interest rates rise"))

    items, total = list_archive(conn, search="nonexistent keyword")
    assert total == 0
    assert items == []


def test_list_archive_empty_search_string_returns_everything(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", title="Interest rates rise"))

    items, total = list_archive(conn, search="")
    assert total == 1


def test_list_archive_combines_search_with_channel_filter(conn, source_id):
    other_channel_id = create_channel(conn, "Other", "profile")
    other_source_id = create_source(
        conn, other_channel_id, "reddit_subreddit", {"subreddit": "y"}
    )
    insert_new(conn, source_id, _raw_item("t1", title="Interest rates rise"))
    insert_new(conn, other_source_id, _raw_item("t2", title="Interest rates elsewhere"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    items, total = list_archive(conn, channel_id=channel_id, search="rates")
    assert total == 1
    assert items[0]["external_id"] == "t1"


def test_list_dashboard_highlights_orders_by_score_across_channels(conn, source_id):
    other_channel_id = create_channel(conn, "Other Channel", "profile")
    other_source_id = create_source(
        conn, other_channel_id, "reddit_subreddit", {"subreddit": "y"}
    )
    insert_new(conn, source_id, _raw_item("t1", title="Low score"))
    insert_new(conn, other_source_id, _raw_item("t2", title="High score"))
    update_ai_ranking(
        conn, source_id, "t1", score=10.0, summary="低分摘要", rationale="r"
    )
    update_ai_ranking(
        conn, other_source_id, "t2", score=90.0, summary="高分摘要", rationale="r"
    )

    highlights = list_dashboard_highlights(conn)
    assert [h["external_id"] for h in highlights] == ["t2", "t1"]
    assert highlights[0]["channel_name"] == "Other Channel"
    assert highlights[1]["channel_name"] == "NZ Finance"


def test_list_dashboard_highlights_filters_by_minimum_score(conn, source_id):
    insert_new(conn, source_id, _raw_item("low", title="Low score"))
    insert_new(conn, source_id, _raw_item("high", title="High score"))
    update_ai_ranking(
        conn, source_id, "low", score=89, summary="低分摘要", rationale="r"
    )
    update_ai_ranking(
        conn, source_id, "high", score=90, summary="高分摘要", rationale="r"
    )

    highlights = list_dashboard_highlights(conn, minimum_score=90)

    assert [highlight["external_id"] for highlight in highlights] == ["high"]


def test_list_dashboard_highlights_excludes_items_without_a_summary(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))  # never ranked, ai_summary is NULL

    assert list_dashboard_highlights(conn) == []


def test_list_dashboard_highlights_excludes_monitor_channel_items(conn):
    """Even a monitor Channel with a (hypothetical) scored item must never leak into Home --
    monitor Channels are excluded from the reading UI entirely, see web/public.py."""
    monitor_channel_id = create_channel(
        conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor"
    )
    monitor_source_id = create_source(
        conn, monitor_channel_id, "shopify_collection",
        {"collection_url": "https://arcteryx.co.nz/collections/outlet"}
    )
    insert_new(conn, monitor_source_id, _raw_item("t1"))
    update_ai_ranking(
        conn, monitor_source_id, "t1", score=90.0, summary="s", rationale="r"
    )

    assert list_dashboard_highlights(conn) == []
    assert count_dashboard_signals(conn) == 0


def test_list_dashboard_highlights_excludes_downvoted_items(conn, source_id):
    from beehive.db.votes import upsert_vote

    insert_new(conn, source_id, _raw_item("t1"))
    update_ai_ranking(conn, source_id, "t1", score=90.0, summary="s", rationale="r")
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(conn, item_id, -1, "not interested")

    assert list_dashboard_highlights(conn) == []


def test_list_dashboard_highlights_includes_upvoted_items(conn, source_id):
    from beehive.db.votes import upsert_vote

    insert_new(conn, source_id, _raw_item("t1"))
    update_ai_ranking(conn, source_id, "t1", score=90.0, summary="s", rationale="r")
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(conn, item_id, 1)

    highlights = list_dashboard_highlights(conn)
    assert len(highlights) == 1
    assert highlights[0]["external_id"] == "t1"


def test_list_dashboard_highlights_respects_limit(conn, source_id):
    for i in range(8):
        insert_new(conn, source_id, _raw_item(f"t{i}", title=f"Item {i}"))
        update_ai_ranking(
            conn, source_id, f"t{i}", score=float(i), summary=f"s{i}", rationale="r"
        )

    assert len(list_dashboard_highlights(conn)) == 5
    assert len(list_dashboard_highlights(conn, limit=3)) == 3


def test_count_dashboard_signals_uses_the_same_visibility_filters(conn, source_id):
    from beehive.db.votes import upsert_vote

    for external_id, score in (("high", 95), ("low", 70), ("down", 99), ("open", 98)):
        insert_new(conn, source_id, _raw_item(external_id))
        update_ai_ranking(
            conn, source_id, external_id, score=score, summary="s", rationale="r"
        )

    down_id = conn.execute(
        "SELECT id FROM items WHERE external_id = 'down'"
    ).fetchone()[0]
    open_id = conn.execute(
        "SELECT id FROM items WHERE external_id = 'open'"
    ).fetchone()[0]
    upsert_vote(conn, down_id, -1, "not relevant")
    mark_item_opened(conn, open_id)
    insert_new(conn, source_id, _raw_item("unranked"))

    assert count_dashboard_signals(conn) == 3
    assert count_dashboard_signals(conn, minimum_score=90) == 2


def test_mark_item_opened_sets_opened_at(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    mark_item_opened(conn, item_id)

    row = conn.execute(
        "SELECT opened_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row["opened_at"] is not None


def test_mark_item_opened_does_not_overwrite_the_first_timestamp(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    conn.execute(
        "UPDATE items SET opened_at = ? WHERE id = ?", ("2020-01-01T00:00:00", item_id)
    )
    conn.commit()

    mark_item_opened(conn, item_id)

    row = conn.execute(
        "SELECT opened_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row["opened_at"] == "2020-01-01T00:00:00"


def test_list_dashboard_highlights_keeps_opened_items_available_in_all_view(
    conn, source_id
):
    insert_new(conn, source_id, _raw_item("t1"))
    update_ai_ranking(conn, source_id, "t1", score=90.0, summary="s", rationale="r")
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    mark_item_opened(conn, item_id)

    highlights = list_dashboard_highlights(conn)
    assert len(highlights) == 1
    assert highlights[0]["external_id"] == "t1"


def test_list_dashboard_highlights_includes_read_but_unopened_items(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    update_ai_ranking(conn, source_id, "t1", score=90.0, summary="s", rationale="r")
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    mark_read(
        conn, item_id
    )  # e.g. via visiting the Channel page -- must NOT hide the highlight

    highlights = list_dashboard_highlights(conn)
    assert len(highlights) == 1
    assert highlights[0]["external_id"] == "t1"


def test_update_best_comment_sets_the_column(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    update_best_comment(conn, item_id, "评论指出实际数字不同")

    row = conn.execute(
        "SELECT best_comment_summary FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row["best_comment_summary"] == "评论指出实际数字不同"


def test_fresh_item_has_no_best_comment_summary(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    row = conn.execute(
        "SELECT best_comment_summary FROM items WHERE external_id='t1'"
    ).fetchone()
    assert row["best_comment_summary"] is None


# ============================================================================
# Summary-only rewrite: list_unread_rewrite_candidates (candidate lookup) and
# revert_ai_summary_if_unchanged (rollback's write half). The forward-apply write itself --
# db/summary_rewrites.py's apply_summary_rewrite -- is tested in tests/db/test_summary_rewrites.py
# alongside the rest of that module, since it has to live and be tested there to guarantee its
# single-transaction seam with the summary_rewrite_log INSERT.
# ============================================================================


def _scored_item_id(conn, source_id, external_id, score=50.0, summary="old summary"):
    insert_new(conn, source_id, _raw_item(external_id, external_id))
    update_ai_ranking(
        conn, source_id, external_id, score=score, summary=summary, rationale="r"
    )
    return conn.execute(
        "SELECT id FROM items WHERE external_id = ?", (external_id,)
    ).fetchone()[0]


def test_list_unread_rewrite_candidates_returns_unread_scored_items(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")

    candidates = list_unread_rewrite_candidates(conn, high_water_item_id=item_id)

    assert [c["id"] for c in candidates] == [item_id]


def test_list_unread_rewrite_candidates_excludes_read_items(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")
    mark_read(conn, item_id)

    candidates = list_unread_rewrite_candidates(conn, high_water_item_id=item_id)

    assert candidates == []


def test_list_unread_rewrite_candidates_excludes_unscored_items(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1", "t1"))  # ai_score/ai_summary stay NULL
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    candidates = list_unread_rewrite_candidates(conn, high_water_item_id=item_id)

    assert candidates == []


def test_list_unread_rewrite_candidates_excludes_items_with_null_summary(
    conn, source_id
):
    """Eligibility is exactly is_read=0 AND ai_score IS NOT NULL AND ai_summary IS NOT NULL --
    a row can only reach ai_score IS NOT NULL with ai_summary NULL via a raw UPDATE (never via
    update_ai_ranking, which always sets both together), but the guard must still hold."""
    insert_new(conn, source_id, _raw_item("t1", "t1"))
    conn.execute("UPDATE items SET ai_score = 50 WHERE external_id = 't1'")
    conn.commit()
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]

    candidates = list_unread_rewrite_candidates(conn, high_water_item_id=item_id)

    assert candidates == []


def test_list_unread_rewrite_candidates_excludes_items_above_the_high_water_mark(
    conn, source_id
):
    first_id = _scored_item_id(conn, source_id, "t1")
    second_id = _scored_item_id(conn, source_id, "t2")
    assert second_id > first_id

    candidates = list_unread_rewrite_candidates(conn, high_water_item_id=first_id)

    assert [c["id"] for c in candidates] == [first_id]
    # second_id exists and is otherwise eligible, but is newer than the pre-deployment
    # watermark, so it must never show up as a rewrite candidate.
    assert second_id not in [c["id"] for c in candidates]


def test_list_unread_rewrite_candidates_orders_oldest_first_by_id(conn, source_id):
    ids = [_scored_item_id(conn, source_id, f"t{i}") for i in range(3)]

    candidates = list_unread_rewrite_candidates(conn, high_water_item_id=ids[-1])

    assert [c["id"] for c in candidates] == sorted(ids)


def test_list_unread_rewrite_candidates_after_id_paginates_deterministically(
    conn, source_id
):
    ids = [_scored_item_id(conn, source_id, f"t{i}") for i in range(5)]

    page_1 = list_unread_rewrite_candidates(conn, high_water_item_id=ids[-1], limit=2)
    page_2 = list_unread_rewrite_candidates(
        conn, high_water_item_id=ids[-1], after_id=page_1[-1]["id"], limit=2
    )

    assert [c["id"] for c in page_1] == ids[:2]
    assert [c["id"] for c in page_2] == ids[2:4]


def test_revert_ai_summary_if_unchanged_restores_the_previous_value(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1", summary="old summary")
    conn.execute("UPDATE items SET ai_summary = 'new summary' WHERE id = ?", (item_id,))
    conn.commit()

    reverted = revert_ai_summary_if_unchanged(
        conn, item_id, "new summary", "old summary"
    )

    assert reverted is True
    assert get_item(conn, item_id)["ai_summary"] == "old summary"


def test_revert_ai_summary_if_unchanged_is_a_noop_when_value_changed_since(
    conn, source_id
):
    item_id = _scored_item_id(conn, source_id, "t1", summary="old summary")
    conn.execute(
        "UPDATE items SET ai_summary = ? WHERE id = ?", ("someone else's edit", item_id)
    )
    conn.commit()

    reverted = revert_ai_summary_if_unchanged(
        conn, item_id, "new summary", "old summary"
    )

    assert reverted is False
    assert get_item(conn, item_id)["ai_summary"] == "someone else's edit"


def test_delete_by_channel_removes_every_item_in_channel(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, source_id, _raw_item("t2"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    deleted = delete_by_channel(conn, channel_id)

    assert deleted == 2
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_delete_by_channel_does_not_affect_other_channels(conn, source_id):
    other_channel_id = create_channel(conn, "Other Channel", "profile")
    other_source_id = create_source(
        conn, other_channel_id, "reddit_subreddit", {"subreddit": "y"}
    )
    insert_new(conn, source_id, _raw_item("t1"))
    insert_new(conn, other_source_id, _raw_item("t2"))
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    delete_by_channel(conn, channel_id)

    remaining = conn.execute("SELECT external_id FROM items").fetchall()
    assert [r["external_id"] for r in remaining] == ["t2"]


def test_delete_by_channel_returns_zero_when_channel_has_no_items(conn, source_id):
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    assert delete_by_channel(conn, channel_id) == 0


def test_delete_by_channel_cascades_to_votes(conn, source_id):
    insert_new(conn, source_id, _raw_item("t1"))
    item_id = conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    upsert_vote(conn, item_id, 1)
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id=?", (source_id,)
    ).fetchone()[0]

    delete_by_channel(conn, channel_id)

    assert conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 0


# ===========================================================================
# Mutable-snapshot persistence: upsert_mutable_item + reconciliation
# ===========================================================================


@pytest.fixture
def monitor_source_id(conn):
    channel_id = create_channel(conn, "Clearance Watch", "shopping", kind="monitor")
    return create_source(
        conn, channel_id, "shopify_collection",
        {"collection_url": "https://store.example.com/collections/outlet"},
    )


def _product(external_id="1001", *, price=50.0, title="Alpha Jacket",
             available=True, on_sale=False):
    return RawItem(
        external_id=external_id,
        title=title,
        url="https://store.example.com/products/alpha",
        body="",
        raw_metadata={"price": price, "available": available, "on_sale": on_sale},
    )


def test_upsert_mutable_item_inserts_and_sets_last_seen(conn, monitor_source_id):
    result = upsert_mutable_item(
        conn, monitor_source_id, _product(), now_iso="2026-07-01T00:00:00"
    )
    assert result.outcome == MutableUpsertOutcome.INSERTED
    assert result.before_metadata is None
    assert result.after_metadata["price"] == 50.0
    assert result.ranking_reset is False
    assert result.reappeared is False
    row = conn.execute("SELECT * FROM items WHERE id = ?", (result.item_id,)).fetchone()
    assert row["last_seen_at"] == "2026-07-01T00:00:00"
    assert row["inactive_at"] is None
    assert row["superseded_at"] is None


def test_upsert_mutable_item_unchanged_only_bumps_last_seen(conn, monitor_source_id):
    first = upsert_mutable_item(
        conn, monitor_source_id, _product(), now_iso="2026-07-01T00:00:00"
    )
    conn.execute("UPDATE items SET ai_score = 80, is_read = 1 WHERE id = ?", (first.item_id,))
    conn.commit()

    result = upsert_mutable_item(
        conn, monitor_source_id, _product(), now_iso="2026-07-02T00:00:00"
    )
    assert result.outcome == MutableUpsertOutcome.UNCHANGED
    assert result.ranking_reset is False
    row = conn.execute("SELECT * FROM items WHERE id = ?", (first.item_id,)).fetchone()
    assert row["last_seen_at"] == "2026-07-02T00:00:00"
    assert row["ai_score"] == 80  # score kept
    assert row["is_read"] == 1  # read state preserved


def test_upsert_mutable_item_price_change_updates_without_reranking(conn, monitor_source_id):
    first = upsert_mutable_item(
        conn, monitor_source_id, _product(price=50.0), now_iso="2026-07-01T00:00:00"
    )
    conn.execute(
        "UPDATE items SET ai_score = 80, ai_summary = 's', ai_rationale = 'r', is_read = 1 "
        "WHERE id = ?", (first.item_id,))
    conn.commit()

    result = upsert_mutable_item(
        conn, monitor_source_id, _product(price=40.0, on_sale=True),
        now_iso="2026-07-02T00:00:00",
    )
    assert result.outcome == MutableUpsertOutcome.UPDATED
    assert result.ranking_reset is False
    # before/after metadata make the price move observable to later event detection.
    assert result.before_metadata["price"] == 50.0
    assert result.after_metadata["price"] == 40.0
    row = conn.execute("SELECT * FROM items WHERE id = ?", (first.item_id,)).fetchone()
    assert row["ai_score"] == 80  # not re-ranked
    assert row["ai_summary"] == "s"
    assert row["is_read"] == 1  # read state preserved
    assert row["raw_metadata"] == '{"available":true,"on_sale":true,"price":40.0}'
    assert row["fetched_at"] != "2026-07-02T00:00:00"  # a price move is not "freshly fetched"


def test_upsert_mutable_item_content_change_resets_ranking_but_keeps_read(conn, monitor_source_id):
    first = upsert_mutable_item(
        conn, monitor_source_id, _product(title="Alpha Jacket"), now_iso="2026-07-01T00:00:00"
    )
    conn.execute(
        "UPDATE items SET ai_score = 80, ai_summary = 's', ai_rationale = 'r', is_read = 1 "
        "WHERE id = ?", (first.item_id,))
    conn.commit()

    result = upsert_mutable_item(
        conn, monitor_source_id, _product(title="Alpha Jacket v2"),
        now_iso="2026-07-02T00:00:00",
    )
    assert result.outcome == MutableUpsertOutcome.UPDATED
    assert result.ranking_reset is True
    row = conn.execute("SELECT * FROM items WHERE id = ?", (first.item_id,)).fetchone()
    assert row["ai_score"] is None  # re-enters the ranking backlog
    assert row["ai_summary"] is None
    assert row["ai_rationale"] is None
    assert row["is_read"] == 1  # read state still preserved (never forced to 0)
    assert row["fetched_at"] == "2026-07-02T00:00:00"  # a real content change is fresh


def test_reconcile_marks_absent_items_inactive_and_returns_ids(conn, monitor_source_id):
    a = upsert_mutable_item(
        conn, monitor_source_id, _product("1001"), now_iso="2026-07-01T00:00:00")
    b = upsert_mutable_item(
        conn, monitor_source_id, _product("1002"), now_iso="2026-07-01T00:00:00")
    c = upsert_mutable_item(
        conn, monitor_source_id, _product("1003"), now_iso="2026-07-01T00:00:00")

    affected = mark_absent_items_inactive(
        conn, monitor_source_id, ["1001", "1003"], now_iso="2026-07-02T00:00:00"
    )
    assert affected == [b.item_id]
    assert conn.execute(
        "SELECT inactive_at FROM items WHERE id = ?", (b.item_id,)
    ).fetchone()["inactive_at"] == "2026-07-02T00:00:00"
    for present in (a.item_id, c.item_id):
        assert conn.execute(
            "SELECT inactive_at FROM items WHERE id = ?", (present,)
        ).fetchone()["inactive_at"] is None
    # Never deletes.
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3


def test_reconcile_empty_snapshot_marks_all_active_items_inactive(conn, monitor_source_id):
    upsert_mutable_item(conn, monitor_source_id, _product("1001"), now_iso="2026-07-01T00:00:00")
    upsert_mutable_item(conn, monitor_source_id, _product("1002"), now_iso="2026-07-01T00:00:00")

    affected = mark_absent_items_inactive(
        conn, monitor_source_id, [], now_iso="2026-07-02T00:00:00"
    )
    assert len(affected) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE inactive_at IS NOT NULL"
    ).fetchone()[0] == 2


def test_upsert_mutable_item_reappearance_clears_inactive_and_flags_back_in_stock(
    conn, monitor_source_id
):
    inserted = upsert_mutable_item(
        conn, monitor_source_id, _product("1001", available=False), now_iso="2026-07-01T00:00:00"
    )
    mark_absent_items_inactive(conn, monitor_source_id, [], now_iso="2026-07-02T00:00:00")

    result = upsert_mutable_item(
        conn, monitor_source_id, _product("1001", available=True), now_iso="2026-07-03T00:00:00"
    )
    assert result.reappeared is True
    assert result.outcome == MutableUpsertOutcome.UPDATED
    row = conn.execute("SELECT * FROM items WHERE id = ?", (inserted.item_id,)).fetchone()
    assert row["inactive_at"] is None
    assert row["last_seen_at"] == "2026-07-03T00:00:00"


def test_reconcile_ignores_superseded_rows(conn, monitor_source_id):
    survivor = upsert_mutable_item(
        conn, monitor_source_id, _product("1001"), now_iso="2026-07-01T00:00:00"
    )
    # A superseded history row for a different external_id must never be revived by reconciliation.
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, superseded_at) "
        "VALUES (?, '1001:49.99', 'old', 'https://x', '2026-07-01T00:00:00')",
        (monitor_source_id,))
    conn.commit()

    affected = mark_absent_items_inactive(
        conn, monitor_source_id, ["1001"], now_iso="2026-07-02T00:00:00"
    )
    assert affected == []  # survivor present, superseded row skipped
    assert conn.execute(
        "SELECT inactive_at FROM items WHERE external_id = '1001:49.99'"
    ).fetchone()["inactive_at"] is None
    assert survivor.item_id  # referenced to keep the survivor meaningful


def test_list_by_channel_hides_superseded_rows_by_default(conn, monitor_source_id):
    current = upsert_mutable_item(
        conn, monitor_source_id, _product("1001"), now_iso="2026-07-01T00:00:00"
    )
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, superseded_at) "
        "VALUES (?, '1001:49.99', 'old', 'https://x', '2026-07-01T00:00:00')",
        (monitor_source_id,))
    conn.commit()
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id = ?", (monitor_source_id,)
    ).fetchone()["channel_id"]

    visible = list_by_channel(conn, channel_id)
    assert [item["id"] for item in visible] == [current.item_id]

    with_history = list_by_channel(conn, channel_id, include_superseded=True)
    assert {item["external_id"] for item in with_history} == {"1001", "1001:49.99"}


# ===========================================================================
# Stable shopping external_id compaction migration (db/connection.py)
# ===========================================================================

_STABLE_SHOPPING_MARKER = "stable_shopping_external_id_migrated_v1"


def _insert_shopping_row(conn, source_id, external_id, fetched_at, *, title="Product"):
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, fetched_at, raw_metadata) "
        "VALUES (?, ?, ?, 'https://store.example.com/p', ?, '{\"price\":1}')",
        (source_id, external_id, title, fetched_at))
    return conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id)).fetchone()["id"]


def _rerun_stable_shopping_migration(conn):
    conn.execute("DELETE FROM app_state WHERE key = ?", (_STABLE_SHOPPING_MARKER,))
    conn.commit()
    init_schema(conn)


def test_stable_shopping_compaction_keeps_newest_survivor_and_supersedes_older(
    conn, monitor_source_id
):
    older = _insert_shopping_row(conn, monitor_source_id, "1001:49.99", "2026-07-01T00:00:00")
    newest = _insert_shopping_row(conn, monitor_source_id, "1001:39.99", "2026-07-03T00:00:00")
    middle = _insert_shopping_row(conn, monitor_source_id, "1001:44.99", "2026-07-02T00:00:00")

    _rerun_stable_shopping_migration(conn)

    survivor = conn.execute("SELECT * FROM items WHERE id = ?", (newest,)).fetchone()
    assert survivor["external_id"] == "1001"
    assert survivor["superseded_at"] is None
    assert survivor["last_seen_at"] == "2026-07-03T00:00:00"  # initialized from fetched_at
    for older_id in (older, middle):
        row = conn.execute("SELECT * FROM items WHERE id = ?", (older_id,)).fetchone()
        assert row["superseded_at"] is not None
    # Nothing is deleted -- history is preserved.
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE source_id = ?", (monitor_source_id,)
    ).fetchone()[0] == 3


def test_stable_shopping_compaction_frees_an_exact_stable_id_collision(conn, monitor_source_id):
    price_row = _insert_shopping_row(conn, monitor_source_id, "1001:39.99", "2026-07-03T00:00:00")
    collision = _insert_shopping_row(conn, monitor_source_id, "1001", "2026-07-01T00:00:00")

    _rerun_stable_shopping_migration(conn)

    # The newer price row wins the stable id; the older bare-id row is moved aside and superseded.
    assert conn.execute(
        "SELECT external_id FROM items WHERE id = ?", (price_row,)
    ).fetchone()["external_id"] == "1001"
    freed = conn.execute("SELECT * FROM items WHERE id = ?", (collision,)).fetchone()
    assert freed["external_id"] != "1001"
    assert freed["superseded_at"] is not None
    # No UNIQUE(source_id, external_id) collision remains.
    duplicates = conn.execute(
        "SELECT external_id, COUNT(*) c FROM items WHERE source_id = ? "
        "GROUP BY external_id HAVING c > 1", (monitor_source_id,)).fetchall()
    assert duplicates == []


def test_stable_shopping_compaction_preserves_dependent_rows_without_cascade(
    conn, monitor_source_id
):
    older = _insert_shopping_row(conn, monitor_source_id, "1001:49.99", "2026-07-01T00:00:00")
    survivor = _insert_shopping_row(conn, monitor_source_id, "1001:39.99", "2026-07-03T00:00:00")
    # A dependent Deep Read on the survivor: it must not be cascaded away by the migration.
    conn.execute(
        "INSERT INTO deep_reads (item_id, requested_at) VALUES (?, '2026-07-03T00:00:00')",
        (survivor,))
    upsert_vote(conn, survivor, 1, "keeper")
    conn.commit()

    _rerun_stable_shopping_migration(conn)

    assert conn.execute(
        "SELECT external_id FROM items WHERE id = ?", (survivor,)
    ).fetchone()["external_id"] == "1001"
    # Survivor id preserved, so its dependents are intact.
    assert conn.execute(
        "SELECT COUNT(*) FROM deep_reads WHERE item_id = ?", (survivor,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM votes WHERE item_id = ?", (survivor,)
    ).fetchone()[0] == 1
    assert older  # older row still present (superseded, not deleted)
    assert conn.execute("SELECT COUNT(*) FROM deep_reads").fetchone()[0] == 1


def test_stable_shopping_compaction_hides_superseded_from_normal_listing(conn, monitor_source_id):
    _insert_shopping_row(conn, monitor_source_id, "1001:49.99", "2026-07-01T00:00:00")
    _insert_shopping_row(conn, monitor_source_id, "1001:39.99", "2026-07-03T00:00:00")
    channel_id = conn.execute(
        "SELECT channel_id FROM sources WHERE id = ?", (monitor_source_id,)
    ).fetchone()["channel_id"]

    _rerun_stable_shopping_migration(conn)

    visible = list_by_channel(conn, channel_id)
    assert [item["external_id"] for item in visible] == ["1001"]
    all_rows = list_by_channel(conn, channel_id, include_superseded=True)
    assert len(all_rows) == 2


def test_stable_shopping_compaction_reruns_are_a_no_op(conn, monitor_source_id):
    _insert_shopping_row(conn, monitor_source_id, "1001:49.99", "2026-07-01T00:00:00")
    survivor = _insert_shopping_row(conn, monitor_source_id, "1001:39.99", "2026-07-03T00:00:00")

    _rerun_stable_shopping_migration(conn)
    after_first = conn.execute(
        "SELECT id, external_id, superseded_at FROM items WHERE source_id = ? ORDER BY id",
        (monitor_source_id,)).fetchall()

    init_schema(conn)  # marker now set -> no further changes
    after_second = conn.execute(
        "SELECT id, external_id, superseded_at FROM items WHERE source_id = ? ORDER BY id",
        (monitor_source_id,)).fetchall()

    assert [tuple(r) for r in after_first] == [tuple(r) for r in after_second]
    assert conn.execute(
        "SELECT external_id FROM items WHERE id = ?", (survivor,)
    ).fetchone()["external_id"] == "1001"


def test_stable_shopping_compaction_does_not_touch_editorial_rows(conn, source_id):
    # source_id is a reddit_subreddit (editorial) source. A colon-with-price-looking id here must
    # be left exactly as-is.
    editorial = _insert_shopping_row(conn, source_id, "t3_abc:12.34", "2026-07-01T00:00:00")

    _rerun_stable_shopping_migration(conn)

    row = conn.execute("SELECT * FROM items WHERE id = ?", (editorial,)).fetchone()
    assert row["external_id"] == "t3_abc:12.34"
    assert row["superseded_at"] is None
