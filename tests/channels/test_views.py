"""Contract tests for the typed Channel page-model layer (beehive.channels.views).

These exercise the seam directly (never through a route): definition-driven template dispatch,
the "no raw_metadata leaks into a model" invariant, and the per-kind presentation/section/
pagination rules the future panel templates depend on.
"""
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

# Registers the auction connector so adapter_for_source resolves inside the Tracker builder.
from beehive.connectors import all_about_auctions  # noqa: F401
from beehive.channels.views import (
    DEFAULT_PER_PAGE,
    MAX_PER_PAGE,
    EditorialQuery,
    EditorialPage,
    MonitorPage,
    MonitorQuery,
    MonitorSort,
    Pagination,
    TrackerDeadline,
    TrackerPage,
    TrackerQuery,
    TrackerStatus,
    WatchlistPage,
    WatchlistQuery,
    build_channel_page,
    build_watchlist_page,
)
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel, get_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import request_deep_read
from beehive.db.item_events import record_or_coalesce_event, suppress_item_events
from beehive.db.items import insert_new, update_ai_ranking, update_best_comment
from beehive.db.sources import create_source
from beehive.db.tracker_watches import add_tracker_watch
from beehive.db.votes import upsert_vote
from beehive.localization import localizer_for

_NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
_EN = localizer_for("en")


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _channel(conn, name, kind, *, minimum_score=0, highlight_count=8):
    channel_id = create_channel(
        conn, name, "profile", kind=kind, minimum_score=minimum_score,
        highlight_count=highlight_count,
    )
    return get_channel(conn, channel_id)


def _add_item(
    conn,
    source_id,
    external_id,
    *,
    title="Item",
    url="https://example.com/x",
    raw_metadata=None,
    score=None,
    summary="",
    rationale="",
    is_read=False,
    inactive_at=None,
):
    insert_new(
        conn,
        source_id,
        RawItem(
            external_id=external_id,
            title=title,
            url=url,
            raw_metadata=raw_metadata or {},
        ),
    )
    if score is not None:
        update_ai_ranking(conn, source_id, external_id, score, summary, rationale)
    item_id = conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()["id"]
    if is_read:
        conn.execute("UPDATE items SET is_read = 1 WHERE id = ?", (item_id,))
    if inactive_at is not None:
        conn.execute(
            "UPDATE items SET inactive_at = ? WHERE id = ?", (inactive_at, item_id)
        )
    conn.commit()
    return item_id


def _shopify_metadata(price, compare, *, on_sale, available=True, vendor="Arc'teryx",
                      product_type="Jackets", image_url="https://cdn/x.jpg"):
    return {
        "price": price,
        "compare_at_price": compare,
        "on_sale": on_sale,
        "available": available,
        "vendor": vendor,
        "product_type": product_type,
        "tags": [],
        "image_url": image_url,
    }


def _auction_metadata(closing_at, *, status="active", auction_title="Weekly auction"):
    return {
        "listing_kind": "auction_lot",
        "auction_title": auction_title,
        "closing_at": closing_at.isoformat() if closing_at is not None else None,
        "status": status,
        "currency_code": "NZD",
        "current_bid": 500.0,
        "buyer_premium_rate": 0.17,
        "estimated_cost": 585.0,
        "rrp": 1040.0,
        "rrp_excludes_gst": True,
        "estimate_low": 700.0,
        "estimate_high": 900.0,
        "image_url": "https://cdn/lot.jpg",
    }


# --------------------------------------------------------------------------------------------
# Definition-driven dispatch
# --------------------------------------------------------------------------------------------
def test_dispatch_editorial_returns_editorial_page_and_template(conn):
    channel = _channel(conn, "News", "editorial")
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert isinstance(page, EditorialPage)
    assert page.template_name == "channel_editorial.html"


def test_dispatch_monitor_returns_monitor_page_and_template(conn):
    channel = _channel(conn, "Outlet", "monitor")
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert isinstance(page, MonitorPage)
    assert page.template_name == "channel_monitor.html"


def test_dispatch_tracker_returns_tracker_page_and_template(conn):
    channel = _channel(conn, "Auctions", "tracker")
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert isinstance(page, TrackerPage)
    assert page.template_name == "channel_tracker.html"


def test_build_channel_page_requires_timezone_aware_now(conn):
    channel = _channel(conn, "News", "editorial")
    with pytest.raises(ValueError, match="timezone-aware"):
        build_channel_page(conn, channel, t=_EN, now=datetime(2026, 7, 22, 10, 0))


# --------------------------------------------------------------------------------------------
# No raw_metadata (or any raw dict) leaks into a page/item model
# --------------------------------------------------------------------------------------------
def _walk(value):
    """Yield every dataclass instance reachable from a built page."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        yield value
        for f in dataclasses.fields(value):
            yield from _walk(getattr(value, f.name))
    elif isinstance(value, (tuple, list)):
        for element in value:
            yield from _walk(element)


def _assert_no_raw_metadata(page):
    models = list(_walk(page))
    assert models, "expected at least one dataclass in the page"
    for model in models:
        for f in dataclasses.fields(model):
            assert f.name != "raw_metadata", f"{type(model).__name__} exposes raw_metadata"
            value = getattr(model, f.name)
            assert not isinstance(value, dict), (
                f"{type(model).__name__}.{f.name} leaks a raw dict"
            )


def test_no_editorial_model_contains_raw_metadata(conn):
    channel = _channel(conn, "News", "editorial")
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    _add_item(conn, source_id, "r1", raw_metadata={"score": 5, "num_comments": 2}, score=80)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    _assert_no_raw_metadata(page)


def test_no_monitor_model_contains_raw_metadata(conn):
    channel = _channel(conn, "Outlet", "monitor")
    source_id = create_source(
        conn, channel["id"], "shopify_collection",
        {"collection_url": "https://example.com/collections/outlet"},
    )
    item_id = _add_item(conn, source_id, "p1",
                        raw_metadata=_shopify_metadata(199.0, 299.0, on_sale=True), score=90)
    # Stage a change so the MonitorChangeMarker is also walked for a leaked payload/dict.
    record_or_coalesce_event(
        conn, item_id, "price_drop", {"old_price": 299.0, "new_price": 199.0},
        "2026-07-20T00:00:00",
    )
    _add_item(conn, source_id, "p2", raw_metadata=_shopify_metadata(60.0, None, on_sale=False),
              score=40, inactive_at=_NOW.isoformat())
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    _assert_no_raw_metadata(page)


def test_no_tracker_model_contains_raw_metadata(conn):
    channel = _channel(conn, "Auctions", "tracker")
    source_id = create_source(conn, channel["id"], "all_about_auctions", {})
    _add_item(conn, source_id, "1-A", raw_metadata=_auction_metadata(_NOW + timedelta(hours=2)),
              score=91)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    _assert_no_raw_metadata(page)


# --------------------------------------------------------------------------------------------
# Editorial: read / vote / deep-read / comment presentation
# --------------------------------------------------------------------------------------------
def test_editorial_preserves_read_vote_deepread_and_comment(conn):
    channel = _channel(conn, "News", "editorial", highlight_count=1)
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    item_id = _add_item(
        conn, source_id, "r1", title="Rates fall",
        raw_metadata={"score": 12, "num_comments": 3}, score=88, summary="AI takeaway",
    )
    upsert_vote(conn, item_id, -1, "not relevant")
    update_best_comment(conn, item_id, "best comment here")
    request_deep_read(conn, item_id, _NOW)  # -> pending

    page = build_channel_page(
        conn, channel, t=_EN, now=_NOW, is_owner=True, csrf_token="csrf-1"
    )
    item = page.highlighted[0]
    assert item.is_read is False
    assert item.ai_score == 88
    assert item.ai_summary == "AI takeaway"
    assert item.source_label == "r/nz"
    assert item.engagement_label == "12 upvotes · 3 comments"
    assert item.best_comment_summary == "best comment here"
    # Vote: value + owner-only reason.
    assert item.vote_value == -1
    assert item.vote_reason == "not relevant"
    # Deep read action bundle.
    assert item.deep_read is not None
    assert item.deep_read.is_pending is True
    assert item.deep_read.csrf_token == "csrf-1"
    assert item.deep_read.brief_url == f"/items/{item_id}/brief?origin=channel&channel_id={channel['id']}"
    assert item.deep_read.request_url == f"/items/{item_id}/deep-read"


def test_editorial_hides_vote_reason_and_csrf_from_anonymous(conn):
    channel = _channel(conn, "News", "editorial")
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    item_id = _add_item(conn, source_id, "r1", raw_metadata={"score": 1, "num_comments": 0},
                        score=70)
    upsert_vote(conn, item_id, -1, "secret reason")

    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=False)
    item = page.highlighted[0]
    assert item.vote_value == -1
    assert item.vote_reason is None
    assert item.deep_read is not None
    assert item.deep_read.csrf_token is None
    assert item.deep_read.can_start is False


def test_editorial_unranked_item_has_no_deep_read(conn):
    channel = _channel(conn, "News", "editorial")
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    _add_item(conn, source_id, "r1", raw_metadata={"score": 1, "num_comments": 0}, score=None)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    assert page.highlighted[0].deep_read is None


def test_editorial_highlight_fold_split_and_show_read(conn):
    channel = _channel(conn, "News", "editorial", highlight_count=1)
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    _add_item(conn, source_id, "top", raw_metadata={"score": 1, "num_comments": 0}, score=95)
    _add_item(conn, source_id, "mid", raw_metadata={"score": 1, "num_comments": 0}, score=80)
    _add_item(conn, source_id, "old", raw_metadata={"score": 1, "num_comments": 0}, score=70,
              is_read=True)

    hidden = build_channel_page(conn, channel, t=_EN, now=_NOW, show_read=False)
    assert hidden.unread_count == 2
    assert hidden.read_count == 1
    assert len(hidden.highlighted) == 1  # highest unread
    assert len(hidden.folded) == 1  # remaining unread; read item excluded
    assert all(not v.is_read for v in hidden.highlighted + hidden.folded)

    shown = build_channel_page(conn, channel, t=_EN, now=_NOW, show_read=True)
    assert len(shown.highlighted) + len(shown.folded) == 3


def test_editorial_minimum_score_hides_low_scored_but_keeps_unranked(conn):
    channel = _channel(conn, "News", "editorial", minimum_score=90)
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    _add_item(conn, source_id, "hi", raw_metadata={"score": 1, "num_comments": 0}, score=91)
    _add_item(conn, source_id, "lo", raw_metadata={"score": 1, "num_comments": 0}, score=50)
    _add_item(conn, source_id, "unranked", raw_metadata={"score": 1, "num_comments": 0})

    page = build_channel_page(conn, channel, t=_EN, now=_NOW, show_read=True)
    kept = {v.id for v in page.highlighted + page.folded}
    ids = {
        ext: conn.execute("SELECT id FROM items WHERE external_id = ?", (ext,)).fetchone()["id"]
        for ext in ("hi", "lo", "unranked")
    }
    assert ids["hi"] in kept
    assert ids["unranked"] in kept
    assert ids["lo"] not in kept


def test_editorial_folded_items_are_paginated_without_repeating_highlights(conn):
    channel = _channel(conn, "News", "editorial", highlight_count=1)
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    for index in range(6):
        _add_item(
            conn,
            source_id,
            f"item-{index}",
            title=f"Item {index}",
            raw_metadata={"score": 1, "num_comments": 0},
            score=100 - index,
            summary=f"Summary {index}",
        )

    first = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        editorial_query=EditorialQuery(page=1, per_page=2),
    )
    second = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        editorial_query=EditorialQuery(page=2, per_page=2),
    )

    assert [item.ai_summary for item in first.highlighted] == ["Summary 0"]
    assert [item.ai_summary for item in first.folded] == ["Summary 1", "Summary 2"]
    assert second.highlighted == ()
    assert [item.ai_summary for item in second.folded] == ["Summary 3", "Summary 4"]
    assert second.folded_pagination.total == 5


def test_channel_criteria_reports_hidden_items_and_owner_override(conn):
    channel = _channel(conn, "News", "editorial", minimum_score=80)
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    _add_item(conn, source_id, "high", score=90)
    _add_item(conn, source_id, "low", score=70)
    _add_item(conn, source_id, "unranked")

    filtered = build_channel_page(conn, channel, t=_EN, now=_NOW, show_read=True)
    expanded = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        show_read=True,
        show_below_score=True,
    )

    assert filtered.criteria.total_count == 3
    assert filtered.criteria.matched_count == 2
    assert filtered.criteria.hidden_count == 1
    assert filtered.criteria.unranked_count == 1
    assert len(filtered.highlighted) == 2
    assert len(expanded.highlighted) == 3


# --------------------------------------------------------------------------------------------
# Monitor: active/history split, price/discount/availability, filter/sort/paginate
# --------------------------------------------------------------------------------------------
def _monitor_channel(conn):
    channel = _channel(conn, "Outlet", "monitor")
    source_id = create_source(
        conn, channel["id"], "shopify_collection",
        {"collection_url": "https://example.com/collections/outlet"},
    )
    return channel, source_id


def test_monitor_active_history_split_and_price_presentation(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "active", title="Beta Jacket",
              raw_metadata=_shopify_metadata(199.0, 299.0, on_sale=True), score=90)
    _add_item(conn, source_id, "gone", title="Sold Vest",
              raw_metadata=_shopify_metadata(60.0, None, on_sale=False, available=False),
              score=40, inactive_at=_NOW.isoformat())

    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert [v.title for v in page.items] == ["Beta Jacket"]
    assert [v.title for v in page.history] == ["Sold Vest"]

    active = page.items[0]
    assert active.price == 199.0
    assert active.price_label == "199"
    assert active.compare_at_price == 299.0
    assert active.discount_percent == 33  # round((299-199)/299*100)
    assert active.is_on_sale is True
    assert active.is_present is True
    assert active.is_available is True
    assert active.image_url == "https://cdn/x.jpg"
    assert active.source_label == "example.com/collections/outlet"

    gone = page.history[0]
    assert gone.discount_percent is None
    assert gone.is_present is False  # inactive/removed
    assert gone.is_available is False


def test_monitor_present_out_of_stock_goes_to_history_with_present_flag(conn):
    channel, source_id = _monitor_channel(conn)
    # Present in the latest snapshot (no inactive_at) but out of stock -> Unavailable history,
    # distinguished from a removed row by is_present staying True.
    _add_item(conn, source_id, "oos", title="Out Of Stock",
              raw_metadata=_shopify_metadata(50.0, 80.0, on_sale=True, available=False), score=70)
    # And an inactive/removed row -> also history, but is_present False.
    _add_item(conn, source_id, "removed", title="Removed",
              raw_metadata=_shopify_metadata(50.0, None, on_sale=False, available=True),
              score=71, inactive_at=_NOW.isoformat())

    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert page.items == ()  # nothing available+present
    history = {v.id: v for v in page.history}
    oos = next(v for v in page.history if v.title == "Out Of Stock")
    removed = next(v for v in page.history if v.title == "Removed")
    assert len(history) == 2
    assert oos.is_present is True and oos.is_available is False
    assert removed.is_present is False and removed.is_available is True


def test_monitor_returning_to_stock_moves_back_to_active(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "p1", title="Beanie",
                        raw_metadata=_shopify_metadata(20.0, None, on_sale=False, available=False),
                        score=80)
    # Out of stock -> history.
    before = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert before.items == ()
    assert [v.id for v in before.history] == [item_id]

    # Back in stock (availability flips true) -> Active on the next build.
    conn.execute(
        "UPDATE items SET raw_metadata = ? WHERE id = ?",
        (json.dumps(_shopify_metadata(20.0, None, on_sale=False, available=True)), item_id),
    )
    conn.commit()
    after = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert [v.id for v in after.items] == [item_id]
    assert after.history == ()
    assert after.items[0].is_present is True and after.items[0].is_available is True



def test_monitor_discount_only_when_on_sale_and_valid(conn):
    channel, source_id = _monitor_channel(conn)
    # on_sale flag false -> no discount even though compare > price.
    _add_item(conn, source_id, "flag_off",
              raw_metadata=_shopify_metadata(80.0, 100.0, on_sale=False), score=50)
    # compare below price -> no negative discount.
    _add_item(conn, source_id, "inverted",
              raw_metadata=_shopify_metadata(120.0, 100.0, on_sale=True), score=50)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    by_title = {v.id: v for v in page.items}
    assert all(v.discount_percent is None for v in by_title.values())


def test_monitor_filters_on_sale_and_vendor(conn):
    channel, source_id = _monitor_channel(conn)
    # All available, so all land in the Active section; filters narrow within it.
    _add_item(conn, source_id, "a", raw_metadata=_shopify_metadata(
        10.0, 20.0, on_sale=True, available=True, vendor="Teva"), score=60)
    _add_item(conn, source_id, "b", raw_metadata=_shopify_metadata(
        10.0, None, on_sale=False, available=True, vendor="Teva"), score=61)
    _add_item(conn, source_id, "c", raw_metadata=_shopify_metadata(
        10.0, 20.0, on_sale=True, available=True, vendor="Arc'teryx"), score=62)

    def ids(**kwargs):
        page = build_channel_page(
            conn, channel, t=_EN, now=_NOW, monitor_query=MonitorQuery(**kwargs)
        )
        return {v.id for v in page.items}

    assert len(ids()) == 3
    assert len(ids(on_sale_only=True)) == 2  # a, c
    assert len(ids(vendor="teva")) == 2  # a, b (case-insensitive)


def test_monitor_blank_vendor_filter_is_no_filter(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "a", raw_metadata=_shopify_metadata(
        10.0, None, on_sale=False, vendor="Teva"), score=60)
    _add_item(conn, source_id, "b", raw_metadata=_shopify_metadata(
        10.0, None, on_sale=False, vendor="Arc'teryx"), score=61)
    page = build_channel_page(
        conn, channel, t=_EN, now=_NOW, monitor_query=MonitorQuery(vendor="   ")
    )
    assert len(page.items) == 2


def test_monitor_search_source_filter_and_options(conn):
    channel, shopify_source_id = _monitor_channel(conn)
    land_sea_source_id = create_source(
        conn,
        channel["id"],
        "land_sea_collection",
        {"collection_url": "https://land-sea.example/sale"},
    )
    _add_item(
        conn,
        shopify_source_id,
        "jacket",
        title="Beta Jacket",
        raw_metadata=_shopify_metadata(
            100.0,
            None,
            on_sale=False,
            vendor="Arc'teryx",
        ),
        score=80,
        summary="Waterproof shell",
    )
    _add_item(
        conn,
        land_sea_source_id,
        "shoe",
        title="Trail Shoe",
        raw_metadata=_shopify_metadata(
            90.0,
            None,
            on_sale=False,
            vendor="Teva",
        ),
        score=81,
    )

    page = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        monitor_query=MonitorQuery(
            search="waterproof",
            source="example.com/collections/outlet",
        ),
    )

    assert [item.title for item in page.items] == ["Beta Jacket"]
    assert page.search == "waterproof"
    assert page.source == "example.com/collections/outlet"
    assert page.vendor_options == ("Arc'teryx", "Teva")
    assert page.source_options == (
        "example.com/collections/outlet",
        "land-sea.example/sale",
    )


def test_monitor_sort_orders_are_deterministic(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "cheap", raw_metadata=_shopify_metadata(
        10.0, 40.0, on_sale=True), score=50)  # 75% off
    _add_item(conn, source_id, "mid", raw_metadata=_shopify_metadata(
        20.0, 25.0, on_sale=True), score=90)  # 20% off
    _add_item(conn, source_id, "dear", raw_metadata=_shopify_metadata(
        30.0, None, on_sale=False), score=70)

    def prices(sort):
        page = build_channel_page(
            conn, channel, t=_EN, now=_NOW, monitor_query=MonitorQuery(sort=sort)
        )
        return [v.price for v in page.items]

    assert prices(MonitorSort.PRICE_ASC) == [10.0, 20.0, 30.0]
    assert prices(MonitorSort.PRICE_DESC) == [30.0, 20.0, 10.0]
    # SCORE: highest first.
    assert prices(MonitorSort.SCORE) == [20.0, 30.0, 10.0]
    # DISCOUNT: highest percent-off first, undiscounted last.
    assert prices(MonitorSort.DISCOUNT) == [10.0, 20.0, 30.0]


def test_monitor_pagination_windows_and_flags(conn):
    channel, source_id = _monitor_channel(conn)
    for i in range(5):
        _add_item(conn, source_id, f"p{i}",
                  raw_metadata=_shopify_metadata(float(i), None, on_sale=False),
                  score=100 - i)

    first = build_channel_page(
        conn, channel, t=_EN, now=_NOW, monitor_query=MonitorQuery(page=1, per_page=2)
    )
    assert len(first.items) == 2
    assert first.pagination.total == 5
    assert first.pagination.page_count == 3
    assert first.pagination.has_previous is False
    assert first.pagination.has_next is True

    last = build_channel_page(
        conn, channel, t=_EN, now=_NOW, monitor_query=MonitorQuery(page=3, per_page=2)
    )
    assert len(last.items) == 1
    assert last.pagination.has_previous is True
    assert last.pagination.has_next is False


def test_monitor_history_is_filtered_and_paginated(conn):
    channel, source_id = _monitor_channel(conn)
    for index in range(5):
        _add_item(
            conn,
            source_id,
            f"history-{index}",
            title=f"Retired {index}",
            raw_metadata=_shopify_metadata(
                float(index),
                None,
                on_sale=False,
                vendor="Retired vendor",
            ),
            score=100 - index,
            inactive_at=_NOW.isoformat(),
        )

    page = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        monitor_query=MonitorQuery(
            history_page=2,
            per_page=2,
            vendor="Retired vendor",
            search="Retired",
        ),
    )

    assert len(page.history) == 2
    assert page.history_pagination.total == 5
    assert page.history_pagination.page == 2
    assert page.history_pagination.has_previous is True
    assert page.history_pagination.has_next is True


def test_monitor_page_past_end_is_empty_not_error(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "only", raw_metadata=_shopify_metadata(1.0, None, on_sale=False),
              score=10)
    page = build_channel_page(
        conn, channel, t=_EN, now=_NOW, monitor_query=MonitorQuery(page=9, per_page=10)
    )
    assert page.items == ()
    assert page.pagination.total == 1
    assert page.pagination.has_next is False
    assert page.pagination.has_previous is True


def test_monitor_empty_channel_reports_empty_pagination(conn):
    channel, _ = _monitor_channel(conn)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert page.items == ()
    assert page.history == ()
    assert page.pagination.total == 0
    assert page.pagination.page_count == 0
    assert page.pagination.has_next is False
    assert page.pagination.has_previous is False


@pytest.mark.parametrize("page,per_page", [(0, 10), (-1, 10), (1, 0), (1, MAX_PER_PAGE + 1)])
def test_monitor_invalid_page_or_per_page_raises(conn, page, per_page):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "x", raw_metadata=_shopify_metadata(1.0, None, on_sale=False),
              score=10)
    with pytest.raises(ValueError):
        build_channel_page(
            conn, channel, t=_EN, now=_NOW,
            monitor_query=MonitorQuery(page=page, per_page=per_page),
        )


def test_pagination_defaults_and_validation_directly():
    assert Pagination(page=1, per_page=DEFAULT_PER_PAGE, total=0).page_count == 0
    p = Pagination(page=2, per_page=10, total=25)
    assert p.page_count == 3
    assert p.offset == 10
    assert p.has_previous is True
    assert p.has_next is True
    with pytest.raises(ValueError):
        Pagination(page=1, per_page=10, total=-1)


def test_monitor_tolerates_malformed_optional_metadata(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "bad", raw_metadata={
        "price": "not-a-number", "compare_at_price": None, "on_sale": True,
        "available": True, "image_url": 123, "vendor": None,
    }, score=50)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    item = page.items[0]
    assert item.price is None
    assert item.price_label is None
    assert item.discount_percent is None
    assert item.image_url is None  # non-string image ignored


def _record_event(conn, item_id, event_type, payload, observed_at="2026-07-20T00:00:00"):
    record_or_coalesce_event(conn, item_id, event_type, payload, observed_at)


def test_monitor_item_exposes_price_drop_marker_with_old_new(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "p1",
                        raw_metadata=_shopify_metadata(199.0, 299.0, on_sale=True), score=90)
    _record_event(conn, item_id, "price_drop", {"old_price": 299.0, "new_price": 199.0})
    change = build_channel_page(conn, channel, t=_EN, now=_NOW).items[0].change
    assert change is not None
    assert change.event_type == "price_drop"
    assert change.old_price == 299.0
    assert change.new_price == 199.0
    assert change.old_price_label == "299"
    assert change.new_price_label == "199"


def test_monitor_item_exposes_discovered_marker_without_prices(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "p1",
                        raw_metadata=_shopify_metadata(10.0, None, on_sale=False), score=50)
    _record_event(conn, item_id, "discovered", {})
    change = build_channel_page(conn, channel, t=_EN, now=_NOW).items[0].change
    assert change is not None
    assert change.event_type == "discovered"
    assert change.old_price is None
    assert change.new_price is None


def test_monitor_item_exposes_back_in_stock_marker(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "p1",
                        raw_metadata=_shopify_metadata(10.0, None, on_sale=False), score=50)
    _record_event(conn, item_id, "back_in_stock", {})
    change = build_channel_page(conn, channel, t=_EN, now=_NOW).items[0].change
    assert change is not None
    assert change.event_type == "back_in_stock"


def test_monitor_item_without_event_has_no_change(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "p1",
              raw_metadata=_shopify_metadata(10.0, None, on_sale=False), score=50)
    assert build_channel_page(conn, channel, t=_EN, now=_NOW).items[0].change is None


def test_monitor_marker_excludes_suppressed_event(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "p1",
                        raw_metadata=_shopify_metadata(10.0, None, on_sale=False), score=50)
    _record_event(conn, item_id, "discovered", {})
    suppress_item_events(conn, item_id, "2026-07-20T01:00:00")
    assert build_channel_page(conn, channel, t=_EN, now=_NOW).items[0].change is None


def test_monitor_marker_reflects_latest_event(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "p1",
                        raw_metadata=_shopify_metadata(199.0, 299.0, on_sale=True), score=90)
    _record_event(conn, item_id, "discovered", {}, "2026-07-19T00:00:00")
    _record_event(conn, item_id, "price_drop", {"old_price": 299.0, "new_price": 199.0},
                  "2026-07-21T00:00:00")
    change = build_channel_page(conn, channel, t=_EN, now=_NOW).items[0].change
    assert change is not None
    assert change.event_type == "price_drop"


def test_monitor_history_item_can_carry_change_marker(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "gone",
                        raw_metadata=_shopify_metadata(10.0, 20.0, on_sale=True, available=False),
                        score=40, inactive_at=_NOW.isoformat())
    _record_event(conn, item_id, "price_drop", {"old_price": 20.0, "new_price": 10.0})
    change = build_channel_page(conn, channel, t=_EN, now=_NOW).history[0].change
    assert change is not None
    assert change.event_type == "price_drop"


def test_tracker_item_has_no_change_marker_field(conn):
    # Tracker regular rows do not carry bid/status event badges.
    channel, source_id = _tracker_channel(conn)
    _add_item(conn, source_id, "1-A",
              raw_metadata=_auction_metadata(_NOW + timedelta(hours=2)), score=80)
    item = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True).ending_soon[0]
    assert not hasattr(item, "change")



@pytest.mark.parametrize("bad_image", [
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "/relative/path.jpg",
    "ftp://example.com/x.jpg",
    "",
])
def test_monitor_rejects_unsafe_image_url(conn, bad_image):
    channel, source_id = _monitor_channel(conn)
    md = _shopify_metadata(10.0, None, on_sale=False)
    md["image_url"] = bad_image
    _add_item(conn, source_id, "x", raw_metadata=md, score=50)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert page.items[0].image_url is None


def test_monitor_keeps_safe_https_image_url(conn):
    channel, source_id = _monitor_channel(conn)
    md = _shopify_metadata(10.0, None, on_sale=False)
    md["image_url"] = "https://cdn.example.com/pic.jpg"
    _add_item(conn, source_id, "x", raw_metadata=md, score=50)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW)
    assert page.items[0].image_url == "https://cdn.example.com/pic.jpg"


def test_monitor_unsafe_item_url_degrades_open_and_safe_url(conn):
    channel, source_id = _monitor_channel(conn)
    _add_item(conn, source_id, "x", url="javascript:alert(1)",
              raw_metadata=_shopify_metadata(10.0, None, on_sale=False), score=50)
    item = build_channel_page(conn, channel, t=_EN, now=_NOW).items[0]
    assert item.open_url == "#"
    assert item.safe_url == "#"


def test_monitor_safe_item_url_uses_internal_open_route(conn):
    channel, source_id = _monitor_channel(conn)
    item_id = _add_item(conn, source_id, "x", url="https://example.com/p",
                        raw_metadata=_shopify_metadata(10.0, None, on_sale=False), score=50)
    item = build_channel_page(conn, channel, t=_EN, now=_NOW).items[0]
    assert item.open_url == f"/items/{item_id}/open"
    assert item.safe_url == "https://example.com/p"


# --------------------------------------------------------------------------------------------
# Tracker: adapter facts, section split, generic watch state
# --------------------------------------------------------------------------------------------
def _tracker_channel(conn):
    channel = _channel(conn, "Auctions", "tracker")
    source_id = create_source(conn, channel["id"], "all_about_auctions", {})
    return channel, source_id


def test_tracker_exposes_adapter_facts(conn):
    channel, source_id = _tracker_channel(conn)
    _add_item(conn, source_id, "1-A", title="Vintage camera",
              raw_metadata=_auction_metadata(_NOW + timedelta(hours=2)), score=91,
              summary="Strong match")

    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    item = page.ending_soon[0]
    assert item.context == "Weekly auction"
    assert item.ai_score == 91
    assert item.ai_summary == "Strong match"
    assert item.status == "active"
    assert item.image_url == "https://cdn/lot.jpg"
    assert item.is_active is True
    assert item.deadline == (_NOW + timedelta(hours=2)).isoformat()
    assert item.deadline_label is not None
    assert item.deadline_relative_label == "Ends in 2 hours"
    assert item.reminder_due_at == (_NOW + timedelta(hours=1)).isoformat()
    assert "Current bid: NZD 500" in item.pricing_facts
    assert "Seller RRP: NZD 1,040 + GST" in item.pricing_facts
    assert "Estimate: NZD 700–NZD 900" in item.pricing_facts


def test_tracker_sections_split_watched_ending_upcoming_history(conn):
    channel, source_id = _tracker_channel(conn)
    ending = _add_item(conn, source_id, "ending",
                       raw_metadata=_auction_metadata(_NOW + timedelta(hours=2)), score=80)
    watched = _add_item(conn, source_id, "watched",
                        raw_metadata=_auction_metadata(_NOW + timedelta(hours=3)), score=81)
    _add_item(conn, source_id, "upcoming",
              raw_metadata=_auction_metadata(_NOW + timedelta(hours=48)), score=82)
    _add_item(conn, source_id, "past",
              raw_metadata=_auction_metadata(_NOW - timedelta(hours=1)), score=83)

    add_tracker_watch(conn, watched, _NOW)

    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    assert [v.id for v in page.watched] == [watched]
    assert [v.id for v in page.ending_soon] == [ending]
    assert {v.id for v in page.upcoming} == {
        conn.execute("SELECT id FROM items WHERE external_id='upcoming'").fetchone()["id"]
    }
    assert [v.id for v in page.history] == [
        conn.execute("SELECT id FROM items WHERE external_id='past'").fetchone()["id"]
    ]
    assert page.watched[0].is_watched is True
    assert page.watched[0].is_watchable is True


def test_tracker_filters_search_score_price_deadline_status_and_category(conn):
    channel, source_id = _tracker_channel(conn)
    cheap = _auction_metadata(
        _NOW + timedelta(hours=2),
        auction_title="Tools auction",
    )
    expensive = _auction_metadata(
        _NOW + timedelta(days=3),
        auction_title="Furniture auction",
    )
    expensive["estimated_cost"] = 2500.0
    _add_item(
        conn,
        source_id,
        "cheap",
        title="Cordless drill",
        raw_metadata=cheap,
        score=95,
        summary="Useful workshop tool",
        rationale="Matches the requested workshop equipment",
    )
    _add_item(
        conn,
        source_id,
        "expensive",
        title="Dining table",
        raw_metadata=expensive,
        score=70,
        summary="Large furniture",
        rationale="Weak match",
    )

    page = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        is_owner=True,
        tracker_query=TrackerQuery(
            search="workshop",
            source="All About Auctions",
            category="Tools auction",
            status=TrackerStatus.ENDING,
            deadline=TrackerDeadline.DAY,
            minimum_score=90,
            maximum_price=600,
        ),
    )

    assert [item.title for item in page.ending_soon] == ["Cordless drill"]
    assert page.ending_soon[0].ai_rationale == "Matches the requested workshop equipment"
    assert page.source_options == ("All About Auctions",)
    assert page.category_options == ("Furniture auction", "Tools auction")


def test_tracker_sections_paginate_independently(conn):
    channel, source_id = _tracker_channel(conn)
    for index in range(15):
        _add_item(
            conn,
            source_id,
            f"ending-{index}",
            raw_metadata=_auction_metadata(_NOW + timedelta(hours=index + 1)),
            score=80,
        )
        _add_item(
            conn,
            source_id,
            f"upcoming-{index}",
            raw_metadata=_auction_metadata(_NOW + timedelta(hours=48 + index)),
            score=80,
        )
        _add_item(
            conn,
            source_id,
            f"history-{index}",
            raw_metadata=_auction_metadata(_NOW - timedelta(hours=index + 1)),
            score=80,
        )

    page = build_channel_page(
        conn,
        channel,
        t=_EN,
        now=_NOW,
        tracker_query=TrackerQuery(
            ending_page=2,
            upcoming_page=2,
            history_page=2,
            per_page=10,
        ),
    )

    assert len(page.ending_soon) == 5
    assert len(page.upcoming) == 5
    assert len(page.history) == 5
    assert page.ending_pagination.total == 15
    assert page.upcoming_pagination.total == 15
    assert page.history_pagination.total == 15
    assert page.ending_pagination.page_count == 2
    assert page.upcoming_pagination.page_count == 2
    assert page.history_pagination.page_count == 2


def test_tracker_watch_state_is_generic_and_owner_scoped(conn):
    channel, source_id = _tracker_channel(conn)
    watched = _add_item(conn, source_id, "w",
                        raw_metadata=_auction_metadata(_NOW + timedelta(hours=2)), score=80)
    add_tracker_watch(conn, watched, _NOW)

    owner = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    assert [v.id for v in owner.watched] == [watched]
    assert owner.watched[0].is_watched is True

    anon = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=False)
    assert anon.watched == ()
    # Same lot is still active for an anonymous reader, just not watched/watchable.
    assert [v.id for v in anon.ending_soon] == [watched]
    assert anon.ending_soon[0].is_watched is False
    assert anon.ending_soon[0].is_watchable is False


def test_tracker_terminal_status_moves_lot_to_history(conn):
    channel, source_id = _tracker_channel(conn)
    _add_item(conn, source_id, "sold",
              raw_metadata=_auction_metadata(_NOW + timedelta(hours=2), status="sold"),
              score=80)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    assert [v.status for v in page.history] == ["sold"]
    assert page.ending_soon == ()
    assert page.history[0].is_active is False
    assert page.history[0].deadline_relative_label == "Closed"


def test_tracker_tolerates_malformed_deadline_without_crashing(conn):
    channel, source_id = _tracker_channel(conn)
    metadata = _auction_metadata(None)
    metadata["closing_at"] = "not-a-date"
    _add_item(conn, source_id, "bad", raw_metadata=metadata, score=80)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    # Unparseable deadline -> not active -> history, and never watchable.
    assert len(page.history) == 1
    assert page.history[0].is_active is False
    assert page.history[0].deadline is None
    assert page.history[0].is_watchable is False


def test_tracker_rejects_unsafe_image_url(conn):
    channel, source_id = _tracker_channel(conn)
    metadata = _auction_metadata(_NOW + timedelta(hours=2))
    metadata["image_url"] = "javascript:alert(1)"
    _add_item(conn, source_id, "1-A", raw_metadata=metadata, score=80)
    page = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True)
    assert page.ending_soon[0].image_url is None


def test_tracker_unsafe_item_url_degrades_open_and_safe_url(conn):
    channel, source_id = _tracker_channel(conn)
    _add_item(conn, source_id, "1-A", url="data:text/html,x",
              raw_metadata=_auction_metadata(_NOW + timedelta(hours=2)), score=80)
    item = build_channel_page(conn, channel, t=_EN, now=_NOW, is_owner=True).ending_soon[0]
    assert item.open_url == "#"
    assert item.safe_url == "#"


def test_editorial_unsafe_item_url_degrades_open_and_safe_url(conn):
    channel = _channel(conn, "News", "editorial")
    source_id = create_source(conn, channel["id"], "reddit_subreddit", {"subreddit": "nz"})
    _add_item(conn, source_id, "r1", url="ftp://example.com/x",
              raw_metadata={"score": 1, "num_comments": 0}, score=80)
    item = build_channel_page(conn, channel, t=_EN, now=_NOW).highlighted[0]
    assert item.open_url == "#"
    assert item.safe_url == "#"


# --------------------------------------------------------------------------------------------
# Watch List (generic, adapter-driven -- no auction-specific closing-time parsing)
# --------------------------------------------------------------------------------------------
def _watch(conn, source_id, external_id, closing_at, *, url="https://auctions.example/lot",
           image="https://cdn/lot.jpg", watched_at=_NOW):
    metadata = _auction_metadata(closing_at)
    metadata["image_url"] = image
    item_id = _add_item(conn, source_id, external_id, url=url, raw_metadata=metadata)
    add_tracker_watch(conn, item_id, watched_at)
    return item_id


def test_watchlist_builder_exposes_adapter_and_display_facts(conn):
    _, source_id = _tracker_channel(conn)
    item_id = _watch(conn, source_id, "1-A", _NOW + timedelta(hours=2))

    page = build_watchlist_page(conn, t=_EN, now=_NOW)
    assert isinstance(page, WatchlistPage)
    assert len(page.items) == 1
    row = page.items[0]
    assert row.id == item_id
    assert row.title == "Item"
    assert row.context == "Weekly auction"  # display_facts.context
    assert "Current bid: NZD 500" in row.pricing_facts  # display_facts.details
    assert "Seller RRP: NZD 1,040 + GST" in row.pricing_facts
    assert row.deadline == (_NOW + timedelta(hours=2)).isoformat()
    assert row.deadline_label is not None
    assert row.deadline_relative_label == "Ends in 2 hours"
    assert row.is_active is True
    assert row.is_closed is False
    assert row.is_watched is True
    assert row.is_watchable is True
    assert row.open_url == f"/items/{item_id}/open"


def test_watchlist_no_model_contains_raw_metadata(conn):
    _, source_id = _tracker_channel(conn)
    _watch(conn, source_id, "1-A", _NOW + timedelta(hours=2))
    _assert_no_raw_metadata(build_watchlist_page(conn, t=_EN, now=_NOW))


def test_watchlist_marks_closed_lot_via_generic_facts(conn):
    _, source_id = _tracker_channel(conn)
    _watch(conn, source_id, "1-A", _NOW + timedelta(hours=2))
    # Viewed after the deadline: closed/active come from adapter facts (list_tracker_watches),
    # not a connector-specific re-parse of closing_at.
    row = build_watchlist_page(
        conn,
        t=_EN,
        now=_NOW + timedelta(hours=3),
        query=WatchlistQuery(status="closed"),
    ).items[0]
    assert row.is_active is False
    assert row.is_closed is True
    assert row.is_watchable is True  # still removable on the owner page
    assert row.deadline_relative_label == "Closed"


def test_watchlist_validates_urls_and_images(conn):
    _, source_id = _tracker_channel(conn)
    _watch(conn, source_id, "1-A", _NOW + timedelta(hours=2),
           url="data:text/html,x", image="javascript:alert(1)")
    row = build_watchlist_page(conn, t=_EN, now=_NOW).items[0]
    assert row.open_url == "#"
    assert row.safe_url == "#"
    assert row.image_url is None


def test_watchlist_orders_active_before_closed(conn):
    _, source_id = _tracker_channel(conn)
    closed = _watch(conn, source_id, "closed", _NOW + timedelta(hours=1))
    active = _watch(conn, source_id, "active", _NOW + timedelta(hours=5))
    # Both watched while open; view once "closed" has elapsed but "active" has not.
    page = build_watchlist_page(
        conn,
        t=_EN,
        now=_NOW + timedelta(hours=2),
        query=WatchlistQuery(status="all"),
    )
    assert [row.id for row in page.items] == [active, closed]
    assert page.items[0].is_closed is False
    assert page.items[1].is_closed is True


def test_watchlist_empty_when_no_watches(conn):
    _tracker_channel(conn)
    assert build_watchlist_page(conn, t=_EN, now=_NOW).items == ()


def test_watchlist_filters_closed_items_and_exposes_counts(conn):
    _, source_id = _tracker_channel(conn)
    _watch(conn, source_id, "closed", _NOW + timedelta(hours=1))
    active = _watch(conn, source_id, "active", _NOW + timedelta(hours=5))

    page = build_watchlist_page(
        conn,
        t=_EN,
        now=_NOW + timedelta(hours=2),
        query=WatchlistQuery(status="active"),
    )

    assert [item.id for item in page.items] == [active]
    assert page.active_count == 1
    assert page.closed_count == 1
    assert page.total_count == 2


def test_watchlist_requires_timezone_aware_now(conn):
    _tracker_channel(conn)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_watchlist_page(conn, t=_EN, now=datetime(2026, 7, 22, 10, 0))
