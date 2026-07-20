"""Public read routes plus owner-gated item actions.

Read pages use optional sessions to expose feedback and deep-read controls to the owner. Mutations
still require an authenticated session and CSRF validation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.collector.deep_read_trigger import request_deep_read_worker
from beehive.db.channels import get_channel, list_channels
from beehive.db.deep_reads import get_deep_read, get_deep_reads_for_items, request_deep_read
from beehive.db.items import (count_dashboard_signals, get_item, list_archive, list_by_channel,
                              list_dashboard_highlights, mark_channel_read, mark_item_opened,
                              mark_read)
from beehive.db.sources import get_source, list_by_channel as list_sources
from beehive.db.votes import delete_vote, get_vote, upsert_vote
from beehive.featured import featured_utc_bounds, load_featured_window_days
from beehive.localization import Localizer
from beehive.web.deep_read_view import (ALLOWED_ORIGINS, brief_url, build_brief_context,
                                        decorate_deep_read_state)
from beehive.web.deps import get_db, get_localizer, get_optional_session, require_admin_session, verify_csrf
from beehive.web.formatting import fetch_stats_label, freshness_exact_time, freshness_label, host_local_time_label, next_fetch_countdown, relative_time
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.link_safety import safe_external_href
from beehive.web.official_feed_labels import official_feed_label


router = APIRouter()

DASHBOARD_SIGNAL_COUNT = 24


def _source_label(item: dict, t: Localizer) -> str:
    config = json.loads(item["source_config"])
    if item["source_type"] == "reddit_subreddit":
        return f"r/{config['subreddit']}"
    if item["source_type"] == "google_news_query":
        return f'"{config["query"]}"'
    if item["source_type"] in {"shopify_collection", "land_sea_collection"}:
        # Both connectors store the same {"collection_url": ...} config shape.
        url = config.get("collection_url", "")
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}" if parsed.netloc else url
    official_label = official_feed_label(item["source_type"])
    if official_label is not None:
        return official_label
    hackernews_label = hackernews_source_label(item["source_type"], config, t)
    return hackernews_label if hackernews_label is not None else item["source_type"]


def _engagement_label(item: dict, t: Localizer) -> str:
    if item["source_type"] == "reddit_subreddit":
        return t.text(
            "web.engagement.reddit",
            score=item["raw_metadata"].get("score", 0),
            comments=item["raw_metadata"].get("num_comments", 0),
        )
    if item["source_type"] == "google_news_query":
        return item["raw_metadata"].get("source_name", "")
    if item["source_type"] in {"hackernews_stories", "hackernews_query"}:
        return t.text(
            "web.engagement.hackernews",
            score=item["raw_metadata"].get("score", 0),
            comments=item["raw_metadata"].get("num_comments", 0),
        )
    if item["source_type"] in {"rbnz_news", "nz_government_news", "federal_reserve_news"}:
        return item["raw_metadata"].get("category", "")
    if item["source_type"] in {"shopify_collection", "land_sea_collection"}:
        metadata = item["raw_metadata"]
        price = metadata.get("price")
        compare_at_price = metadata.get("compare_at_price")
        # Neither connector's product data exposes a currency code (confirmed against real
        # stores), so this never hardcodes a currency symbol -- a percent-off figure is
        # the only currency-agnostic discount signal available.
        if metadata.get("on_sale") and compare_at_price and price is not None:
            percent_off = round((compare_at_price - price) / compare_at_price * 100)
            return t.text("web.engagement.shopify_discount", percent=percent_off)
        return metadata.get("vendor") or ""
    return ""


def _decorate_item(item: dict, t: Localizer) -> None:
    item["source_label"] = _source_label(item, t)
    item["engagement_label"] = _engagement_label(item, t)
    item["age"] = relative_time(item["created_at"], t) if item["created_at"] else ""
    item["exact_time"] = host_local_time_label(item["created_at"]) if item["created_at"] else ""
    item["safe_url"] = safe_external_href(item["url"])
    item["open_url"] = f"/items/{item['id']}/open" if item["safe_url"] != "#" else "#"


def _source_summary(sources: list[dict], t: Localizer) -> str:
    """Channel drill-down's page-sub line lists every Source feeding the Channel, e.g.
    "Sources: r/PersonalFinanceNZ". Mirrors _source_label's reddit_subreddit convention, but over
    raw `sources` rows (type/config), not the source_type/source_config aliases
    list_by_channel's item-join produces."""
    labels = []
    for s in sources:
        config = json.loads(s["config"])
        if s["type"] == "reddit_subreddit":
            labels.append(f"r/{config['subreddit']}")
        elif s["type"] == "google_news_query":
            labels.append(f'"{config["query"]}"')
        elif official_feed_label(s["type"]) is not None:
            labels.append(official_feed_label(s["type"]))
        else:
            hackernews_label = hackernews_source_label(s["type"], config, t)
            labels.append(hackernews_label if hackernews_label is not None else s["type"])
    return t.text("web.channel.source_list_separator").join(labels)


def _group_by_day(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        day = item["fetched_at"][:10]
        groups.setdefault(day, []).append(item)
    return list(groups.items())


def _time_label(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).strftime("%H:%M")


def _dashboard_url(view: str, minimum_score: int | None) -> str:
    params: dict[str, str | int] = {}
    if view != "all":
        params["view"] = view
    if minimum_score is not None:
        params["minimum_score"] = minimum_score
    return f"/?{urlencode(params)}" if params else "/"


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    view: Literal["all", "unread", "read"] = Query(default="all"),
    minimum_score: int | None = Query(default=None, ge=0, le=100),
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    now = datetime.now(timezone.utc)
    featured_window_days = load_featured_window_days(conn)
    published_from, published_to = featured_utc_bounds(now, featured_window_days)
    day_filters = {
        "published_from": published_from,
        "published_to": published_to,
    }
    signal_counts = {
        state: count_dashboard_signals(conn, read_state=state, **day_filters)
        for state in ("all", "unread", "read")
    }
    pending_signal_count = count_dashboard_signals(
        conn,
        minimum_score=minimum_score,
        read_state=view,
        **day_filters,
    )
    highlights = list_dashboard_highlights(
        conn,
        limit=DASHBOARD_SIGNAL_COUNT,
        minimum_score=minimum_score,
        published_from=published_from,
        published_to=published_to,
        read_state=view,
    )
    has_more_signals = pending_signal_count > DASHBOARD_SIGNAL_COUNT
    for item in highlights:
        _decorate_item(item, t)
    channels = []
    for channel in list_channels(conn):
        items = [
            item
            for item in list_by_channel(conn, channel["id"])
            if item["ai_score"] is None or item["ai_score"] >= channel["minimum_score"]
        ]
        unread_count = sum(1 for i in items if not i["is_read"])
        sources = list_sources(conn, channel["id"])
        channels.append({
            "id": channel["id"], "name": channel["name"],
            "unread_count": unread_count,
            "freshness": freshness_label(sources, t),
            "freshness_exact": freshness_exact_time(sources),
            "next_fetch": next_fetch_countdown(
                sources,
                channel["fetch_interval_hours"],
                now,
                t,
            ),
            "fetch_stats": fetch_stats_label(sources, t),
        })

    is_admin = session is not None
    csrf_token = session["csrf_token"] if is_admin else None
    deep_reads = get_deep_reads_for_items(conn, [i["id"] for i in highlights])
    for item in highlights:
        decorate_deep_read_state(
            item, deep_reads.get(item["id"]), is_admin, "dashboard", None, csrf_token)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "channels": channels,
        "highlights": highlights,
        "has_more_signals": has_more_signals,
        "high_priority_count": count_dashboard_signals(
            conn,
            minimum_score=90,
            read_state="all",
            **day_filters,
        ),
        "pending_signal_count": pending_signal_count,
        "all_signal_count": signal_counts["all"],
        "unread_signal_count": signal_counts["unread"],
        "read_signal_count": signal_counts["read"],
        "dashboard_time": host_local_time_label(now.isoformat())[-5:],
        "featured_window_days": featured_window_days,
        "view": view,
        "minimum_score": minimum_score,
        "all_url": _dashboard_url("all", None),
        "unread_url": _dashboard_url("unread", None),
        "read_url": _dashboard_url("read", None),
        "high_priority_url": _dashboard_url("all", 90),
        "total_unread": signal_counts["unread"],
        "is_admin": is_admin,
        "csrf_token": csrf_token,
    })

@router.get("/items/{item_id}/open")
def open_item(item_id: int, conn: sqlite3.Connection = Depends(get_db)):
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    mark_item_opened(conn, item_id)
    mark_read(conn, item_id)
    return RedirectResponse(safe_external_href(item["url"]), status_code=302)


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel_drilldown(channel_id: int, request: Request, show_read: int | None = None,
                       session: dict | None = Depends(get_optional_session),
                       conn: sqlite3.Connection = Depends(get_db),
                       t: Localizer = Depends(get_localizer)):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    is_admin = session is not None
    csrf_token = session["csrf_token"] if is_admin else None
    items = [
        item
        for item in list_by_channel(conn, channel_id)
        if item["ai_score"] is None or item["ai_score"] >= channel["minimum_score"]
    ]
    deep_reads = get_deep_reads_for_items(conn, [i["id"] for i in items])
    for item in items:
        _decorate_item(item, t)
        if not is_admin:
            item["vote_reason"] = None
        decorate_deep_read_state(
            item, deep_reads.get(item["id"]), is_admin, "channel", channel_id, csrf_token)

    sources = list_sources(conn, channel_id)
    unread_count = sum(1 for i in items if not i["is_read"])
    read_count = sum(1 for i in items if i["is_read"])
    visible_items = items if show_read else [i for i in items if not i["is_read"]]

    if is_admin:
        mark_channel_read(conn, channel_id)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "channel_drilldown.html", {
        "channel": channel,
        "nav_channels": list_channels(conn),
        "highlighted": visible_items[:channel["highlight_count"]],
        "folded": visible_items[channel["highlight_count"]:],
        "freshness": freshness_label(sources, t),
        "freshness_exact": freshness_exact_time(sources),
        "fetch_stats": fetch_stats_label(sources, t),
        "unread_count": unread_count,
        "read_count": read_count,
        "show_read": bool(show_read),
        "source_summary": _source_summary(sources, t),
        "is_admin": is_admin,
        "csrf_token": csrf_token,
    })


@router.post("/items/{item_id}/vote", response_class=HTMLResponse)
def vote_on_item(item_id: int, request: Request, value: int = Form(...),
                  csrf_token: str = Form(...), reason: str | None = Form(None),
                  origin: str = Form("channel"),
                  session: dict = Depends(require_admin_session),
                  conn: sqlite3.Connection = Depends(get_db),
                  t: Localizer = Depends(get_localizer)):
    verify_csrf(session, csrf_token)
    if value not in (1, -1):
        raise HTTPException(status_code=422, detail="value must be 1 or -1")

    existing = get_vote(conn, item_id)
    if reason is not None:
        upsert_vote(conn, item_id, value, reason)
    elif existing is not None and existing["value"] == value:
        delete_vote(conn, item_id)
    else:
        upsert_vote(conn, item_id, value, None)

    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _decorate_item(item, t)
    # _item_card.html/_folded_item.html are only ever rendered from the Channel drill-down (the
    # vote widget's own host page), so this fragment's origin is always "channel" -- decorate
    # with the item's ACTUAL owning channel (never trust anything client-supplied; there is no
    # channel_id on this route at all) so the outerHTML-swapped card keeps its deep-read
    # action/control instead of silently losing it on every vote. The partials also key the vote
    # widget's visibility off channel.kind, so this same channel is passed into the template.
    channel_id = _item_channel_id(conn, item)
    channel = get_channel(conn, channel_id) if channel_id is not None else None
    decorate_deep_read_state(
        item, get_deep_read(conn, item_id), True, "channel", channel_id, session["csrf_token"])

    templates = request.app.state.templates
    template_name = "_folded_item.html" if origin == "folded" else "_item_card.html"
    return templates.TemplateResponse(request, template_name, {
        "item": item, "channel": channel, "is_admin": True, "csrf_token": session["csrf_token"],
    })


@router.post("/channels/{channel_id}/mark-all-read")
def mark_all_read_route(channel_id: int, csrf_token: str = Form(...),
                         session: dict = Depends(require_admin_session),
                         conn: sqlite3.Connection = Depends(get_db)):
    verify_csrf(session, csrf_token)
    mark_channel_read(conn, channel_id)
    return RedirectResponse(f"/channels/{channel_id}", status_code=303)


_ARCHIVE_PAGE_SIZE = 30


@router.get("/archive", response_class=HTMLResponse)
def archive(request: Request, channel: str | None = None,
            from_: str | None = Query(None, alias="from"), to: str | None = None,
            read_state: str | None = None, q: str | None = None, page: int = 1,
            session: dict | None = Depends(get_optional_session),
            conn: sqlite3.Connection = Depends(get_db),
            t: Localizer = Depends(get_localizer)):
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
    items, total = list_archive(conn, channel_id=channel_id, date_from=from_, date_to=to,
                                read_state=read_state, search=q, page=page,
                                page_size=_ARCHIVE_PAGE_SIZE)
    is_admin = session is not None
    csrf_token = session["csrf_token"] if is_admin else None
    deep_reads = get_deep_reads_for_items(conn, [i["id"] for i in items])
    for item in items:
        _decorate_item(item, t)
        item["time_label"] = _time_label(item["fetched_at"])
        item["vote_reason"] = None  # archive is always anonymous: never surface the private reason
        decorate_deep_read_state(
            item, deep_reads.get(item["id"]), is_admin, "archive", None, csrf_token)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "archive.html", {
        "groups": _group_by_day(items),
        "channels": list_channels(conn),
        "total": total,
        "page": page,
        "has_prev": page > 1,
        "has_next": page * _ARCHIVE_PAGE_SIZE < total,
        "selected_channel": channel_id,
        "selected_from": from_ or "",
        "selected_to": to or "",
        "selected_read_state": read_state or "",
        "selected_search": q or "",
        "is_admin": is_admin,
        "csrf_token": csrf_token,
    })


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


def _resolve_brief_origin(conn: sqlite3.Connection, item: dict, origin: str | None,
                           channel_id: int | None) -> tuple[str | None, int | None, str | None]:
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

    deep_read = request_deep_read(conn, item_id, datetime.now(timezone.utc), regenerate=regenerate)

    # The marker is a wakeup HINT for systemd (SQLite is the durable queue, see
    # collector/deep_read_trigger.py) -- it is only worth writing when this call actually left
    # (or put) work in 'pending'; a cache hit (ready/failed without regenerate) or an
    # already-'processing' row never needs one, since either nothing changed or a worker is
    # already on it. Any failure writing the marker must never roll back or fail this request --
    # the DB commit inside request_deep_read already happened, so the queued state is real even
    # if the wakeup hint is lost; the reconciliation timer (deep_read_worker) will still pick it
    # up. Just log it.
    if deep_read.status == "pending":
        data_dir = os.path.dirname(request.app.state.db_path)
        try:
            request_deep_read_worker(data_dir)
        except OSError as exc:
            print(
                f"[deep-read] failed to write wakeup marker for item {item_id}: "
                f"{type(exc).__name__}: {exc}"
            )

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
    _decorate_item(item, t)

    is_owner = session is not None
    resolved_origin, resolved_channel_id, channel_name = _resolve_brief_origin(
        conn, item, origin, channel_id)
    deep_read = get_deep_read(conn, item_id)
    context = build_brief_context(
        item=item,
        deep_read=deep_read,
        is_owner=is_owner,
        origin=resolved_origin,
        channel_id=resolved_channel_id,
        channel_name=channel_name,
        csrf_token=session["csrf_token"] if is_owner else None,
        t=t,
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "deep_read_brief.html", context)


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

    is_owner = session is not None
    resolved_origin, resolved_channel_id, channel_name = _resolve_brief_origin(
        conn, item, origin, channel_id)
    deep_read = get_deep_read(conn, item_id)
    context = build_brief_context(
        item=item,
        deep_read=deep_read,
        is_owner=is_owner,
        origin=resolved_origin,
        channel_id=resolved_channel_id,
        channel_name=channel_name,
        csrf_token=session["csrf_token"] if is_owner else None,
        t=t,
    )

    templates = request.app.state.templates
    response = templates.TemplateResponse(request, "_deep_read_status.html", context)
    # Ready is terminal regardless of whether the cached result later turns out to parse (a
    # malformed cache still stops polling -- the brief page itself renders the localized
    # unavailable failure for that case, see deep_read_view.build_brief_context).
    if deep_read is not None and deep_read.status == "ready":
        response.headers["HX-Redirect"] = context["brief_url"]
    return response
