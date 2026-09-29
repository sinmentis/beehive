"""The home page's channel desk: one section per Channel, its state in the heading and its best
few rows beneath.

An Editorial Channel shows its top featured stories in the window (unread first for the Owner),
a Monitor Channel its best live listings, and a Tracker Channel its open lots at or above the
desk's score floor, soonest deadline first. Rows are chosen in SQL and dressed by the channel
pages' own view builders, so the home page never builds a whole Channel page."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from beehive.channels import get_definition, require_channel_kind
from beehive.channels.views import (
    EditorialItemView,
    MonitorItemView,
    TrackerItemView,
    build_editorial_item_views,
    build_monitor_item_views,
    build_open_tracker_views,
)
from beehive.db.items import (
    count_active_monitor_listings_by_channel,
    list_active_monitor_listings,
    list_dashboard_highlights,
    list_listed_lots_scoring,
)
from beehive.db.sources import list_by_channel as list_sources
from beehive.db.tracker_watches import list_tracker_watches
from beehive.domain.channels import ChannelKind
from beehive.localization import Localizer
from beehive.scheduling import ChannelFetchSchedule
from beehive.web.formatting import (
    fetch_stats_label,
    freshness_exact_time,
    freshness_label,
    next_fetch_countdown,
)

DESK_STORY_COUNT = 4
DESK_LISTING_COUNT = 5
DESK_LOT_COUNT = 5
# Auction lots are listed at any score, so the desk shows only the good ones: at least this
# score, or the Channel's own minimum when that is higher.
DESK_LOT_MINIMUM_SCORE = 80
# The brief page's back link returns to the home page.
DEEP_READ_ORIGIN = "dashboard"


@dataclass(frozen=True, slots=True)
class DeskSection:
    number: str
    anchor: str
    channel_id: int
    name: str
    kind: str
    href: str
    freshness: str
    freshness_detail: str
    stories: tuple[EditorialItemView, ...] = ()
    story_total: int = 0
    story_unread: int = 0
    listings: tuple[MonitorItemView, ...] = ()
    listing_total: int = 0
    lots: tuple[TrackerItemView, ...] = ()
    lot_total: int = 0
    lot_minimum_score: int = 0
    watched_total: int | None = None


@dataclass(frozen=True, slots=True)
class RankedStory:
    """A row of the ranked list: a story and the Channel it came from."""

    story: EditorialItemView
    channel_id: int
    channel_name: str


def _freshness_detail(sources: list[dict], channel: dict, now: datetime, t: Localizer) -> str:
    """The heading's tooltip: exact fetch time, then the relative one, the next fetch and the
    last fetch's numbers, each only when known."""
    parts = (
        freshness_exact_time(sources),
        freshness_label(sources, t),
        next_fetch_countdown(sources, ChannelFetchSchedule.from_channel(channel), now, t),
        fetch_stats_label(sources, t),
    )
    return " · ".join(part for part in parts if part)


def build_channel_desk(
    conn: sqlite3.Connection,
    channels: list[dict],
    *,
    t: Localizer,
    now: datetime,
    is_owner: bool,
    csrf_token: str | None,
    published_from: str,
    published_to: str,
    story_counts: dict[int, dict[str, int]],
) -> tuple[DeskSection, ...]:
    """`story_counts` is `count_dashboard_signals_by_channel` for the same window; the route
    already needs it for the page's totals."""
    kinds = {
        channel["id"]: get_definition(require_channel_kind(channel["kind"])).kind
        for channel in channels
    }
    watches = (
        list_tracker_watches(conn, now)
        if is_owner and ChannelKind.TRACKER in kinds.values()
        else []
    )
    listing_counts = count_active_monitor_listings_by_channel(
        conn, [channel_id for channel_id, kind in kinds.items() if kind is ChannelKind.MONITOR]
    )
    sections = []
    for index, channel in enumerate(channels, start=1):
        channel_id = channel["id"]
        kind = kinds[channel_id]
        sources = list_sources(conn, channel_id)
        heading = {
            "number": f"1.{index}",
            "anchor": f"desk-{channel_id}",
            "channel_id": channel_id,
            "name": channel["name"],
            "kind": kind.value,
            "href": f"/channels/{channel_id}",
            "freshness": freshness_label(sources, t),
            "freshness_detail": _freshness_detail(sources, channel, now, t),
        }
        if kind is ChannelKind.EDITORIAL:
            rows = list_dashboard_highlights(
                conn,
                limit=DESK_STORY_COUNT,
                channel_id=channel_id,
                published_from=published_from,
                published_to=published_to,
                unread_first=is_owner,
            )
            counts = story_counts.get(channel_id, {"all": 0, "unread": 0, "high": 0})
            sections.append(
                DeskSection(
                    **heading,
                    stories=build_editorial_item_views(
                        conn,
                        rows,
                        t=t,
                        now=now,
                        is_owner=is_owner,
                        csrf_token=csrf_token,
                        deep_read_origin=DEEP_READ_ORIGIN,
                    ),
                    story_total=counts["all"],
                    story_unread=counts["unread"],
                )
            )
        elif kind is ChannelKind.MONITOR:
            rows = list_active_monitor_listings(conn, channel_id, limit=DESK_LISTING_COUNT)
            sections.append(
                DeskSection(
                    **heading,
                    listings=build_monitor_item_views(conn, rows, t=t),
                    listing_total=listing_counts.get(channel_id, 0),
                )
            )
        elif kind is ChannelKind.TRACKER:
            floor = max(channel["minimum_score"], DESK_LOT_MINIMUM_SCORE)
            lots, lot_total = build_open_tracker_views(
                conn,
                list_listed_lots_scoring(
                    conn, channel_id, minimum_score=DESK_LOT_MINIMUM_SCORE
                ),
                t=t,
                now=now,
                is_owner=is_owner,
                limit=DESK_LOT_COUNT,
            )
            sections.append(
                DeskSection(
                    **heading,
                    lots=lots,
                    lot_total=lot_total,
                    lot_minimum_score=floor,
                    watched_total=(
                        sum(
                            1
                            for watch in watches
                            if watch["channel_id"] == channel_id and watch["is_active"]
                        )
                        if is_owner
                        else None
                    ),
                )
            )
    return tuple(sections)


def build_ranked_stories(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    t: Localizer,
    now: datetime,
    is_owner: bool,
    csrf_token: str | None,
) -> tuple[RankedStory, ...]:
    """The ranked list's rows, from `list_dashboard_highlights` rows, in their order."""
    stories = build_editorial_item_views(
        conn,
        rows,
        t=t,
        now=now,
        is_owner=is_owner,
        csrf_token=csrf_token,
        deep_read_origin=DEEP_READ_ORIGIN,
    )
    return tuple(
        RankedStory(story=story, channel_id=row["item_channel_id"], channel_name=row["channel_name"])
        for story, row in zip(stories, rows, strict=True)
    )
