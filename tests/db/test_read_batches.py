from datetime import datetime, timedelta, timezone

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new, mark_channel_read, mark_read
from beehive.db.read_batches import (
    UNDO_WINDOW,
    get_read_batch,
    record_read_batch,
    undo_read_batch,
)
from beehive.db.sources import create_source

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def items(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    channel_id = create_channel(conn, "News", "news")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    for external_id in ("a", "b", "c"):
        insert_new(conn, source_id, RawItem(external_id=external_id, title=external_id, url="https://x"))
    ids = [row[0] for row in conn.execute("SELECT id FROM items ORDER BY id")]
    return conn, channel_id, ids


def _unread(conn):
    return [row[0] for row in conn.execute("SELECT id FROM items WHERE is_read = 0 ORDER BY id")]


def test_marking_a_channel_read_returns_only_the_stories_it_changed(items):
    conn, channel_id, (a, b, c) = items
    mark_read(conn, a)

    assert sorted(mark_channel_read(conn, channel_id)) == [b, c]
    assert mark_channel_read(conn, channel_id) == []


def test_undo_restores_exactly_the_batch(items):
    conn, channel_id, (a, b, c) = items
    mark_read(conn, a)
    batch = record_read_batch(conn, mark_channel_read(conn, channel_id), _NOW)

    assert batch is not None and batch.count == 2
    assert get_read_batch(conn, batch.token, _NOW + timedelta(minutes=5)) == batch
    assert undo_read_batch(conn, batch.token, _NOW + timedelta(minutes=5)) == 2
    # The story read by hand before the batch stays read.
    assert _unread(conn) == [b, c]
    # An undone batch is gone.
    assert get_read_batch(conn, batch.token, _NOW) is None
    assert undo_read_batch(conn, batch.token, _NOW) == 0


def test_a_batch_expires_and_needs_its_own_token(items):
    conn, channel_id, _ = items
    batch = record_read_batch(conn, mark_channel_read(conn, channel_id), _NOW)
    late = _NOW + UNDO_WINDOW + timedelta(seconds=1)

    assert get_read_batch(conn, "not-the-token", _NOW) is None
    assert get_read_batch(conn, None, _NOW) is None
    assert get_read_batch(conn, batch.token, late) is None
    assert undo_read_batch(conn, batch.token, late) == 0
    assert _unread(conn) == []


def test_nothing_marked_keeps_no_batch_and_a_new_batch_replaces_the_last(items):
    conn, channel_id, (a, b, c) = items
    assert record_read_batch(conn, [], _NOW) is None

    first = record_read_batch(conn, [a], _NOW)
    second = record_read_batch(conn, [b, c], _NOW)

    assert get_read_batch(conn, first.token, _NOW) is None
    assert get_read_batch(conn, second.token, _NOW).count == 2
