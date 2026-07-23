from datetime import datetime, timedelta, timezone

import pytest

from beehive.channels.tracker import adapter_for_source
from beehive.connectors import all_about_auctions as _all_about_auctions  # noqa: F401
from beehive.localization import localizer_for

_NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def test_all_about_auctions_adapter_exposes_generic_tracker_facts():
    adapter = adapter_for_source("all_about_auctions")
    closing_at = _NOW + timedelta(hours=2)

    facts = adapter.facts(
        {
            "closing_at": closing_at.isoformat(),
            "status": "active",
        },
        is_present=True,
        now=_NOW,
    )

    assert facts.active is True
    assert facts.watchable is True
    assert facts.deadline == closing_at
    assert facts.reminder_key == closing_at.isoformat()
    assert facts.reminder_due_at == _NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    ("is_present", "status", "closing_offset"),
    [
        (False, "active", timedelta(hours=2)),
        (True, "sold", timedelta(hours=2)),
        (True, "active", timedelta(seconds=-1)),
    ],
)
def test_all_about_auctions_adapter_moves_unavailable_lots_to_history(
    is_present, status, closing_offset
):
    facts = adapter_for_source("all_about_auctions").facts(
        {
            "closing_at": (_NOW + closing_offset).isoformat(),
            "status": status,
        },
        is_present=is_present,
        now=_NOW,
    )

    assert facts.active is False
    assert facts.watchable is False


def test_all_about_auctions_adapter_builds_shared_display_facts():
    display = adapter_for_source("all_about_auctions").display_facts(
        {
            "auction_title": "Commercial Equipment",
            "currency_code": "NZD",
            "current_bid": 500,
            "buyer_premium_rate": 0.17,
            "estimated_cost": 585,
            "rrp": 1500,
            "rrp_excludes_gst": True,
        },
        localizer_for("en"),
    )

    assert display.context == "Commercial Equipment"
    assert display.details == (
        "Current bid: NZD 500",
        "Est. with 17% premium: NZD 585",
        "Seller RRP: NZD 1,500 + GST",
    )
