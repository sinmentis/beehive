import json
from datetime import datetime, timedelta, timezone

import pytest

from beehive.channels.source_policy import connector_supports_kind
from beehive.connectors.base import RawItem
from beehive.db.auction_watches import (
    add_auction_watch,
    claim_due_auction_reminders,
    complete_auction_reminder_claim,
    fail_auction_reminder_claim,
    get_watched_item_ids,
    list_auction_watches,
    remove_auction_watch,
)
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.sources import create_source
from beehive.domain.channels import ChannelKind

_NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    return connection


def _add_item(
    conn,
    external_id: str,
    *,
    closing_at: datetime | None,
    source_type: str = "all_about_auctions",
) -> int:
    # Pair the Channel kind with whatever kind this Source type is compatible with (each real
    # connector supports exactly one), so create_source's compatibility gate is satisfied while
    # still exercising both an auction (tracker) and a non-auction (editorial) Source.
    kind = next(k for k in ChannelKind if connector_supports_kind(source_type, k))
    channel_id = create_channel(
        conn,
        f"Channel {external_id}",
        "profile",
        kind=kind.value,
    )
    source_id = create_source(conn, channel_id, source_type, {})
    metadata = {"listing_kind": "auction_lot"}
    if closing_at is not None:
        metadata["closing_at"] = closing_at.isoformat()
    insert_new(
        conn,
        source_id,
        RawItem(
            external_id=external_id,
            title=f"Lot {external_id}",
            url=f"https://example.com/{external_id}",
            raw_metadata=metadata,
        ),
    )
    return conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()["id"]


def _set_closing_at(conn, item_id: int, closing_at: datetime) -> None:
    row = conn.execute(
        "SELECT raw_metadata FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    metadata = json.loads(row["raw_metadata"])
    metadata["closing_at"] = closing_at.isoformat()
    conn.execute(
        "UPDATE items SET raw_metadata = ? WHERE id = ?",
        (json.dumps(metadata), item_id),
    )
    conn.commit()


def test_add_list_and_remove_one_watch_per_auction_item(conn):
    item_id = _add_item(conn, "lot-1", closing_at=_NOW + timedelta(hours=2))

    assert add_auction_watch(conn, item_id, _NOW) is True
    assert add_auction_watch(conn, item_id, _NOW + timedelta(minutes=1)) is False
    assert get_watched_item_ids(conn, [item_id, 999]) == {item_id}

    watches = list_auction_watches(conn, _NOW)
    assert len(watches) == 1
    assert watches[0]["item_id"] == item_id
    assert watches[0]["title"] == "Lot lot-1"
    assert watches[0]["closing_at"] == (_NOW + timedelta(hours=2)).isoformat()
    assert watches[0]["is_closed"] is False

    assert remove_auction_watch(conn, item_id) is True
    assert remove_auction_watch(conn, item_id) is False


@pytest.mark.parametrize(
    ("source_type", "closing_at"),
    [
        ("reddit_subreddit", _NOW + timedelta(hours=2)),
        ("all_about_auctions", None),
        ("all_about_auctions", _NOW),
        ("all_about_auctions", _NOW - timedelta(seconds=1)),
    ],
)
def test_add_rejects_non_auction_missing_or_closed_items(conn, source_type, closing_at):
    item_id = _add_item(
        conn,
        f"invalid-{source_type}-{closing_at}",
        source_type=source_type,
        closing_at=closing_at,
    )

    with pytest.raises(ValueError):
        add_auction_watch(conn, item_id, _NOW)


def test_item_deletion_cascades_to_watch(conn):
    item_id = _add_item(conn, "lot-delete", closing_at=_NOW + timedelta(hours=2))
    add_auction_watch(conn, item_id, _NOW)

    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()

    assert list_auction_watches(conn, _NOW) == []


def test_claims_only_unsent_watches_inside_the_one_hour_window(conn):
    due_id = _add_item(conn, "due", closing_at=_NOW + timedelta(hours=1))
    later_id = _add_item(conn, "later", closing_at=_NOW + timedelta(hours=1, seconds=1))
    for item_id in (due_id, later_id):
        add_auction_watch(conn, item_id, _NOW - timedelta(hours=1))

    claim = claim_due_auction_reminders(conn, _NOW)

    assert {item["item_id"] for item in claim.items} == {due_id}
    assert claim.token
    assert (
        claim.items[0]["claimed_closing_at"] == (_NOW + timedelta(hours=1)).isoformat()
    )
    assert claim_due_auction_reminders(conn, _NOW).items == []

    assert (
        complete_auction_reminder_claim(conn, claim.token, _NOW + timedelta(minutes=1))
        == 1
    )
    next_claim = claim_due_auction_reminders(conn, _NOW + timedelta(minutes=2))
    assert [item["item_id"] for item in next_claim.items] == [later_id]


def test_extended_closing_time_can_generate_a_new_reminder(conn):
    item_id = _add_item(conn, "extended", closing_at=_NOW + timedelta(hours=1))
    add_auction_watch(conn, item_id, _NOW - timedelta(hours=2))
    first_claim = claim_due_auction_reminders(conn, _NOW)
    complete_auction_reminder_claim(conn, first_claim.token, _NOW)

    extended_close = _NOW + timedelta(hours=2)
    _set_closing_at(conn, item_id, extended_close)

    assert claim_due_auction_reminders(conn, _NOW + timedelta(minutes=30)).items == []
    second_claim = claim_due_auction_reminders(conn, _NOW + timedelta(hours=1))
    assert [item["item_id"] for item in second_claim.items] == [item_id]
    assert second_claim.items[0]["claimed_closing_at"] == extended_close.isoformat()


def test_failed_claim_is_released_for_retry(conn):
    item_id = _add_item(conn, "retry", closing_at=_NOW + timedelta(minutes=45))
    add_auction_watch(conn, item_id, _NOW - timedelta(hours=1))
    claim = claim_due_auction_reminders(conn, _NOW)

    assert fail_auction_reminder_claim(conn, claim.token, "provider down") == 1

    retry = claim_due_auction_reminders(conn, _NOW + timedelta(minutes=1))
    assert [item["item_id"] for item in retry.items] == [item_id]


def test_expired_claim_is_recovered_after_fifteen_minutes(conn):
    item_id = _add_item(conn, "lease", closing_at=_NOW + timedelta(minutes=50))
    add_auction_watch(conn, item_id, _NOW - timedelta(hours=1))
    first = claim_due_auction_reminders(conn, _NOW)

    assert (
        claim_due_auction_reminders(
            conn, _NOW + timedelta(minutes=14, seconds=59)
        ).items
        == []
    )
    recovered = claim_due_auction_reminders(
        conn, _NOW + timedelta(minutes=15, seconds=1)
    )
    assert [item["item_id"] for item in recovered.items] == [item_id]
    assert recovered.token != first.token
