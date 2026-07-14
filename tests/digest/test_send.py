from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sources import create_source, record_fetch_error
from beehive.digest.send import send_daily_digest
from beehive.email_routing import ResolvedRecipient

DEFAULT_RECIPIENT = ResolvedRecipient("default@example.com", "database")
RUN_TIME = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _set_channel_email(conn, channel_id, address):
    conn.execute(
        "UPDATE channels SET digest_email = ? WHERE id = ?",
        (address, channel_id))
    conn.commit()


def test_first_ever_digest_includes_all_scored_items(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(conn, source_id, "t1", score=90, summary="RBNZ 降息", rationale="r")

    notifier = MagicMock()
    send_daily_digest(conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, html = notifier.send.call_args[0]
    assert "RBNZ 降息" in plain_text
    assert "RBNZ 降息" in html


def test_next_days_digest_only_includes_items_since_first_send(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(
        conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(
        conn, source_id,
        RawItem(external_id="t1", title="Old", url="https://x"))
    update_ai_ranking(
        conn, source_id, "t1",
        score=90, summary="old news", rationale="r")
    conn.execute(
        "UPDATE items SET fetched_at = ? WHERE source_id = ? AND external_id = ?",
        ((RUN_TIME - timedelta(hours=1)).isoformat(), source_id, "t1"),
    )
    conn.commit()

    notifier = MagicMock()
    send_daily_digest(
        conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)
    notifier.reset_mock()
    next_day = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    send_daily_digest(
        conn, notifier, DEFAULT_RECIPIENT, now=next_day)

    _, plain_text, html = notifier.send.call_args.args
    assert "old news" not in plain_text
    assert "今天没有新内容" in plain_text
    assert "今天没有新内容" in html


def test_digest_includes_source_failure_warning(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T00:00:00")

    notifier = MagicMock()
    send_daily_digest(conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)
    _, plain_text, html = notifier.send.call_args[0]
    assert "timeout" in plain_text
    assert "timeout" in html


def test_successful_send_updates_channel_checkpoint(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    send_daily_digest(
        conn, MagicMock(), DEFAULT_RECIPIENT, now=RUN_TIME)
    channel = conn.execute(
        "SELECT last_digest_sent_at, last_digest_date FROM channels WHERE id = ?",
        (channel_id,)).fetchone()
    assert channel["last_digest_sent_at"] == RUN_TIME.isoformat()
    assert channel["last_digest_date"] == "2026-07-13"


def test_channels_with_same_recipient_share_one_email(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    _set_channel_email(conn, first, "shared@example.com")
    _set_channel_email(conn, second, "shared@example.com")
    notifier = MagicMock()

    send_daily_digest(
        conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)

    notifier.send.assert_called_once()
    _subject, plain_text, html = notifier.send.call_args.args
    assert "First" in plain_text and "Second" in plain_text
    assert "First" in html and "Second" in html
    assert notifier.send.call_args.kwargs["to_addr"] == "shared@example.com"


def test_different_recipients_receive_only_their_channels(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    _set_channel_email(conn, first, "first@example.com")
    _set_channel_email(conn, second, "second@example.com")
    notifier = MagicMock()

    send_daily_digest(
        conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)

    assert notifier.send.call_count == 2
    bodies = {
        call.kwargs["to_addr"]: call.args[1]
        for call in notifier.send.call_args_list
    }
    assert "First" in bodies["first@example.com"]
    assert "Second" not in bodies["first@example.com"]
    assert "Second" in bodies["second@example.com"]
    assert "First" not in bodies["second@example.com"]


def test_partial_failure_advances_only_successful_group_and_continues(conn):
    success = create_channel(conn, "Success", "profile")
    failure = create_channel(conn, "Failure", "profile")
    _set_channel_email(conn, success, "success@example.com")
    _set_channel_email(conn, failure, "failure@example.com")
    notifier = MagicMock()

    def send(subject, plain_text, html=None, *, to_addr=None):
        if to_addr == "failure@example.com":
            raise RuntimeError("mailbox unavailable")

    notifier.send.side_effect = send

    with pytest.raises(ExceptionGroup, match="digest recipient"):
        send_daily_digest(
            conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)

    rows = {
        row["name"]: row
        for row in conn.execute(
            "SELECT name, last_digest_sent_at, last_digest_date FROM channels")
    }
    assert rows["Success"]["last_digest_date"] == "2026-07-13"
    assert rows["Failure"]["last_digest_sent_at"] is None
    assert rows["Failure"]["last_digest_date"] is None
    assert notifier.send.call_count == 2


def test_same_date_retry_skips_successful_channel_entirely(conn):
    success = create_channel(conn, "Success", "profile")
    failure = create_channel(conn, "Failure", "profile")
    _set_channel_email(conn, success, "success@example.com")
    _set_channel_email(conn, failure, "failure@example.com")
    conn.execute(
        "UPDATE channels SET last_digest_sent_at = ?, last_digest_date = ? WHERE id = ?",
        (RUN_TIME.isoformat(), "2026-07-13", success))
    conn.commit()
    notifier = MagicMock()

    send_daily_digest(
        conn, notifier, DEFAULT_RECIPIENT, now=RUN_TIME)

    notifier.send.assert_called_once()
    assert notifier.send.call_args.kwargs["to_addr"] == "failure@example.com"
    assert "Success" not in notifier.send.call_args.args[1]


def test_missing_recipient_is_logged_by_channel_and_still_raised(conn, capsys):
    create_channel(conn, "Orphan Channel", "profile")
    missing_default = ResolvedRecipient(None, "missing")
    notifier = MagicMock()

    with pytest.raises(ExceptionGroup, match="digest recipient"):
        send_daily_digest(conn, notifier, missing_default, now=RUN_TIME)

    assert "Orphan Channel" in capsys.readouterr().out
    notifier.send.assert_not_called()


def test_later_date_includes_previously_successful_channel_again(conn):
    channel_id = create_channel(conn, "Success", "profile")
    _set_channel_email(conn, channel_id, "success@example.com")
    conn.execute(
        "UPDATE channels SET last_digest_sent_at = ?, last_digest_date = ? WHERE id = ?",
        (RUN_TIME.isoformat(), "2026-07-13", channel_id))
    conn.commit()
    notifier = MagicMock()
    next_day = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)

    send_daily_digest(
        conn, notifier, DEFAULT_RECIPIENT, now=next_day)

    notifier.send.assert_called_once()
    assert "今天没有新内容" in notifier.send.call_args.args[1]
