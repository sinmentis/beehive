import pytest

from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import (assign_channel, create_email_group, delete_email_group,
                                      get_channel_group, get_email_group, list_email_groups,
                                      list_member_channels, mark_sent, unassign_channel,
                                      update_email_group)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_create_and_get_email_group(conn):
    group_id = create_email_group(
        conn, "Weekly roundup", subject_template="Weekly · {date}",
        recipient_email="owner@example.com", send_interval_hours=168)
    group = get_email_group(conn, group_id)
    assert group["name"] == "Weekly roundup"
    assert group["subject_template"] == "Weekly · {date}"
    assert group["recipient_email"] == "owner@example.com"
    assert group["send_interval_hours"] == 168
    assert group["last_sent_at"] is None


def test_create_email_group_defaults(conn):
    group_id = create_email_group(conn, "Weekly roundup")
    group = get_email_group(conn, group_id)
    assert group["subject_template"] == ""
    assert group["recipient_email"] is None
    assert group["send_interval_hours"] == 24


def test_get_email_group_returns_none_for_missing_group(conn):
    assert get_email_group(conn, 999) is None


def test_list_email_groups_orders_by_id(conn):
    # A fresh DB already contains one migration-created "Default" email group (see
    # _migrate_default_email_group) -- assert only that newly created groups are appended
    # after whatever already exists, in id order.
    existing_ids = [g["id"] for g in list_email_groups(conn)]
    first = create_email_group(conn, "A")
    second = create_email_group(conn, "B")
    groups = list_email_groups(conn)
    assert [g["id"] for g in groups] == existing_ids + [first, second]


def test_update_email_group_changes_fields(conn):
    group_id = create_email_group(conn, "Weekly roundup", send_interval_hours=24)
    update_email_group(
        conn, group_id, "Renamed", "New subject · {date}", "new@example.com", 168)
    group = get_email_group(conn, group_id)
    assert group["name"] == "Renamed"
    assert group["subject_template"] == "New subject · {date}"
    assert group["recipient_email"] == "new@example.com"
    assert group["send_interval_hours"] == 168


def test_update_email_group_blank_recipient_clears_override(conn):
    group_id = create_email_group(conn, "Weekly roundup", recipient_email="owner@example.com")
    update_email_group(conn, group_id, "Weekly roundup", "", "", 24)
    assert get_email_group(conn, group_id)["recipient_email"] is None


def test_delete_email_group_removes_it(conn):
    group_id = create_email_group(conn, "Weekly roundup")
    delete_email_group(conn, group_id)
    assert get_email_group(conn, group_id) is None


def test_delete_email_group_leaves_member_channels_intact(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    group_id = create_email_group(conn, "Weekly roundup")
    assign_channel(conn, group_id, channel_id)

    delete_email_group(conn, group_id)

    assert get_channel_group(conn, channel_id) is None
    conn.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone()


def test_assign_channel_adds_membership(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    group_id = create_email_group(conn, "Weekly roundup")
    assign_channel(conn, group_id, channel_id)
    assert get_channel_group(conn, channel_id)["id"] == group_id
    assert [c["id"] for c in list_member_channels(conn, group_id)] == [channel_id]


def test_assign_channel_moves_between_groups(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    first_group = create_email_group(conn, "Weekly roundup")
    second_group = create_email_group(conn, "Monthly digest")
    assign_channel(conn, first_group, channel_id)

    assign_channel(conn, second_group, channel_id)

    assert get_channel_group(conn, channel_id)["id"] == second_group
    assert list_member_channels(conn, first_group) == []
    assert [c["id"] for c in list_member_channels(conn, second_group)] == [channel_id]


def test_unassign_channel_removes_membership(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    group_id = create_email_group(conn, "Weekly roundup")
    assign_channel(conn, group_id, channel_id)

    unassign_channel(conn, channel_id)

    assert get_channel_group(conn, channel_id) is None
    assert list_member_channels(conn, group_id) == []


def test_get_channel_group_returns_none_when_unassigned(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    assert get_channel_group(conn, channel_id) is None


def test_deleting_a_channel_removes_its_group_membership(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    group_id = create_email_group(conn, "Weekly roundup")
    assign_channel(conn, group_id, channel_id)

    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()

    assert list_member_channels(conn, group_id) == []


def test_mark_sent_updates_last_sent_at(conn):
    group_id = create_email_group(conn, "Weekly roundup")
    mark_sent(conn, group_id, "2026-07-13T20:00:00+00:00")
    assert get_email_group(conn, group_id)["last_sent_at"] == "2026-07-13T20:00:00+00:00"
