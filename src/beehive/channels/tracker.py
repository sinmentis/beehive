"""Tracker adapter contract and the first auction implementation.

Tracker Channels retain mutable listings, but each Source decides how its normalized metadata
maps to generic lifecycle, deadline, watchability, reminder, and display facts. The web and
reminder services consume these facts instead of comparing connector names or parsing auction
metadata themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, cast

from beehive.auction import (
    canonical_auction_closing_at,
    format_auction_amount,
    parse_auction_closing_at,
)
from beehive.connectors.registry import get as get_connector
from beehive.domain.channels import ChannelKind
from beehive.localization import Localizer

_AUCTION_REMINDER_LEAD = timedelta(hours=1)
_TERMINAL_AUCTION_STATUSES = frozenset(
    {
        "cancelled",
        "closed",
        "ended",
        "passed",
        "removed",
        "sold",
        "withdrawn",
    }
)


@dataclass(frozen=True)
class TrackerFacts:
    """Generic state used by Tracker panels, watches, and reminder workers."""

    active: bool
    deadline: datetime | None
    watchable: bool
    reminder_key: str | None
    reminder_due_at: datetime | None


@dataclass(frozen=True)
class TrackerDisplayFacts:
    """Localized connector-specific facts that remain useful across panel and email views."""

    context: str
    details: tuple[str, ...]


class TrackerAdapter(Protocol):
    """Converts one connector's normalized metadata into generic Tracker behavior."""

    def facts(
        self,
        metadata: Mapping[str, Any],
        *,
        is_present: bool,
        now: datetime,
    ) -> TrackerFacts:
        ...

    def display_facts(
        self,
        metadata: Mapping[str, Any],
        localizer: Localizer,
    ) -> TrackerDisplayFacts:
        ...


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _auction_pricing_details(
    metadata: Mapping[str, Any], localizer: Localizer
) -> tuple[str, ...]:
    currency = metadata.get("currency_code")
    current_bid = format_auction_amount(metadata.get("current_bid"), currency)
    sold_price = format_auction_amount(metadata.get("sold_price"), currency)
    details: list[str] = []
    if current_bid is not None:
        details.append(localizer.text("web.auction.current_bid", amount=current_bid))
    elif sold_price is None:
        details.append(localizer.text("web.auction.no_public_bid"))

    estimated_cost = format_auction_amount(metadata.get("estimated_cost"), currency)
    premium_rate = metadata.get("buyer_premium_rate")
    if (
        estimated_cost is not None
        and not isinstance(premium_rate, bool)
        and isinstance(premium_rate, (int, float))
    ):
        premium = f"{premium_rate * 100:.2f}".rstrip("0").rstrip(".")
        details.append(
            localizer.text(
                "web.auction.estimated_cost",
                premium=premium,
                amount=estimated_cost,
            )
        )

    rrp = format_auction_amount(metadata.get("rrp"), currency)
    if rrp is not None:
        gst_note = " + GST" if metadata.get("rrp_excludes_gst") else ""
        details.append(
            localizer.text(
                "web.auction.seller_rrp",
                amount=rrp,
                gst_note=gst_note,
            )
        )

    starting_price = format_auction_amount(metadata.get("starting_price"), currency)
    if starting_price is not None:
        details.append(
            localizer.text("web.auction.starting_price", amount=starting_price)
        )

    estimate_low = format_auction_amount(metadata.get("estimate_low"), currency)
    estimate_high = format_auction_amount(metadata.get("estimate_high"), currency)
    if estimate_low is not None and estimate_high is not None:
        details.append(
            localizer.text(
                "web.auction.estimate",
                low=estimate_low,
                high=estimate_high,
            )
        )
    elif estimate_low is not None:
        details.append(localizer.text("web.auction.estimate_low", amount=estimate_low))
    elif estimate_high is not None:
        details.append(localizer.text("web.auction.estimate_high", amount=estimate_high))

    if sold_price is not None:
        details.append(localizer.text("web.auction.sold_price", amount=sold_price))
    return tuple(details)


class AllAboutAuctionsTrackerAdapter:
    """Tracker behavior for normalized All About Auctions lot metadata."""

    def facts(
        self,
        metadata: Mapping[str, Any],
        *,
        is_present: bool,
        now: datetime,
    ) -> TrackerFacts:
        utc_now = _require_aware(now)
        deadline = parse_auction_closing_at(metadata.get("closing_at"))
        status = str(metadata.get("status") or "").strip().lower()
        active = bool(
            is_present
            and deadline is not None
            and deadline > utc_now
            and status not in _TERMINAL_AUCTION_STATUSES
        )
        reminder_key = canonical_auction_closing_at(metadata.get("closing_at"))
        return TrackerFacts(
            active=active,
            deadline=deadline,
            watchable=active,
            reminder_key=reminder_key,
            reminder_due_at=(
                deadline - _AUCTION_REMINDER_LEAD if deadline is not None else None
            ),
        )

    def display_facts(
        self,
        metadata: Mapping[str, Any],
        localizer: Localizer,
    ) -> TrackerDisplayFacts:
        return TrackerDisplayFacts(
            context=str(metadata.get("auction_title") or "Auction lot"),
            details=_auction_pricing_details(metadata, localizer),
        )


def adapter_for_source(source_type: str) -> TrackerAdapter:
    """Resolve one Tracker Source's required adapter, failing closed if it is malformed."""

    connector = get_connector(source_type)
    supported_kinds = getattr(connector, "supported_channel_kinds", None)
    if (
        not isinstance(supported_kinds, frozenset)
        or ChannelKind.TRACKER not in supported_kinds
    ):
        raise ValueError(f"Source type {source_type!r} is not a Tracker Source")
    adapter = getattr(connector, "tracker_adapter", None)
    if adapter is None:
        raise ValueError(f"Tracker Source type {source_type!r} declares no tracker_adapter")
    if not callable(getattr(adapter, "facts", None)) or not callable(
        getattr(adapter, "display_facts", None)
    ):
        raise ValueError(
            f"Tracker Source type {source_type!r} declares a malformed tracker_adapter"
        )
    return cast(TrackerAdapter, adapter)
