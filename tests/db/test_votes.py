import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.sources import create_source
from beehive.db.votes import delete_vote, get_vote, get_vote_examples_for_channel, upsert_vote


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
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'Title', 'https://x')",
        (source_id,))
    conn.commit()
    return conn.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]


def test_upsert_vote_creates_vote(conn, item_id):
    upsert_vote(conn, item_id, 1)
    vote = get_vote(conn, item_id)
    assert vote["value"] == 1
    assert vote["reason"] is None


def test_upsert_vote_switches_polarity_and_sets_reason(conn, item_id):
    upsert_vote(conn, item_id, 1)
    upsert_vote(conn, item_id, -1, "not relevant to me")
    vote = get_vote(conn, item_id)
    assert vote["value"] == -1
    assert vote["reason"] == "not relevant to me"


def test_upsert_vote_updates_reason_only_keeping_polarity(conn, item_id):
    upsert_vote(conn, item_id, -1, "first reason")
    upsert_vote(conn, item_id, -1, "revised reason")
    vote = get_vote(conn, item_id)
    assert vote["value"] == -1
    assert vote["reason"] == "revised reason"


def test_delete_vote_removes_it(conn, item_id):
    upsert_vote(conn, item_id, 1)
    delete_vote(conn, item_id)
    assert get_vote(conn, item_id) is None


def test_get_vote_returns_none_when_never_voted(conn, item_id):
    assert get_vote(conn, item_id) is None


def _make_item_and_vote(conn, channel_id, external_id, value, reason=None):
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id=external_id, title=f"Title {external_id}",
                                          url="https://x"))
    item_id = conn.execute(
        "SELECT id FROM items WHERE external_id=?", (external_id,)).fetchone()[0]
    upsert_vote(conn, item_id, value, reason)
    return item_id


def test_sampling_returns_all_votes_when_under_target(conn):
    channel_id = create_channel(conn, "C", "profile")
    _make_item_and_vote(conn, channel_id, "t1", 1)
    _make_item_and_vote(conn, channel_id, "t2", -1, "not relevant")

    examples = get_vote_examples_for_channel(conn, channel_id)
    assert len(examples) == 2
    values = sorted(e["value"] for e in examples)
    assert values == [-1, 1]


def test_sampling_caps_each_polarity_at_15_when_both_plentiful(conn):
    channel_id = create_channel(conn, "C", "profile")
    for i in range(20):
        _make_item_and_vote(conn, channel_id, f"up{i}", 1)
    for i in range(20):
        _make_item_and_vote(conn, channel_id, f"down{i}", -1)

    examples = get_vote_examples_for_channel(conn, channel_id)
    ups = [e for e in examples if e["value"] == 1]
    downs = [e for e in examples if e["value"] == -1]
    assert len(ups) == 15
    assert len(downs) == 15
    assert len(examples) == 30


def test_sampling_backfills_from_majority_when_minority_short(conn):
    channel_id = create_channel(conn, "C", "profile")
    for i in range(8):
        _make_item_and_vote(conn, channel_id, f"up{i}", 1)
    for i in range(25):
        _make_item_and_vote(conn, channel_id, f"down{i}", -1)

    examples = get_vote_examples_for_channel(conn, channel_id)
    ups = [e for e in examples if e["value"] == 1]
    downs = [e for e in examples if e["value"] == -1]
    assert len(ups) == 8  # never evicted, even though it's the minority
    assert len(downs) == 22  # 15 target + 7 backfilled from the shortfall
    assert len(examples) == 30


def test_sampling_prefers_most_recent_within_polarity(conn):
    # Both polarities must be plentiful (well above the 15 target) so the shortfall-backfill
    # path (Task 2's other test) is not triggered — this test isolates the plain "most recent
    # 15" selection within a single polarity. With downs left empty, the backfill rule would
    # instead pull in ALL 20 ups (nothing dropped), which is correct behavior but would make
    # this specific recency assertion meaningless.
    channel_id = create_channel(conn, "C", "profile")
    for i in range(20):
        item_id = _make_item_and_vote(conn, channel_id, f"up{i}", 1)
        conn.execute("UPDATE votes SET voted_at = ? WHERE item_id = ?",
                     (f"2026-01-{i + 1:02d}T00:00:00", item_id))
    for i in range(20):
        _make_item_and_vote(conn, channel_id, f"down{i}", -1)
    conn.commit()

    examples = get_vote_examples_for_channel(conn, channel_id)
    titles = {e["title"] for e in examples if e["value"] == 1}
    # the 15 most recent are up19..up5 (voted_at 2026-01-20 down to 2026-01-06)
    assert "Title up19" in titles
    assert "Title up0" not in titles  # oldest, dropped by the cap (both polarities plentiful)
    assert "Title up0" not in titles  # oldest, should have been dropped by the cap
