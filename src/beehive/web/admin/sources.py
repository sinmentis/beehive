"""Source pages: add, edit, test, pause, resume and delete a Source."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.channels import require_channel_kind
from beehive.channels.source_policy import connector_supports_kind, source_types_for_kind
from beehive.connectors.base import PreviewSourceConnector
from beehive.connectors.registry import get as get_connector
from beehive.db.admin_actions import (
    delete_source_with_undo,
    record_admin_action,
)
from beehive.db.channels import (
    get_channel,
)
from beehive.db.sources import (
    create_source,
    find_duplicate_source,
    get_source,
    set_source_paused,
    update_source,
)
from beehive.domain.channels import ChannelKind
from beehive.localization import (
    Localizer,
)
from beehive.web.deps import (
    get_db,
    get_localizer,
    require_admin_session,
    verify_csrf,
)
from beehive.web.link_safety import safe_external_href
from beehive.source_labels import source_display_name
from beehive.web.admin.common import (
    _admin_source_label,
    _channel_kind_label,
    _render_admin,
    _safe_return_path,
    _source_confirmation_value,
)

router = APIRouter()


def _confirmation_matches(submitted: str, expected: str) -> bool:
    return submitted.strip().casefold() == expected.strip().casefold()


def _source_type_options(t: Localizer) -> tuple[dict, ...]:
    """Built per-request (not module-level) since every label is a translated string --
    Reddit/Google News/Hacker News/RBNZ/Federal Reserve stay in their own proper names in every
    language; only the descriptor after the em dash (e.g. "Subreddit", "Keyword query") changes
    per locale. Each input_id is the id admin.css keys the matching config fields to, and each
    hint is the one-line description shown beside the radio."""
    return (
        {
            "type_key": "reddit_subreddit",
            "input_id": "type-reddit",
            "hint": t.text("web.admin.source_type_hint.reddit_subreddit"),
            "label": t.text("web.source_type.reddit_subreddit"),
        },
        {
            "type_key": "google_news_query",
            "input_id": "type-google",
            "hint": t.text("web.admin.source_type_hint.google_news_query"),
            "label": t.text("web.source_type.google_news_query"),
        },
        {
            "type_key": "hackernews_stories",
            "input_id": "type-hn-stories",
            "hint": t.text("web.admin.source_type_hint.hackernews_stories"),
            "label": t.text("web.source_type.hackernews_stories"),
        },
        {
            "type_key": "hackernews_query",
            "input_id": "type-hn-query",
            "hint": t.text("web.admin.source_type_hint.hackernews_query"),
            "label": t.text("web.source_type.hackernews_query"),
        },
        {
            "type_key": "rbnz_news",
            "input_id": "type-rbnz",
            "hint": t.text("web.admin.source_type_hint.rbnz_news"),
            "label": t.text("web.source_type.rbnz_news"),
        },
        {
            "type_key": "nz_government_news",
            "input_id": "type-nz-gov",
            "hint": t.text("web.admin.source_type_hint.nz_government_news"),
            "label": t.text("web.source_type.nz_government_news"),
        },
        {
            "type_key": "federal_reserve_news",
            "input_id": "type-fed",
            "hint": t.text("web.admin.source_type_hint.federal_reserve_news"),
            "label": t.text("web.source_type.federal_reserve_news"),
        },
        {
            "type_key": "shopify_collection",
            "input_id": "type-shopify",
            "hint": t.text("web.admin.source_type_hint.shopify_collection"),
            "label": t.text("web.source_type.shopify_collection"),
        },
        {
            "type_key": "land_sea_collection",
            "input_id": "type-land-sea",
            "hint": t.text("web.admin.source_type_hint.land_sea_collection"),
            "label": t.text("web.source_type.land_sea_collection"),
        },
        {
            "type_key": "international_clearance",
            "input_id": "type-international-clearance",
            "hint": t.text("web.admin.source_type_hint.international_clearance"),
            "label": t.text("web.source_type.international_clearance"),
        },
        {
            "type_key": "all_about_auctions",
            "input_id": "type-all-about-auctions",
            "hint": t.text("web.admin.source_type_hint.all_about_auctions"),
            "label": t.text("web.source_type.all_about_auctions"),
        },
    )


# Every value here is a translations/web.py key, not display text -- see _EMAIL_ERROR_KEYS above.
_SOURCE_ERROR_KEYS = {
    "hackernews_query config needs a non-empty 'query' key": "web.source_error.hn_query_required",
    "reddit_subreddit config needs a non-empty 'subreddit' key": "web.source_error.reddit_subreddit_required",
    "google_news_query config needs a non-empty 'query' key": "web.source_error.google_query_required",
    "shopify_collection config needs a non-empty 'collection_url' key": "web.source_error.shopify_collection_url_required",
    "shopify_collection config needs 'collection_url' to be a valid http(s) URL": "web.source_error.shopify_collection_url_invalid",
    "land_sea_collection config needs a non-empty 'collection_url' key": "web.source_error.land_sea_collection_url_required",
    "land_sea_collection config needs 'collection_url' to be a valid http(s) URL": "web.source_error.land_sea_collection_url_invalid",
    "international_clearance config needs 'retailer' to be one of: the_outnet, mytheresa, end, yoox": "web.source_error.international_clearance_retailer_invalid",
    "international_clearance config needs 'minimum_discount_percent' to be an integer from 50 to 90": "web.source_error.international_clearance_discount_invalid",
    "international_clearance Mytheresa sources support discounts up to 70": "web.source_error.international_clearance_mytheresa_discount_invalid",
}


def _source_error_message(error: ValueError, t: Localizer) -> str:
    message = str(error)
    if message.startswith("hackernews_stories config needs 'feed'"):
        return t.text("web.source_error.hn_feed_invalid")
    if message.startswith("hackernews_query config needs 'sort'"):
        return t.text("web.source_error.hn_sort_invalid")
    key = _SOURCE_ERROR_KEYS.get(message)
    return t.text(key) if key is not None else message


def _source_config_from_form(
    source_type: str,
    *,
    subreddit: str,
    query: str,
    hn_feed: str,
    hn_query: str,
    hn_sort: str,
    shopify_collection_url: str,
    shopify_collection_vendors: str,
    land_sea_collection_url: str,
    international_clearance_retailer: str,
    international_clearance_minimum_discount_percent: str,
) -> dict:
    if source_type == "reddit_subreddit":
        return {"subreddit": subreddit}
    if source_type == "google_news_query":
        return {"query": query}
    if source_type == "hackernews_stories":
        return {"feed": hn_feed}
    if source_type == "hackernews_query":
        return {"query": hn_query, "sort": hn_sort}
    if source_type == "shopify_collection":
        config: dict = {"collection_url": shopify_collection_url}
        vendors = [
            vendor.strip()
            for vendor in shopify_collection_vendors.split(",")
            if vendor.strip()
        ]
        if vendors:
            config["vendors"] = vendors
        return config
    if source_type == "land_sea_collection":
        return {"collection_url": land_sea_collection_url}
    if source_type == "international_clearance":
        try:
            minimum_discount_percent = int(
                international_clearance_minimum_discount_percent
            )
        except ValueError as exc:
            raise ValueError(
                "international_clearance config needs 'minimum_discount_percent' "
                "to be an integer from 50 to 90"
            ) from exc
        return {
            "retailer": international_clearance_retailer,
            "minimum_discount_percent": minimum_discount_percent,
        }
    if source_type in {
        "rbnz_news",
        "nz_government_news",
        "federal_reserve_news",
        "all_about_auctions",
    }:
        return {}
    raise ValueError(f"unknown Source type: {source_type!r}")


def _compatible_source_type_options(t: Localizer, kind: ChannelKind) -> tuple[dict, ...]:
    """The subset of _source_type_options compatible with a Channel of `kind`, kept in the
    display order defined there. The compatible set itself comes from the shared source policy,
    so the Add Source page never offers a Source type persistence would reject."""
    allowed = set(source_types_for_kind(kind))
    return tuple(
        option for option in _source_type_options(t) if option["type_key"] in allowed
    )


_SOURCE_FORM_DEFAULTS = {
    "subreddit": "",
    "query": "",
    "hn_feed": "top",
    "hn_query": "",
    "hn_sort": "relevance",
    "shopify_collection_url": "",
    "shopify_collection_vendors": "",
    "land_sea_collection_url": "",
    "international_clearance_retailer": "mytheresa",
    "international_clearance_minimum_discount_percent": "70",
}


def _form_values_from_source(source: dict) -> dict:
    """Reverse of _source_config_from_form: turn a stored Source's type+config back into the flat
    form-field values the Add/Edit Source form renders, so editing prefills exactly what was saved.
    Only the fields for the Source's own type are populated; the rest keep the shared defaults."""
    config = json.loads(source["config"])
    values = dict(_SOURCE_FORM_DEFAULTS)
    source_type = source["type"]
    if source_type == "reddit_subreddit":
        values["subreddit"] = config.get("subreddit", "")
    elif source_type == "google_news_query":
        values["query"] = config.get("query", "")
    elif source_type == "hackernews_stories":
        values["hn_feed"] = config.get("feed", "top")
    elif source_type == "hackernews_query":
        values["hn_query"] = config.get("query", "")
        values["hn_sort"] = config.get("sort", "relevance")
    elif source_type == "shopify_collection":
        values["shopify_collection_url"] = config.get("collection_url", "")
        values["shopify_collection_vendors"] = ", ".join(config.get("vendors", []))
    elif source_type == "land_sea_collection":
        values["land_sea_collection_url"] = config.get("collection_url", "")
    elif source_type == "international_clearance":
        values["international_clearance_retailer"] = config.get(
            "retailer",
            "mytheresa",
        )
        values["international_clearance_minimum_discount_percent"] = str(
            config.get("minimum_discount_percent", 70)
        )
    return values


def _validated_source_config(
    conn: sqlite3.Connection,
    channel: dict,
    source_type: str,
    form_values: dict,
    t: Localizer,
    *,
    exclude_source_id: int | None = None,
) -> tuple[dict | None, str | None]:
    """The shared new/edit Source validation pipeline. Builds the config and rejects, in order, an
    unknown Source type, a Source/Channel kind mismatch, a bad config, and a duplicate of another
    Source in the same Channel -- each as a localized message. Returns (config, None) on success or
    (None, error) on the first failure, and never persists. exclude_source_id skips the row being
    edited so re-saving a Source unchanged is not flagged as a duplicate of itself."""
    channel_kind = require_channel_kind(channel["kind"])
    try:
        config = _source_config_from_form(source_type, **form_values)
        connector = get_connector(source_type)
    except ValueError as exc:
        return None, _source_error_message(exc, t)
    # Reject a Source type incompatible with this Channel's kind with the same localized 400 flow
    # as a bad config -- persistence would reject it anyway (db.sources), this just turns that into
    # a friendly re-render instead of a 500.
    if not connector_supports_kind(source_type, channel_kind):
        return None, t.text("web.source_error.incompatible_kind")
    try:
        connector.validate_config(config)
    except ValueError as exc:
        return None, _source_error_message(exc, t)
    if find_duplicate_source(
        conn, channel["id"], source_type, config, exclude_source_id=exclude_source_id
    ) is not None:
        return None, t.text("web.source_error.duplicate")
    return config, None


def _render_source_form_page(
    request: Request,
    channel: dict,
    session: dict,
    t: Localizer,
    *,
    mode: str,
    form_action: str,
    cancel_url: str,
    error: str | None = None,
    selected_type: str | None = None,
    form_values: dict | None = None,
    status_code: int = 200,
    source_name: str = "",
    source: dict | None = None,
) -> HTMLResponse:
    """Renders admin_add_source.html for either a brand-new Source (mode="new") or an edit of an
    existing one (mode="edit"). Both modes share the same compatible-type radios, per-type config
    fields, and optional display-name field; only the form target, page copy, and submit label
    differ, so the validation/prefill pipeline is written once and reused by both routes."""
    values = {**_SOURCE_FORM_DEFAULTS, **(form_values or {})}
    channel_kind = require_channel_kind(channel["kind"])
    options = _compatible_source_type_options(t, channel_kind)
    option_keys = {option["type_key"] for option in options}
    # Default (and fall back after an incompatible submission) to the first compatible type, so a
    # radio is always pre-selected with something this Channel can actually accept.
    default_type = options[0]["type_key"] if options else ""
    effective_selected = selected_type if selected_type in option_keys else default_type
    if mode == "edit":
        page_heading = t.text("web.admin.source_edit.heading")
        page_lede = (
            t.text(
                "web.admin.source_edit.meta",
                source=source_display_name(source, t),
                channel=channel["name"],
            )
            if source is not None
            else t.text("web.admin.source_edit.lede", channel=channel["name"])
        )
        submit_label = t.text("web.admin.source_edit.submit")
    else:
        page_heading = t.text("web.admin.source_new.heading")
        page_lede = t.text("web.admin.source_new.lede", channel=channel["name"])
        submit_label = t.text("web.admin.source_new.submit")
    return _render_admin(
        request,
        t,
        "admin_add_source.html",
        {
            "channel": channel,
            "csrf_token": session["csrf_token"],
            "error": error,
            "source_type_options": options,
            "selected_type": effective_selected,
            # The Phase 3 "coming soon" placeholder is an editorial source, so only editorial
            # Channels show it -- a monitor/tracker Channel lists only its own compatible types.
            "show_twitter_soon": channel_kind is ChannelKind.EDITORIAL,
            "form_action": form_action,
            "cancel_url": cancel_url,
            "is_edit": mode == "edit",
            "unsaved_submission": mode == "edit" and status_code >= 400,
            # Editing shows the label an empty name falls back to; a new Source has none yet.
            "name_placeholder": (
                _admin_source_label(source, t)
                if source is not None
                else t.text("web.admin.source_new.name_placeholder")
            ),
            "channel_kind_label": _channel_kind_label(channel_kind, t),
            "page_heading": page_heading,
            "page_lede": page_lede,
            "submit_label": submit_label,
            "source_name": source_name,
            **values,
        },
        status_code=status_code,
    )


def _render_new_source_page(
    request: Request,
    channel: dict,
    session: dict,
    t: Localizer,
    *,
    error: str | None = None,
    selected_type: str | None = None,
    form_values: dict | None = None,
    status_code: int = 200,
    source_name: str = "",
) -> HTMLResponse:
    return _render_source_form_page(
        request,
        channel,
        session,
        t,
        mode="new",
        form_action=f"/admin/channels/{channel['id']}/sources/new",
        cancel_url=f"/admin/channels/{channel['id']}/edit",
        error=error,
        selected_type=selected_type,
        form_values=form_values,
        status_code=status_code,
        source_name=source_name,
    )


def _render_edit_source_page(
    request: Request,
    channel: dict,
    source: dict,
    session: dict,
    t: Localizer,
    *,
    error: str | None = None,
    selected_type: str | None = None,
    form_values: dict | None = None,
    status_code: int = 200,
    source_name: str | None = None,
) -> HTMLResponse:
    return _render_source_form_page(
        request,
        channel,
        session,
        t,
        mode="edit",
        form_action=f"/admin/sources/{source['id']}/edit",
        source=source,
        cancel_url=f"/admin/channels/{channel['id']}/edit",
        error=error,
        selected_type=selected_type,
        form_values=form_values,
        status_code=status_code,
        source_name=(source["name"] or "") if source_name is None else source_name,
    )


@router.get("/channels/{channel_id}/sources/new", response_class=HTMLResponse)
def new_source_form(
    channel_id: int,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_new_source_page(request, channel, session, t)


@router.post("/channels/{channel_id}/sources/new")
def new_source_submit(
    channel_id: int,
    request: Request,
    type: str = Form(...),
    subreddit: str = Form(""),
    query: str = Form(""),
    hn_feed: str = Form("top"),
    hn_query: str = Form(""),
    hn_sort: str = Form("relevance"),
    shopify_collection_url: str = Form(""),
    shopify_collection_vendors: str = Form(""),
    land_sea_collection_url: str = Form(""),
    international_clearance_retailer: str = Form("mytheresa"),
    international_clearance_minimum_discount_percent: str = Form("70"),
    source_name: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    form_values = {
        "subreddit": subreddit,
        "query": query,
        "hn_feed": hn_feed,
        "hn_query": hn_query,
        "hn_sort": hn_sort,
        "shopify_collection_url": shopify_collection_url,
        "shopify_collection_vendors": shopify_collection_vendors,
        "land_sea_collection_url": land_sea_collection_url,
        "international_clearance_retailer": international_clearance_retailer,
        "international_clearance_minimum_discount_percent": (
            international_clearance_minimum_discount_percent
        ),
    }
    config, error = _validated_source_config(conn, channel, type, form_values, t)
    if error is not None:
        return _render_new_source_page(
            request,
            channel,
            session,
            t,
            error=error,
            selected_type=type,
            form_values=form_values,
            status_code=400,
            source_name=source_name,
        )
    source_id = create_source(conn, channel_id, type, config, name=source_name)
    record_admin_action(
        conn,
        action_type="source_created",
        target_type="source",
        target_id=source_id,
        target_label=source_display_name(get_source(conn, source_id), t),
        detail={"channel_id": channel_id},
    )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?source_saved=1",
        status_code=303,
    )


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
def edit_source_form(
    source_id: int,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    channel = get_channel(conn, source["channel_id"])
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_edit_source_page(
        request,
        channel,
        source,
        session,
        t,
        selected_type=source["type"],
        form_values=_form_values_from_source(source),
    )


@router.post("/sources/{source_id}/edit")
def edit_source_submit(
    source_id: int,
    request: Request,
    type: str = Form(...),
    subreddit: str = Form(""),
    query: str = Form(""),
    hn_feed: str = Form("top"),
    hn_query: str = Form(""),
    hn_sort: str = Form("relevance"),
    shopify_collection_url: str = Form(""),
    shopify_collection_vendors: str = Form(""),
    land_sea_collection_url: str = Form(""),
    international_clearance_retailer: str = Form("mytheresa"),
    international_clearance_minimum_discount_percent: str = Form("70"),
    source_name: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    channel = get_channel(conn, source["channel_id"])
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    form_values = {
        "subreddit": subreddit,
        "query": query,
        "hn_feed": hn_feed,
        "hn_query": hn_query,
        "hn_sort": hn_sort,
        "shopify_collection_url": shopify_collection_url,
        "shopify_collection_vendors": shopify_collection_vendors,
        "land_sea_collection_url": land_sea_collection_url,
        "international_clearance_retailer": international_clearance_retailer,
        "international_clearance_minimum_discount_percent": (
            international_clearance_minimum_discount_percent
        ),
    }
    config, error = _validated_source_config(
        conn, channel, type, form_values, t, exclude_source_id=source_id
    )
    if error is not None:
        return _render_edit_source_page(
            request,
            channel,
            source,
            session,
            t,
            error=error,
            selected_type=type,
            form_values=form_values,
            status_code=400,
            source_name=source_name,
        )
    update_source(conn, source_id, type, config, name=source_name)
    record_admin_action(
        conn,
        action_type="source_updated",
        target_type="source",
        target_id=source_id,
        target_label=source_display_name(get_source(conn, source_id), t),
        detail={"channel_id": channel["id"]},
    )
    return RedirectResponse(
        f"/admin/channels/{channel['id']}/edit?source_saved=1",
        status_code=303,
    )


@router.post("/sources/{source_id}/delete")
def delete_source_submit(
    source_id: int,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    channel_id = source["channel_id"]
    if not _confirmation_matches(confirmation, _source_confirmation_value(source, t)):
        raise HTTPException(status_code=409, detail="Source confirmation did not match")
    action_id = delete_source_with_undo(
        conn,
        source_id,
        target_label=source_display_name(source, t),
    )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?source_removed=1"
        f"&undo_action={action_id}",
        status_code=303,
    )


@router.post("/sources/{source_id}/test", response_class=HTMLResponse)
def test_source_submit(
    source_id: int,
    request: Request,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    channel = get_channel(conn, source["channel_id"])
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    started = time.monotonic()
    error = None
    raw_items = []
    try:
        connector = get_connector(source["type"])
        config = json.loads(source["config"])
        raw_items = (
            connector.fetch_preview(config, limit=10)
            if isinstance(connector, PreviewSourceConnector)
            else connector.fetch(config)
        )
    except Exception as exc:
        error = str(exc)
    duration_ms = round((time.monotonic() - started) * 1000)
    record_admin_action(
        conn,
        action_type="source_tested",
        target_type="source",
        target_id=source_id,
        target_label=source_display_name(source, t),
        detail={"items": len(raw_items), "duration_ms": duration_ms, "success": error is None},
    )
    return _render_admin(
        request,
        t,
        "admin_source_test.html",
        {
            "channel": channel,
            "source": source,
            "source_label": _admin_source_label(source, t),
            "items": [
                {
                    "title": item.title,
                    "url": safe_external_href(item.url),
                }
                for item in raw_items[:10]
            ],
            "total_count": len(raw_items),
            "duration_ms": duration_ms,
            "error": error,
        },
        status_code=200 if error is None else 502,
    )


@router.post("/sources/{source_id}/pause")
def pause_source_submit(
    source_id: int,
    csrf_token: str = Form(...),
    return_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    set_source_paused(
        conn, source_id, True, now_iso=datetime.now(timezone.utc).isoformat()
    )
    record_admin_action(
        conn,
        action_type="source_paused",
        target_type="source",
        target_id=source_id,
        target_label=source_display_name(source, t),
        detail={"channel_id": source["channel_id"]},
    )
    fallback = f"/admin/channels/{source['channel_id']}/edit"
    return RedirectResponse(
        _safe_return_path(return_url, fallback) if return_url else fallback,
        status_code=303,
    )


@router.post("/sources/{source_id}/resume")
def resume_source_submit(
    source_id: int,
    csrf_token: str = Form(...),
    return_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    set_source_paused(conn, source_id, False)
    record_admin_action(
        conn,
        action_type="source_resumed",
        target_type="source",
        target_id=source_id,
        target_label=source_display_name(source, t),
        detail={"channel_id": source["channel_id"]},
    )
    fallback = f"/admin/channels/{source['channel_id']}/edit"
    return RedirectResponse(
        _safe_return_path(return_url, fallback) if return_url else fallback,
        status_code=303,
    )
