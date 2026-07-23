"""Typed Channel page-model layer.

`build_channel_page` is the single seam a Channel drill-down route calls: it resolves the
Channel's `ChannelDefinition` once and returns a discriminated, frozen page object
(`EditorialPage` / `MonitorPage` / `TrackerPage`) whose `template_name` is the definition's
`panel_template`. Callers never branch on `channel["kind"]` and templates never inspect
`raw_metadata` -- every presentation value (labels, prices, discounts, deadlines, deep-read and
watch state) is normalized here into typed fields.

Layering: this module belongs to the same lower layers as `channels/tracker.py`, so -- like that
module -- it does all presentation through `beehive.localization` directly and never imports
`beehive.web`. The few pure helpers it needs (safe external href, host-local time formatting) and
the small Source-label maps that `web/` also owns are reproduced here rather than imported upward;
when a route is finally wired to this seam those duplicated maps should be unified into one shared
lower-layer home.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping
from urllib.parse import urlencode, urlparse

from beehive.auction import format_auction_amount
from beehive.channels.definitions import ChannelDefinition, get_definition, require_channel_kind
from beehive.channels.tracker import adapter_for_source
from beehive.db.deep_reads import DeepRead, get_deep_reads_for_items
from beehive.db.item_events import latest_actionable_events_for_items
from beehive.db.items import list_by_channel
from beehive.db.tracker_watches import get_watched_item_ids, list_tracker_watches
from beehive.domain.channels import ChannelKind
from beehive.localization import Localizer
from beehive.scheduling import HOST_TZ

Row = Mapping[str, object]

DEFAULT_PER_PAGE = 24
MAX_PER_PAGE = 100

# An active Tracker listing whose deadline falls within this window is surfaced under "ending
# soon" rather than "upcoming". One day is a reasonable first default; a route may later make it
# configurable.
ENDING_SOON_WINDOW = timedelta(hours=24)

_SAFE_URL_SCHEMES = frozenset({"http", "https"})
_DEEP_READ_ORIGIN = "channel"

# Reproduced from web/official_feed_labels.py and web/hackernews_labels.py (which web/ keeps as
# its single source of truth). Duplicated here only because a lower layer must not import web/;
# unify when a route is wired to this seam.
_OFFICIAL_FEED_LABELS = {
    "rbnz_news": "RBNZ News",
    "nz_government_news": "NZ Government",
    "federal_reserve_news": "Federal Reserve",
}
_OFFICIAL_FEED_CATEGORY_SOURCES = frozenset(_OFFICIAL_FEED_LABELS)
_HN_FEED_KEYS = {
    "top": "web.hn.feed.top",
    "best": "web.hn.feed.best",
    "new": "web.hn.feed.new",
    "ask": "web.hn.feed.ask",
    "show": "web.hn.feed.show",
    "job": "web.hn.feed.job",
}


# --------------------------------------------------------------------------------------------
# Schema-safe field readers. A missing/mistyped column is an impossible schema/programmer error
# and raises loudly; only optional metadata *values* are coerced leniently (see below).
# --------------------------------------------------------------------------------------------
def _req_int(row: Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected int for {key!r}, got {type(value).__name__}")
    return value


def _req_str(row: Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"expected str for {key!r}, got {type(value).__name__}")
    return value


def _req_bool(row: Row, key: str) -> bool:
    value = row[key]
    if not isinstance(value, bool):
        raise TypeError(f"expected bool for {key!r}, got {type(value).__name__}")
    return value


def _opt_str(row: Row, key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) else None


def _raw_number(row: Row, key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _opt_score(row: Row, key: str) -> int | None:
    """AI score normalized to a display integer in Python (templates never round)."""
    raw = _raw_number(row, key)
    return int(round(raw)) if raw is not None else None


def _metadata(item: Row) -> Mapping[str, object]:
    value = item["raw_metadata"]
    if not isinstance(value, dict):
        raise TypeError("items row is missing decoded raw_metadata dict")
    return value


def _source_config(item: Row) -> Mapping[str, object]:
    raw = item.get("source_config")
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _plain_display_text(value: object) -> str:
    return (_clean_text(value) or "").replace("**", "")


def _is_safe_url(url: str) -> bool:
    """True only for an http/https URL -- the one scheme set safe to emit as a real link or an
    image `src`. Mirrors web/link_safety.safe_external_href's rule, reproduced here because a
    lower layer must not import web/."""
    try:
        return urlparse(url).scheme in _SAFE_URL_SCHEMES
    except ValueError:
        return False


def _safe_external_href(url: str) -> str:
    return url if _is_safe_url(url) else "#"


def _safe_image_url(value: object) -> str | None:
    """An external image `src` only when it is a non-empty http/https URL, else None, so a
    template can render <img> without validating the URL itself."""
    text = _clean_text(value)
    return text if text is not None and _is_safe_url(text) else None


def _open_url(item_id: int, item_url: str) -> str:
    """The internal open/redirect route, but only when the original item URL is itself safe;
    an unsafe URL degrades to a non-navigating anchor. The /items/{id}/open route re-validates
    the destination on redirect."""
    return f"/items/{item_id}/open" if _is_safe_url(item_url) else "#"


def _relative_time(iso_str: str, t: Localizer, now: datetime) -> str:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    minutes = int((now - dt).total_seconds() // 60)
    if minutes < 1:
        return t.text("web.time.just_now")
    if minutes < 60:
        return t.text("web.time.minutes_ago", count=minutes)
    hours = minutes // 60
    if hours < 24:
        return t.text("web.time.hours_ago", count=hours)
    return t.text("web.time.days_ago", count=hours // 24)


def _host_local_label(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(HOST_TZ).strftime("%Y-%m-%d %H:%M")


def _collection_host_label(config: Mapping[str, object]) -> str:
    url = config.get("collection_url")
    url = url if isinstance(url, str) else ""
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}" if parsed.netloc else url


def _source_label(item: Row, t: Localizer) -> str:
    source_type = _req_str(item, "source_type")
    config = _source_config(item)
    if source_type == "reddit_subreddit":
        return f"r/{config.get('subreddit', '')}"
    if source_type == "google_news_query":
        return f'"{config.get("query", "")}"'
    if source_type == "all_about_auctions":
        return "All About Auctions"
    if source_type in {"shopify_collection", "land_sea_collection"}:
        return _collection_host_label(config)
    official = _OFFICIAL_FEED_LABELS.get(source_type)
    if official is not None:
        return official
    if source_type == "hackernews_stories":
        feed = config.get("feed", "")
        feed = feed if isinstance(feed, str) else ""
        feed_label = t.text(_HN_FEED_KEYS[feed]) if feed in _HN_FEED_KEYS else feed
        return t.text("web.hn.stories_label", feed=feed_label)
    if source_type == "hackernews_query":
        return t.text("web.hn.query_label", query=config.get("query", ""))
    return source_type


def _editorial_engagement_label(item: Row, t: Localizer) -> str:
    source_type = _req_str(item, "source_type")
    metadata = _metadata(item)
    if source_type == "reddit_subreddit":
        return t.text(
            "web.engagement.reddit",
            score=metadata.get("score", 0),
            comments=metadata.get("num_comments", 0),
        )
    if source_type == "google_news_query":
        return _clean_text(metadata.get("source_name")) or ""
    if source_type in {"hackernews_stories", "hackernews_query"}:
        return t.text(
            "web.engagement.hackernews",
            score=metadata.get("score", 0),
            comments=metadata.get("num_comments", 0),
        )
    if source_type in _OFFICIAL_FEED_CATEGORY_SOURCES:
        return _clean_text(metadata.get("category")) or ""
    return ""


def _discount_percent(price: float | None, compare_at_price: float | None, on_sale: bool) -> int | None:
    """Currency-agnostic percent-off, the only discount signal these storefront feeds expose
    (no feed carries a currency code)."""
    if not on_sale or price is None or compare_at_price is None:
        return None
    if compare_at_price <= 0 or price < 0 or compare_at_price <= price:
        return None
    return round((compare_at_price - price) / compare_at_price * 100)


# --------------------------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Pagination:
    """Deterministic page window over a materialized, already-sorted row set. `page`/`per_page`
    are validated on construction (an out-of-range value is a bad request, not an empty page);
    a `page` past the last page is a normal empty state, not an error."""

    page: int
    per_page: int
    total: int

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"page must be >= 1, got {self.page}")
        if not 1 <= self.per_page <= MAX_PER_PAGE:
            raise ValueError(f"per_page must be between 1 and {MAX_PER_PAGE}, got {self.per_page}")
        if self.total < 0:
            raise ValueError(f"total must be >= 0, got {self.total}")

    @property
    def page_count(self) -> int:
        return (self.total + self.per_page - 1) // self.per_page

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.page_count

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


# --------------------------------------------------------------------------------------------
# Deep read (Editorial only)
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeepReadActionView:
    """State + allowlisted URLs for one ranked Editorial item's Deep Read control. Mirrors the
    display contract of web/deep_read_view.decorate_deep_read_state; `csrf_token` is present only
    for the owner."""

    status: str
    origin: str
    channel_id: int
    brief_url: str
    request_url: str
    csrf_token: str | None
    is_ready: bool
    is_failed: bool
    is_pending: bool
    can_start: bool
    can_regenerate: bool


def _deep_read_action(
    item_id: int,
    channel_id: int,
    deep_read: DeepRead | None,
    *,
    is_owner: bool,
    csrf_token: str | None,
) -> DeepReadActionView:
    status = deep_read.status if deep_read is not None else "not_requested"
    query = urlencode({"origin": _DEEP_READ_ORIGIN, "channel_id": channel_id})
    return DeepReadActionView(
        status=status,
        origin=_DEEP_READ_ORIGIN,
        channel_id=channel_id,
        brief_url=f"/items/{item_id}/brief?{query}",
        request_url=f"/items/{item_id}/deep-read",
        csrf_token=csrf_token if is_owner else None,
        is_ready=status == "ready",
        is_failed=status == "failed",
        is_pending=status in ("pending", "processing"),
        can_start=is_owner and status == "not_requested",
        can_regenerate=is_owner and status in ("ready", "failed"),
    )


# --------------------------------------------------------------------------------------------
# Editorial
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EditorialItemView:
    id: int
    is_read: bool
    ai_score: int | None
    ai_summary: str | None
    ai_rationale: str | None
    title: str
    source_label: str
    engagement_label: str
    age: str
    exact_time: str
    open_url: str
    safe_url: str
    vote_value: int | None
    vote_reason: str | None
    best_comment_summary: str | None
    deep_read: DeepReadActionView | None


@dataclass(frozen=True, slots=True)
class EditorialPage:
    template_name: str
    channel_id: int
    channel_name: str
    highlighted: tuple[EditorialItemView, ...]
    folded: tuple[EditorialItemView, ...]
    unread_count: int
    read_count: int
    show_read: bool


def _editorial_item(
    item: Row,
    t: Localizer,
    now: datetime,
    *,
    is_owner: bool,
    channel_id: int,
    deep_read: DeepRead | None,
    csrf_token: str | None,
) -> EditorialItemView:
    item_id = _req_int(item, "id")
    url = _req_str(item, "url")
    safe_url = _safe_external_href(url)
    created_at = _opt_str(item, "created_at")
    ai_score = _opt_score(item, "ai_score")
    return EditorialItemView(
        id=item_id,
        is_read=bool(_req_int(item, "is_read")),
        ai_score=ai_score,
        ai_summary=_clean_text(item.get("ai_summary")),
        ai_rationale=_clean_text(item.get("ai_rationale")),
        title=_req_str(item, "title"),
        source_label=_source_label(item, t),
        engagement_label=_editorial_engagement_label(item, t),
        age=_relative_time(created_at, t, now) if created_at else "",
        exact_time=_host_local_label(created_at) if created_at else "",
        open_url=_open_url(item_id, url),
        safe_url=safe_url,
        vote_value=_opt_score(item, "vote_value"),
        # The private downvote reason is owner-only; anonymous readers only ever see the arrow.
        vote_reason=_clean_text(item.get("vote_reason")) if is_owner else None,
        best_comment_summary=_clean_text(item.get("best_comment_summary")),
        # Only a ranked item can carry a Deep Read (the request route and worker both reject an
        # unranked item), matching web/deep_read_view.decorate_deep_read_state.
        deep_read=(
            _deep_read_action(
                item_id, channel_id, deep_read, is_owner=is_owner, csrf_token=csrf_token
            )
            if ai_score is not None
            else None
        ),
    )


def _build_editorial_page(
    conn: sqlite3.Connection,
    items: list[Row],
    definition: ChannelDefinition,
    channel_id: int,
    channel_name: str,
    highlight_count: int,
    t: Localizer,
    now: datetime,
    *,
    is_owner: bool,
    csrf_token: str | None,
    show_read: bool,
) -> EditorialPage:
    deep_reads = get_deep_reads_for_items(conn, [_req_int(item, "id") for item in items])
    views = [
        _editorial_item(
            item,
            t,
            now,
            is_owner=is_owner,
            channel_id=channel_id,
            deep_read=deep_reads.get(_req_int(item, "id")),
            csrf_token=csrf_token,
        )
        for item in items
    ]
    unread_count = sum(1 for v in views if not v.is_read)
    read_count = sum(1 for v in views if v.is_read)
    visible = views if show_read else [v for v in views if not v.is_read]
    return EditorialPage(
        template_name=definition.panel_template,
        channel_id=channel_id,
        channel_name=channel_name,
        highlighted=tuple(visible[:highlight_count]),
        folded=tuple(visible[highlight_count:]),
        unread_count=unread_count,
        read_count=read_count,
        show_read=show_read,
    )


# --------------------------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------------------------
class MonitorSort(str, Enum):
    SCORE = "score"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    DISCOUNT = "discount"


@dataclass(frozen=True, slots=True)
class MonitorQuery:
    """The route-supplied catalogue view: which page of the active listing to show, how to sort
    it, and which listings to keep. Every field has a stable default so callers that only page
    need not restate the rest, and the same values are echoed back on `MonitorPage` for form
    state when a route is wired. Availability is not a filter here -- it decides which section a
    listing lands in (see _build_monitor_page)."""

    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    sort: MonitorSort = MonitorSort.SCORE
    on_sale_only: bool = False
    vendor: str | None = None
    source: str | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class MonitorChangeMarker:
    """A presentation-safe "what changed" badge for a listing, distilled from the latest
    item_events row (see db.item_events.latest_actionable_events_for_items). `event_type` is one of
    'discovered' / 'price_drop' / 'back_in_stock'; the old/new price fields are populated only for
    a price drop. The raw event payload/JSON never reaches this model -- only these normalized
    values do."""

    event_type: str
    old_price: float | None
    old_price_label: str | None
    new_price: float | None
    new_price_label: str | None


@dataclass(frozen=True, slots=True)
class MonitorItemView:
    id: int
    title: str
    open_url: str
    safe_url: str
    image_url: str | None
    source_label: str
    ai_score: int | None
    ai_summary: str | None
    ai_rationale: str | None
    price: float | None
    price_label: str | None
    compare_at_price: float | None
    compare_at_price_label: str | None
    discount_percent: int | None
    is_on_sale: bool
    # A listing's status is the (is_present, is_available) pair: present + available is the live
    # catalogue; present + unavailable is out of stock; not present is removed/delisted. The
    # Active section is exactly present + available; everything else is Unavailable history.
    is_present: bool
    is_available: bool
    vendor: str | None
    product_type: str | None
    change: MonitorChangeMarker | None


@dataclass(frozen=True, slots=True)
class MonitorPage:
    template_name: str
    channel_id: int
    channel_name: str
    items: tuple[MonitorItemView, ...]
    history: tuple[MonitorItemView, ...]
    pagination: Pagination
    sort: MonitorSort
    on_sale_only: bool
    vendor: str | None
    source: str | None
    search: str | None
    vendor_options: tuple[str, ...]
    source_options: tuple[str, ...]


_MONITOR_EVENT_TYPES = frozenset({"discovered", "price_drop", "back_in_stock"})


def _monitor_change_marker(event: Row | None) -> MonitorChangeMarker | None:
    if event is None:
        return None
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or event_type not in _MONITOR_EVENT_TYPES:
        return None
    old_price: float | None = None
    new_price: float | None = None
    if event_type == "price_drop":
        payload = event.get("payload")
        if isinstance(payload, dict):
            old_price = _as_number(payload.get("old_price"))
            new_price = _as_number(payload.get("new_price"))
    return MonitorChangeMarker(
        event_type=event_type,
        old_price=old_price,
        old_price_label=format_auction_amount(old_price, None),
        new_price=new_price,
        new_price_label=format_auction_amount(new_price, None),
    )


def _monitor_item(item: Row, t: Localizer, event: Row | None) -> MonitorItemView:
    item_id = _req_int(item, "id")
    url = _req_str(item, "url")
    metadata = _metadata(item)
    price = _as_number(metadata.get("price"))
    compare_at_price = _as_number(metadata.get("compare_at_price"))
    on_sale = bool(metadata.get("on_sale"))
    return MonitorItemView(
        id=item_id,
        title=_req_str(item, "title"),
        open_url=_open_url(item_id, url),
        safe_url=_safe_external_href(url),
        image_url=_safe_image_url(metadata.get("image_url")),
        source_label=_source_label(item, t),
        ai_score=_opt_score(item, "ai_score"),
        ai_summary=_clean_text(item.get("ai_summary")),
        ai_rationale=_clean_text(item.get("ai_rationale")),
        price=price,
        price_label=format_auction_amount(price, None),
        compare_at_price=compare_at_price,
        compare_at_price_label=format_auction_amount(compare_at_price, None),
        discount_percent=_discount_percent(price, compare_at_price, on_sale),
        is_on_sale=on_sale,
        is_present=_opt_str(item, "inactive_at") is None,
        is_available=bool(metadata.get("available")),
        vendor=_clean_text(metadata.get("vendor")),
        product_type=_clean_text(metadata.get("product_type")),
        change=_monitor_change_marker(event),
    )


def _monitor_sort_key(sort: MonitorSort):
    # Every key ends in the item id so ties break deterministically and pagination is stable.
    if sort is MonitorSort.PRICE_ASC:
        return lambda v: (v.price is None, v.price if v.price is not None else 0.0, v.id)
    if sort is MonitorSort.PRICE_DESC:
        return lambda v: (v.price is None, -(v.price if v.price is not None else 0.0), v.id)
    if sort is MonitorSort.DISCOUNT:
        return lambda v: (
            v.discount_percent is None,
            -(v.discount_percent if v.discount_percent is not None else 0),
            v.id,
        )
    # SCORE: highest AI score first, unranked last, id tie-break.
    return lambda v: (v.ai_score is None, -(v.ai_score if v.ai_score is not None else 0), v.id)


def _passes_monitor_filters(view: MonitorItemView, query: MonitorQuery) -> bool:
    if query.on_sale_only and not view.is_on_sale:
        return False
    # A blank vendor is "no filter", not "match the empty vendor" -- mirrors how the archive
    # route normalizes an empty form field so a route can forward its raw value here safely.
    wanted_vendor = (query.vendor or "").strip()
    if wanted_vendor and (view.vendor or "").casefold() != wanted_vendor.casefold():
        return False
    wanted_source = (query.source or "").strip()
    if wanted_source and view.source_label.casefold() != wanted_source.casefold():
        return False
    wanted_search = (query.search or "").strip().casefold()
    if wanted_search:
        haystack = " ".join(
            text
            for text in (
                view.title,
                view.ai_summary,
                view.ai_rationale,
                view.vendor,
                view.product_type,
                view.source_label,
            )
            if text
        ).casefold()
        if wanted_search not in haystack:
            return False
    return True


def _build_monitor_page(
    conn: sqlite3.Connection,
    items: list[Row],
    definition: ChannelDefinition,
    channel_id: int,
    channel_name: str,
    t: Localizer,
    query: MonitorQuery,
) -> MonitorPage:
    latest_events = latest_actionable_events_for_items(
        conn, [_req_int(item, "id") for item in items]
    )
    active_views: list[MonitorItemView] = []
    history_views: list[MonitorItemView] = []
    for item in items:
        view = _monitor_item(item, t, latest_events.get(_req_int(item, "id")))
        # Active/Available = present AND in stock. Unavailable history holds both present-but-
        # out-of-stock listings and inactive/removed rows; each keeps its own is_present flag so
        # status text can still tell "out of stock" from "removed". A return to stock flips
        # is_available true and the listing lands back in Active on the next build.
        if view.is_present and view.is_available:
            active_views.append(view)
        else:
            history_views.append(view)

    sort_key = _monitor_sort_key(query.sort)
    # Sort + filter + paginate the Active catalogue (the primary view). The Unavailable history is
    # only sorted, never filtered (an on-sale/vendor filter is a catalogue concern) and not
    # paginated in this first pass.
    filtered = sorted(
        (v for v in active_views if _passes_monitor_filters(v, query)), key=sort_key
    )
    pagination = Pagination(page=query.page, per_page=query.per_page, total=len(filtered))
    window = filtered[pagination.offset : pagination.offset + pagination.per_page]
    history = tuple(sorted(history_views, key=sort_key))
    return MonitorPage(
        template_name=definition.panel_template,
        channel_id=channel_id,
        channel_name=channel_name,
        items=tuple(window),
        history=history,
        pagination=pagination,
        sort=query.sort,
        on_sale_only=query.on_sale_only,
        vendor=query.vendor,
        source=query.source,
        search=query.search,
        vendor_options=tuple(
            sorted({view.vendor for view in active_views if view.vendor}, key=str.casefold)
        ),
        source_options=tuple(
            sorted({view.source_label for view in active_views if view.source_label}, key=str.casefold)
        ),
    )


# --------------------------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TrackerItemView:
    id: int
    title: str
    open_url: str
    safe_url: str
    image_url: str | None
    context: str
    ai_score: int | None
    ai_summary: str | None
    status: str | None
    pricing_facts: tuple[str, ...]
    deadline: str | None
    deadline_label: str | None
    deadline_relative_label: str | None
    reminder_due_at: str | None
    is_active: bool
    is_watched: bool
    is_watchable: bool


@dataclass(frozen=True, slots=True)
class TrackerQuery:
    ending_page: int = 1
    upcoming_page: int = 1
    history_page: int = 1
    per_page: int = DEFAULT_PER_PAGE


@dataclass(frozen=True, slots=True)
class TrackerPage:
    template_name: str
    channel_id: int
    channel_name: str
    watched: tuple[TrackerItemView, ...]
    ending_soon: tuple[TrackerItemView, ...]
    upcoming: tuple[TrackerItemView, ...]
    history: tuple[TrackerItemView, ...]
    ending_pagination: Pagination
    upcoming_pagination: Pagination
    history_pagination: Pagination


def _deadline_relative_label(
    deadline: datetime | None,
    *,
    is_active: bool,
    now: datetime,
    t: Localizer,
) -> str | None:
    if deadline is None:
        return None
    seconds = (deadline - now).total_seconds()
    if not is_active or seconds <= 0:
        return t.text("web.tracker.closed")
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return t.text("web.tracker.ends_in_minutes", count=minutes)
    hours = math.ceil(seconds / 3600)
    if hours < 24:
        return t.text("web.tracker.ends_in_hours", count=hours)
    return t.text("web.tracker.ends_in_days", count=math.ceil(seconds / 86400))


def _tracker_item(
    item: Row,
    t: Localizer,
    now: datetime,
    *,
    is_watched: bool,
    is_owner: bool,
) -> TrackerItemView:
    item_id = _req_int(item, "id")
    url = _req_str(item, "url")
    metadata = _metadata(item)
    adapter = adapter_for_source(_req_str(item, "source_type"))
    try:
        facts = adapter.facts(
            metadata, is_present=_opt_str(item, "inactive_at") is None, now=now
        )
    except ValueError:
        # Malformed lifecycle metadata (e.g. an unparseable deadline) drops the lot into history
        # and out of watch eligibility rather than crashing the page -- same lenient stance as the
        # existing watch-state decoration.
        facts = None
    display = adapter.display_facts(metadata, t)
    # Deadline/active/watchable come straight from the adapter's generic facts -- this builder
    # never parses a connector-specific field itself. A rare facts failure just yields no deadline.
    deadline = facts.deadline if facts is not None else None
    is_active = facts.active if facts is not None else False
    watchable = bool(
        is_owner and (is_watched or (facts is not None and facts.watchable))
    )
    return TrackerItemView(
        id=item_id,
        title=_req_str(item, "title"),
        open_url=_open_url(item_id, url),
        safe_url=_safe_external_href(url),
        image_url=_safe_image_url(metadata.get("image_url")),
        context=_plain_display_text(display.context),
        ai_score=_opt_score(item, "ai_score"),
        ai_summary=_clean_text(item.get("ai_summary")),
        status=_clean_text(metadata.get("status")),
        pricing_facts=tuple(display.details),
        deadline=deadline.isoformat() if deadline is not None else None,
        deadline_label=_host_local_label(deadline.isoformat()) if deadline is not None else None,
        deadline_relative_label=_deadline_relative_label(
            deadline,
            is_active=is_active,
            now=now,
            t=t,
        ),
        reminder_due_at=(
            facts.reminder_due_at.isoformat()
            if facts is not None and facts.reminder_due_at is not None
            else None
        ),
        is_active=is_active,
        is_watched=is_watched,
        is_watchable=watchable,
    )


def _build_tracker_page(
    conn: sqlite3.Connection,
    items: list[Row],
    definition: ChannelDefinition,
    channel_id: int,
    channel_name: str,
    t: Localizer,
    now: datetime,
    query: TrackerQuery,
    *,
    is_owner: bool,
) -> TrackerPage:
    watched_ids = (
        get_watched_item_ids(conn, [_req_int(item, "id") for item in items])
        if is_owner
        else set()
    )
    views = [
        _tracker_item(
            item,
            t,
            now,
            is_watched=_req_int(item, "id") in watched_ids,
            is_owner=is_owner,
        )
        for item in items
    ]

    ending_threshold = now + ENDING_SOON_WINDOW
    watched: list[TrackerItemView] = []
    ending_soon: list[TrackerItemView] = []
    upcoming: list[TrackerItemView] = []
    history: list[TrackerItemView] = []
    for view in views:
        if not view.is_active:
            history.append(view)
        elif view.is_watched:
            watched.append(view)
        elif view.deadline is not None and datetime.fromisoformat(view.deadline) <= ending_threshold:
            ending_soon.append(view)
        else:
            upcoming.append(view)

    sorted_ending = _sort_tracker(ending_soon)
    sorted_upcoming = _sort_tracker(upcoming)
    sorted_history = _sort_tracker(history)
    ending_pagination = Pagination(
        page=query.ending_page,
        per_page=query.per_page,
        total=len(sorted_ending),
    )
    upcoming_pagination = Pagination(
        page=query.upcoming_page,
        per_page=query.per_page,
        total=len(sorted_upcoming),
    )
    history_pagination = Pagination(
        page=query.history_page,
        per_page=query.per_page,
        total=len(sorted_history),
    )
    return TrackerPage(
        template_name=definition.panel_template,
        channel_id=channel_id,
        channel_name=channel_name,
        watched=tuple(_sort_tracker(watched)),
        ending_soon=tuple(
            sorted_ending[
                ending_pagination.offset : ending_pagination.offset
                + ending_pagination.per_page
            ]
        ),
        upcoming=tuple(
            sorted_upcoming[
                upcoming_pagination.offset : upcoming_pagination.offset
                + upcoming_pagination.per_page
            ]
        ),
        history=tuple(
            sorted_history[
                history_pagination.offset : history_pagination.offset
                + history_pagination.per_page
            ]
        ),
        ending_pagination=ending_pagination,
        upcoming_pagination=upcoming_pagination,
        history_pagination=history_pagination,
    )


def _sort_tracker(views: list[TrackerItemView]) -> list[TrackerItemView]:
    """Soonest deadline first (undated last), id tie-break -- deterministic within a section."""
    far_future = "9999-12-31T23:59:59+00:00"
    return sorted(views, key=lambda v: (v.deadline or far_future, v.id))


def build_tracker_item_view(
    conn: sqlite3.Connection,
    item: Row,
    *,
    t: Localizer,
    now: datetime,
    is_owner: bool,
) -> TrackerItemView:
    """Build one Tracker row for an HTMX fragment without depending on its current section/page."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    item_id = _req_int(item, "id")
    watched = item_id in get_watched_item_ids(conn, [item_id]) if is_owner else False
    return _tracker_item(
        item,
        t,
        now,
        is_watched=watched,
        is_owner=is_owner,
    )


# --------------------------------------------------------------------------------------------
# Watch List
#
# The Watch List is an owner-only, cross-Channel surface over the generic tracker_watches rows.
# Every deadline/active/closed fact already comes from the adapter (via list_tracker_watches),
# and context/pricing come from display_facts, so this builder -- unlike the current
# public.py _decorate_watchlist_item -- needs no connector-specific closing-time parsing.
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WatchlistItemView:
    id: int
    title: str
    open_url: str
    safe_url: str
    image_url: str | None
    context: str
    pricing_facts: tuple[str, ...]
    deadline: str | None
    deadline_label: str | None
    deadline_relative_label: str | None
    is_active: bool
    is_closed: bool
    is_watched: bool
    is_watchable: bool


@dataclass(frozen=True, slots=True)
class WatchlistPage:
    items: tuple[WatchlistItemView, ...]


def _watchlist_item(row: Row, t: Localizer, now: datetime) -> WatchlistItemView:
    item_id = _req_int(row, "item_id")
    url = _req_str(row, "url")
    metadata = _metadata(row)
    display = adapter_for_source(_req_str(row, "source_type")).display_facts(metadata, t)
    deadline = _opt_str(row, "deadline")
    deadline_dt = datetime.fromisoformat(deadline) if deadline is not None else None
    is_active = _req_bool(row, "is_active")
    return WatchlistItemView(
        id=item_id,
        title=_req_str(row, "title"),
        open_url=_open_url(item_id, url),
        safe_url=_safe_external_href(url),
        image_url=_safe_image_url(metadata.get("image_url")),
        context=_plain_display_text(display.context),
        pricing_facts=tuple(display.details),
        deadline=deadline,
        deadline_label=_host_local_label(deadline) if deadline is not None else None,
        deadline_relative_label=_deadline_relative_label(
            deadline_dt,
            is_active=is_active,
            now=now,
            t=t,
        ),
        is_active=is_active,
        is_closed=_req_bool(row, "is_closed"),
        # Every row is an existing watch on an owner-only page, so removal is always offered even
        # once a lot has closed.
        is_watched=True,
        is_watchable=True,
    )


def build_watchlist_page(
    conn: sqlite3.Connection, *, t: Localizer, now: datetime
) -> WatchlistPage:
    """Typed model for the owner-only Watch List, in list_tracker_watches' order (active first,
    then by deadline). `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return WatchlistPage(
        items=tuple(_watchlist_item(row, t, now) for row in list_tracker_watches(conn, now))
    )


# --------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------
ChannelPage = EditorialPage | MonitorPage | TrackerPage


def build_channel_page(
    conn: sqlite3.Connection,
    channel: Row,
    *,
    t: Localizer,
    now: datetime,
    is_owner: bool = False,
    csrf_token: str | None = None,
    show_read: bool = False,
    monitor_query: MonitorQuery | None = None,
    tracker_query: TrackerQuery | None = None,
) -> ChannelPage:
    """Resolve a Channel's definition and build its typed page model.

    `now` must be timezone-aware (the Tracker adapter requires it). `show_read` applies only to
    Editorial; `monitor_query` and `tracker_query` apply only to their matching workflows and
    default to their respective query objects. The return type is discriminated by the concrete
    dataclass and its `template_name`, so callers dispatch without reading `channel["kind"]`.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    definition = get_definition(require_channel_kind(_req_str(channel, "kind")))
    channel_id = _req_int(channel, "id")
    channel_name = _req_str(channel, "name")
    minimum_score = _req_int(channel, "minimum_score")

    # Items below the Channel's configured minimum score are hidden on every kind's page, exactly
    # as the current drill-down route filters them; an unranked item (score None) is always kept.
    items: list[Row] = [
        item
        for item in list_by_channel(conn, channel_id)
        if (score := _raw_number(item, "ai_score")) is None or score >= minimum_score
    ]

    if definition.kind is ChannelKind.EDITORIAL:
        return _build_editorial_page(
            conn,
            items,
            definition,
            channel_id,
            channel_name,
            _req_int(channel, "highlight_count"),
            t,
            now,
            is_owner=is_owner,
            csrf_token=csrf_token,
            show_read=show_read,
        )
    if definition.kind is ChannelKind.MONITOR:
        return _build_monitor_page(
            conn,
            items,
            definition,
            channel_id,
            channel_name,
            t,
            monitor_query if monitor_query is not None else MonitorQuery(),
        )
    if definition.kind is ChannelKind.TRACKER:
        return _build_tracker_page(
            conn,
            items,
            definition,
            channel_id,
            channel_name,
            t,
            now,
            tracker_query if tracker_query is not None else TrackerQuery(),
            is_owner=is_owner,
        )
    raise ValueError(f"unsupported Channel kind: {definition.kind!r}")
