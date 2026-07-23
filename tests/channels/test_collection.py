"""Unit tests for beehive.channels.collection.ChannelCollection: the typed handle that turns a
successful fetch into the right persistence (append vs reconciled snapshot) and actionable-event
staging for a Channel's kind, and settles those events on the AI score. These drive the collection
directly against a real SQLite connection; the end-to-end wiring through the collector lives in
tests/collector/test_run_cycle.py."""
import dataclasses
import json

import pytest

from beehive.ai.prompt_builder import ProductCandidate
from beehive.channels.collection import RANKING_METADATA_KEYS, ChannelCollection
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel, get_channel
from beehive.db.connection import connect, init_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _channel(conn, kind, *, minimum_score=0):
    channel_id = create_channel(
        conn, f"{kind} channel", "profile", minimum_score=minimum_score, kind=kind
    )
    return get_channel(conn, channel_id)


def _source(conn, channel_id, type_key="stub_source"):
    # A bare Source row: ingest_fetch takes RawItems directly, so the connector type is irrelevant
    # here and the create_source compatibility gate is deliberately bypassed.
    cur = conn.execute(
        "INSERT INTO sources (channel_id, type, config) VALUES (?, ?, '{}')",
        (channel_id, type_key),
    )
    conn.commit()
    return cur.lastrowid


def _product(external_id="1001", *, title="Alpha", price=50.0, available=True, **extra):
    metadata = {"price": price, "available": available}
    metadata.update(extra)
    return RawItem(
        external_id=external_id,
        title=title,
        url=f"https://store.example.com/products/{external_id}",
        body="",
        raw_metadata=metadata,
    )


def _events(conn, item_id):
    return conn.execute(
        "SELECT event_type, ready_at, suppressed_at, delivered_at, payload "
        "FROM item_events WHERE item_id = ? ORDER BY id",
        (item_id,),
    ).fetchall()


def _item_id(conn, source_id, external_id):
    return conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()["id"]


# ---------------------------------------------------------------------------
# Registry / definition wiring
# ---------------------------------------------------------------------------


def test_ranking_metadata_keys_match_product_candidate_fields():
    # The rerank fingerprint must track exactly the raw_metadata a ProductCandidate consumes, so a
    # future field added to the ranker is not silently ignored by snapshot reranking.
    candidate_fields = {f.name for f in dataclasses.fields(ProductCandidate)}
    expected = candidate_fields - {"item_key", "title", "description"}
    assert RANKING_METADATA_KEYS == expected


def test_for_channel_rejects_an_unknown_kind(conn):
    with pytest.raises(ValueError, match="unknown Channel kind"):
        ChannelCollection.for_channel({"kind": "subscription", "minimum_score": 0})


def test_is_mutable_follows_the_definition(conn):
    assert ChannelCollection.for_channel(_channel(conn, "editorial")).is_mutable is False
    assert ChannelCollection.for_channel(_channel(conn, "monitor")).is_mutable is True
    assert ChannelCollection.for_channel(_channel(conn, "tracker")).is_mutable is True


# ---------------------------------------------------------------------------
# APPEND (editorial) persistence + discovered events
# ---------------------------------------------------------------------------


def test_append_inserts_new_rows_and_stages_discovered(conn):
    channel = _channel(conn, "editorial")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    new_count = collection.ingest_fetch(
        conn,
        source_id,
        [
            RawItem(external_id="a", title="A", url="https://x/a"),
            RawItem(external_id="b", title="B", url="https://x/b"),
        ],
        now_iso="2026-07-01T00:00:00",
    )
    assert new_count == 2
    for external_id in ("a", "b"):
        events = _events(conn, _item_id(conn, source_id, external_id))
        assert [e["event_type"] for e in events] == ["discovered"]
        assert events[0]["ready_at"] is None  # pending until ranking


def test_append_duplicate_refetch_inserts_nothing_and_stages_no_event(conn):
    channel = _channel(conn, "editorial")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)
    item = [RawItem(external_id="a", title="A", url="https://x/a")]

    assert collection.ingest_fetch(conn, source_id, item, now_iso="2026-07-01T00:00:00") == 1
    # Second sighting of the same external_id is a dedup drop: no new row, no second discovered.
    assert collection.ingest_fetch(conn, source_id, item, now_iso="2026-07-02T00:00:00") == 0
    assert len(_events(conn, _item_id(conn, source_id, "a"))) == 1


def test_append_does_not_backfill_a_preexisting_row(conn):
    # A row already present before this cycle (no historical backfill) must never be discovered.
    channel = _channel(conn, "editorial")
    source_id = _source(conn, channel["id"])
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 'old', 'Old', 'https://x')",
        (source_id,),
    )
    conn.commit()
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn,
        source_id,
        [RawItem(external_id="old", title="Old", url="https://x")],
        now_iso="2026-07-01T00:00:00",
    )
    assert _events(conn, _item_id(conn, source_id, "old")) == []


# ---------------------------------------------------------------------------
# MUTABLE_SNAPSHOT (monitor) persistence, reranking, reconciliation
# ---------------------------------------------------------------------------


def test_snapshot_inserts_new_row_and_stages_discovered(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    new_count = collection.ingest_fetch(
        conn, source_id, [_product("1001")], now_iso="2026-07-01T00:00:00"
    )
    assert new_count == 1
    events = _events(conn, _item_id(conn, source_id, "1001"))
    assert [e["event_type"] for e in events] == ["discovered"]


def test_snapshot_refreshes_a_stable_row_in_place_preserving_id(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(conn, source_id, [_product("1001")], now_iso="2026-07-01T00:00:00")
    first_id = _item_id(conn, source_id, "1001")
    conn.execute("UPDATE items SET ai_score = 70 WHERE id = ?", (first_id,))
    conn.commit()

    # Same external_id, a changed title (ranking-relevant) -> same row, re-enters the backlog.
    new_count = collection.ingest_fetch(
        conn,
        source_id,
        [_product("1001", title="Alpha v2")],
        now_iso="2026-07-02T00:00:00",
    )
    assert new_count == 0
    assert _item_id(conn, source_id, "1001") == first_id  # id preserved
    row = conn.execute("SELECT * FROM items WHERE id = ?", (first_id,)).fetchone()
    assert row["title"] == "Alpha v2"
    assert row["ai_score"] is None  # reranked
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_snapshot_price_drop_reranks_and_stages_price_drop(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, body, raw_metadata, "
        "ai_score, ai_summary, ai_rationale, last_seen_at) "
        "VALUES (?, '1001', 'Alpha', 'https://store.example.com/products/1001', '', ?, 80, 's', 'r', '2026-07-01T00:00:00')",
        (source_id, json.dumps({"price": 50.0, "available": True})),
    )
    conn.commit()
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn,
        source_id,
        [_product("1001", price=40.0)],
        now_iso="2026-07-02T00:00:00",
    )
    item_id = _item_id(conn, source_id, "1001")
    # A price move is ranking-relevant now, so the row re-enters the backlog.
    assert conn.execute("SELECT ai_score FROM items WHERE id = ?", (item_id,)).fetchone()[0] is None
    events = _events(conn, item_id)
    assert [e["event_type"] for e in events] == ["price_drop"]
    assert json.loads(events[0]["payload"]) == {"old_price": 50.0, "new_price": 40.0}


def test_snapshot_image_only_change_does_not_rerank_or_stage_an_event(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, body, raw_metadata, "
        "ai_score, ai_summary, ai_rationale, last_seen_at) "
        "VALUES (?, '1001', 'Alpha', 'https://store.example.com/products/1001', '', ?, 80, 's', 'r', '2026-07-01T00:00:00')",
        (source_id, json.dumps({"price": 50.0, "available": True, "image_url": "a.jpg"})),
    )
    conn.commit()
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn,
        source_id,
        [_product("1001", price=50.0, available=True, image_url="b.jpg")],
        now_iso="2026-07-02T00:00:00",
    )
    item_id = _item_id(conn, source_id, "1001")
    row = conn.execute("SELECT ai_score, raw_metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["ai_score"] == 80  # not reranked -- image url is outside the ranking fingerprint
    assert json.loads(row["raw_metadata"])["image_url"] == "b.jpg"  # still refreshed in place
    assert _events(conn, item_id) == []


def test_snapshot_back_in_stock_transition_stages_event(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn, source_id, [_product("1001", available=False)], now_iso="2026-07-01T00:00:00"
    )
    item_id = _item_id(conn, source_id, "1001")
    conn.execute("DELETE FROM item_events WHERE item_id = ?", (item_id,))  # drop the discovered
    conn.commit()

    collection.ingest_fetch(
        conn, source_id, [_product("1001", available=True)], now_iso="2026-07-02T00:00:00"
    )
    assert [e["event_type"] for e in _events(conn, item_id)] == ["back_in_stock"]


def test_snapshot_reappearance_after_inactive_stages_back_in_stock(conn):
    # The reappearance path the spec calls out: a listing recorded unavailable is retired by an
    # empty snapshot, then returns available -- the false->true metadata transition still fires
    # back_in_stock even though the row went inactive in between.
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn, source_id, [_product("1001", available=False)], now_iso="2026-07-01T00:00:00"
    )
    item_id = _item_id(conn, source_id, "1001")
    conn.execute("DELETE FROM item_events WHERE item_id = ?", (item_id,))
    conn.commit()
    # A complete empty snapshot retires the listing.
    collection.ingest_fetch(conn, source_id, [], now_iso="2026-07-02T00:00:00")
    assert conn.execute(
        "SELECT inactive_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()["inactive_at"] is not None

    # It comes back, now available.
    collection.ingest_fetch(
        conn, source_id, [_product("1001", available=True)], now_iso="2026-07-03T00:00:00"
    )
    assert conn.execute(
        "SELECT inactive_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()["inactive_at"] is None  # revived in place
    assert [e["event_type"] for e in _events(conn, item_id)] == ["back_in_stock"]


def test_snapshot_new_count_counts_only_genuine_inserts(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    assert collection.ingest_fetch(
        conn, source_id, [_product("a"), _product("b")], now_iso="2026-07-01T00:00:00"
    ) == 2
    # a and b already exist; only c is genuinely new.
    assert collection.ingest_fetch(
        conn,
        source_id,
        [_product("a"), _product("b"), _product("c")],
        now_iso="2026-07-02T00:00:00",
    ) == 1


def test_snapshot_reconciles_absent_items_to_inactive(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn,
        source_id,
        [_product("a"), _product("b"), _product("c")],
        now_iso="2026-07-01T00:00:00",
    )
    # b is gone from the next complete snapshot -> it goes inactive, a and c stay active.
    collection.ingest_fetch(
        conn, source_id, [_product("a"), _product("c")], now_iso="2026-07-02T00:00:00"
    )
    inactive = {
        row["external_id"]
        for row in conn.execute(
            "SELECT external_id FROM items WHERE inactive_at IS NOT NULL"
        ).fetchall()
    }
    assert inactive == {"b"}
    item_b = _item_id(conn, source_id, "b")
    event = conn.execute(
        "SELECT suppressed_at FROM item_events WHERE item_id = ?", (item_b,)
    ).fetchone()
    assert event["suppressed_at"] == "2026-07-02T00:00:00"


def test_snapshot_empty_fetch_retires_every_active_item(conn):
    channel = _channel(conn, "monitor")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn, source_id, [_product("a"), _product("b")], now_iso="2026-07-01T00:00:00"
    )
    # A successful fetch that returned nothing means the whole catalogue is gone.
    assert collection.ingest_fetch(conn, source_id, [], now_iso="2026-07-02T00:00:00") == 0
    active = conn.execute(
        "SELECT COUNT(*) FROM items WHERE inactive_at IS NULL"
    ).fetchone()[0]
    assert active == 0


# ---------------------------------------------------------------------------
# TRACKER: mutable, but the definition permits only DISCOVERED
# ---------------------------------------------------------------------------


def test_tracker_stages_discovered_but_never_price_or_stock_events(conn):
    channel = _channel(conn, "tracker")
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)

    collection.ingest_fetch(
        conn,
        source_id,
        [_product("lot-1", price=100.0, available=False)],
        now_iso="2026-07-01T00:00:00",
    )
    item_id = _item_id(conn, source_id, "lot-1")
    assert [e["event_type"] for e in _events(conn, item_id)] == ["discovered"]

    # A price drop AND a restock in one update -- a monitor would stage both, a tracker neither.
    collection.ingest_fetch(
        conn,
        source_id,
        [_product("lot-1", price=60.0, available=True)],
        now_iso="2026-07-02T00:00:00",
    )
    assert [e["event_type"] for e in _events(conn, item_id)] == ["discovered"]


# ---------------------------------------------------------------------------
# settle_item_events: gate staged events on the AI score
# ---------------------------------------------------------------------------


def test_settle_marks_events_ready_at_or_above_minimum_score(conn):
    channel = _channel(conn, "monitor", minimum_score=50)
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)
    collection.ingest_fetch(conn, source_id, [_product("1001")], now_iso="2026-07-01T00:00:00")
    item_id = _item_id(conn, source_id, "1001")

    collection.settle_item_events(conn, item_id, 60.0, now_iso="2026-07-01T01:00:00")
    event = _events(conn, item_id)[0]
    assert event["ready_at"] == "2026-07-01T01:00:00"
    assert event["suppressed_at"] is None


def test_settle_suppresses_events_below_minimum_score(conn):
    channel = _channel(conn, "monitor", minimum_score=50)
    source_id = _source(conn, channel["id"])
    collection = ChannelCollection.for_channel(channel)
    collection.ingest_fetch(conn, source_id, [_product("1001")], now_iso="2026-07-01T00:00:00")
    item_id = _item_id(conn, source_id, "1001")

    collection.settle_item_events(conn, item_id, 40.0, now_iso="2026-07-01T01:00:00")
    event = _events(conn, item_id)[0]
    assert event["ready_at"] is None
    assert event["suppressed_at"] == "2026-07-01T01:00:00"


def test_settle_is_a_noop_for_an_item_with_no_events(conn):
    channel = _channel(conn, "monitor", minimum_score=50)
    source_id = _source(conn, channel["id"])
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 'x', 'X', 'https://x')",
        (source_id,),
    )
    conn.commit()
    collection = ChannelCollection.for_channel(channel)
    item_id = _item_id(conn, source_id, "x")

    collection.settle_item_events(conn, item_id, 10.0, now_iso="2026-07-01T01:00:00")  # no raise
    assert _events(conn, item_id) == []
