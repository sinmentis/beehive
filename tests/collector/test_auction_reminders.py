from datetime import datetime, timedelta, timezone

import pytest

from beehive.auction_reminders import send_due_auction_reminders
from beehive.connectors.base import RawItem
from beehive.db.auction_watches import (
    add_auction_watch,
    claim_due_auction_reminders,
)
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.sources import create_source
from beehive.email_routing import ResolvedRecipient
from beehive.localization import localizer_for
from beehive.notify import Notifier

_NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


class _RecordingNotifier(Notifier):
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def send(
        self,
        subject: str,
        plain_text: str,
        html: str | None = None,
        *,
        to_addr: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "subject": subject,
                "plain_text": plain_text,
                "html": html,
                "to_addr": to_addr,
            }
        )
        if self.error is not None:
            raise self.error


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    return connection


def _watch_lot(
    conn,
    external_id: str,
    *,
    closing_at: datetime,
    current_bid: float | None = 500.0,
    estimated_cost: float | None = 585.0,
    rrp: float | None = 1500.0,
) -> int:
    channel_id = create_channel(
        conn,
        f"Auction {external_id}",
        "find bargains",
        kind="tracker",
    )
    source_id = create_source(conn, channel_id, "all_about_auctions", {})
    insert_new(
        conn,
        source_id,
        RawItem(
            external_id=external_id,
            title=f"Lot {external_id}",
            url=f"https://example.com/{external_id}?a=1&b=2",
            raw_metadata={
                "listing_kind": "auction_lot",
                "auction_title": "Commercial Equipment Auction",
                "closing_at": closing_at.isoformat(),
                "currency_code": "NZD",
                "current_bid": current_bid,
                "buyer_premium_rate": 0.17,
                "estimated_cost": estimated_cost,
                "rrp": rrp,
                "rrp_excludes_gst": True,
            },
        ),
    )
    item_id = conn.execute(
        "SELECT id FROM items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()["id"]
    add_auction_watch(conn, item_id, _NOW - timedelta(hours=2))
    return item_id


def test_sends_one_localized_email_and_marks_the_reminder_sent(conn):
    _watch_lot(conn, "coffee", closing_at=_NOW + timedelta(minutes=55))
    notifier = _RecordingNotifier()

    sent = send_due_auction_reminders(
        conn,
        notifier,
        ResolvedRecipient("owner@example.com", "database"),
        localizer_for("en"),
        now=_NOW,
    )

    assert sent == 1
    assert len(notifier.calls) == 1
    message = notifier.calls[0]
    assert message["to_addr"] == "owner@example.com"
    assert "Tracker reminder" in message["subject"]
    assert "watched item needs attention" in message["subject"]
    assert "Lot coffee" in message["plain_text"]
    assert "Listing: Commercial Equipment Auction" in message["plain_text"]
    assert "Deadline:" in message["plain_text"]
    assert "Current bid: NZD 500" in message["plain_text"]
    assert "Est. with 17% premium: NZD 585" in message["plain_text"]
    assert "Seller RRP: NZD 1,500 + GST" in message["plain_text"]
    assert "https://example.com/coffee?a=1&amp;b=2" in message["html"]
    row = conn.execute("SELECT * FROM auction_watches").fetchone()
    assert (
        row["reminder_sent_for_closing_at"]
        == (_NOW + timedelta(minutes=55)).isoformat()
    )
    assert row["reminder_sent_at"] == _NOW.isoformat()
    assert row["claim_token"] is None


def test_groups_multiple_due_watches_into_one_email(conn):
    _watch_lot(conn, "one", closing_at=_NOW + timedelta(minutes=40))
    _watch_lot(conn, "two", closing_at=_NOW + timedelta(minutes=50))
    notifier = _RecordingNotifier()

    sent = send_due_auction_reminders(
        conn,
        notifier,
        ResolvedRecipient("owner@example.com", "database"),
        localizer_for("zh-CN"),
        now=_NOW,
    )

    assert sent == 2
    assert len(notifier.calls) == 1
    assert "追踪提醒" in notifier.calls[0]["subject"]
    assert "2 个关注项目需要处理" in notifier.calls[0]["subject"]
    assert "Lot one" in notifier.calls[0]["plain_text"]
    assert "Lot two" in notifier.calls[0]["plain_text"]


def test_delivery_failure_releases_claim_for_the_next_run(conn):
    item_id = _watch_lot(conn, "retry", closing_at=_NOW + timedelta(minutes=45))
    notifier = _RecordingNotifier(RuntimeError("provider down"))

    with pytest.raises(RuntimeError, match="provider down"):
        send_due_auction_reminders(
            conn,
            notifier,
            ResolvedRecipient("owner@example.com", "database"),
            localizer_for("en"),
            now=_NOW,
        )

    row = conn.execute(
        "SELECT * FROM auction_watches WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert row["claim_token"] is None
    assert row["reminder_sent_at"] is None
    assert row["last_error"] == "provider down"
    assert claim_due_auction_reminders(conn, _NOW + timedelta(minutes=1)).items


def test_missing_default_recipient_leaves_due_watches_pending(conn):
    _watch_lot(conn, "pending", closing_at=_NOW + timedelta(minutes=45))
    notifier = _RecordingNotifier()

    sent = send_due_auction_reminders(
        conn,
        notifier,
        ResolvedRecipient(None, "missing"),
        localizer_for("en"),
        now=_NOW,
    )

    assert sent == 0
    assert notifier.calls == []
    row = conn.execute("SELECT * FROM auction_watches").fetchone()
    assert row["claim_token"] is None
    assert row["reminder_sent_at"] is None
