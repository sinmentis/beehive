import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import (create_channel, delete_channel, get_channel, list_channels,
                                     mark_digest_sent, update_channel)
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.sources import create_source


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_create_and_get_channel(conn):
    channel_id = create_channel(conn, "NZ Finance", "economic news", fetch_interval_hours=3)
    row = get_channel(conn, channel_id)
    assert row["name"] == "NZ Finance"
    assert row["profile"] == "economic news"
    assert row["fetch_interval_hours"] == 3
    assert row["highlight_count"] == 8
    assert row["minimum_score"] == 0


def test_create_channel_saves_display_settings(conn):
    channel_id = create_channel(
        conn,
        "NZ Finance",
        "economic news",
        highlight_count=5,
        minimum_score=72,
    )

    row = get_channel(conn, channel_id)
    assert row["highlight_count"] == 5
    assert row["minimum_score"] == 72


def test_create_channel_defaults_kind_to_editorial(conn):
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    assert get_channel(conn, channel_id)["kind"] == "editorial"


def test_create_channel_accepts_monitor_kind(conn):
    channel_id = create_channel(conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor")
    assert get_channel(conn, channel_id)["kind"] == "monitor"


def test_create_channel_rejects_unknown_kind(conn):
    with pytest.raises(ValueError):
        create_channel(conn, "Bad", "profile", kind="subscription")


def test_get_channel_missing_returns_none(conn):
    assert get_channel(conn, 999) is None


def test_list_channels(conn):
    create_channel(conn, "A", "profile a")
    create_channel(conn, "B", "profile b")
    names = {row["name"] for row in list_channels(conn)}
    assert names == {"A", "B"}


def test_list_channels_unfiltered_includes_both_kinds(conn):
    create_channel(conn, "News", "profile a", kind="editorial")
    create_channel(conn, "Outlet", "profile b", kind="monitor")
    names = {row["name"] for row in list_channels(conn)}
    assert names == {"News", "Outlet"}


def test_list_channels_filters_by_kind(conn):
    create_channel(conn, "News", "profile a", kind="editorial")
    create_channel(conn, "Outlet", "profile b", kind="monitor")

    editorial_names = {row["name"] for row in list_channels(conn, kind="editorial")}
    monitor_names = {row["name"] for row in list_channels(conn, kind="monitor")}

    assert editorial_names == {"News"}
    assert monitor_names == {"Outlet"}


def test_list_channels_rejects_unknown_kind_filter(conn):
    with pytest.raises(ValueError):
        list_channels(conn, kind="subscription")


def test_update_channel_changes_fields(conn):
    channel_id = create_channel(conn, "Old Name", "old profile", fetch_interval_hours=3)
    update_channel(conn, channel_id, "New Name", "new profile",
                   fetch_interval_hours=6, digest_email=None,
                   highlight_count=4, minimum_score=65)
    channel = get_channel(conn, channel_id)
    assert channel["name"] == "New Name"
    assert channel["profile"] == "new profile"
    assert channel["fetch_interval_hours"] == 6
    assert channel["highlight_count"] == 4
    assert channel["minimum_score"] == 65


def test_delete_channel_removes_it(conn):
    channel_id = create_channel(conn, "Test", "profile")
    delete_channel(conn, channel_id)
    assert get_channel(conn, channel_id) is None


def test_delete_channel_cascades_to_sources_and_items(conn):
    channel_id = create_channel(conn, "Test", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="T", url="https://x"))
    delete_channel(conn, channel_id)
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_update_channel_saves_and_clears_digest_email(conn):
    channel_id = create_channel(conn, "Email Test", "profile")
    update_channel(
        conn, channel_id, "Email Test", "profile",
        fetch_interval_hours=3, digest_email="channel@example.com")
    assert get_channel(conn, channel_id)["digest_email"] == "channel@example.com"

    update_channel(
        conn, channel_id, "Email Test", "profile",
        fetch_interval_hours=3, digest_email=None)
    assert get_channel(conn, channel_id)["digest_email"] is None


def test_update_channel_requires_explicit_digest_email(conn):
    """digest_email must be passed explicitly so a caller can never silently clear an
    override by omitting the argument."""
    channel_id = create_channel(conn, "Email Test", "profile")
    with pytest.raises(TypeError):
        update_channel(conn, channel_id, "Email Test", "profile", fetch_interval_hours=3)


def test_update_channel_blank_email_stores_sql_null(conn):
    """An empty override string is normalized to SQL NULL, not stored as ''."""
    channel_id = create_channel(conn, "Email Test", "profile")
    update_channel(
        conn, channel_id, "Email Test", "profile",
        fetch_interval_hours=3, digest_email="channel@example.com")
    update_channel(
        conn, channel_id, "Email Test", "profile",
        fetch_interval_hours=3, digest_email="")
    stored = conn.execute(
        "SELECT digest_email FROM channels WHERE id = ?", (channel_id,)).fetchone()[0]
    assert stored is None


def test_mark_digest_sent_updates_all_channels_in_one_call(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    mark_digest_sent(
        conn, [first, second],
        sent_at="2026-07-13T20:00:00+00:00",
        digest_date="2026-07-13")
    rows = list_channels(conn)
    assert {row["last_digest_sent_at"] for row in rows} == {
        "2026-07-13T20:00:00+00:00"
    }
    assert {row["last_digest_date"] for row in rows} == {"2026-07-13"}
