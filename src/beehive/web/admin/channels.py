"""Channel pages: create, edit, duplicate, clear and delete a Channel, and Fetch now."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.channels import all_definitions, require_channel_kind
from beehive.collector.manual_trigger import (
    clear_stale_manual_triggers,
    list_manual_trigger_states,
    request_channel_fetch,
    request_channel_fetch_batch,
)
from beehive.db.admin_actions import (
    clear_channel_with_undo,
    delete_channel_with_undo,
    record_admin_action,
)
from beehive.db.channels import (
    channel_impact_counts,
    create_channel,
    duplicate_channel,
    get_channel,
    update_channel,
)
from beehive.db.email_groups import (
    get_channel_group,
)
from beehive.db.sources import (
    list_by_channel as list_sources,
    source_impact_counts,
)
from beehive.domain.channels import ChannelKind
from beehive.email_routing import (
    EmailConfigurationError,
    resolve_channel_email,
    validate_email,
)
from beehive.localization import (
    Localizer,
)
from beehive.scheduling import (
    DEFAULT_CHANNEL_FETCH_TIME,
    DEFAULT_SCHEDULE_TIMEZONE,
    ScheduleMode,
)
from beehive.web.deps import (
    get_db,
    get_localizer,
    require_admin_session,
    verify_csrf,
)
from beehive.web.formatting import (
    host_local_time_label,
    relative_time,
)
from beehive.web.admin.common import (
    _SCHEDULE_TIMEZONES,
    _URL_SOURCE_TYPES,
    _admin_source_label,
    _channel_fetch_schedule_label,
    _channel_kind_label,
    _channel_next_fetch_label,
    _channel_next_fetch_short,
    _channel_schedule_short,
    _email_error_message,
    _fetch_error_kind_label,
    _fetch_interval_label,
    _render_admin,
    _resolve_default_for_admin,
    _safe_return_path,
    _source_confirmation_value,
    _source_state,
    _source_summary,
)
from beehive.web.admin.home import (
    _render_admin_home_page,
)

router = APIRouter()


# Interval mode is for frequent polling only; a once-a-day Channel belongs in calendar mode, where
# it gets a real wall-clock time instead of "24 hours after the last success".
_FETCH_INTERVAL_CHOICES = (3, 6)


def _fetch_interval_options(selected_hours: int, t: Localizer) -> tuple[dict, ...]:
    """The interval-mode dropdown: the frequent cadences only. "Once a day" now belongs to
    calendar mode (a real wall-clock time), but a Channel saved before that existed may still hold
    24 -- or any other value -- so its stored cadence is appended rather than silently rewritten to
    something the Owner never chose."""
    hours = list(_FETCH_INTERVAL_CHOICES)
    if selected_hours not in hours:
        hours.append(selected_hours)
    return tuple(
        {
            "value": value,
            "label": _fetch_interval_label(value, t),
            "selected": value == selected_hours,
        }
        for value in sorted(hours)
    )


def _schedule_timezone_options(selected: str) -> tuple[str, ...]:
    """The curated timezone dropdown, plus the selected zone when it is not one of them. Any valid
    IANA zone can be stored, so an edit round-trip must not silently drop an Owner's choice just
    because the shortlist does not name it. An unusable zone is never offered: on a rejected save
    the Owner picks again from the shortlist."""
    if selected in _SCHEDULE_TIMEZONES:
        return _SCHEDULE_TIMEZONES
    try:
        ZoneInfo(selected)
    except (ZoneInfoNotFoundError, ValueError):
        return _SCHEDULE_TIMEZONES
    return (*_SCHEDULE_TIMEZONES, selected)


def _normalize_channel_fetch_schedule(
    *,
    fetch_schedule_mode: str,
    fetch_schedule_timezone: str,
    fetch_schedule_time: str,
) -> tuple[str, str, str]:
    """Validate a submitted Channel fetch schedule, returning the values to store. Raises
    ValueError carrying a translation key, exactly like _normalize_email_schedule."""
    if fetch_schedule_mode not in {mode.value for mode in ScheduleMode}:
        raise ValueError("web.admin.channel_schedule.error_mode")
    timezone_name = fetch_schedule_timezone.strip() or DEFAULT_SCHEDULE_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("web.admin.channel_schedule.error_timezone") from exc
    try:
        parsed_time = datetime.strptime(
            fetch_schedule_time.strip() or DEFAULT_CHANNEL_FETCH_TIME, "%H:%M"
        )
    except ValueError as exc:
        raise ValueError("web.admin.channel_schedule.error_time") from exc
    return fetch_schedule_mode, timezone_name, parsed_time.strftime("%H:%M")


def _split_source_label(source: dict, label: str) -> tuple[str, str]:
    """A collection URL label split into its bold host and quieter path; other labels stay whole."""
    if source["type"] in _URL_SOURCE_TYPES and "/" in label:
        host, _, path = label.partition("/")
        return host, f"/{path}"
    return label, ""


@router.post("/channels/{channel_id}/trigger-fetch")
def trigger_channel_fetch(
    channel_id: int,
    request: Request,
    csrf_token: str = Form(...),
    return_url: str | None = Form(None),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    data_dir = os.path.dirname(request.app.state.db_path)
    request_channel_fetch(data_dir, channel_id)
    record_admin_action(
        conn,
        action_type="channel_fetch_requested",
        target_type="channel",
        target_id=channel_id,
        target_label=channel["name"],
    )
    if return_url:
        target = _safe_return_path(return_url, f"/admin/channels/{channel_id}/edit")
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{separator}fetch_requested=1",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/?tab=channels&triggered={channel_id}",
        status_code=303,
    )


@router.post("/channels/trigger-fetch", response_class=HTMLResponse)
def trigger_channel_fetch_batch(
    request: Request,
    channel_ids: list[int] | None = Form(None),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    selected_ids = list(dict.fromkeys(channel_ids or []))
    if not selected_ids:
        return _render_admin_home_page(
            request,
            conn,
            session,
            t,
            active_tab="channels",
            bulk_error=t.text("web.admin.settings.bulk_fetch_empty"),
            status_code=400,
        )
    for channel_id in selected_ids:
        if get_channel(conn, channel_id) is None:
            raise HTTPException(status_code=404, detail="Channel not found")
    data_dir = os.path.dirname(request.app.state.db_path)
    request_channel_fetch_batch(data_dir, selected_ids)
    record_admin_action(
        conn,
        action_type="batch_fetch_requested",
        target_type="channel",
        target_id=None,
        target_label=str(len(selected_ids)),
        detail={"channels": len(selected_ids)},
    )
    return RedirectResponse(
        f"/admin/?tab=channels&triggered_count={len(selected_ids)}",
        status_code=303,
    )


def _channel_kind_options(t: Localizer) -> tuple[dict, ...]:
    """The New Channel form's kind radios, generated from the ChannelDefinition registry (not a
    hardcoded list) so a newly declared kind appears automatically. input_id matches the
    CSS/hint toggling convention (`kind-<value>` / `.kind-only-<value>`) in admin.css."""
    return tuple(
        {
            "value": definition.kind.value,
            "input_id": f"kind-{definition.kind.value}",
            "label": t.text(f"web.admin.channel_new.kind_{definition.kind.value}_label"),
            "hint": t.text(f"web.admin.channel_new.kind_{definition.kind.value}_hint"),
        }
        for definition in all_definitions()
    )


def _channel_kind_display(kind: ChannelKind, t: Localizer) -> dict:
    """The static kind label and per-field hints shown on the Edit Channel page, resolved for the
    Channel's (immutable) kind so the page renders the correct copy for editorial, monitor, and
    tracker alike. editorial keeps its distinct edit-page profile hint; the other kinds share the
    New Channel form's per-kind hint keys."""
    if kind is ChannelKind.EDITORIAL:
        profile_hint = t.text("web.admin.channel_edit.profile_hint")
        highlight_hint = t.text("web.admin.channel_new.highlight_count_hint")
        minimum_hint = t.text("web.admin.channel_new.minimum_score_hint")
    else:
        profile_hint = t.text(f"web.admin.channel_new.profile_hint_{kind.value}")
        highlight_hint = t.text(f"web.admin.channel_new.highlight_count_hint_{kind.value}")
        minimum_hint = t.text(f"web.admin.channel_new.minimum_score_hint_{kind.value}")
    return {
        "label": _channel_kind_label(kind, t),
        "hint": t.text(f"web.admin.channel_new.kind_{kind.value}_hint"),
        "profile_hint": profile_hint,
        "highlight_count_hint": highlight_hint,
        "minimum_score_hint": minimum_hint,
    }


def _render_new_channel_page(
    request: Request,
    session: dict,
    t: Localizer,
    *,
    name: str = "",
    profile: str = "",
    fetch_interval_hours: int = 3,
    highlight_count: int = 8,
    minimum_score: int = 0,
    kind: str = ChannelKind.EDITORIAL.value,
    fetch_schedule_mode: str = ScheduleMode.INTERVAL.value,
    fetch_schedule_timezone: str = DEFAULT_SCHEDULE_TIMEZONE,
    fetch_schedule_time: str = DEFAULT_CHANNEL_FETCH_TIME,
    schedule_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render_admin(
        request,
        t,
        "admin_new_channel.html",
        {
            "csrf_token": session["csrf_token"],
            "kind_options": _channel_kind_options(t),
            "selected_kind": kind,
            "name": name,
            "profile": profile,
            "highlight_count": highlight_count,
            "minimum_score": minimum_score,
            "fetch_schedule_mode": fetch_schedule_mode,
            "fetch_schedule_timezone": fetch_schedule_timezone,
            "fetch_schedule_time": fetch_schedule_time,
            "fetch_interval_options": _fetch_interval_options(fetch_interval_hours, t),
            "schedule_timezones": _schedule_timezone_options(fetch_schedule_timezone),
            "schedule_error": schedule_error,
        },
        status_code=status_code,
    )


@router.get("/channels/new", response_class=HTMLResponse)
def new_channel_form(
    request: Request,
    session: dict = Depends(require_admin_session),
    t: Localizer = Depends(get_localizer),
):
    return _render_new_channel_page(request, session, t)


@router.post("/channels/new")
def new_channel_submit(
    request: Request,
    name: str = Form(...),
    profile: str = Form(...),
    fetch_interval_hours: int = Form(..., ge=1),
    highlight_count: int = Form(8, ge=1, le=50),
    minimum_score: int = Form(0, ge=0, le=100),
    kind: Literal["editorial", "monitor", "tracker"] = Form("editorial"),
    fetch_schedule_mode: str = Form(ScheduleMode.INTERVAL.value),
    fetch_schedule_timezone: str = Form(DEFAULT_SCHEDULE_TIMEZONE),
    fetch_schedule_time: str = Form(DEFAULT_CHANNEL_FETCH_TIME),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    try:
        normalized_schedule = _normalize_channel_fetch_schedule(
            fetch_schedule_mode=fetch_schedule_mode,
            fetch_schedule_timezone=fetch_schedule_timezone,
            fetch_schedule_time=fetch_schedule_time,
        )
    except ValueError as exc:
        return _render_new_channel_page(
            request,
            session,
            t,
            name=name,
            profile=profile,
            fetch_interval_hours=fetch_interval_hours,
            highlight_count=highlight_count,
            minimum_score=minimum_score,
            kind=kind,
            fetch_schedule_mode=fetch_schedule_mode,
            fetch_schedule_timezone=fetch_schedule_timezone,
            fetch_schedule_time=fetch_schedule_time,
            schedule_error=t.text(str(exc)),
            status_code=400,
        )
    channel_id = create_channel(
        conn,
        name,
        profile,
        fetch_interval_hours=fetch_interval_hours,
        highlight_count=highlight_count,
        minimum_score=minimum_score,
        kind=kind,
        fetch_schedule_mode=normalized_schedule[0],
        fetch_schedule_timezone=normalized_schedule[1],
        fetch_schedule_time=normalized_schedule[2],
    )
    record_admin_action(
        conn,
        action_type="channel_created",
        target_type="channel",
        target_id=channel_id,
        target_label=name,
    )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?created=1",
        status_code=303,
    )


def _admin_source_copy_value(source: dict, label: str) -> str:
    """Full, untruncated value for the "copy" button -- unlike `_admin_source_label`, this
    keeps any query string/fragment (e.g. Shopify vendor filters) so it can be pasted straight
    back into a new source. Falls back to the display label for source types that have nothing
    to truncate in the first place."""
    if source["type"] in {"shopify_collection", "land_sea_collection"}:
        config = json.loads(source["config"])
        return config.get("collection_url") or label
    return label


def _source_observability(source: dict, t: Localizer) -> dict:
    """The per-Source operational read-out shown in the Channel editor: whether it is paused, its
    most recent attempt (regardless of outcome), its most recent SUCCESSFUL fetch, that fetch's
    raw/new counts, and its current error. Each timestamp carries a relative label plus an exact
    host-local tooltip, mirroring the freshness helpers used elsewhere."""
    last_attempt = source["last_attempt_at"] or source["last_fetch_at"]
    last_fetch = source["last_fetch_at"]
    return {
        "paused": bool(source["paused_at"]),
        "status": (
            source["last_fetch_status"]
            or ("error" if source["last_fetch_error"] else "ok" if last_fetch else None)
        ),
        "last_attempt_relative": relative_time(last_attempt, t) if last_attempt else None,
        "last_attempt_exact": host_local_time_label(last_attempt) if last_attempt else "",
        "last_fetch_relative": relative_time(last_fetch, t) if last_fetch else None,
        "last_fetch_exact": host_local_time_label(last_fetch) if last_fetch else "",
        "raw_count": source["last_fetch_raw_count"],
        "new_count": source["last_fetch_new_count"],
        "error": source["last_fetch_error"],
    }


def _channel_has_stale_fetch(request: Request, channel_id: int) -> bool:
    """Whether this Channel currently owns a STALE manual-fetch marker (a worker that took the
    inflight marker but never cleared it). Reused verbatim from list_manual_trigger_states, so the
    recovery control renders only when there is genuinely something stuck to clear."""
    data_dir = os.path.dirname(request.app.state.db_path)
    return list_manual_trigger_states(data_dir).get(channel_id) == "stale"


def _channel_setup_progress(
    conn: sqlite3.Connection,
    channel_id: int,
) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT sources.id) AS source_count,
            COUNT(DISTINCT CASE
                WHEN COALESCE(sources.last_attempt_at, sources.last_fetch_at) IS NOT NULL
                THEN sources.id
            END) AS attempted_source_count,
            COUNT(DISTINCT items.id) AS item_count,
            COUNT(DISTINCT CASE
                WHEN items.ai_score IS NOT NULL THEN items.id
            END) AS ranked_item_count
        FROM channels
        LEFT JOIN sources ON sources.channel_id = channels.id
        LEFT JOIN items ON items.source_id = sources.id
        WHERE channels.id = ?
        """,
        (channel_id,),
    ).fetchone()
    progress = {key: int(row[key]) for key in row.keys()}
    progress["complete"] = bool(
        progress["source_count"]
        and progress["attempted_source_count"]
        and progress["item_count"]
        and progress["ranked_item_count"]
    )
    return progress


def _render_edit_channel_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    channel: dict,
    t: Localizer,
    *,
    effective_channel: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
    cleared_count: int | None = None,
    recovered: bool = False,
    saved: bool = False,
    created: bool = False,
    source_saved: bool = False,
    source_removed: bool = False,
    undo_action_id: int | None = None,
    undone: bool = False,
    fetch_requested: bool = False,
    schedule_error: str | None = None,
) -> HTMLResponse:
    sources = []
    source_rows = list_sources(conn, channel["id"])
    # Mutable-snapshot Channels treat a Source's first successful fetch as a baseline that sends
    # no alerts (collector.run_cycle). A fetch where every listing was new is that baseline.
    mutable = require_channel_kind(channel["kind"]) is not ChannelKind.EDITORIAL
    for source in source_rows:
        label = _admin_source_label(source, t)
        name = (source["name"] or "").strip()
        host, path = _split_source_label(source, label)
        state = _source_state(source)
        sources.append(
            {
                "id": source["id"],
                "label": label,
                "name": name,
                "host": host,
                "path": path,
                "type_label": t.text(f"web.source_type.{source['type']}"),
                "state": state,
                "state_label": t.text(f"web.admin.source_state.{state}"),
                "error_kind_label": (
                    _fetch_error_kind_label(source, t) if source["last_fetch_error"] else None
                ),
                "consecutive_failures": int(source["consecutive_failures"] or 0),
                "baseline": bool(
                    mutable
                    and source["last_fetch_raw_count"]
                    and source["last_fetch_new_count"] == source["last_fetch_raw_count"]
                ),
                "confirmation_value": _source_confirmation_value(source, t),
                "copy_value": _admin_source_copy_value(source, label),
                "impact": source_impact_counts(conn, source["id"]),
                **_source_observability(source, t),
            }
        )
    source_state, source_summary = _source_summary(source_rows, t)
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    # The effective hint reflects what a *saved* value would resolve to, so a rejected
    # override (kept only in the display `channel` for the field) must not poison it --
    # resolve it from the unmodified existing row instead.
    source_channel = effective_channel if effective_channel is not None else channel
    try:
        effective = resolve_channel_email(source_channel, default_recipient).address
    except EmailConfigurationError:
        effective = None
    # Same rule as the effective recipient above: the next-run preview describes the *saved*
    # schedule, so a rejected submission (kept only in the display `channel`) cannot show a
    # countdown for a schedule that was never stored.
    next_fetch_label = _channel_next_fetch_label(
        source_channel, source_rows, datetime.now(timezone.utc)
    )
    return _render_admin(
        request,
        t,
        "admin_edit_channel.html",
        {
            "channel": channel,
            "kind_display": _channel_kind_display(require_channel_kind(channel["kind"]), t),
            "sources": sources,
            "source_state": source_state,
            "source_summary": source_summary,
            "csrf_token": session["csrf_token"],
            "effective_email": effective,
            "error": error,
            "default_error": default_error,
            "cleared_count": cleared_count,
            "recovered": recovered,
            "saved": saved,
            "created": created,
            "source_saved": source_saved,
            "source_removed": source_removed,
            "undo_action_id": undo_action_id,
            "undone": undone,
            "fetch_requested": fetch_requested,
            "schedule_error": schedule_error,
            "fetch_interval_options": _fetch_interval_options(
                channel["fetch_interval_hours"], t
            ),
            "schedule_timezones": _schedule_timezone_options(
                channel["fetch_schedule_timezone"]
            ),
            "schedule_label": _channel_fetch_schedule_label(source_channel, t),
            "schedule_short": _channel_schedule_short(source_channel, t),
            "next_fetch_label": next_fetch_label,
            "next_fetch_short": _channel_next_fetch_short(
                source_channel, source_rows, datetime.now(timezone.utc), t
            ),
            "unsaved_submission": status_code >= 400,
            "stale_recovery": _channel_has_stale_fetch(request, channel["id"]),
            "current_group": get_channel_group(conn, channel["id"]),
            "impact": channel_impact_counts(conn, channel["id"]),
            "setup": _channel_setup_progress(conn, channel["id"]),
        },
        status_code=status_code,
    )


@router.get("/channels/{channel_id}/edit", response_class=HTMLResponse)
def edit_channel_form(
    channel_id: int,
    request: Request,
    cleared: int | None = None,
    recovered: int | None = None,
    saved: int | None = None,
    created: int | None = None,
    source_saved: int | None = None,
    source_removed: int | None = None,
    undo_action: int | None = None,
    undone: int | None = None,
    fetch_requested: int | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _render_edit_channel_page(
        request, conn, session, channel, t, cleared_count=cleared,
        recovered=bool(recovered),
        saved=saved == 1,
        created=created == 1,
        source_saved=source_saved == 1,
        source_removed=source_removed == 1,
        undo_action_id=undo_action,
        undone=undone == 1,
        fetch_requested=fetch_requested == 1,
    )


@router.post("/channels/{channel_id}/edit")
def edit_channel_submit(
    channel_id: int,
    request: Request,
    name: str = Form(...),
    profile: str = Form(...),
    fetch_interval_hours: int = Form(..., ge=1),
    highlight_count: int | None = Form(None, ge=1, le=50),
    minimum_score: int | None = Form(None, ge=0, le=100),
    digest_email: str = Form(""),
    fetch_schedule_mode: str = Form(ScheduleMode.INTERVAL.value),
    fetch_schedule_timezone: str = Form(DEFAULT_SCHEDULE_TIMEZONE),
    fetch_schedule_time: str = Form(DEFAULT_CHANNEL_FETCH_TIME),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    existing = get_channel(conn, channel_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    def _submitted_channel() -> dict:
        """The row as the Owner just submitted it, for re-rendering a rejected save without
        losing their edits. Only ever used for display -- never written."""
        return {
            **existing,
            "name": name,
            "profile": profile,
            "fetch_interval_hours": fetch_interval_hours,
            "highlight_count": (
                existing["highlight_count"]
                if highlight_count is None
                else highlight_count
            ),
            "minimum_score": (
                existing["minimum_score"] if minimum_score is None else minimum_score
            ),
            "digest_email": digest_email,
            "fetch_schedule_mode": fetch_schedule_mode,
            "fetch_schedule_timezone": fetch_schedule_timezone,
            "fetch_schedule_time": fetch_schedule_time,
        }

    submitted_email = digest_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        return _render_edit_channel_page(
            request,
            conn,
            session,
            _submitted_channel(),
            t,
            effective_channel=existing,
            error=_email_error_message(exc, t),
            status_code=400,
        )
    try:
        normalized_schedule = _normalize_channel_fetch_schedule(
            fetch_schedule_mode=fetch_schedule_mode,
            fetch_schedule_timezone=fetch_schedule_timezone,
            fetch_schedule_time=fetch_schedule_time,
        )
    except ValueError as exc:
        return _render_edit_channel_page(
            request,
            conn,
            session,
            _submitted_channel(),
            t,
            effective_channel=existing,
            schedule_error=t.text(str(exc)),
            status_code=400,
        )
    update_channel(
        conn,
        channel_id,
        name,
        profile,
        fetch_interval_hours,
        normalized_email,
        highlight_count=highlight_count,
        minimum_score=minimum_score,
        fetch_schedule_mode=normalized_schedule[0],
        fetch_schedule_timezone=normalized_schedule[1],
        fetch_schedule_time=normalized_schedule[2],
    )
    record_admin_action(
        conn,
        action_type="channel_updated",
        target_type="channel",
        target_id=channel_id,
        target_label=name,
    )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?saved=1",
        status_code=303,
    )


@router.post("/channels/{channel_id}/clear-data")
def clear_channel_data_submit(
    channel_id: int,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if confirmation.strip() != channel["name"]:
        raise HTTPException(status_code=409, detail="Channel confirmation did not match")
    action_id, cleared_count = clear_channel_with_undo(
        conn,
        channel_id,
        target_label=channel["name"],
    )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?cleared={cleared_count}"
        f"&undo_action={action_id}",
        status_code=303,
    )


@router.post("/channels/{channel_id}/recover-stale-fetch")
def recover_stale_fetch_submit(
    channel_id: int,
    request: Request,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Owner recovery for a manual fetch whose worker took the inflight marker but never cleared
    it (a crash/kill). Clears ONLY a stale marker, reusing the marker helpers -- a genuinely
    running fetch is left untouched -- and never rewrites the marker protocol."""
    verify_csrf(session, csrf_token)
    if get_channel(conn, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    data_dir = os.path.dirname(request.app.state.db_path)
    recovered = clear_stale_manual_triggers(data_dir)
    if recovered:
        channel = get_channel(conn, channel_id)
        record_admin_action(
            conn,
            action_type="stale_fetch_recovered",
            target_type="channel",
            target_id=channel_id,
            target_label=channel["name"] if channel else str(channel_id),
        )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?recovered={1 if recovered else 0}",
        status_code=303,
    )


@router.post("/channels/{channel_id}/duplicate")
def duplicate_channel_submit(
    channel_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    if get_channel(conn, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    new_channel_id = duplicate_channel(conn, channel_id)
    new_channel = get_channel(conn, new_channel_id)
    record_admin_action(
        conn,
        action_type="channel_duplicated",
        target_type="channel",
        target_id=new_channel_id,
        target_label=new_channel["name"] if new_channel else str(new_channel_id),
        detail={"source_channel_id": channel_id},
    )
    return RedirectResponse(
        f"/admin/channels/{new_channel_id}/edit?created=1",
        status_code=303,
    )


@router.post("/channels/{channel_id}/delete")
def delete_channel_submit(
    channel_id: int,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if confirmation.strip() != channel["name"]:
        raise HTTPException(status_code=409, detail="Channel confirmation did not match")
    action_id = delete_channel_with_undo(
        conn,
        channel_id,
        target_label=channel["name"],
    )
    return RedirectResponse(
        f"/admin/?tab=system&action={action_id}",
        status_code=303,
    )
