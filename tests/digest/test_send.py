"""End-to-end tests for the event-driven Email Group send. Content comes only from ready,
unsuppressed, undelivered item_events -- never from an item's fetched_at watermark -- so a
deployment can never backfill historical rows into an email. Each due group sends at most one
email covering every member Channel with something to say, marks exactly the included event ids
delivered, and advances its checkpoints; an empty evaluation advances only last_checked_at."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import assign_channel, create_email_group, mark_sent
from beehive.db.item_events import (
    mark_item_events_ready,
    record_or_coalesce_event,
    suppress_item_events,
)
from beehive.db.items import insert_new, insert_new_returning_id, update_ai_ranking_by_id
from beehive.db.sources import create_source, record_fetch_error
from beehive.digest.send import send_email_group_digests
from beehive.email_routing import ResolvedRecipient
from beehive.localization import localizer_for

DEFAULT_RECIPIENT = ResolvedRecipient("default@example.com", "database")
RUN_TIME = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
_SENT_AT = RUN_TIME.isoformat()
_EN = localizer_for("en")
_ZH = localizer_for("zh-CN")

_REDDIT = ("reddit_subreddit", {"subreddit": "x"})
_SHOPIFY = ("shopify_collection", {"collection_url": "https://s.example.com/collections/outlet"})
_AUCTIONS = ("all_about_auctions", {"url": "https://auctions.example.com"})


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


def _stage_event(conn, source_id, external_id, *, title="Item", url="https://x",
                 raw_metadata=None, event_type="discovered", payload=None,
                 score=90, summary="summary", observed_at="2026-07-10T00:00:00",
                 ready=True, ready_at="2026-07-10T01:00:00"):
    """Mirror the collector: insert an item, record its AI score, stage one actionable event, and
    (by default) settle it ready as AI scoring would. Returns (item_id, event_id)."""
    item_id = insert_new_returning_id(
        conn, source_id,
        RawItem(external_id=external_id, title=title, url=url,
                raw_metadata=raw_metadata or {}))
    update_ai_ranking_by_id(conn, item_id, score=score, summary=summary, rationale="r")
    event_id = record_or_coalesce_event(conn, item_id, event_type, payload or {}, observed_at)
    if ready:
        mark_item_events_ready(conn, item_id, ready_at)
    return item_id, event_id


def _group_row(conn, group_id):
    return conn.execute(
        "SELECT last_sent_at, last_checked_at FROM email_groups WHERE id = ?",
        (group_id,)).fetchone()


def _delivered_at(conn, event_id):
    return conn.execute(
        "SELECT delivered_at FROM item_events WHERE id = ?", (event_id,)).fetchone()["delivered_at"]


# --- No historical backfill -------------------------------------------------------------------

def test_historical_items_without_events_are_never_backfilled(conn):
    """The whole point of the event model: an AI-ranked item that predates the event system (no
    item_events row) must never be mailed just because a group was assigned to its Channel."""
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    insert_new(conn, source_id, RawItem(external_id="old", title="Old", url="https://x"))
    update_ai_ranking_by_id(
        conn,
        conn.execute("SELECT id FROM items WHERE external_id = 'old'").fetchone()["id"],
        score=95, summary="old news", rationale="r")
    group_id = _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()
    group = _group_row(conn, group_id)
    assert group["last_checked_at"] == _SENT_AT
    assert group["last_sent_at"] is None


# --- Successful exact delivery ----------------------------------------------------------------

def test_ready_event_is_delivered_and_advances_checkpoints(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _, event_id = _stage_event(conn, source_id, "t1", summary="RBNZ cuts rates")
    group_id = _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, html = notifier.send.call_args.args
    assert "RBNZ cuts rates" in plain_text
    assert "RBNZ cuts rates" in html
    assert _delivered_at(conn, event_id) == _SENT_AT
    group = _group_row(conn, group_id)
    assert group["last_sent_at"] == _SENT_AT
    assert group["last_checked_at"] == _SENT_AT
    channel = conn.execute(
        "SELECT last_digest_sent_at, last_digest_date FROM channels WHERE id = ?",
        (channel_id,)).fetchone()
    assert channel["last_digest_sent_at"] == _SENT_AT
    assert channel["last_digest_date"] == "2026-07-13"


# --- Mixed three-kind group + labels/details --------------------------------------------------

def test_mixed_editorial_monitor_tracker_group_renders_in_one_email(conn):
    editorial = create_channel(conn, "NZ Finance", "profile")
    monitor = create_channel(conn, "Clearance", "deals", kind="monitor")
    tracker = create_channel(conn, "Auctions", "lots", kind="tracker")
    ed_src = create_source(conn, editorial, *_REDDIT)
    mon_src = create_source(conn, monitor, *_SHOPIFY)
    trk_src = create_source(conn, tracker, *_AUCTIONS)
    _stage_event(conn, ed_src, "e1", summary="RBNZ cuts rates")
    _stage_event(conn, mon_src, "m1", event_type="discovered", summary="New tent", score=95)
    _stage_event(conn, mon_src, "m2", event_type="price_drop", summary="Cheap jacket", score=90,
                 payload={"old_price": 200.0, "new_price": 150.0})
    _stage_event(conn, mon_src, "m3", event_type="back_in_stock", summary="Restocked boots",
                 score=85)
    _stage_event(conn, trk_src, "t1", summary="Rare coin lot",
                 raw_metadata={"closing_at": "2026-08-01T10:00:00"})
    _make_group(conn, editorial, monitor, tracker)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, html = notifier.send.call_args.args
    # Every member Channel's section is present.
    for name in ("NZ Finance", "Clearance", "Auctions"):
        assert name in plain_text
    # Editorial discovery is the bare familiar linked summary (no bracketed tag).
    assert "- RBNZ cuts rates (https://x)" in plain_text
    # Monitor semantics.
    assert "[New] New tent" in plain_text
    assert "[Price drop \u00b7 200 \u2192 150] Cheap jacket" in plain_text
    assert "[Back in stock] Restocked boots" in plain_text
    # Tracker discovery carries its localized label and closing time.
    assert "[New tracked item \u00b7 Closes: 2026-08-01T10:00:00] Rare coin lot" in plain_text
    # HTML shows the same semantics, still no raw JSON payload.
    assert "Price drop" in html
    assert "200 \u2192 150" in html
    assert "old_price" not in html


def test_only_channels_with_content_get_a_section_and_watermark(conn):
    """Requirement pin: in a group where one Channel has a deliverable event and another has
    nothing, only the contributing Channel gets a section and its legacy digest watermark; the
    silent Channel is left untouched even though the group as a whole did send."""
    with_content = create_channel(conn, "Has News", "profile")
    without_content = create_channel(conn, "No News", "profile")
    src = create_source(conn, with_content, "reddit_subreddit", {"subreddit": "a"})
    create_source(conn, without_content, "reddit_subreddit", {"subreddit": "b"})
    _stage_event(conn, src, "t1", summary="only news")
    _make_group(conn, with_content, without_content)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, _ = notifier.send.call_args.args
    assert "Has News" in plain_text
    assert "No News" not in plain_text  # the silent Channel contributes no section
    watermarks = {
        row["name"]: row["last_digest_sent_at"]
        for row in conn.execute("SELECT name, last_digest_sent_at FROM channels")
    }
    assert watermarks["Has News"] == _SENT_AT
    assert watermarks["No News"] is None


# --- Highest-score cap + exact delivered ids --------------------------------------------------

def test_highlight_count_caps_by_highest_score_and_marks_exact_ids(conn):
    channel_id = create_channel(conn, "Clearance", "deals", kind="monitor", highlight_count=1)
    source_id = create_source(conn, channel_id, *_SHOPIFY)
    _, top = _stage_event(conn, source_id, "top", score=90, summary="top pick")
    _, mid = _stage_event(conn, source_id, "mid", score=80, summary="mid pick")
    _, low = _stage_event(conn, source_id, "low", score=70, summary="low pick")
    _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    _, plain_text, _ = notifier.send.call_args.args
    assert "top pick" in plain_text
    assert "mid pick" not in plain_text
    assert "low pick" not in plain_text
    # Only the single highest-scored event is marked delivered; the capped-out ones are untouched.
    assert _delivered_at(conn, top) == _SENT_AT
    assert _delivered_at(conn, mid) is None
    assert _delivered_at(conn, low) is None


def test_capped_out_event_is_delivered_on_the_next_due_evaluation(conn):
    channel_id = create_channel(conn, "Clearance", "deals", kind="monitor", highlight_count=1)
    source_id = create_source(conn, channel_id, *_SHOPIFY)
    _, top = _stage_event(conn, source_id, "top", score=90, summary="top pick")
    _, second = _stage_event(conn, source_id, "second", score=80, summary="second pick")
    _make_group(conn, channel_id, send_interval_hours=24)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)
    assert "top pick" in notifier.send.call_args.args[1]
    notifier.reset_mock()

    next_day = RUN_TIME + timedelta(hours=24)
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=next_day)

    _, plain_text, _ = notifier.send.call_args.args
    assert "second pick" in plain_text
    assert "top pick" not in plain_text  # already delivered last interval
    assert _delivered_at(conn, top) == _SENT_AT
    assert _delivered_at(conn, second) == next_day.isoformat()


# --- Empty evaluation: last_checked only, and cadence -----------------------------------------

def test_due_group_with_no_content_updates_only_last_checked_at(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    create_source(conn, channel_id, *_REDDIT)
    group_id = _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()
    group = _group_row(conn, group_id)
    assert group["last_checked_at"] == _SENT_AT
    assert group["last_sent_at"] is None
    channel = conn.execute(
        "SELECT last_digest_sent_at FROM channels WHERE id = ?", (channel_id,)).fetchone()
    assert channel["last_digest_sent_at"] is None


def test_empty_check_paces_the_next_due_evaluation(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _make_group(conn, channel_id, send_interval_hours=24)
    notifier = MagicMock()

    # First evaluation finds nothing -> only last_checked_at advances.
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)
    # An event arrives an hour later, but the group is not due again yet.
    _stage_event(conn, source_id, "t1", summary="fresh news")
    send_email_group_digests(
        conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME + timedelta(hours=1))
    notifier.send.assert_not_called()
    # A full interval after the check it becomes due and sends.
    send_email_group_digests(
        conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME + timedelta(hours=24))
    notifier.send.assert_called_once()
    assert "fresh news" in notifier.send.call_args.args[1]


# --- Warnings still count as content ----------------------------------------------------------

def test_warning_only_group_sends_and_advances_checkpoints(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T00:00:00")
    group_id = _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, html = notifier.send.call_args.args
    assert "timeout" in plain_text  # raw provider error survives untranslated
    assert "reddit_subreddit" in plain_text
    assert "source fetch failed" in plain_text
    assert "timeout" in html
    assert _group_row(conn, group_id)["last_sent_at"] == _SENT_AT


def test_warning_is_rendered_in_the_selected_language(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    record_fetch_error(conn, source_id, "timeout", "2026-07-09T00:00:00")
    _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _ZH, now=RUN_TIME)

    _, plain_text, _ = notifier.send.call_args.args
    assert "\u4fe1\u6e90\u6293\u53d6\u5931\u8d25" in plain_text  # localized warning wording
    assert "timeout" in plain_text  # raw provider error still untranslated


# --- Failure retries, marks nothing -----------------------------------------------------------

def test_delivery_failure_marks_no_events_and_advances_no_checkpoints(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _, event_id = _stage_event(conn, source_id, "t1", summary="news")
    group_id = _make_group(conn, channel_id)
    notifier = MagicMock()
    notifier.send.side_effect = RuntimeError("smtp down")

    with pytest.raises(ExceptionGroup, match="email groups"):
        send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert _delivered_at(conn, event_id) is None
    group = _group_row(conn, group_id)
    assert group["last_sent_at"] is None
    assert group["last_checked_at"] is None  # a failed send advances nothing, so it retries
    channel = conn.execute(
        "SELECT last_digest_sent_at FROM channels WHERE id = ?", (channel_id,)).fetchone()
    assert channel["last_digest_sent_at"] is None

    # Next cycle with a healthy notifier: the exact same event goes out.
    healthy = MagicMock()
    send_email_group_digests(
        conn, healthy, DEFAULT_RECIPIENT, _EN, now=RUN_TIME + timedelta(hours=1))
    healthy.send.assert_called_once()
    assert "news" in healthy.send.call_args.args[1]
    assert _delivered_at(conn, event_id) == (RUN_TIME + timedelta(hours=1)).isoformat()


# --- Missing recipient: empty vs content ------------------------------------------------------

def test_missing_recipient_with_no_content_is_silently_checked(conn, capsys):
    channel_id = create_channel(conn, "Orphan Channel", "profile")
    create_source(conn, channel_id, *_REDDIT)
    group_id = _make_group(conn, channel_id, name="Orphan Group")
    missing_default = ResolvedRecipient(None, "missing")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, missing_default, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()
    assert "Orphan Group" not in capsys.readouterr().out  # not a configuration failure
    group = _group_row(conn, group_id)
    assert group["last_checked_at"] == _SENT_AT
    assert group["last_sent_at"] is None


def test_missing_recipient_with_content_is_raised_and_marks_nothing(conn, capsys):
    channel_id = create_channel(conn, "Orphan Channel", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _, event_id = _stage_event(conn, source_id, "t1", summary="news")
    group_id = _make_group(conn, channel_id, name="Orphan Group")
    missing_default = ResolvedRecipient(None, "missing")
    notifier = MagicMock()

    with pytest.raises(ExceptionGroup, match="email groups"):
        send_email_group_digests(conn, notifier, missing_default, _EN, now=RUN_TIME)

    assert "Orphan Group" in capsys.readouterr().out
    notifier.send.assert_not_called()
    assert _delivered_at(conn, event_id) is None
    group = _group_row(conn, group_id)
    assert group["last_checked_at"] is None  # nothing advances, so the event retries
    assert group["last_sent_at"] is None


# --- Only ready events are eligible -----------------------------------------------------------

def test_only_ready_events_are_delivered_pending_and_suppressed_excluded(conn):
    channel_id = create_channel(conn, "Clearance", "deals", kind="monitor")
    source_id = create_source(conn, channel_id, *_SHOPIFY)
    _, pending_id = _stage_event(conn, source_id, "pending", summary="pending item", ready=False)
    suppressed_item, suppressed_id = _stage_event(conn, source_id, "supp", summary="suppressed item")
    suppress_item_events(conn, suppressed_item, "2026-07-11T00:00:00")
    _, ready_id = _stage_event(conn, source_id, "ready", summary="ready item")
    _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    _, plain_text, _ = notifier.send.call_args.args
    assert "ready item" in plain_text
    assert "pending item" not in plain_text
    assert "suppressed item" not in plain_text
    assert _delivered_at(conn, ready_id) == _SENT_AT
    assert _delivered_at(conn, pending_id) is None
    assert _delivered_at(conn, suppressed_id) is None


# --- Group scoping / one email per group ------------------------------------------------------

def test_channels_in_the_same_group_share_one_email(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    first_src = create_source(conn, first, "reddit_subreddit", {"subreddit": "a"})
    second_src = create_source(conn, second, "reddit_subreddit", {"subreddit": "b"})
    _stage_event(conn, first_src, "f1", summary="first news")
    _stage_event(conn, second_src, "s1", summary="second news")
    _make_group(conn, first, second, recipient_email="shared@example.com")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    _, plain_text, html = notifier.send.call_args.args
    assert "First" in plain_text and "Second" in plain_text
    assert "First" in html and "Second" in html
    assert notifier.send.call_args.kwargs["to_addr"] == "shared@example.com"


def test_channels_in_different_groups_receive_only_their_own_channels(conn):
    first = create_channel(conn, "First", "profile")
    second = create_channel(conn, "Second", "profile")
    first_src = create_source(conn, first, "reddit_subreddit", {"subreddit": "a"})
    second_src = create_source(conn, second, "reddit_subreddit", {"subreddit": "b"})
    _stage_event(conn, first_src, "f1", summary="first news")
    _stage_event(conn, second_src, "s1", summary="second news")
    _make_group(conn, first, name="First Group", recipient_email="first@example.com")
    _make_group(conn, second, name="Second Group", recipient_email="second@example.com")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_count == 2
    bodies = {call.kwargs["to_addr"]: call.args[1] for call in notifier.send.call_args_list}
    assert "First" in bodies["first@example.com"]
    assert "Second" not in bodies["first@example.com"]
    assert "Second" in bodies["second@example.com"]
    assert "First" not in bodies["second@example.com"]


def test_partial_failure_advances_only_the_successful_group_and_continues(conn):
    success = create_channel(conn, "Success", "profile")
    failure = create_channel(conn, "Failure", "profile")
    success_src = create_source(conn, success, "reddit_subreddit", {"subreddit": "s"})
    failure_src = create_source(conn, failure, "reddit_subreddit", {"subreddit": "f"})
    _, success_event = _stage_event(conn, success_src, "s1", summary="good news")
    _, failure_event = _stage_event(conn, failure_src, "f1", summary="bad news")
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
        send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert _delivered_at(conn, success_event) == _SENT_AT
    assert _group_row(conn, success_group)["last_sent_at"] == _SENT_AT
    assert _delivered_at(conn, failure_event) is None
    failure_row = _group_row(conn, failure_group)
    assert failure_row["last_sent_at"] is None
    assert failure_row["last_checked_at"] is None
    assert notifier.send.call_count == 2


def test_group_not_due_yet_is_skipped_entirely(conn):
    due = create_channel(conn, "Due", "profile")
    not_due = create_channel(conn, "NotDue", "profile")
    due_src = create_source(conn, due, "reddit_subreddit", {"subreddit": "d"})
    not_due_src = create_source(conn, not_due, "reddit_subreddit", {"subreddit": "n"})
    _stage_event(conn, due_src, "d1", summary="due news")
    _, not_due_event = _stage_event(conn, not_due_src, "n1", summary="notdue news")
    _make_group(conn, due, name="Due Group", recipient_email="due@example.com")
    not_due_group = _make_group(
        conn, not_due, name="NotDue Group", recipient_email="notdue@example.com")
    mark_sent(conn, not_due_group, sent_at=(RUN_TIME - timedelta(hours=1)).isoformat())
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_called_once()
    assert notifier.send.call_args.kwargs["to_addr"] == "due@example.com"
    assert _delivered_at(conn, not_due_event) is None


def test_channel_not_in_any_group_never_receives_a_digest(conn):
    channel_id = create_channel(conn, "Ungrouped", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _stage_event(conn, source_id, "t1", summary="news")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()


def test_group_with_no_member_channels_is_checked_but_never_sends(conn):
    group_id = create_email_group(conn, "Empty Group")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    notifier.send.assert_not_called()
    group = _group_row(conn, group_id)
    assert group["last_checked_at"] == _SENT_AT
    assert group["last_sent_at"] is None


def test_group_with_six_hour_interval_can_send_more_than_once_per_calendar_day(conn):
    channel_id = create_channel(conn, "NZ Outdoor Gear", "watch for price drops", kind="monitor")
    source_id = create_source(conn, channel_id, *_SHOPIFY)
    _stage_event(conn, source_id, "t1", summary="First drop")
    _make_group(conn, channel_id, send_interval_hours=6)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)
    notifier.send.assert_called_once()
    notifier.reset_mock()

    _stage_event(conn, source_id, "t2", summary="Second drop")
    six_hours_later = RUN_TIME + timedelta(hours=6)
    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=six_hours_later)

    notifier.send.assert_called_once()
    body = notifier.send.call_args.args[1]
    assert "Second drop" in body
    assert "First drop" not in body  # already delivered in the first send


# --- Localization: stored artifacts are never rewritten ---------------------------------------

def test_stored_summary_text_is_never_translated_or_altered(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _stage_event(conn, source_id, "t1", summary="RBNZ \u964d\u606f")
    _make_group(conn, channel_id)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    _, plain_text, html = notifier.send.call_args.args
    assert "RBNZ \u964d\u606f" in plain_text  # untouched even though the digest itself is English
    assert "RBNZ \u964d\u606f" in html


# --- Subject + recipient plumbing (unchanged from the group model) ----------------------------

def test_subject_template_is_formatted_with_the_digest_date(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _stage_event(conn, source_id, "t1", summary="news")
    _make_group(conn, channel_id, subject_template="Weekly Roundup \u00b7 {date}")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_args.args[0] == "Weekly Roundup \u00b7 2026-07-13"


def test_malformed_subject_template_falls_back_to_the_raw_template_text(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _stage_event(conn, source_id, "t1", summary="news")
    _make_group(conn, channel_id, subject_template="Oops {not_a_real_field}")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_args.args[0] == "Oops {not_a_real_field}"


def test_group_recipient_overrides_the_default(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _stage_event(conn, source_id, "t1", summary="news")
    _make_group(conn, channel_id, recipient_email="group-owner@example.com")
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_args.kwargs["to_addr"] == "group-owner@example.com"


def test_group_without_its_own_recipient_falls_back_to_default(conn):
    channel_id = create_channel(conn, "NZ Finance", "profile")
    source_id = create_source(conn, channel_id, *_REDDIT)
    _stage_event(conn, source_id, "t1", summary="news")
    _make_group(conn, channel_id, recipient_email=None)
    notifier = MagicMock()

    send_email_group_digests(conn, notifier, DEFAULT_RECIPIENT, _EN, now=RUN_TIME)

    assert notifier.send.call_args.kwargs["to_addr"] == DEFAULT_RECIPIENT.address
