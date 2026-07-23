import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.item_events import (
    latest_actionable_events_for_items,
    list_ready_events_for_channels,
    mark_events_delivered,
    mark_item_events_ready,
    record_or_coalesce_event,
    suppress_item_events,
)
from beehive.db.items import insert_new
from beehive.db.sources import create_source


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def channel_id(conn):
    return create_channel(conn, "Clearance Watch", "shopping", kind="monitor")


@pytest.fixture
def source_id(conn, channel_id):
    return create_source(
        conn, channel_id, "shopify_collection",
        {"collection_url": "https://store.example.com/collections/outlet"},
    )


def _item(conn, source_id, external_id="1001", *, price=50.0):
    insert_new(
        conn, source_id,
        RawItem(
            external_id=external_id,
            title=f"Product {external_id}",
            url="https://store.example.com/products/alpha",
            raw_metadata={"price": price},
        ),
    )
    return conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id)).fetchone()["id"]


def test_record_inserts_a_pending_event(conn, source_id):
    item_id = _item(conn, source_id)
    event_id = record_or_coalesce_event(
        conn, item_id, "discovered", {"price": 50.0}, "2026-07-01T00:00:00"
    )
    row = conn.execute("SELECT * FROM item_events WHERE id = ?", (event_id,)).fetchone()
    assert row["event_type"] == "discovered"
    assert row["observed_at"] == "2026-07-01T00:00:00"
    assert row["ready_at"] is None
    assert row["suppressed_at"] is None
    assert row["delivered_at"] is None


def test_record_rejects_an_unknown_event_type(conn, source_id):
    item_id = _item(conn, source_id)
    with pytest.raises(ValueError, match="unknown item event type"):
        record_or_coalesce_event(conn, item_id, "flash_sale", {}, "2026-07-01T00:00:00")


def test_record_coalesces_a_second_open_event_of_the_same_type(conn, source_id):
    item_id = _item(conn, source_id)
    first = record_or_coalesce_event(
        conn, item_id, "price_drop", {"new": 40.0}, "2026-07-01T00:00:00"
    )
    second = record_or_coalesce_event(
        conn, item_id, "price_drop", {"new": 35.0}, "2026-07-02T00:00:00"
    )
    assert first == second
    rows = conn.execute(
        "SELECT * FROM item_events WHERE item_id = ? AND event_type = 'price_drop'", (item_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["observed_at"] == "2026-07-02T00:00:00"
    assert '"new":35.0' in rows[0]["payload"]


def test_coalescing_a_ready_event_requires_fresh_ai_approval(conn, source_id):
    item_id = _item(conn, source_id)
    event_id = record_or_coalesce_event(
        conn, item_id, "price_drop", {"new": 40.0}, "2026-07-01T00:00:00"
    )
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")

    coalesced_id = record_or_coalesce_event(
        conn, item_id, "price_drop", {"new": 35.0}, "2026-07-02T00:00:00"
    )

    assert coalesced_id == event_id
    row = conn.execute(
        "SELECT ready_at, payload FROM item_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row["ready_at"] is None
    assert '"new":35.0' in row["payload"]


def test_record_keeps_distinct_event_types_separate(conn, source_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    record_or_coalesce_event(conn, item_id, "price_drop", {}, "2026-07-01T00:00:00")
    assert conn.execute(
        "SELECT COUNT(*) FROM item_events WHERE item_id = ?", (item_id,)
    ).fetchone()[0] == 2


def test_a_delivered_event_does_not_block_a_new_one_of_the_same_type(conn, source_id, channel_id):
    item_id = _item(conn, source_id)
    first = record_or_coalesce_event(
        conn, item_id, "price_drop", {"new": 40.0}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")
    mark_events_delivered(conn, [first], "2026-07-01T02:00:00")

    second = record_or_coalesce_event(
        conn, item_id, "price_drop", {"new": 30.0}, "2026-07-05T00:00:00")
    assert second != first
    assert conn.execute(
        "SELECT COUNT(*) FROM item_events WHERE item_id = ?", (item_id,)
    ).fetchone()[0] == 2


def test_mark_ready_only_promotes_pending_events(conn, source_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    suppressed = record_or_coalesce_event(conn, item_id, "price_drop", {}, "2026-07-01T00:00:00")
    suppress_item_events(conn, item_id, "2026-07-01T00:30:00")  # suppress everything open
    # re-open a fresh discovered by recording again (the suppressed one is now closed)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T01:00:00")

    promoted = mark_item_events_ready(conn, item_id, "2026-07-01T02:00:00")
    assert promoted == 1  # only the still-pending discovered event
    assert conn.execute(
        "SELECT suppressed_at FROM item_events WHERE id = ?", (suppressed,)
    ).fetchone()["suppressed_at"] == "2026-07-01T00:30:00"


def test_suppress_closes_open_events_including_ready_ones(conn, source_id, channel_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")

    suppressed = suppress_item_events(conn, item_id, "2026-07-01T02:00:00")
    assert suppressed == 1
    assert list_ready_events_for_channels(conn, [channel_id]) == []


def test_list_ready_returns_only_ready_undelivered_events_with_context(
    conn, source_id, channel_id
):
    item_id = _item(conn, source_id, "1001", price=40.0)
    pending = record_or_coalesce_event(
        conn, item_id, "price_drop", {"old": 50, "new": 40}, "2026-07-01T00:00:00")
    # Not ready yet -> not listed.
    assert list_ready_events_for_channels(conn, [channel_id]) == []

    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")
    ready = list_ready_events_for_channels(conn, [channel_id])
    assert len(ready) == 1
    event = ready[0]
    assert event["id"] == pending
    assert event["payload"] == {"old": 50, "new": 40}  # decoded
    assert event["item_external_id"] == "1001"
    assert event["item_title"] == "Product 1001"
    assert event["source_type"] == "shopify_collection"
    assert event["channel_id"] == channel_id
    assert event["channel_kind"] == "monitor"
    assert event["item_raw_metadata"] == {"price": 40.0}


def test_list_ready_includes_ai_score_and_summary_context(conn, source_id, channel_id):
    item_id = _item(conn, source_id, "1001")
    conn.execute(
        "UPDATE items SET ai_score = 87, ai_summary = 'Great deal' WHERE id = ?", (item_id,))
    conn.commit()
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")

    event = list_ready_events_for_channels(conn, [channel_id])[0]
    assert event["item_ai_score"] == 87
    assert event["item_ai_summary"] == "Great deal"


def test_list_ready_orders_by_ai_score_high_to_low(conn, source_id, channel_id):
    for external_id, score in (("low", 10), ("high", 90), ("mid", 50)):
        item_id = _item(conn, source_id, external_id)
        conn.execute("UPDATE items SET ai_score = ? WHERE id = ?", (score, item_id))
        conn.commit()
        record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
        mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")

    ordered = list_ready_events_for_channels(conn, [channel_id])
    assert [e["item_external_id"] for e in ordered] == ["high", "mid", "low"]


def test_list_ready_breaks_score_ties_by_oldest_observed_then_id(conn, source_id, channel_id):
    # Same score -> oldest observed_at wins; identical observed_at -> lower event id wins.
    newer = _item(conn, source_id, "newer")
    older = _item(conn, source_id, "older")
    tie_a = _item(conn, source_id, "tie_a")
    tie_b = _item(conn, source_id, "tie_b")
    for item_id in (newer, older, tie_a, tie_b):
        conn.execute("UPDATE items SET ai_score = 70 WHERE id = ?", (item_id,))
    conn.commit()
    record_or_coalesce_event(conn, newer, "discovered", {}, "2026-07-02T00:00:00")
    record_or_coalesce_event(conn, older, "discovered", {}, "2026-07-01T00:00:00")
    first_tie = record_or_coalesce_event(conn, tie_a, "discovered", {}, "2026-07-03T00:00:00")
    second_tie = record_or_coalesce_event(conn, tie_b, "discovered", {}, "2026-07-03T00:00:00")
    for item_id in (newer, older, tie_a, tie_b):
        mark_item_events_ready(conn, item_id, "2026-07-04T00:00:00")

    ordered = list_ready_events_for_channels(conn, [channel_id])
    assert [e["item_external_id"] for e in ordered] == ["older", "newer", "tie_a", "tie_b"]
    assert first_tie < second_tie  # the id tie-break is deterministic, not incidental


def test_list_ready_filters_by_channel_and_handles_empty_input(conn, channel_id):
    other_channel = create_channel(conn, "Other", "p", kind="monitor")
    source_a = create_source(
        conn, channel_id, "shopify_collection",
        {"collection_url": "https://a.example.com/collections/o"})
    source_b = create_source(
        conn, other_channel, "shopify_collection",
        {"collection_url": "https://b.example.com/collections/o"})
    item_a = _item(conn, source_a, "a1")
    item_b = _item(conn, source_b, "b1")
    for item_id in (item_a, item_b):
        record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
        mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")

    only_a = list_ready_events_for_channels(conn, [channel_id])
    assert {e["item_external_id"] for e in only_a} == {"a1"}
    both = list_ready_events_for_channels(conn, [channel_id, other_channel])
    assert {e["item_external_id"] for e in both} == {"a1", "b1"}
    assert list_ready_events_for_channels(conn, []) == []


def test_list_ready_excludes_events_on_superseded_items(conn, source_id, channel_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")
    conn.execute(
        "UPDATE items SET superseded_at = '2026-07-02T00:00:00' WHERE id = ?", (item_id,))
    conn.commit()

    assert list_ready_events_for_channels(conn, [channel_id]) == []


def test_list_ready_excludes_events_on_inactive_items(conn, source_id, channel_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")
    conn.execute(
        "UPDATE items SET inactive_at = '2026-07-02T00:00:00' WHERE id = ?",
        (item_id,),
    )
    conn.commit()

    assert list_ready_events_for_channels(conn, [channel_id]) == []


def test_mark_delivered_only_touches_selected_ids(conn, source_id, channel_id):
    item_a = _item(conn, source_id, "a1")
    item_b = _item(conn, source_id, "b1")
    event_a = record_or_coalesce_event(conn, item_a, "discovered", {}, "2026-07-01T00:00:00")
    event_b = record_or_coalesce_event(conn, item_b, "discovered", {}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_a, "2026-07-01T01:00:00")
    mark_item_events_ready(conn, item_b, "2026-07-01T01:00:00")

    delivered = mark_events_delivered(conn, [event_a], "2026-07-01T02:00:00")
    assert delivered == 1
    remaining = list_ready_events_for_channels(conn, [channel_id])
    assert [e["id"] for e in remaining] == [event_b]
    assert conn.execute(
        "SELECT delivered_at FROM item_events WHERE id = ?", (event_a,)
    ).fetchone()["delivered_at"] == "2026-07-01T02:00:00"


def test_mark_delivered_is_idempotent_and_handles_empty(conn, source_id):
    item_id = _item(conn, source_id)
    event_id = record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")

    assert mark_events_delivered(conn, [event_id], "2026-07-01T02:00:00") == 1
    # Re-delivering the same id is a no-op (guarded by delivered_at IS NULL).
    assert mark_events_delivered(conn, [event_id], "2026-07-03T00:00:00") == 0
    assert mark_events_delivered(conn, [], "2026-07-03T00:00:00") == 0


def test_events_cascade_when_their_item_is_deleted(conn, source_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == 0


def test_latest_actionable_events_returns_most_recent_per_item(conn, source_id):
    a = _item(conn, source_id, "a")
    b = _item(conn, source_id, "b")
    record_or_coalesce_event(conn, a, "discovered", {}, "2026-07-01T00:00:00")
    record_or_coalesce_event(
        conn, a, "price_drop", {"old_price": 50.0, "new_price": 30.0}, "2026-07-02T00:00:00"
    )
    record_or_coalesce_event(conn, b, "discovered", {}, "2026-07-01T00:00:00")

    latest = latest_actionable_events_for_items(conn, [a, b, 999])
    assert set(latest) == {a, b}
    assert latest[a]["event_type"] == "price_drop"  # most recent for item a
    assert latest[a]["payload"] == {"old_price": 50.0, "new_price": 30.0}  # decoded
    assert latest[b]["event_type"] == "discovered"


def test_latest_actionable_events_excludes_suppressed(conn, source_id):
    item_id = _item(conn, source_id)
    record_or_coalesce_event(conn, item_id, "discovered", {}, "2026-07-01T00:00:00")
    suppress_item_events(conn, item_id, "2026-07-01T00:30:00")
    # Only event is suppressed -> item absent.
    assert latest_actionable_events_for_items(conn, [item_id]) == {}
    # A later, unsuppressed event becomes the marker.
    record_or_coalesce_event(
        conn, item_id, "price_drop", {"old_price": 9.0, "new_price": 5.0}, "2026-07-02T00:00:00"
    )
    latest = latest_actionable_events_for_items(conn, [item_id])
    assert latest[item_id]["event_type"] == "price_drop"


def test_latest_actionable_events_includes_delivered(conn, source_id):
    item_id = _item(conn, source_id)
    event_id = record_or_coalesce_event(
        conn, item_id, "back_in_stock", {}, "2026-07-01T00:00:00"
    )
    mark_item_events_ready(conn, item_id, "2026-07-01T01:00:00")
    mark_events_delivered(conn, [event_id], "2026-07-01T02:00:00")
    # Delivery state is ignored: a delivered change is still the listing's latest change.
    latest = latest_actionable_events_for_items(conn, [item_id])
    assert latest[item_id]["event_type"] == "back_in_stock"


def test_latest_actionable_events_empty_ids(conn):
    assert latest_actionable_events_for_items(conn, []) == {}
