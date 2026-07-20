import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import (create_channel, delete_channel, duplicate_channel, get_channel,
                                     list_channels, mark_digest_sent, update_channel)
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import (assign_channel, create_email_group, get_channel_group,
                                      list_member_channels)
from beehive.db.items import insert_new
from beehive.db.sources import create_source, list_by_channel


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


def test_duplicate_channel_copies_settings_with_suffixed_name(conn):
    channel_id = create_channel(
        conn, "NZ Finance", "economic news", fetch_interval_hours=6,
        highlight_count=5, minimum_score=20, kind="editorial")
    update_channel(
        conn, channel_id, "NZ Finance", "economic news", fetch_interval_hours=6,
        digest_email="owner@example.com", highlight_count=5, minimum_score=20)

    new_id = duplicate_channel(conn, channel_id)

    assert new_id != channel_id
    copy = get_channel(conn, new_id)
    assert copy["name"] == "NZ Finance (copy)"
    assert copy["profile"] == "economic news"
    assert copy["fetch_interval_hours"] == 6
    assert copy["highlight_count"] == 5
    assert copy["minimum_score"] == 20
    assert copy["kind"] == "editorial"
    assert copy["digest_email"] == "owner@example.com"


def test_duplicate_channel_avoids_name_collisions(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    first_copy_id = duplicate_channel(conn, channel_id)
    second_copy_id = duplicate_channel(conn, channel_id)

    assert get_channel(conn, first_copy_id)["name"] == "NZ Finance (copy)"
    assert get_channel(conn, second_copy_id)["name"] == "NZ Finance (copy 2)"


def test_duplicate_channel_copies_sources_verbatim(conn):
    channel_id = create_channel(conn, "Clearance watch", "profile", kind="monitor")
    create_source(conn, channel_id, "shopify_collection", {
        "collection_url": "https://bivouac.co.nz/collections/clearance",
        "vendors": ["Arcteryx", "Patagonia"],
    })
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "nzoutdoors"})

    new_id = duplicate_channel(conn, channel_id)

    original_sources = {s["type"]: s["config"] for s in list_by_channel(conn, channel_id)}
    copied_sources = {s["type"]: s["config"] for s in list_by_channel(conn, new_id)}
    assert copied_sources == original_sources
    assert len(list_by_channel(conn, new_id)) == 2


def test_duplicate_channel_does_not_copy_items_or_digest_state(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    mark_digest_sent(
        conn, [channel_id], sent_at="2026-07-13T20:00:00+00:00", digest_date="2026-07-13")

    new_id = duplicate_channel(conn, channel_id)

    copy = get_channel(conn, new_id)
    assert copy["last_digest_sent_at"] is None
    assert copy["last_digest_date"] is None
    new_source_id = list_by_channel(conn, new_id)[0]["id"]
    rows = conn.execute(
        "SELECT COUNT(*) FROM items WHERE source_id = ?", (new_source_id,)).fetchone()
    assert rows[0] == 0


def test_duplicate_channel_returns_none_for_missing_channel(conn):
    assert duplicate_channel(conn, 999) is None


def test_duplicate_channel_copies_email_group_membership(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    group_id = create_email_group(conn, "Weekly roundup")
    assign_channel(conn, group_id, channel_id)

    new_id = duplicate_channel(conn, channel_id)

    assert get_channel_group(conn, new_id)["id"] == group_id
    assert {c["id"] for c in list_member_channels(conn, group_id)} == {channel_id, new_id}


def test_duplicate_channel_without_a_group_leaves_the_copy_ungrouped(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")

    new_id = duplicate_channel(conn, channel_id)

    assert get_channel_group(conn, new_id) is None
