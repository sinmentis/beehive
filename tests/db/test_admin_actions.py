import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from beehive.connectors.base import RawItem
from beehive.db.admin_actions import (
    clear_channel_with_undo,
    delete_channel_with_undo,
    delete_email_group_with_undo,
    delete_source_with_undo,
    list_admin_actions,
    undo_admin_action,
)
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import assign_channel, create_email_group
from beehive.db.items import insert_new
from beehive.db.sources import create_source, record_fetch_success


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    return connection


def _channel_content(conn):
    channel_id = create_channel(conn, "News", "profile")
    source_id = create_source(
        conn,
        channel_id,
        "reddit_subreddit",
        {"subreddit": "nz"},
    )
    insert_new(
        conn,
        source_id,
        RawItem(
            external_id="one",
            title="One",
            url="https://example.com/one",
        ),
    )
    item_id = conn.execute(
        "SELECT id FROM items WHERE source_id = ?",
        (source_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO votes (item_id, value) VALUES (?, 1)",
        (item_id,),
    )
    conn.execute(
        """
        INSERT INTO deep_reads (item_id, status, request_version, requested_at)
        VALUES (?, 'pending', 1, ?)
        """,
        (item_id, "2026-07-01T00:00:00+00:00"),
    )
    group_id = create_email_group(conn, "Daily", "owner@example.com", 24)
    assign_channel(conn, group_id, channel_id)
    conn.commit()
    return channel_id, source_id, item_id


def test_channel_delete_can_restore_cascading_content(conn):
    channel_id, source_id, item_id = _channel_content(conn)

    action_id = delete_channel_with_undo(
        conn,
        channel_id,
        target_label="News",
    )

    assert conn.execute(
        "SELECT 1 FROM channels WHERE id = ?", (channel_id,)
    ).fetchone() is None
    action = list_admin_actions(conn)[0]
    assert action["id"] == action_id
    assert action["detail"]["items"] == 1
    assert action["can_undo"] is True

    undo_admin_action(conn, action_id)

    assert conn.execute(
        "SELECT 1 FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT value FROM votes WHERE item_id = ?", (item_id,)
    ).fetchone()["value"] == 1
    assert conn.execute(
        "SELECT 1 FROM deep_reads WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM email_group_channels WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert list_admin_actions(conn)[0]["can_undo"] is False


def test_clear_channel_restores_items_and_source_fetch_state(conn):
    channel_id, source_id, item_id = _channel_content(conn)
    record_fetch_success(conn, source_id, "2026-07-02T00:00:00+00:00", 12, 3)

    action_id, cleared = clear_channel_with_undo(
        conn,
        channel_id,
        target_label="News",
    )

    assert cleared == 1
    assert conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone() is None
    assert conn.execute(
        "SELECT last_fetch_at FROM sources WHERE id = ?", (source_id,)
    ).fetchone()["last_fetch_at"] is None

    undo_admin_action(conn, action_id)

    assert conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    source = conn.execute(
        "SELECT last_fetch_at, last_fetch_status FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    assert source["last_fetch_at"] == "2026-07-02T00:00:00+00:00"
    assert source["last_fetch_status"] == "ok"


def test_source_delete_can_restore_source_and_items(conn):
    _, source_id, item_id = _channel_content(conn)

    action_id = delete_source_with_undo(
        conn,
        source_id,
        target_label="r/nz",
    )
    assert conn.execute(
        "SELECT 1 FROM sources WHERE id = ?", (source_id,)
    ).fetchone() is None

    undo_admin_action(conn, action_id)

    assert conn.execute(
        "SELECT 1 FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()


def test_email_group_delete_restores_group_and_memberships(conn):
    channel_id, _, _ = _channel_content(conn)
    group_id = conn.execute(
        "SELECT email_group_id FROM email_group_channels WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()["email_group_id"]

    action_id = delete_email_group_with_undo(
        conn,
        group_id,
        target_label="Daily",
    )
    assert conn.execute(
        "SELECT 1 FROM email_groups WHERE id = ?",
        (group_id,),
    ).fetchone() is None

    undo_admin_action(conn, action_id)

    assert conn.execute(
        "SELECT 1 FROM email_groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    assert conn.execute(
        """
        SELECT 1
        FROM email_group_channels
        WHERE email_group_id = ? AND channel_id = ?
        """,
        (group_id, channel_id),
    ).fetchone()


def test_undo_rejects_expired_action(conn):
    channel_id, _, _ = _channel_content(conn)
    action_id = delete_channel_with_undo(conn, channel_id, target_label="News")
    conn.execute(
        "UPDATE admin_actions SET undo_expires_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), action_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="expired"):
        undo_admin_action(conn, action_id)
    assert list_admin_actions(conn)[0]["can_undo"] is False


def test_undo_rejects_already_recovered_action(conn):
    channel_id, _, _ = _channel_content(conn)
    action_id = delete_channel_with_undo(conn, channel_id, target_label="News")
    undo_admin_action(conn, action_id)

    with pytest.raises(ValueError, match="already undone"):
        undo_admin_action(conn, action_id)


def test_restore_conflict_rolls_back_every_partial_write(conn):
    channel_id, source_id, _ = _channel_content(conn)
    action_id = delete_channel_with_undo(conn, channel_id, target_label="News")

    replacement_channel_id = create_channel(conn, "Replacement", "profile")
    conflicting_source_id = create_source(
        conn,
        replacement_channel_id,
        "reddit_subreddit",
        {"subreddit": "replacement"},
    )
    conn.execute(
        "UPDATE sources SET id = ? WHERE id = ?",
        (source_id, conflicting_source_id),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        undo_admin_action(conn, action_id)

    assert conn.execute(
        "SELECT 1 FROM channels WHERE id = ?",
        (channel_id,),
    ).fetchone() is None
    conflict = conn.execute(
        "SELECT channel_id FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    assert conflict["channel_id"] == replacement_channel_id
    assert list_admin_actions(conn)[0]["can_undo"] is True
