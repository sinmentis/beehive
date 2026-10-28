import json
from datetime import datetime, timedelta, timezone

import pytest

from beehive.connectors.base import RawItem
from beehive.db import app_state
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.reading_actions import (
    change_read_state,
    get_reading_action,
    remove_watch_with_undo,
    undo_reading_action,
)
from beehive.db.sources import create_source
from beehive.db.tracker_watches import add_tracker_watch, claim_due_tracker_reminders

NOW = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
CLOSES = (NOW + timedelta(minutes=30)).isoformat()


@pytest.fixture
def data(tmp_path):
    conn = connect(str(tmp_path / "reading.db"))
    init_schema(conn)
    news = create_channel(conn, "News", "news")
    feed = create_source(conn, news, "rss_feed", {"feed_url": "https://example.com/feed"})
    lots = create_channel(conn, "Lots", "tools", kind="tracker")
    auction = create_source(conn, lots, "all_about_auctions", {})
    insert_new(conn, feed, RawItem(external_id="story", title="Story", url="https://example.com/story"))
    insert_new(
        conn, auction, RawItem(
            external_id="lot", title="Lot", url="https://example.com/lot",
            raw_metadata={"closing_at": CLOSES, "auction_title": "Workshop"},
        ),
    )
    story = conn.execute("SELECT id FROM items WHERE external_id = 'story'").fetchone()[0]
    lot = conn.execute("SELECT id FROM items WHERE external_id = 'lot'").fetchone()[0]
    add_tracker_watch(conn, lot, NOW)
    yield conn, story, lot
    conn.close()


@pytest.mark.parametrize("initial", [0, 1])
def test_read_undo_restores_the_original_state_once(data, initial):
    conn, story, _ = data
    conn.execute("UPDATE items SET is_read = ? WHERE id = ?", (initial, story))
    conn.commit()
    action = change_read_state(conn, story, not initial, NOW)
    assert action.can_undo
    assert undo_reading_action(conn, action.token, NOW + timedelta(seconds=5))
    assert conn.execute("SELECT is_read FROM items WHERE id = ?", (story,)).fetchone()[0] == initial
    assert not undo_reading_action(conn, action.token, NOW + timedelta(seconds=6))


def test_read_undo_does_not_override_a_later_open_or_replace_a_newer_action(data):
    conn, story, lot = data
    action = change_read_state(conn, story, True, NOW)
    conn.execute("UPDATE items SET opened_at = ? WHERE id = ?", ((NOW + timedelta(seconds=1)).isoformat(), story))
    conn.commit()
    assert not undo_reading_action(conn, action.token, NOW + timedelta(seconds=2))
    assert conn.execute("SELECT is_read FROM items WHERE id = ?", (story,)).fetchone()[0] == 1
    newer = remove_watch_with_undo(conn, lot, NOW + timedelta(seconds=3))
    assert not undo_reading_action(conn, action.token, NOW + timedelta(seconds=4))
    assert get_reading_action(conn, newer.token, NOW + timedelta(seconds=4)) == newer


def test_unchanged_read_state_and_expired_actions_do_not_offer_recovery(data):
    conn, story, _ = data
    no_change = change_read_state(conn, story, False, NOW)
    assert not no_change.can_undo
    action = change_read_state(conn, story, True, NOW)
    later = NOW + timedelta(minutes=11)
    assert get_reading_action(conn, action.token, later) is None
    assert not undo_reading_action(conn, action.token, later)
    assert conn.execute("SELECT is_read FROM items WHERE id = ?", (story,)).fetchone()[0] == 1


@pytest.mark.parametrize("sent", [False, True])
def test_unwatch_undo_restores_reminder_state_without_resetting_sent_delivery(data, sent):
    conn, _, lot = data
    conn.execute(
        "UPDATE auction_watches SET reminder_sent_for_closing_at = ?, reminder_sent_at = ?, "
        "last_error = ? WHERE item_id = ?",
        (CLOSES if sent else None, NOW.isoformat() if sent else None, None if sent else "Delivery failed", lot),
    )
    conn.commit()
    before = dict(conn.execute("SELECT * FROM auction_watches WHERE item_id = ?", (lot,)).fetchone())
    action = remove_watch_with_undo(conn, lot, NOW)
    assert conn.execute("SELECT 1 FROM auction_watches WHERE item_id = ?", (lot,)).fetchone() is None
    assert undo_reading_action(conn, action.token, NOW + timedelta(minutes=1))
    after = dict(conn.execute("SELECT * FROM auction_watches WHERE item_id = ?", (lot,)).fetchone())
    assert after == before
    if sent:
        assert claim_due_tracker_reminders(conn, NOW + timedelta(minutes=2)).items == []


def test_unwatch_undo_does_not_replace_a_later_watch(data):
    conn, _, lot = data
    action = remove_watch_with_undo(conn, lot, NOW)
    selected_at = NOW + timedelta(seconds=5)
    add_tracker_watch(conn, lot, selected_at)
    assert not undo_reading_action(conn, action.token, NOW + timedelta(seconds=6))
    assert conn.execute(
        "SELECT watched_at FROM auction_watches WHERE item_id = ?", (lot,),
    ).fetchone()[0] == selected_at.isoformat()


def test_an_inflight_reminder_is_not_promised_an_undo(data):
    conn, _, lot = data
    assert claim_due_tracker_reminders(conn, NOW).items
    action = remove_watch_with_undo(conn, lot, NOW)
    assert action.kind == "unwatch_inflight"
    assert not action.can_undo
    assert not undo_reading_action(conn, action.token, NOW + timedelta(seconds=1))
    assert conn.execute("SELECT 1 FROM auction_watches WHERE item_id = ?", (lot,)).fetchone() is None


def test_recording_failure_rolls_back_the_unwatch(data, monkeypatch):
    conn, _, lot = data

    def fail(*args, **kwargs):
        raise RuntimeError("Cannot record recovery")

    monkeypatch.setattr("beehive.db.reading_actions._record", fail)
    with pytest.raises(RuntimeError, match="Cannot record recovery"):
        remove_watch_with_undo(conn, lot, NOW)
    assert conn.execute("SELECT 1 FROM auction_watches WHERE item_id = ?", (lot,)).fetchone()


def test_corrupt_undo_snapshot_is_unavailable_without_changing_the_watch(data):
    conn, _, lot = data
    action = remove_watch_with_undo(conn, lot, NOW)
    record = json.loads(app_state.get(conn, "reading_action"))
    record["before"]["watched_at"] = {"invalid": "value"}
    app_state.set(conn, "reading_action", json.dumps(record))
    assert get_reading_action(conn, action.token, NOW) is None
    assert not undo_reading_action(conn, action.token, NOW)
    assert conn.execute("SELECT 1 FROM auction_watches WHERE item_id = ?", (lot,)).fetchone() is None
