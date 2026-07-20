from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import assign_channel, create_email_group, mark_sent
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sources import create_source, record_fetch_error
from beehive.digest.send import send_email_group_digests
from beehive.email_routing import ResolvedRecipient
from beehive.localization import localizer_for

DEFAULT_RECIPIENT = ResolvedRecipient("default@example.com", "database")
RUN_TIME = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
_EN = localizer_for("en")
_ZH = localizer_for("zh-CN")


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def _make_group(conn, *channel_ids, name="Group", subject_template="Digest \u00b7 {date}",
                recipient_email=None, send_interval_hours=24):
    group_id = create_email_group(
        conn, name, subject_template=subject_template,
        recipient_email=recipient_email, send_interval_hours=send_interval_hours)
    for channel_id in channel_ids:
        assign_channel(conn, group_id, channel_id)
    return group_id


def test_first_ever_digest_includes_all_scored_items(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(conn, source_id, "t1", score=90, summary="RBNZ cuts rates", rationale="r")
    _make_group(conn, channel_id)

    notifier = MagicMock()
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, html = notifier.send.call_args[0]
    assert "RBNZ cuts rates" in plain_text
    assert "RBNZ cuts rates" in html


def test_digest_uses_channel_highlight_count_and_minimum_score(conn):
    channel_id = create_channel(
        conn,
        "NZ Finance",
        "profile",
        highlight_count=1,
        minimum_score=80,
    )
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    for external_id, score in (("top", 90), ("second", 85), ("low", 79)):
        insert_new(
            conn,
            source_id,
            RawItem(external_id=external_id, title=external_id, url=f"https://x/{external_id}"),
        )
        update_ai_ranking(
            conn,
            source_id,
            external_id,
            score=score,
            summary=f"{external_id} summary",
            rationale="r",
        )
    _make_group(conn, channel_id)

    notifier = MagicMock()
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    _, plain_text, _html = notifier.send.call_args.args
    assert "top summary" in plain_text
    assert "second summary" not in plain_text
    assert "low summary" not in plain_text


def test_next_digest_only_includes_items_since_first_send(conn):
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
    _make_group(conn, channel_id, send_interval_hours=24)

    notifier = MagicMock()
    send_email_group_digests(
        conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)
    notifier.reset_mock()
    next_day = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    send_email_group_digests(
        conn, notifier, DEFAULT_RECIPIENT, _EN, now=next_day)

    _, plain_text, html = notifier.send.call_args.args
    assert "old news" not in plain_text
    assert "No new items today" in plain_text
    assert "No new items today" in html


def test_digest_includes_source_failure_warning(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T00:00:00")
    _make_group(conn, channel_id)

    notifier = MagicMock()
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)
    _, plain_text, html = notifier.send.call_args[0]
    assert "timeout" in plain_text  # raw provider error survives untranslated
    assert "timeout" in html
    assert "reddit_subreddit" in plain_text
    assert "source fetch failed" in plain_text


def test_successful_send_updates_channel_and_group_checkpoints(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    group_id = _make_group(conn, channel_id)

    send_email_group_digests(
        conn, MagicMock(), DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    channel = conn.execute(
        "SELECT last_digest_sent_at, last_digest_date FROM channels WHERE id = ?",
        (channel_id,)).fetchone()
    assert channel["last_digest_sent_at"] == RUN_TIME.isoformat()
    assert channel["last_digest_date"] == "2026-07-13"
    group = conn.execute(
        "SELECT last_sent_at FROM email_groups WHERE id = ?", (group_id,)).fetchone()
    assert group["last_sent_at"] == RUN_TIME.isoformat()


def test_channels_in_the_same_group_share_one_email(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    _make_group(conn, first, second, recipient_email="shared@example.com")
    notifier = MagicMock()

    send_email_group_digests(
        conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _subject, plain_text, html = notifier.send.call_args.args
    assert "First" in plain_text and "Second" in plain_text
    assert "First" in html and "Second" in html
    assert notifier.send.call_args.kwargs["to_addr"] == "shared@example.com"


def test_channels_in_different_groups_receive_only_their_own_channels(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    _make_group(conn, first, name="First Group", recipient_email="first@example.com")
    _make_group(conn, second, name="Second Group", recipient_email="second@example.com")
    notifier = MagicMock()

    send_email_group_digests(
        conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

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
    success_group = _make_group(
        conn, success, name="Success Group", recipient_email="success@example.com")
    failure_group = _make_group(
        conn, failure, name="Failure Group", recipient_email="failure@example.com")
    notifier = MagicMock()

    def send(subject, plain_text, html=None, *, to_addr=None):
        if to_addr == "failure@example.com":
            raise RuntimeError("mailbox unavailable")

    notifier.send.side_effect = send

    with pytest.raises(ExceptionGroup, match="email groups"):
        send_email_group_digests(
            conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    channel_rows = {
        row["name"]: row
        for row in conn.execute(
            "SELECT name, last_digest_sent_at, last_digest_date FROM channels")
    }
    assert channel_rows["Success"]["last_digest_date"] == "2026-07-13"
    assert channel_rows["Failure"]["last_digest_sent_at"] is None
    assert channel_rows["Failure"]["last_digest_date"] is None
    group_rows = {
        row["id"]: row["last_sent_at"]
        for row in conn.execute("SELECT id, last_sent_at FROM email_groups")
    }
    assert group_rows[success_group] == RUN_TIME.isoformat()
    assert group_rows[failure_group] is None
    assert notifier.send.call_count == 2


def test_group_not_due_yet_is_skipped_entirely(conn):
    due_channel = create_channel(conn, "Due", "profile")
    not_due_channel = create_channel(conn, "NotDue", "profile")
    _make_group(conn, due_channel, name="Due Group", recipient_email="due@example.com")
    not_due_group = _make_group(
        conn, not_due_channel, name="NotDue Group", recipient_email="notdue@example.com")
    mark_sent(conn, not_due_group, sent_at=(RUN_TIME - timedelta(hours=1)).isoformat())
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    assert notifier.send.call_args.kwargs["to_addr"] == "due@example.com"
    not_due_row = conn.execute(
        "SELECT last_digest_sent_at FROM channels WHERE id = ?", (not_due_channel,)).fetchone()
    assert not_due_row["last_digest_sent_at"] is None


def test_missing_recipient_is_logged_by_group_and_still_raised(conn, capsys):
    channel_id = create_channel(conn, "Orphan Channel", "profile")
    _make_group(conn, channel_id, name="Orphan Group")
    missing_default = ResolvedRecipient(None, "missing")
    notifier = MagicMock()

    with pytest.raises(ExceptionGroup, match="email groups"):
        send_email_group_digests(conn, notifier, missing_default, _EN, now=RUN_TIME)

    assert "Orphan Group" in capsys.readouterr().out
    notifier.send.assert_not_called()


def test_group_with_six_hour_interval_can_send_more_than_once_per_calendar_day(conn):
    """The old per-channel same-day dedup (last_digest_date == today) would have wrongly
    blocked a second same-day send -- removing it is what lets a sub-daily group interval
    actually work."""
    channel_id = create_channel(conn, "NZ Outdoor Gear", "watch for price drops", kind="monitor")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    _make_group(conn, channel_id, send_interval_hours=6)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)
    notifier.reset_mock()

    insert_new(conn, source_id, RawItem(external_id="t1", title="Drop", url="https://x/1"))
    update_ai_ranking(conn, source_id, "t1", score=90, summary="Price dropped", rationale="r")
    six_hours_later = RUN_TIME + timedelta(hours=6)

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=six_hours_later)

    notifier.send.assert_called_once()
    assert "Price dropped" in notifier.send.call_args.args[1]


def test_digest_body_is_rendered_in_the_selected_non_english_language(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T00:00:00")
    _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _ZH, now=RUN_TIME)

    _subject, plain_text, html = notifier.send.call_args.args
    assert "信源抓取失败" in plain_text  # localized warning wording
    assert "timeout" in plain_text  # raw provider error still untranslated
    assert channel_id  # sanity: channel was actually processed


def test_stored_summary_text_is_never_translated_or_altered(conn):
    """A stored ai_summary is a historical AI-generated artifact -- sending a digest must
    never rewrite or clear it even when the platform language differs from whatever language
    it was originally generated in."""
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(conn, source_id, "t1", score=90, summary="RBNZ \u964d\u606f", rationale="r")
    _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    _, plain_text, html = notifier.send.call_args.args
    assert "RBNZ \u964d\u606f" in plain_text  # untouched even though the digest itself is English
    assert "RBNZ \u964d\u606f" in html
    assert channel_id


def test_monitor_channel_in_a_group_is_included_in_its_digest(conn):
    """Unlike the old fixed daily digest (editorial Channels only), a 'monitor' Channel can
    now join a periodic email group and receive a digest for the first time."""
    channel_id = create_channel(conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="Price drop", url="https://x"))
    update_ai_ranking(conn, source_id, "t1", score=90, summary="summary", rationale="r")
    _make_group(conn, channel_id)

    notifier = MagicMock()
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    assert "summary" in notifier.send.call_args.args[1]


def test_channel_not_in_any_group_never_receives_a_digest(conn):
    channel_id = create_channel(conn, "Ungrouped", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(conn, source_id, RawItem(external_id="t1", title="News", url="https://x"))
    update_ai_ranking(conn, source_id, "t1", score=90, summary="summary", rationale="r")

    notifier = MagicMock()
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()


def test_editorial_and_monitor_channels_can_share_the_same_group(conn):
    editorial_id = create_channel(conn, "NZ Finance", "profile")
    monitor_id = create_channel(conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor")
    editorial_source = create_source(conn, editorial_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(
        conn, editorial_source, RawItem(external_id="t1", title="Rates fall", url="https://x"))
    update_ai_ranking(
        conn, editorial_source, "t1", score=90, summary="RBNZ cuts rates", rationale="r")
    monitor_source = create_source(conn, monitor_id, "reddit_subreddit", {"subreddit": "y"})
    insert_new(
        conn, monitor_source, RawItem(external_id="t2", title="Price drop", url="https://y"))
    update_ai_ranking(conn, monitor_source, "t2", score=90, summary="Price dropped", rationale="r")
    _make_group(conn, editorial_id, monitor_id)

    notifier = MagicMock()
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, _html = notifier.send.call_args.args
    assert "RBNZ cuts rates" in plain_text
    assert "Price dropped" in plain_text


def test_empty_group_with_no_member_channels_never_sends(conn):
    create_email_group(conn, "Empty Group")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()


def test_subject_template_is_formatted_with_the_digest_date(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    _make_group(conn, channel_id, subject_template="Weekly Roundup \u00b7 {date}")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    subject = notifier.send.call_args.args[0]
    assert subject == "Weekly Roundup \u00b7 2026-07-13"


def test_malformed_subject_template_falls_back_to_the_raw_template_text(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    _make_group(conn, channel_id, subject_template="Oops {not_a_real_field}")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    subject = notifier.send.call_args.args[0]
    assert subject == "Oops {not_a_real_field}"


def test_group_recipient_overrides_the_default(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    _make_group(conn, channel_id, recipient_email="group-owner@example.com")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_args.kwargs["to_addr"] == "group-owner@example.com"


def test_group_without_its_own_recipient_falls_back_to_default(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    _make_group(conn, channel_id, recipient_email=None)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_args.kwargs["to_addr"] == DEFAULT_RECIPIENT.address
