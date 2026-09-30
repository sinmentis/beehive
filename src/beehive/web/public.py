"""Public read routes plus owner-gated item actions.

Read pages use optional sessions to expose feedback and deep-read controls to the owner. Mutations
still require an authenticated session and CSRF validation.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import parse_qsl, unquote_plus, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BeforeValidator, Field

from beehive.channels import get_definition, require_channel_kind
from beehive.channels.views import (
    EditorialItemView,
    EditorialPage,
    EditorialQuery,
    MonitorGender,
    MonitorPage,
    MonitorQuery,
    MonitorSort,
    Pagination,
    TrackerPage,
    TrackerDeadline,
    TrackerQuery,
    TrackerStatus,
    WatchlistQuery,
    build_channel_page,
    build_editorial_item_views,
    build_monitor_item_views,
    build_tracker_item_view,
    build_tracker_item_views,
    build_watchlist_page,
)
from beehive.db.tracker_watches import (
    add_tracker_watch,
    get_watched_item_ids,
    remove_closed_tracker_watches,
    remove_tracker_watch,
    remove_tracker_watches,
)
from beehive.email_routing import resolve_default_email
from beehive.notify import build_notifier
from beehive.db.channels import get_channel, list_channels
from beehive.db.deep_reads import (
    get_deep_read,
    request_deep_read,
)
from beehive.db.items import (
    count_archive_by_day,
    count_dashboard_signals,
    count_dashboard_signals_by_channel,
    count_search_results_by_channel,
    get_item,
    list_archive,
    list_dashboard_highlights,
    list_search_results,
    mark_dashboard_signals_read,
    mark_channel_read,
    mark_item_opened,
    mark_read,
    mark_unread,
)
from beehive.db.read_batches import get_read_batch, record_read_batch, undo_read_batch
from beehive.db.sources import get_source, list_by_channel as list_sources
from beehive.db.votes import delete_vote, get_vote, upsert_vote
from beehive.featured import featured_utc_bounds, load_featured_window_days
from beehive.domain.channels import ReadModel
from beehive.localization import Localizer
from beehive.scheduling import HOST_TZ
from beehive.source_labels import parse_source_config, reading_source_label
from beehive.web.deep_read_view import (
    ALLOWED_ORIGINS,
    brief_url,
    build_brief_context,
)
from beehive.web.deps import (
    get_db,
    get_localizer,
    get_optional_session,
    require_admin_session,
    require_reader,
    verify_csrf,
)
from beehive.web.formatting import (
    fetch_stats_label,
    freshness_exact_time,
    freshness_label,
    host_local_time_label,
)
from beehive.web.link_safety import safe_external_href
from beehive.web.home import build_channel_desk, build_ranked_stories
from beehive.web.reading import channel_chapter_number, number_sections, render_reading
from beehive.web.reading_prefs import (
    DEFAULT_PER_PAGE as DEFAULT_LISTING_PER_PAGE,
    PER_PAGE_CHOICES,
    ListingPrefs,
    ListingView,
    resolve_listing_prefs,
)
from beehive.web.workspace import render_workspace
from beehive.tracker_reminders import send_tracker_reminder_for_item


# Every reading page and action passes the reading-access gate first (ADR-0011).
router = APIRouter(dependencies=[Depends(require_reader)])
_LOGGER = logging.getLogger(__name__)

DASHBOARD_SIGNAL_COUNT = 24


def _empty_string_to_none(value: object) -> object | None:
    return None if value == "" else value


_OptionalScoreQuery = Annotated[
    Annotated[int, Field(ge=0, le=100)] | None,
    BeforeValidator(_empty_string_to_none),
]
_OptionalPriceQuery = Annotated[
    Annotated[float, Field(ge=0)] | None,
    BeforeValidator(_empty_string_to_none),
]


def _require_editorial_item(item: dict) -> None:
    definition = get_definition(require_channel_kind(item["channel_kind"]))
    if definition.read_model is not ReadModel.TRACKED:
        raise HTTPException(
            status_code=422,
            detail="This action is only available for Editorial items",
        )


# Query parameters that belong to one render: a story kept in place after a vote, and the
# batch a "mark all as read" can take back. Links and forms a page builds never carry them on.
_ONE_RENDER_KEYS = frozenset({"keep", "read_batch"})
# SQLite stores integers in 64 bits; a larger id could never match and would not bind.
_MAX_SQL_INTEGER = 2**63 - 1


def _url_without(request: Request, keys: frozenset[str]) -> str:
    """The page's own address, path and query as the browser sent them, without `keys`."""
    kept = [
        pair
        for pair in request.url.query.split("&")
        if pair and unquote_plus(pair.partition("=")[0]) not in keys
    ]
    return f"{request.url.path}?{'&'.join(kept)}" if kept else request.url.path


def _source_summary(sources: list[dict], t: Localizer) -> str:
    """The Channel page's "Sources: r/PersonalFinanceNZ, ..." line: the Owner's name for each
    Source, else its reader-facing label, each store named once."""
    labels = [
        str(s.get("name") or "").strip()
        or reading_source_label(s["type"], parse_source_config(s["config"]), t)
        for s in sources
    ]
    return t.text("web.channel.source_list_separator").join(dict.fromkeys(labels))


def _monitor_page_url(
    page: MonitorPage,
    *,
    active_page: int | None = None,
    history_page: int | None = None,
    display: tuple[tuple[str, str], ...] = (),
    anchor: str = "",
) -> str:
    params: list[tuple[str, str]] = [("sort", page.sort.value)]
    active_page = active_page if active_page is not None else page.pagination.page
    history_page = (
        history_page
        if history_page is not None
        else page.history_pagination.page
    )
    if active_page != 1:
        params.append(("page", str(active_page)))
    if history_page != 1:
        params.append(("history_page", str(history_page)))
    if page.on_sale_only:
        params.append(("on_sale", "1"))
    params.extend(("vendor", vendor) for vendor in page.vendors)
    params.extend(("source", source) for source in page.sources)
    params.extend(("gender", gender.value) for gender in page.genders)
    if page.search:
        params.append(("q", page.search))
    if page.criteria.showing_below_threshold:
        params.append(("show_below", "1"))
    params.extend(display)
    fragment = f"#{anchor}" if anchor else ""
    return f"/channels/{page.channel_id}?{urlencode(params)}{fragment}"


def _tracker_page_url(
    page: TrackerPage,
    *,
    ending_page: int | None = None,
    upcoming_page: int | None = None,
    history_page: int | None = None,
    display: tuple[tuple[str, str], ...] = (),
    anchor: str = "",
) -> str:
    pages = {
        "ending_page": (
            ending_page if ending_page is not None else page.ending_pagination.page
        ),
        "upcoming_page": (
            upcoming_page
            if upcoming_page is not None
            else page.upcoming_pagination.page
        ),
        "history_page": (
            history_page if history_page is not None else page.history_pagination.page
        ),
    }
    params = {key: str(value) for key, value in pages.items() if value != 1}
    if page.search:
        params["q"] = page.search
    if page.source:
        params["source"] = page.source
    if page.category:
        params["category"] = page.category
    if page.status is not TrackerStatus.ALL:
        params["status"] = page.status.value
    if page.deadline is not TrackerDeadline.ALL:
        params["deadline"] = page.deadline.value
    if page.minimum_score is not None:
        params["min_score"] = str(page.minimum_score)
    if page.maximum_price is not None:
        params["max_price"] = str(page.maximum_price)
    if page.criteria.showing_below_threshold:
        params["show_below"] = "1"
    params.update(display)
    query = urlencode(params)
    fragment = f"#{anchor}" if anchor else ""
    base = f"/channels/{page.channel_id}?{query}" if query else f"/channels/{page.channel_id}"
    return f"{base}{fragment}"


def _display_params(prefs: ListingPrefs) -> tuple[tuple[str, str], ...]:
    """The view and page size a store or auction page's own links carry, so they hold even
    without the remembering cookie. Defaults are left out."""
    params: list[tuple[str, str]] = []
    if prefs.view is not prefs.default_view:
        params.append(("view", prefs.view.value))
    if prefs.per_page != DEFAULT_LISTING_PER_PAGE:
        params.append(("per_page", str(prefs.per_page)))
    return tuple(params)


def _current_url_with(
    request: Request, updates: dict[str, str], drop: frozenset[str] = frozenset()
) -> str:
    """The page's own address with `updates` set and `drop` removed, other filters kept."""
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in updates and key not in drop
    ]
    params.extend(updates.items())
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def _read_batch_context(
    request: Request, conn: sqlite3.Connection, token: str | None, now: datetime
) -> dict:
    """The undo offer after "mark all as read", while that batch can still be taken back."""
    return {
        "read_batch": get_read_batch(conn, token, now),
        "read_batch_next_url": _url_without(request, _ONE_RENDER_KEYS),
    }


# Changing the page size starts every list on the page over.
_PAGE_KEYS = frozenset({"page", "ending_page", "upcoming_page", "history_page", *_ONE_RENDER_KEYS})


def _listing_display_links(request: Request, prefs: ListingPrefs) -> dict:
    """The head's view switch and page-size choice for a store or auction page."""
    return {
        "listing_view": prefs.view.value,
        "per_page": prefs.per_page,
        "view_links": [
            (
                choice.value,
                _current_url_with(request, {"view": choice.value}, _ONE_RENDER_KEYS),
            )
            for choice in ListingView
        ],
        "per_page_links": [
            (size, _current_url_with(request, {"per_page": str(size)}, _PAGE_KEYS))
            for size in PER_PAGE_CHOICES
        ],
    }


def _local_day_bounds(
    date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    """The UTC fetch-time range that covers the given Auckland days, both inclusive, so the
    Archive filters by the same day it files each story under. A date that does not parse, or
    whose bound falls outside the calendar, is ignored, like a blank one."""

    def start_of(day: str | None, days_later: int) -> str | None:
        if not day:
            return None
        try:
            local_day = date.fromisoformat(day) + timedelta(days=days_later)
            start = datetime.combine(local_day, time.min, tzinfo=HOST_TZ).astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return None
        return start.replace(tzinfo=None).isoformat(timespec="seconds")

    return start_of(date_from, 0), start_of(date_to, 1)


_WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _archive_days(
    groups: list[tuple[str, list[dict]]], totals: dict[str, int], t: Localizer
) -> list[dict]:
    """The Archive page's day sections: each Auckland day on the page, its anchor and weekday,
    and how many stories it holds under the current filters across every page."""
    return [
        {
            "key": day,
            "anchor": f"day-{day}",
            "label": t.text(
                f"web.weekday.{_WEEKDAY_KEYS[date.fromisoformat(day).weekday()]}"
            ),
            "rows": rows,
            "total": totals.get(day, len(rows)),
        }
        for day, rows in groups
    ]


def _group_by_local_day(
    items: list[dict], stories: tuple[EditorialItemView, ...]
) -> list[tuple[str, list[dict]]]:
    """Archive rows grouped by the Auckland day they were fetched, newest first as listed, each
    with its local fetch time. Stored times are UTC, so the day and the time are converted
    together; grouping on the stored date would file an Auckland morning under yesterday."""
    groups: dict[str, list[dict]] = {}
    for item, story in zip(items, stories, strict=True):
        fetched = datetime.fromisoformat(item["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        local = fetched.astimezone(HOST_TZ)
        groups.setdefault(local.date().isoformat(), []).append(
            {
                "story": story,
                "channel_name": item["channel_name"],
                "channel_id": item["item_channel_id"],
                "time": local.strftime("%H:%M"),
                "exact": host_local_time_label(item["fetched_at"]),
            }
        )
    return list(groups.items())


def _ranked_list_url(
    view: str, minimum_score: int | None, page: int = 1
) -> str:
    """The home page's ranked list. It always carries `view`, which is what opens the list
    instead of the channel desk."""
    params: dict[str, str | int] = {"view": view}
    if minimum_score is not None:
        params["minimum_score"] = minimum_score
    if page != 1:
        params["page"] = page
    return f"/?{urlencode(params)}"


def _archive_page_url(
    page: int,
    *,
    channel_id: int | None,
    date_from: str | None,
    date_to: str | None,
    read_state: str | None,
    search: str | None,
) -> str:
    params: dict[str, str | int] = {}
    if channel_id is not None:
        params["channel"] = channel_id
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    if read_state:
        params["read_state"] = read_state
    if search:
        params["q"] = search
    if page != 1:
        params["page"] = page
    query = urlencode(params)
    return f"/archive?{query}" if query else "/archive"


def _editorial_page_url(
    page: EditorialPage,
    page_number: int,
    *,
    include_read_filter: bool,
    anchor: str = "",
) -> str:
    params: dict[str, str | int] = {}
    if include_read_filter and page.show_read:
        params["show_read"] = 1
    if page.search:
        params["q"] = page.search
    if page.criteria.showing_below_threshold:
        params["show_below"] = 1
    if page_number != 1:
        params["page"] = page_number
    query = urlencode(params)
    fragment = f"#{anchor}" if anchor else ""
    base = f"/channels/{page.channel_id}?{query}" if query else f"/channels/{page.channel_id}"
    return f"{base}{fragment}"


def _editorial_clear_search_url(page: EditorialPage) -> str:
    params: dict[str, str | int] = {}
    if page.show_read:
        params["show_read"] = 1
    if page.criteria.showing_below_threshold:
        params["show_below"] = 1
    query = urlencode(params)
    return f"/channels/{page.channel_id}?{query}" if query else f"/channels/{page.channel_id}"


def _editorial_show_read_url(page: EditorialPage) -> str:
    params: dict[str, str | int] = {"show_read": 1}
    if page.search:
        params["q"] = page.search
    if page.criteria.showing_below_threshold:
        params["show_below"] = 1
    return f"/channels/{page.channel_id}?{urlencode(params)}"


def _safe_return_url(value: str | None, fallback: str) -> str:
    # Browsers read a backslash as a slash, so "/\host" would leave the site like "//host".
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return fallback
    return value


def _criteria_toggle_url(request: Request, *, showing_below: bool) -> str:
    reset_keys = {
        "page", "ending_page", "upcoming_page", "history_page", "show_below", *_ONE_RENDER_KEYS
    }
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in reset_keys
    ]
    if not showing_below:
        params.append(("show_below", "1"))
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def _search_url(search: str, page: int = 1) -> str:
    params: dict[str, str | int] = {"q": search}
    if page != 1:
        params["page"] = page
    return f"/search?{urlencode(params)}"


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    view: Literal["all", "unread", "read"] | None = Query(default=None),
    minimum_score: int | None = Query(default=None, ge=0, le=100),
    page: int | None = Query(default=None, ge=1),
    keep: int | None = Query(default=None, ge=1, le=_MAX_SQL_INTEGER),
    read_batch: str | None = Query(default=None, max_length=64),
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    """The home page. By default it is the channel desk: one section per Channel. Any of
    `view`, `minimum_score` or `page` opens the ranked list of every featured story instead,
    which is where the head's counts lead and what older links to the home page expect."""
    is_admin = session is not None
    csrf_token = session["csrf_token"] if is_admin else None
    now = datetime.now(timezone.utc)
    featured_window_days = load_featured_window_days(conn)
    published_from, published_to = featured_utc_bounds(now, featured_window_days)
    day_filters = {
        "published_from": published_from,
        "published_to": published_to,
    }
    # One scan of the window serves every count on the page: the head's totals and each
    # Channel's section.
    story_counts = count_dashboard_signals_by_channel(conn, **day_filters)
    all_signal_count = sum(counts["all"] for counts in story_counts.values())
    unread_signal_count = sum(counts["unread"] for counts in story_counts.values())
    channels = list_channels(conn)
    context = {
        "is_admin": is_admin,
        "csrf_token": csrf_token,
        "featured_window_days": featured_window_days,
        "all_signal_count": all_signal_count,
        "unread_signal_count": unread_signal_count,
        "read_signal_count": all_signal_count - unread_signal_count,
        "high_priority_count": sum(counts["high"] for counts in story_counts.values()),
        "all_url": _ranked_list_url("all", None),
        "unread_url": _ranked_list_url("unread", None),
        "read_url": _ranked_list_url("read", None),
        "high_priority_url": _ranked_list_url("all", 90),
        "has_channels": bool(channels),
        **_read_batch_context(request, conn, read_batch if is_admin else None, now),
    }
    if view is None and minimum_score is None and page is None:
        context.update(
            list_view=False,
            desk=build_channel_desk(
                conn,
                channels,
                t=t,
                now=now,
                is_owner=is_admin,
                csrf_token=csrf_token,
                story_counts=story_counts,
                **day_filters,
            ),
            return_url="/",
        )
    else:
        effective_view = (view or "all") if is_admin else "all"
        pending_signal_count = count_dashboard_signals(
            conn,
            minimum_score=minimum_score,
            read_state=effective_view,
            **day_filters,
        )
        kept = keep if is_admin else None
        # The pager counts a story kept after a vote, so it stays on the page it sat on.
        listed_total = (
            pending_signal_count
            if kept is None
            else count_dashboard_signals(
                conn,
                minimum_score=minimum_score,
                read_state=effective_view,
                keep_item_id=kept,
                **day_filters,
            )
        )
        # A page past the end (after marking the last unread page read, say) shows the last one.
        page_count = -(-listed_total // DASHBOARD_SIGNAL_COUNT)
        pagination = Pagination(
            page=min(page or 1, max(page_count, 1)),
            per_page=DASHBOARD_SIGNAL_COUNT,
            total=listed_total,
        )
        rows = list_dashboard_highlights(
            conn,
            limit=DASHBOARD_SIGNAL_COUNT,
            offset=pagination.offset,
            minimum_score=minimum_score,
            read_state=effective_view,
            keep_item_id=kept,
            **day_filters,
        )
        context.update(
            list_view=True,
            view=effective_view,
            minimum_score=minimum_score,
            pagination=pagination,
            pending_signal_count=pending_signal_count,
            ranked=build_ranked_stories(
                conn, rows, t=t, now=now, is_owner=is_admin, csrf_token=csrf_token
            ),
            previous_url=(
                _ranked_list_url(effective_view, minimum_score, pagination.page - 1)
                if pagination.has_previous
                else None
            ),
            next_url=(
                _ranked_list_url(effective_view, minimum_score, pagination.page + 1)
                if pagination.has_next
                else None
            ),
            return_url=_ranked_list_url(effective_view, minimum_score, pagination.page),
            filter_url=_ranked_list_url(effective_view, minimum_score),
        )
    return render_reading(
        request,
        t,
        "dashboard.html",
        context,
        is_owner=is_admin,
        channels=channels,
        featured_unread=unread_signal_count,
    )


@router.get("/items/{item_id}/open")
def open_item(
    item_id: int,
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if session is not None:
        mark_item_opened(conn, item_id)
        if item["channel_kind"] == "editorial":
            mark_read(conn, item_id)
    return RedirectResponse(safe_external_href(item["url"]), status_code=302)


@router.get("/channels", include_in_schema=False)
@router.get("/channels/", include_in_schema=False)
def channels_index() -> RedirectResponse:
    """There is no separate Channel index: the home page's desk lists every Channel."""
    return RedirectResponse("/", status_code=302)


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel_drilldown(
    channel_id: int,
    request: Request,
    show_read: int | None = None,
    page_number: int = Query(1, alias="page", ge=1),
    sort: MonitorSort = MonitorSort.SCORE,
    on_sale: bool = False,
    vendor: list[str] | None = Query(None),
    source: list[str] | None = Query(None),
    gender: list[MonitorGender] | None = Query(None),
    category: str | None = None,
    q: str | None = None,
    show_below: bool = False,
    tracker_status: TrackerStatus = Query(TrackerStatus.ALL, alias="status"),
    deadline: TrackerDeadline = TrackerDeadline.ALL,
    min_score: Annotated[_OptionalScoreQuery, Query()] = None,
    max_price: Annotated[_OptionalPriceQuery, Query()] = None,
    ending_page: int = Query(1, ge=1),
    upcoming_page: int = Query(1, ge=1),
    history_page: int = Query(1, ge=1),
    view: str | None = None,
    per_page: int | None = None,
    keep: int | None = Query(None, ge=1, le=_MAX_SQL_INTEGER),
    read_batch: str | None = Query(None, max_length=64),
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    is_admin = session is not None
    csrf_token = session["csrf_token"] if is_admin else None
    now = datetime.now(timezone.utc)
    # A store or auction Channel remembers its list or gallery view and its page size.
    prefs = (
        resolve_listing_prefs(
            request, channel_id=channel_id, kind=channel["kind"], view=view, per_page=per_page
        )
        if channel["kind"] in {"monitor", "tracker"}
        else None
    )
    listing_per_page = prefs.per_page if prefs is not None else DEFAULT_LISTING_PER_PAGE
    display = _display_params(prefs) if prefs is not None else ()
    page = build_channel_page(
        conn,
        channel,
        t=t,
        now=now,
        is_owner=is_admin,
        csrf_token=csrf_token,
        show_read=bool(show_read) if is_admin else True,
        show_below_score=is_admin and show_below,
        # A story just marked not relevant stays in place until the next load, so its reason
        # field is still there to fill in.
        editorial_query=EditorialQuery(
            page=page_number, search=q, keep_item_id=keep if is_admin else None
        ),
        monitor_query=MonitorQuery(
            page=page_number,
            history_page=history_page,
            per_page=listing_per_page,
            sort=sort,
            on_sale_only=on_sale,
            vendors=tuple(vendor or ()),
            sources=tuple(source or ()),
            genders=tuple(gender or ()),
            search=q,
        ),
        tracker_query=TrackerQuery(
            ending_page=ending_page,
            upcoming_page=upcoming_page,
            history_page=history_page,
            per_page=listing_per_page,
            search=q,
            source=next(
                (value for value in (source or ()) if value.strip()),
                None,
            ),
            category=category,
            status=tracker_status,
            deadline=deadline,
            minimum_score=min_score,
            maximum_price=max_price,
        ),
    )

    sources = list_sources(conn, channel_id)
    channels = list_channels(conn)
    context = {
        "page": page,
        "freshness": freshness_label(sources, t),
        "freshness_exact": freshness_exact_time(sources),
        "fetch_stats": fetch_stats_label(sources, t),
        "source_summary": _source_summary(sources, t),
        "is_admin": is_admin,
        "csrf_token": csrf_token,
        "monitor_previous_url": None,
        "monitor_next_url": None,
        "monitor_history_previous_url": None,
        "monitor_history_next_url": None,
        "editorial_previous_url": None,
        "editorial_next_url": None,
        "tracker_ending_previous_url": None,
        "tracker_ending_next_url": None,
        "tracker_upcoming_previous_url": None,
        "tracker_upcoming_next_url": None,
        "tracker_history_previous_url": None,
        "tracker_history_next_url": None,
        "return_url": _url_without(request, _ONE_RENDER_KEYS),
        "criteria_toggle_url": _criteria_toggle_url(
            request,
            showing_below=page.criteria.showing_below_threshold,
        ),
        **_read_batch_context(request, conn, read_batch if is_admin else None, now),
    }
    sections: list[tuple[str, str, str]] = []
    if isinstance(page, EditorialPage):
        if page.folded_pagination.page == 1:
            sections.append(("top", t.text("web.channel.priority_heading"), "#top"))
        # A later page keeps its section when it has emptied (its last unread story was just
        # read), so the reader still has the pager back.
        if page.folded or page.folded_pagination.page > 1:
            sections.append(("more", t.text("web.channel.folded_heading"), "#more"))
        if page.folded_pagination.has_previous:
            context["editorial_previous_url"] = _editorial_page_url(
                page,
                page.folded_pagination.previous_page,
                include_read_filter=is_admin,
                anchor="more",
            )
        if page.folded_pagination.has_next:
            context["editorial_next_url"] = _editorial_page_url(
                page,
                page.folded_pagination.page + 1,
                include_read_filter=is_admin,
                anchor="more",
            )
        context["editorial_show_read_url"] = _editorial_show_read_url(page)
        context["editorial_unread_url"] = _editorial_page_url(
            page, 1, include_read_filter=False
        )
        context["editorial_clear_search_url"] = _editorial_clear_search_url(page)
    if isinstance(page, MonitorPage):
        sections.append(("available", t.text("web.monitor.available_heading"), "#available"))
        if page.history_pagination.total:
            sections.append(("history", t.text("web.monitor.unavailable_heading"), "#history"))
        context["source_choices"] = [
            {"value": option, "label": option, "selected": option in page.sources}
            for option in page.source_options
        ]
        context["vendor_choices"] = [
            {"value": option, "label": option, "selected": option in page.vendors}
            for option in page.vendor_options
        ]
        context["gender_choices"] = [
            {"value": option.value, "label": option.label, "selected": option.selected}
            for option in page.gender_options
        ]
        if page.pagination.has_previous:
            context["monitor_previous_url"] = _monitor_page_url(
                page,
                active_page=page.pagination.previous_page,
                display=display,
                anchor="available",
            )
        if page.pagination.has_next:
            context["monitor_next_url"] = _monitor_page_url(
                page,
                active_page=page.pagination.page + 1,
                display=display,
                anchor="available",
            )
        if page.history_pagination.has_previous:
            context["monitor_history_previous_url"] = _monitor_page_url(
                page,
                history_page=page.history_pagination.previous_page,
                display=display,
                anchor="history",
            )
        if page.history_pagination.has_next:
            context["monitor_history_next_url"] = _monitor_page_url(
                page,
                history_page=page.history_pagination.page + 1,
                display=display,
                anchor="history",
            )
    if isinstance(page, TrackerPage):
        if page.watched:
            sections.append(("watched", t.text("web.tracker.watched_heading"), "#watched"))
        if page.ending_pagination.total:
            sections.append(("ending", t.text("web.tracker.ending_heading"), "#ending"))
        if page.upcoming_pagination.total:
            sections.append(("upcoming", t.text("web.tracker.upcoming_heading"), "#upcoming"))
        if page.history_pagination.total:
            sections.append(("history", t.text("web.tracker.history_heading"), "#history"))
        context["open_total"] = (
            len(page.watched) + page.ending_pagination.total + page.upcoming_pagination.total
        )
        if page.ending_pagination.has_previous:
            context["tracker_ending_previous_url"] = _tracker_page_url(
                page,
                ending_page=page.ending_pagination.previous_page,
                display=display,
                anchor="ending",
            )
        if page.ending_pagination.has_next:
            context["tracker_ending_next_url"] = _tracker_page_url(
                page,
                ending_page=page.ending_pagination.page + 1,
                display=display,
                anchor="ending",
            )
        if page.upcoming_pagination.has_previous:
            context["tracker_upcoming_previous_url"] = _tracker_page_url(
                page,
                upcoming_page=page.upcoming_pagination.previous_page,
                display=display,
                anchor="upcoming",
            )
        if page.upcoming_pagination.has_next:
            context["tracker_upcoming_next_url"] = _tracker_page_url(
                page,
                upcoming_page=page.upcoming_pagination.page + 1,
                display=display,
                anchor="upcoming",
            )
        if page.history_pagination.has_previous:
            context["tracker_history_previous_url"] = _tracker_page_url(
                page,
                history_page=page.history_pagination.previous_page,
                display=display,
                anchor="history",
            )
        if page.history_pagination.has_next:
            context["tracker_history_next_url"] = _tracker_page_url(
                page,
                history_page=page.history_pagination.page + 1,
                display=display,
                anchor="history",
            )
    toc_sections, section_numbers = number_sections(
        channel_chapter_number(channels, channel_id), sections
    )
    context.update(
        current_chapter=f"channel-{channel_id}",
        toc_sections=toc_sections,
        sec=section_numbers,
    )
    if prefs is not None:
        context.update(_listing_display_links(request, prefs))
    response = render_reading(
        request, t, page.template_name, context, is_owner=is_admin, channels=channels
    )
    return prefs.remember(response) if prefs is not None else response


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist(
    request: Request,
    q: str = Query("", max_length=200),
    status: Literal["active", "closed", "all"] = Query("active"),
    reminder: Literal[
        "all", "scheduled", "due", "sent", "error", "inactive"
    ] = Query("all"),
    page: int = Query(1, ge=1),
    retry: str | None = Query(None),
    removed: int | None = Query(None, ge=0),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    now = datetime.now(timezone.utc)
    watchlist_page = build_watchlist_page(
        conn,
        t=t,
        now=now,
        query=WatchlistQuery(
            search=q,
            status=status,
            reminder=reminder,
            page=page,
        ),
    )

    def page_url(target_page: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(target_page)
        params.pop("retry", None)
        params.pop("removed", None)
        return f"/watchlist?{urlencode(params)}"

    def status_url(target_status: str) -> str:
        params = dict(request.query_params)
        params["status"] = target_status
        params.pop("page", None)
        params.pop("retry", None)
        params.pop("removed", None)
        return f"/watchlist?{urlencode(params)}"

    return render_workspace(
        request,
        t,
        "watchlist.html",
        {
            "page": watchlist_page,
            "default_email": resolve_default_email(
                conn,
                os.environ.get("DIGEST_EMAIL_TO"),
            ).address,
            "csrf_token": session["csrf_token"],
            "retry_result": retry,
            "removed_count": removed,
            "watchlist_previous_url": (
                page_url(watchlist_page.pagination.page - 1)
                if watchlist_page.pagination.has_previous
                else None
            ),
            "watchlist_next_url": (
                page_url(watchlist_page.pagination.page + 1)
                if watchlist_page.pagination.has_next
                else None
            ),
            "watchlist_return_url": str(
                request.url.path
                if not request.url.query
                else f"{request.url.path}?{request.url.query}"
            ),
            "watchlist_active_url": status_url("active"),
            "watchlist_closed_url": status_url("closed"),
            "watchlist_all_url": status_url("all"),
            "watchlist_range_start": (
                watchlist_page.pagination.offset + 1
                if watchlist_page.pagination.total
                else 0
            ),
            "watchlist_range_end": min(
                watchlist_page.pagination.offset + len(watchlist_page.items),
                watchlist_page.pagination.total,
            ),
        },
    )


@router.post("/watchlist/remove-selected")
def remove_selected_watches(
    csrf_token: str = Form(...),
    item_ids: list[int] | None = Form(None),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    removed = remove_tracker_watches(conn, item_ids or [])
    target = _safe_return_url(next_url, "/watchlist")
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}removed={removed}", status_code=303)


@router.post("/watchlist/clear-closed")
def clear_closed_watches(
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    removed = remove_closed_tracker_watches(conn, datetime.now(timezone.utc))
    return RedirectResponse(
        f"/watchlist?status=closed&removed={removed}",
        status_code=303,
    )


@router.post("/items/{item_id}/retry-reminder")
def retry_tracker_reminder(
    item_id: int,
    csrf_token: str = Form(...),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    recipient = resolve_default_email(conn, os.environ.get("DIGEST_EMAIL_TO"))
    if recipient.address is None:
        result = "no_recipient"
    else:
        try:
            sent = send_tracker_reminder_for_item(
                conn,
                build_notifier(dict(os.environ), recipient.address),
                recipient,
                t,
                item_id,
            )
            result = "sent" if sent else "unavailable"
        except Exception:
            _LOGGER.exception("Tracker reminder retry failed for item %s", item_id)
            result = "failed"
    target = _safe_return_url(next_url, "/watchlist")
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}retry={result}", status_code=303)


@router.post("/items/{item_id}/watch", response_class=HTMLResponse)
def toggle_tracker_watch(
    item_id: int,
    request: Request,
    csrf_token: str = Form(...),
    origin: str = Form("channel"),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    removed = item_id in get_watched_item_ids(conn, [item_id])
    if removed:
        remove_tracker_watch(conn, item_id)
    else:
        try:
            add_tracker_watch(conn, item_id, datetime.now(timezone.utc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    actual_channel_id = _item_channel_id(conn, item)
    if origin == "watchlist":
        # The Watch List reloads the same view. htmx follows the redirect and swaps the list,
        # counts and warnings from the fresh page, so nothing on it describes a removed lot.
        return RedirectResponse(_safe_return_url(next_url, "/watchlist"), status_code=303)
    channel_url = f"/channels/{actual_channel_id}" if actual_channel_id is not None else "/"
    if request.headers.get("HX-Request") == "true":
        item_view = build_tracker_item_view(
            conn,
            item,
            t=t,
            now=datetime.now(timezone.utc),
            is_owner=True,
        )
        return request.app.state.templates.TemplateResponse(
            request,
            "_tracker_watch_control.html",
            {
                "item": item_view,
                "csrf_token": session["csrf_token"],
                "return_url": _safe_return_url(next_url, channel_url),
                "watch_origin": "channel",
                "watch_target": "this",
                "watch_swap": "outerHTML",
                "watch_remove_mode": False,
            },
        )

    # Without JavaScript the post returns to the page it came from, filters and all.
    if origin in {"channel", "folded"}:
        return RedirectResponse(_safe_return_url(next_url, channel_url), status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/items/{item_id}/vote")
def vote_on_item(
    item_id: int,
    value: int = Form(...),
    csrf_token: str = Form(...),
    reason: str | None = Form(None),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    """The Owner's relevance vote on an Editorial story. Voting the same way again clears it; a
    reason keeps the down vote and saves the note. Judging a story not relevant also reads it,
    and taking that judgement back (clearing it, or switching to relevant) makes it unread
    again. The response redirects back to the page, where htmx swaps the story's row from; a
    vote that read the story asks the page to keep it listed for that render."""
    verify_csrf(session, csrf_token)
    if value not in (1, -1):
        raise HTTPException(status_code=422, detail="value must be 1 or -1")

    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _require_editorial_item(item)

    existing = get_vote(conn, item_id)
    clearing = reason is None and existing is not None and existing["value"] == value
    if clearing:
        delete_vote(conn, item_id)
    else:
        upsert_vote(conn, item_id, value, reason)
    judged_not_relevant = value == -1 and not clearing
    if judged_not_relevant:
        mark_read(conn, item_id)
    elif existing is not None and existing["value"] == -1:
        mark_unread(conn, item_id)

    channel_id = _item_channel_id(conn, item)
    fallback = f"/channels/{channel_id}" if channel_id is not None else "/"
    target = _safe_return_url(next_url, fallback)
    if judged_not_relevant:
        target = _url_with_param(target, "keep", str(item_id))
    return RedirectResponse(target, status_code=303)


def _url_with_param(url: str, key: str, value: str) -> str:
    """`url` with its `key` parameter set to `value`, anything else kept."""
    address, _, fragment = url.partition("#")
    path, _, query = address.partition("?")
    params = [
        (name, current)
        for name, current in parse_qsl(query, keep_blank_values=True)
        if name != key
    ]
    params.append((key, value))
    return f"{path}?{urlencode(params)}" + (f"#{fragment}" if fragment else "")


@router.post("/items/{item_id}/relevance", response_class=HTMLResponse)
def set_listing_relevance(
    item_id: int,
    request: Request,
    value: int = Form(...),
    csrf_token: str = Form(...),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    if value not in (1, -1):
        raise HTTPException(status_code=422, detail="value must be 1 or -1")
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    channel_id = _item_channel_id(conn, item)
    channel = get_channel(conn, channel_id) if channel_id is not None else None
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    definition = get_definition(require_channel_kind(channel["kind"]))
    if definition.read_model is ReadModel.TRACKED:
        raise HTTPException(
            status_code=422,
            detail="Use Editorial feedback controls for this item",
        )

    existing = get_vote(conn, item_id)
    if existing is not None and existing["value"] == value:
        delete_vote(conn, item_id)
        feedback_value = None
    else:
        upsert_vote(conn, item_id, value)
        feedback_value = value

    if request.headers.get("HX-Request") == "true":
        return request.app.state.templates.TemplateResponse(
            request,
            "_listing_feedback_control.html",
            {
                "item": {"id": item_id, "feedback_value": feedback_value},
                "csrf_token": session["csrf_token"],
                "return_url": _safe_return_url(next_url, f"/channels/{channel_id}"),
                "feedback_hx": True,
            },
        )
    return RedirectResponse(
        _safe_return_url(next_url, f"/channels/{channel_id}"),
        status_code=303,
    )


@router.post("/items/{item_id}/read-state")
def set_item_read_state(
    item_id: int,
    csrf_token: str = Form(...),
    is_read: bool = Form(...),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _require_editorial_item(item)
    if is_read:
        mark_read(conn, item_id)
    else:
        mark_unread(conn, item_id)
    return RedirectResponse(_safe_return_url(next_url, "/"), status_code=303)


@router.post("/dashboard/mark-all-read")
def mark_dashboard_read_route(
    csrf_token: str = Form(...),
    minimum_score: int | None = Form(None),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    now = datetime.now(timezone.utc)
    published_from, published_to = featured_utc_bounds(
        now, load_featured_window_days(conn)
    )
    marked = mark_dashboard_signals_read(
        conn,
        minimum_score=minimum_score,
        published_from=published_from,
        published_to=published_to,
        read_state="unread",
    )
    return _redirect_after_read_batch(
        conn, marked, _safe_return_url(next_url, "/"), now
    )


def _redirect_after_read_batch(
    conn: sqlite3.Connection, marked: list[int], target: str, now: datetime
) -> RedirectResponse:
    """Back to the page, which offers to undo the batch for a few minutes."""
    batch = record_read_batch(conn, marked, now)
    if batch is not None:
        target = _url_with_param(target, "read_batch", batch.token)
    return RedirectResponse(target, status_code=303)


@router.post("/read-batches/undo")
def undo_read_batch_route(
    csrf_token: str = Form(...),
    token: str = Form(..., max_length=64),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    undo_read_batch(conn, token, datetime.now(timezone.utc))
    return RedirectResponse(_safe_return_url(next_url, "/"), status_code=303)


@router.post("/channels/{channel_id}/mark-all-read")
def mark_all_read_route(
    channel_id: int,
    csrf_token: str = Form(...),
    next_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    definition = get_definition(require_channel_kind(channel["kind"]))
    if definition.read_model is not ReadModel.TRACKED:
        raise HTTPException(
            status_code=422,
            detail="This Channel does not use read state",
        )
    marked = mark_channel_read(conn, channel_id)
    return _redirect_after_read_batch(
        conn,
        marked,
        _safe_return_url(next_url, f"/channels/{channel_id}"),
        datetime.now(timezone.utc),
    )


_ARCHIVE_PAGE_SIZE = 30
_SEARCH_PAGE_SIZE = 30


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    page: int = Query(1, ge=1),
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    items, total = list_search_results(
        conn,
        search=q,
        page=page,
        page_size=_SEARCH_PAGE_SIZE,
    )
    is_admin = session is not None
    csrf_token = session["csrf_token"] if is_admin else None
    now = datetime.now(timezone.utc)
    channels = list_channels(conn)
    # The page's hits, newest first, sorted into one section per Channel in rail order. Each
    # section uses its Channel's own rows: stories, listings or lots.
    hits_by_channel: dict[int, list[dict]] = {}
    for item in items:
        hits_by_channel.setdefault(item["item_channel_id"], []).append(item)
    totals = count_search_results_by_channel(conn, search=q) if items else {}
    sections = []
    for channel in channels:
        hits = hits_by_channel.get(channel["id"])
        if not hits:
            continue
        if channel["kind"] == "editorial":
            rows = build_editorial_item_views(
                conn, hits, t=t, now=now, is_owner=is_admin, csrf_token=csrf_token
            )
        elif channel["kind"] == "monitor":
            rows = build_monitor_item_views(conn, hits, t=t)
        else:
            rows = build_tracker_item_views(conn, hits, t=t, now=now, is_owner=is_admin)
        sections.append(
            {
                "anchor": f"channel-{channel['id']}",
                "name": channel["name"],
                "kind": channel["kind"],
                "rows": rows,
                "total": totals.get(channel["id"], len(hits)),
                "href": f"/channels/{channel['id']}?{urlencode({'q': q.strip()})}",
            }
        )
    toc_sections, section_numbers = number_sections(
        1, [(section["anchor"], section["name"], f"#{section['anchor']}") for section in sections]
    )

    pagination = Pagination(page=page, per_page=_SEARCH_PAGE_SIZE, total=total)
    previous_url = _search_url(q, pagination.previous_page) if pagination.has_previous else None
    next_url = _search_url(q, page + 1) if pagination.has_next else None
    return render_reading(
        request,
        t,
        "search.html",
        {
            "sections": sections,
            "sec": section_numbers,
            "toc_sections": toc_sections,
            "query": q,
            "total": total,
            "pagination": pagination,
            "previous_url": previous_url,
            "next_url": next_url,
            "is_admin": is_admin,
            "csrf_token": csrf_token,
            "return_url": _search_url(q, page),
        },
        is_owner=is_admin,
        channels=channels,
    )


@router.get("/archive", response_class=HTMLResponse)
def archive(
    request: Request,
    channel: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    read_state: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    keep: int | None = Query(None, ge=1, le=_MAX_SQL_INTEGER),
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    # The filter form's <select>/<input type=date> fields submit an EMPTY STRING when left at
    # their default/blank state (e.g. channel="", from="") -- not an omitted query param. FastAPI
    # would reject an empty string for `channel: int` outright (422 "int_parsing"), and an empty
    # string for from_/to would otherwise pass list_archive's `is not None` check and become a
    # literal SQL `date('')` comparison, which is NULL and matches nothing. Normalizing every
    # blank string to None here, before any of them reach list_archive, fixes both failure modes
    # in one place instead of duplicating "was this actually left blank" logic downstream. A
    # non-numeric channel value can only reach here via a hand-crafted URL (the <select>'s only
    # non-empty options are real channel ids) -- treat it the same as "no filter" rather than a
    # 500, since this is a public read-only filter with nothing security-sensitive at stake.
    try:
        channel_id = int(channel) if channel else None
    except ValueError:
        channel_id = None
    from_ = from_ or None
    to = to or None
    is_admin = session is not None
    effective_read_state = read_state if is_admin else None
    fetched_from, fetched_before = _local_day_bounds(from_, to)
    items, total = list_archive(
        conn,
        channel_id=channel_id,
        fetched_from=fetched_from,
        fetched_before=fetched_before,
        read_state=effective_read_state,
        search=q,
        page=page,
        page_size=_ARCHIVE_PAGE_SIZE,
        # A story just judged not relevant stays under an unread filter for this render.
        keep_item_id=keep if is_admin else None,
    )
    csrf_token = session["csrf_token"] if is_admin else None
    stories = build_editorial_item_views(
        conn,
        items,
        t=t,
        now=datetime.now(timezone.utc),
        is_owner=is_admin,
        csrf_token=csrf_token,
        deep_read_origin="archive",
    )
    groups = _group_by_local_day(items, stories)
    day_totals = count_archive_by_day(
        conn,
        [(day, *_local_day_bounds(day, day)) for day, _ in groups],
        channel_id=channel_id,
        read_state=effective_read_state,
        search=q,
        keep_item_id=keep if is_admin else None,
    )
    days = _archive_days(groups, day_totals, t)
    channels = list_channels(conn)
    # The Archive is the rail's last chapter; its sections are the days on this page.
    toc_sections, section_numbers = number_sections(
        len(channels) + 2,
        [(day["key"], f"{day['key']} {day['label']}", f"#{day['anchor']}") for day in days],
    )
    pagination = Pagination(page=page, per_page=_ARCHIVE_PAGE_SIZE, total=total)

    def page_url(target: int) -> str:
        return _archive_page_url(
            target,
            channel_id=channel_id,
            date_from=from_,
            date_to=to,
            read_state=effective_read_state,
            search=q,
        )

    return render_reading(
        request,
        t,
        "archive.html",
        {
            "days": days,
            "sec": section_numbers,
            "toc_sections": toc_sections,
            "channels": [channel for channel in channels if channel["kind"] == "editorial"],
            "total": total,
            "pagination": pagination,
            "previous_url": (
                page_url(pagination.previous_page) if pagination.has_previous else None
            ),
            "next_url": page_url(page + 1) if pagination.has_next else None,
            "selected_channel": channel_id,
            "selected_from": from_ or "",
            "selected_to": to or "",
            "selected_read_state": effective_read_state or "",
            "selected_search": q or "",
            "is_admin": is_admin,
            "csrf_token": csrf_token,
            "return_url": page_url(page),
        },
        is_owner=is_admin,
        channels=channels,
    )


# ============================================================================
# Deep read: owner-only request/regenerate, public/optional-session brief + HTMX status
# polling. web/deep_read_view.py owns the view-model/parsing/URL-building; these route bodies
# only do request handling (auth, CSRF, input validation, DB reads, and dispatching into the
# deep_reads repository).
# ============================================================================


def _item_channel_id(conn: sqlite3.Connection, item: dict) -> int | None:
    """Derives the Channel that actually owns this item, via its Source -- never trusts a
    caller-supplied channel_id at face value. Used to validate a "channel" origin's channel_id
    genuinely belongs to this item (not just any existing channel), so a crafted request can
    never produce a brief page/back-link pointed at an unrelated channel."""
    source = get_source(conn, item["source_id"])
    return source["channel_id"] if source is not None else None


def _resolve_brief_origin(
    conn: sqlite3.Connection, item: dict, origin: str | None, channel_id: int | None
) -> tuple[str | None, int | None, str | None]:
    """Lenient origin/channel_id resolution for the public GET brief/status routes: an
    unrecognized origin, or a channel_id that is not the channel actually owning this item, is
    just dropped back to "no back-nav context" (default back link) rather than erroring a
    read-only page over a bad/crafted query param.
    Returns (origin_or_None, channel_id_or_None, channel_name_or_None)."""
    if origin not in ALLOWED_ORIGINS:
        return None, None, None
    if origin != "channel":
        return origin, None, None
    if channel_id is None or channel_id != _item_channel_id(conn, item):
        return None, None, None
    channel = get_channel(conn, channel_id)
    if channel is None:
        return None, None, None
    return origin, channel_id, channel["name"]


def _brief_item(
    conn: sqlite3.Connection,
    item: dict,
    t: Localizer,
    *,
    is_owner: bool,
    csrf_token: str | None,
) -> EditorialItemView:
    """The same typed view the Channel pages show, so a brief labels its story the same way."""
    return build_editorial_item_views(
        conn,
        [item],
        t=t,
        now=datetime.now(timezone.utc),
        is_owner=is_owner,
        csrf_token=csrf_token,
    )[0]


def _brief_chapter(
    channels: list[dict], origin: str | None, channel_id: int | None
) -> tuple[str, int]:
    """The rail chapter a brief sits in and its number: the Channel it was opened from, the
    Archive, or Featured."""
    if origin == "channel" and channel_id is not None:
        return f"channel-{channel_id}", channel_chapter_number(channels, channel_id)
    if origin == "archive":
        return "archive", len(channels) + 2
    return "featured", 1


def _brief_sections(
    t: Localizer, context: dict, chapter_number: int
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """A brief's numbered sections under its chapter: a finished brief's five parts, or else the
    one status section that says where it stands."""
    if context["status"] == "ready" and context["brief"] is not None:
        sections = [
            ("bottom_line", t.text("web.deep_read.section_bottom_line"), "#brief-bottom-line"),
            ("findings", t.text("web.deep_read.section_key_findings"), "#brief-findings"),
            ("why", t.text("web.deep_read.section_why_it_matters"), "#brief-why"),
            ("figures", t.text("web.deep_read.section_important_figures"), "#brief-figures"),
            ("source", t.text("web.reading.col_source"), "#brief-source"),
        ]
    else:
        sections = [("status", t.text("web.deep_read.eyebrow"), "#deep-read-status")]
    return number_sections(chapter_number, sections)


@router.post("/items/{item_id}/deep-read")
def request_deep_read_route(
    item_id: int,
    request: Request,
    csrf_token: str = Form(...),
    origin: str = Form(...),
    regenerate: bool = Form(False),
    channel_id: int | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    if origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=422, detail="Invalid origin")

    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _require_editorial_item(item)

    if origin == "channel":
        # channel_id must be the channel that ACTUALLY owns this item (derived through its own
        # Source, never taken at face value just because it names *some* existing channel) --
        # otherwise a crafted request could redirect to a brief page whose back link points at
        # an unrelated channel.
        if channel_id is None or channel_id != _item_channel_id(conn, item):
            raise HTTPException(status_code=404, detail="Channel not found")
    else:
        channel_id = None

    if item["ai_score"] is None:
        raise HTTPException(status_code=422, detail="Item has not been AI-ranked")

    # The row is the queue: the worker's deep-read lane polls for pending work, so a committed
    # request is all it takes.
    request_deep_read(conn, item_id, datetime.now(timezone.utc), regenerate=regenerate)

    return RedirectResponse(brief_url(item_id, origin, channel_id), status_code=303)


@router.get("/items/{item_id}/brief", response_class=HTMLResponse)
def deep_read_brief(
    item_id: int,
    request: Request,
    origin: str | None = None,
    channel_id: int | None = None,
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _require_editorial_item(item)

    is_owner = session is not None
    csrf_token = session["csrf_token"] if is_owner else None
    resolved_origin, resolved_channel_id, channel_name = _resolve_brief_origin(
        conn, item, origin, channel_id
    )
    deep_read = get_deep_read(conn, item_id)
    context = build_brief_context(
        item=_brief_item(conn, item, t, is_owner=is_owner, csrf_token=csrf_token),
        deep_read=deep_read,
        is_owner=is_owner,
        origin=resolved_origin,
        channel_id=resolved_channel_id,
        channel_name=channel_name,
        csrf_token=csrf_token,
        t=t,
    )

    channels = list_channels(conn)
    context["current_chapter"], chapter_number = _brief_chapter(
        channels, resolved_origin, resolved_channel_id
    )
    context["toc_sections"], context["sec"] = _brief_sections(t, context, chapter_number)
    return render_reading(
        request, t, "deep_read_brief.html", context, is_owner=is_owner, channels=channels
    )


@router.get("/items/{item_id}/brief/status", response_class=HTMLResponse)
def deep_read_brief_status(
    item_id: int,
    request: Request,
    origin: str | None = None,
    channel_id: int | None = None,
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _require_editorial_item(item)

    is_owner = session is not None
    csrf_token = session["csrf_token"] if is_owner else None
    resolved_origin, resolved_channel_id, channel_name = _resolve_brief_origin(
        conn, item, origin, channel_id
    )
    deep_read = get_deep_read(conn, item_id)
    context = build_brief_context(
        item=_brief_item(conn, item, t, is_owner=is_owner, csrf_token=csrf_token),
        deep_read=deep_read,
        is_owner=is_owner,
        origin=resolved_origin,
        channel_id=resolved_channel_id,
        channel_name=channel_name,
        csrf_token=csrf_token,
        t=t,
    )

    templates = request.app.state.templates
    # Each poll re-renders the status section with its numbered heading, so it is numbered the
    # same way as on the page.
    _, chapter_number = _brief_chapter(list_channels(conn), resolved_origin, resolved_channel_id)
    _, context["sec"] = _brief_sections(t, context, chapter_number)
    response = templates.TemplateResponse(request, "_deep_read_status.html", context)
    # Ready is terminal regardless of whether the cached result later turns out to parse (a
    # malformed cache still stops polling -- the brief page itself renders the localized
    # unavailable failure for that case, see deep_read_view.build_brief_context).
    if deep_read is not None and deep_read.status == "ready":
        response.headers["HX-Redirect"] = context["brief_url"]
    return response
