"""Fully public read routes — no auth (ADR-0003). Slice 1 omits the vote buttons and
mark-all-read control, since no session mechanism exists yet to gate them (ADR-0005) —
Slice 2 adds both once a minimal login exists."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.db.channels import get_channel, list_channels
from beehive.db.items import (count_dashboard_signals, get_item, list_archive, list_by_channel,
                              list_dashboard_highlights, mark_channel_read, mark_item_opened)
from beehive.db.sources import list_by_channel as list_sources
from beehive.db.votes import delete_vote, get_vote, upsert_vote
from beehive.localization import Localizer
from beehive.web.deps import get_db, get_localizer, get_optional_session, require_admin_session, verify_csrf
from beehive.web.formatting import fetch_stats_label, freshness_exact_time, freshness_label, host_local_time_label, next_fetch_countdown, relative_time
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.official_feed_labels import official_feed_label


def _safe_href(url: str) -> str:
    return url if urlparse(url).scheme in ("http", "https") else "#"


router = APIRouter()

HIGHLIGHT_COUNT = 8
DASHBOARD_SIGNAL_COUNT = 24


def _source_label(item: dict, t: Localizer) -> str:
    config = json.loads(item["source_config"])
    if item["source_type"] == "reddit_subreddit":
        return f"r/{config['subreddit']}"
    if item["source_type"] == "google_news_query":
        return f'"{config["query"]}"'
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
    return ""


def _decorate_item(item: dict, t: Localizer) -> None:
    item["source_label"] = _source_label(item, t)
    item["engagement_label"] = _engagement_label(item, t)
    item["age"] = relative_time(item["created_at"], t) if item["created_at"] else ""
    item["exact_time"] = host_local_time_label(item["created_at"]) if item["created_at"] else ""
    item["safe_url"] = _safe_href(item["url"])
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


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    minimum_score: int | None = Query(default=None, ge=0, le=100),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    pending_signal_count = count_dashboard_signals(conn, minimum_score=minimum_score)
    highlights = list_dashboard_highlights(
        conn,
        limit=DASHBOARD_SIGNAL_COUNT,
        minimum_score=minimum_score,
    )
    has_more_signals = pending_signal_count > DASHBOARD_SIGNAL_COUNT
    for item in highlights:
        _decorate_item(item, t)
    now = datetime.now(timezone.utc)
    channels = []
    total_unread = 0
    for channel in list_channels(conn):
        items = list_by_channel(conn, channel["id"])
        unread_count = sum(1 for i in items if not i["is_read"])
        total_unread += unread_count
        teaser = next((i for i in items if i["ai_summary"]), None)
        if teaser is not None:
            _decorate_item(teaser, t)
        sources = list_sources(conn, channel["id"])
        channels.append({
            "id": channel["id"], "name": channel["name"],
            "unread_count": unread_count, "teaser": teaser,
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
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "channels": channels,
        "highlights": highlights,
        "has_more_signals": has_more_signals,
        "high_priority_count": count_dashboard_signals(conn, minimum_score=90),
        "pending_signal_count": pending_signal_count,
        "dashboard_time": host_local_time_label(now.isoformat())[-5:],
        "minimum_score": minimum_score,
        "total_unread": total_unread,
    })

@router.get("/items/{item_id}/open")
def open_item(item_id: int, conn: sqlite3.Connection = Depends(get_db)):
    item = get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    mark_item_opened(conn, item_id)
    return RedirectResponse(_safe_href(item["url"]), status_code=302)


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel_drilldown(channel_id: int, request: Request, show_read: int | None = None,
                       session: dict | None = Depends(get_optional_session),
                       conn: sqlite3.Connection = Depends(get_db),
                       t: Localizer = Depends(get_localizer)):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    is_admin = session is not None
    items = list_by_channel(conn, channel_id)
    for item in items:
        _decorate_item(item, t)
        if not is_admin:
            item["vote_reason"] = None

    sources = list_sources(conn, channel_id)
    unread_count = sum(1 for i in items if not i["is_read"])
    read_count = sum(1 for i in items if i["is_read"])
    visible_items = items if show_read else [i for i in items if not i["is_read"]]

    if is_admin:
        mark_channel_read(conn, channel_id)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "channel_drilldown.html", {
        "channel": channel,
        "highlighted": visible_items[:HIGHLIGHT_COUNT],
        "folded": visible_items[HIGHLIGHT_COUNT:],
        "freshness": freshness_label(sources, t),
        "freshness_exact": freshness_exact_time(sources),
        "fetch_stats": fetch_stats_label(sources, t),
        "unread_count": unread_count,
        "read_count": read_count,
        "show_read": bool(show_read),
        "source_summary": _source_summary(sources, t),
        "is_admin": is_admin,
        "csrf_token": session["csrf_token"] if is_admin else None,
    })


@router.post("/items/{item_id}/vote", response_class=HTMLResponse)
def vote_on_item(item_id: int, request: Request, value: int = Form(...),
                  csrf_token: str = Form(...), reason: str | None = Form(None),
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

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "_item_card.html", {
        "item": item, "is_admin": True, "csrf_token": session["csrf_token"],
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
    for item in items:
        _decorate_item(item, t)
        item["time_label"] = _time_label(item["fetched_at"])
        item["vote_reason"] = None  # archive is always anonymous: never surface the private reason

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
    })
