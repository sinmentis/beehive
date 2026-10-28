"""Channel pages: create, edit, duplicate, clear and delete a Channel, and Fetch now."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from beehive.channels import all_definitions, require_channel_kind
from beehive.db.fetch_requests import (
    clear_stale_fetch_requests,
    fetch_request_states,
    request_fetch,
)
from beehive.db.admin_actions import (
    clear_channel_with_undo,
    delete_channel_with_undo,
    record_admin_action,
)
from beehive.db.channels import (
    advance_channel_setup,
    channel_impact_counts,
    create_channel,
    duplicate_channel,
    finish_channel_setup,
    get_channel,
    update_channel,
)
from beehive.db.connection import write_transaction
from beehive.db.email_groups import (
    assign_channel,
    create_email_group,
    get_channel_group,
    get_email_group,
    list_email_groups,
    list_member_channels,
    unassign_channel,
)
from beehive.db.ranking_requests import (
    RankingRequestError,
    cancel_ranking_request,
    create_ranking_request,
    get_ranking_request,
    list_ranking_requests_for_channel,
    ranking_candidate_count,
)
from beehive.db.sources import (
    list_by_channel as list_sources,
    source_impact_counts,
)
from beehive.domain.channels import ChannelKind
from beehive.digest.send import build_email_group_delivery_status
from beehive.email_routing import (
    EmailConfigurationError,
    resolve_channel_email,
    resolve_group_email,
    validate_email,
)
from beehive.localization import (
    Localizer,
)
from beehive.scheduling import (
    DEFAULT_CHANNEL_FETCH_TIME,
    DEFAULT_SCHEDULE_TIMEZONE,
    ScheduleMode,
    next_email_group_due_at,
)
from beehive.web.deps import (
    get_db,
    get_localizer,
    require_admin_session,
    verify_csrf,
)
from beehive.source_labels import parse_source_config, source_url
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
    _email_group_schedule_label,
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
from beehive.web.admin.email_groups import _normalize_email_schedule

router = APIRouter()
_WORKSPACE_SECTIONS = ("overview", "sources", "rules", "delivery", "settings")


# Interval mode is for frequent polling only; a once-a-day Channel belongs in calendar mode, where
# it gets a real wall-clock time instead of "24 hours after the last success".
_FETCH_INTERVAL_CHOICES = (3, 6)


def _ranking_request_view(operation: dict | None) -> dict | None:
    if operation is None:
        return None
    results = []
    for item in operation.get("result") or []:
        display = dict(item)
        for field in ("previous_score", "score"):
            key = "previous_display_score" if field == "previous_score" else "display_score"
            display[key] = round(item[field]) if item[field] is not None else None
        results.append(display)
    return {**operation, "result": results}


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
    if channel.get("setup_stage"):
        raise HTTPException(status_code=409, detail="Complete channel setup before fetching")
    request_fetch(conn, [channel_id], datetime.now(timezone.utc))
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
    active_ids = []
    for channel_id in selected_ids:
        channel = get_channel(conn, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        if not channel.get("setup_stage"):
            active_ids.append(channel_id)
    if not active_ids:
        return _render_admin_home_page(
            request, conn, session, t, active_tab="channels",
            bulk_error=t.text("web.admin.task.setup_incomplete"), status_code=400,
        )
    selected_ids = active_ids
    request_fetch(conn, selected_ids, datetime.now(timezone.utc))
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
    name_error: str | None = None,
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
            "fetch_interval_hours": fetch_interval_hours,
            "highlight_count": highlight_count,
            "minimum_score": minimum_score,
            "fetch_schedule_mode": fetch_schedule_mode,
            "fetch_schedule_timezone": fetch_schedule_timezone,
            "fetch_schedule_time": fetch_schedule_time,
            "fetch_interval_options": _fetch_interval_options(fetch_interval_hours, t),
            "schedule_timezones": _schedule_timezone_options(fetch_schedule_timezone),
            "schedule_error": schedule_error,
            "name_error": name_error,
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
    profile: str = Form(""),
    fetch_interval_hours: int = Form(..., ge=1),
    highlight_count: int = Form(8, ge=1, le=50),
    minimum_score: int = Form(0, ge=0, le=100),
    kind: Literal["editorial", "monitor", "tracker"] = Form("editorial"),
    fetch_schedule_mode: str = Form(ScheduleMode.INTERVAL.value),
    fetch_schedule_timezone: str = Form(DEFAULT_SCHEDULE_TIMEZONE),
    fetch_schedule_time: str = Form(DEFAULT_CHANNEL_FETCH_TIME),
    setup: bool = Form(False),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    if not setup and not profile:
        raise HTTPException(status_code=422, detail="Profile is required")
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
    try:
        channel_id = create_channel(
            conn, name, profile, fetch_interval_hours=fetch_interval_hours,
            highlight_count=highlight_count, minimum_score=minimum_score, kind=kind,
            fetch_schedule_mode=normalized_schedule[0],
            fetch_schedule_timezone=normalized_schedule[1],
            fetch_schedule_time=normalized_schedule[2],
            setup_stage="sources" if setup else None,
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "channels.name" not in str(exc):
            raise
        return _render_new_channel_page(
            request, session, t, name=name, profile=profile, kind=kind,
            fetch_interval_hours=fetch_interval_hours, highlight_count=highlight_count,
            minimum_score=minimum_score, fetch_schedule_mode=fetch_schedule_mode,
            fetch_schedule_timezone=fetch_schedule_timezone, fetch_schedule_time=fetch_schedule_time,
            name_error=t.text("web.admin.task.channel_name_taken"), status_code=400,
        )
    record_admin_action(
        conn,
        action_type="channel_created",
        target_type="channel",
        target_id=channel_id,
        target_label=name,
    )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?created=1"
        + ("&section=sources" if setup else ""),
        status_code=303,
    )


def _admin_source_copy_value(source: dict, label: str) -> str:
    """Full, untruncated value for the "copy" button -- unlike `_admin_source_label`, this
    keeps any query string/fragment (e.g. Shopify vendor filters) so it can be pasted straight
    back into a new source. Falls back to the display label for source types that have nothing
    to truncate in the first place."""
    if source["type"] in _URL_SOURCE_TYPES:
        return source_url(source["type"], parse_source_config(source["config"])) or label
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


def _channel_has_stale_fetch(conn: sqlite3.Connection, channel_id: int) -> bool:
    """Whether this Channel's "Fetch now" request is held by a worker that stopped, so the
    recovery control shows only when there is something stuck to clear."""
    return fetch_request_states(conn, datetime.now(timezone.utc)).get(channel_id) == "stale"


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
    section: str = "overview",
    workspace_error: str | None = None,
    workspace_error_field: str | None = None,
    ranking_request_id: str | None = None,
    delivery_values: dict | None = None,
    activated: bool = False,
    force_dirty: bool | None = None,
) -> HTMLResponse:
    selected_section = section if section in _WORKSPACE_SECTIONS else "overview"
    if selected_section == "overview" and (error or schedule_error):
        selected_section = "settings"
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
    current_group = get_channel_group(conn, channel["id"])
    groups = list_email_groups(conn)
    now = datetime.now(timezone.utc)
    group_recipient = None
    next_email_label = None
    if current_group:
        try:
            group_recipient = resolve_group_email(current_group, default_recipient).address
        except EmailConfigurationError:
            pass
        due = next_email_group_due_at(current_group, now)
        next_email_label = due.astimezone(
            ZoneInfo(current_group["schedule_timezone"])
        ).strftime("%Y-%m-%d %H:%M %Z")
    requests = (
        list_ranking_requests_for_channel(conn, channel["id"])
        if selected_section == "rules" else []
    )
    ranking_request = None
    if ranking_request_id:
        ranking_request = get_ranking_request(conn, ranking_request_id)
        if ranking_request is None or ranking_request["channel_id"] != channel["id"]:
            raise HTTPException(status_code=404, detail="Rule operation not found")
    elif requests:
        ranking_request = requests[0]
    default_group_name = f"{channel['name']} Daily"
    group_names = {group["name"] for group in groups}
    suffix = 2
    while default_group_name in group_names:
        default_group_name = f"{channel['name']} Daily ({suffix})"
        suffix += 1
    delivery_form = {
        "delivery_mode": "existing" if current_group else "none",
        "group_id": current_group["id"] if current_group else "",
        "group_name": default_group_name,
        "subject_template": f"{channel['name']} | {{date}}",
        "schedule_time": "09:00",
        "schedule_timezone": channel["fetch_schedule_timezone"],
        "recipient_email": "",
    }
    delivery_form.update(delivery_values or {})
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
            "unsaved_submission": status_code >= 400 if force_dirty is None else force_dirty,
            "legacy_dirty_rules": (
                status_code >= 400 and request.url.path.endswith("/edit")
                and effective_channel is not None
                and any(channel[key] != effective_channel[key]
                        for key in ("profile", "minimum_score", "highlight_count"))
            ),
            "stale_recovery": _channel_has_stale_fetch(conn, channel["id"]),
            "current_group": current_group,
            "group_recipient": group_recipient,
            "group_channel_count": len(list_member_channels(conn, current_group["id"])) if current_group else 0,
            "next_email_label": next_email_label,
            "email_groups": [
                {**group, "schedule_label": _email_group_schedule_label(group, t)}
                for group in groups
            ],
            "delivery_form": delivery_form,
            "delivery_timezones": _schedule_timezone_options(delivery_form["schedule_timezone"]),
            "delivery_status": (
                build_email_group_delivery_status(conn, current_group)
                if current_group and selected_section == "delivery" else None
            ),
            "default_recipient": default_recipient.address,
            "section": selected_section,
            "workspace_error": workspace_error,
            "workspace_error_field": workspace_error_field,
            "activated": activated,
            "ranking_request": _ranking_request_view(ranking_request),
            "ranking_requests": requests,
            "ranking_busy": any(row["status"] in ("queued", "running") for row in requests),
            "ranking_candidate_count": (
                ranking_candidate_count(conn, channel["id"]) if selected_section == "rules" else 0
            ),
            "fetch_state": fetch_request_states(conn, now).get(channel["id"]),
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
    section: str = "overview",
    ranking_request: str | None = None,
    activated: int | None = None,
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
        section=section,
        ranking_request_id=ranking_request,
        activated=activated == 1,
    )


def _workspace_channel(conn: sqlite3.Connection, channel_id: int) -> dict:
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


@router.get("/channels/{channel_id}/workspace-status")
def channel_workspace_status(
    channel_id: int,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    _workspace_channel(conn, channel_id)
    progress = _channel_setup_progress(conn, channel_id)
    return JSONResponse({
        "state": fetch_request_states(conn, datetime.now(timezone.utc)).get(channel_id),
        "progress": t.text(
            "web.admin.task.collected", count=progress["item_count"],
            ranked=progress["ranked_item_count"],
        ),
    })


def _workspace_error(t: Localizer, code: str) -> str:
    known = {
        "no_candidates", "rules_changed", "request_in_progress", "confirm_required",
        "setup_incomplete", "source_required", "profile_required", "choose_group",
    }
    return t.text(f"web.admin.task.{code if code in known else 'job_failed'}")


@router.post("/channels/{channel_id}/rules")
def save_channel_rules(
    channel_id: int,
    request: Request,
    profile: str = Form(...),
    highlight_count: int = Form(..., ge=1, le=50),
    minimum_score: int = Form(..., ge=0, le=100),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    channel = _workspace_channel(conn, channel_id)
    update_channel(
        conn, channel_id, channel["name"], profile, channel["fetch_interval_hours"],
        channel["digest_email"], highlight_count=highlight_count, minimum_score=minimum_score,
    )
    if list_sources(conn, channel_id):
        advance_channel_setup(conn, channel_id, "delivery")
    record_admin_action(
        conn, action_type="channel_updated", target_type="channel",
        target_id=channel_id, target_label=channel["name"],
    )
    section = "rules"
    if channel.get("setup_stage"):
        next_stage = get_channel(conn, channel_id)["setup_stage"]
        section = "overview" if next_stage == "review" else next_stage
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?section={section}&saved=1", status_code=303,
    )


@router.post("/channels/{channel_id}/settings")
async def save_channel_settings(
    channel_id: int,
    request: Request,
    name: str | None = Form(None),
    fetch_interval_hours: int | None = Form(None, ge=1),
    fetch_schedule_mode: str | None = Form(None),
    fetch_schedule_timezone: str | None = Form(None),
    fetch_schedule_time: str | None = Form(None),
    digest_email: str | None = Form(None),
    clear_alert_email: bool = Form(False),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    channel = _workspace_channel(conn, channel_id)
    submitted_form = await request.form()
    submitted = {
        **channel,
        "name": name if name is not None else channel["name"],
        "fetch_interval_hours": fetch_interval_hours if fetch_interval_hours is not None else channel["fetch_interval_hours"],
        "fetch_schedule_mode": fetch_schedule_mode if fetch_schedule_mode is not None else channel["fetch_schedule_mode"],
        "fetch_schedule_timezone": fetch_schedule_timezone if fetch_schedule_timezone is not None else channel["fetch_schedule_timezone"],
        "fetch_schedule_time": fetch_schedule_time if fetch_schedule_time is not None else channel["fetch_schedule_time"],
        "digest_email": (
            None if clear_alert_email
            else (digest_email or "") if "digest_email" in submitted_form
            else channel["digest_email"]
        ),
    }
    try:
        normalized = _normalize_channel_fetch_schedule(
            fetch_schedule_mode=submitted["fetch_schedule_mode"],
            fetch_schedule_timezone=submitted["fetch_schedule_timezone"],
            fetch_schedule_time=submitted["fetch_schedule_time"],
        )
    except ValueError as exc:
        return _render_edit_channel_page(
            request, conn, session, submitted, t, effective_channel=channel, section="settings",
            schedule_error=t.text(str(exc)), status_code=400,
        )
    try:
        email = validate_email(submitted["digest_email"]) if submitted["digest_email"] else None
    except EmailConfigurationError as exc:
        return _render_edit_channel_page(
            request, conn, session, submitted, t, effective_channel=channel, section="settings",
            error=_email_error_message(exc, t), status_code=400,
        )
    update_channel(
        conn, channel_id, submitted["name"], channel["profile"],
        submitted["fetch_interval_hours"], email,
        fetch_schedule_mode=normalized[0], fetch_schedule_timezone=normalized[1],
        fetch_schedule_time=normalized[2],
    )
    record_admin_action(
        conn, action_type="channel_updated", target_type="channel",
        target_id=channel_id, target_label=submitted["name"],
    )
    return RedirectResponse(f"/admin/channels/{channel_id}/edit?section=settings&saved=1", 303)


@router.post("/channels/{channel_id}/delivery")
def save_channel_delivery(
    channel_id: int,
    request: Request,
    delivery_mode: Literal["none", "existing", "new"] = Form(...),
    group_id: int | None = Form(None),
    group_name: str = Form(""),
    subject_template: str = Form(""),
    schedule_time: str = Form("09:00"),
    schedule_timezone: str = Form(DEFAULT_SCHEDULE_TIMEZONE),
    recipient_email: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    channel = _workspace_channel(conn, channel_id)
    values = {
        "delivery_mode": delivery_mode, "group_id": group_id or "", "group_name": group_name,
        "subject_template": subject_template, "schedule_time": schedule_time,
        "schedule_timezone": schedule_timezone, "recipient_email": recipient_email,
    }
    problem = None
    problem_field = None
    if delivery_mode == "existing" and (
        group_id is None or get_email_group(conn, group_id) is None
    ):
        problem = t.text("web.admin.task.choose_group")
        problem_field = "delivery-group"
    elif delivery_mode == "existing":
        default, _ = _resolve_default_for_admin(conn, t)
        try:
            if resolve_group_email(get_email_group(conn, group_id), default).address is None:
                problem = t.text("web.admin.task.recipient_required")
        except EmailConfigurationError:
            problem = t.text("web.admin.task.recipient_required")
        if problem:
            problem_field = "delivery-group"
    if delivery_mode == "new":
        try:
            schedule = _normalize_email_schedule(
                schedule_mode="calendar", schedule_timezone=schedule_timezone,
                schedule_time=schedule_time, schedule_weekdays=list(range(7)),
            )
            email = validate_email(recipient_email) if recipient_email.strip() else None
            default, default_error = _resolve_default_for_admin(conn, t)
            if email is None and (default_error or not default.address):
                raise EmailConfigurationError("No email recipient is configured")
        except EmailConfigurationError as exc:
            problem = _email_error_message(exc, t)
            problem_field = "new-group-recipient"
        except ValueError as exc:
            problem = t.text(str(exc))
            problem_field = "new-group-zone" if "timezone" in str(exc) else "new-group-time"
    if problem:
        return _render_edit_channel_page(
            request, conn, session, channel, t, section="delivery",
            workspace_error=problem, workspace_error_field=problem_field,
            delivery_values=values, status_code=400,
        )
    if delivery_mode == "none":
        unassign_channel(conn, channel_id)
    elif delivery_mode == "existing":
        assign_channel(conn, group_id, channel_id)
    else:
        new_name = group_name.strip() or f"{channel['name']} Daily"
        new_subject = subject_template.strip() or f"{channel['name']} | {{date}}"
        with write_transaction(conn):
            current = get_channel_group(conn, channel_id)
            same_arrangement = current and (
                current["name"] == new_name and current["subject_template"] == new_subject
                and current["recipient_email"] == email and current["schedule_mode"] == "calendar"
                and current["schedule_timezone"] == schedule[1]
                and current["schedule_time"] == schedule[2]
                and current["schedule_weekdays"] == schedule[3]
                and len(list_member_channels(conn, current["id"])) == 1
            )
            name_conflict = not same_arrangement and conn.execute(
                "SELECT 1 FROM email_groups WHERE name = ?", (new_name,),
            ).fetchone() is not None
            if same_arrangement:
                group_id = current["id"]
            elif not name_conflict:
                group_id = create_email_group(
                    conn, new_name, new_subject, email, schedule_mode=schedule[0],
                    schedule_timezone=schedule[1], schedule_time=schedule[2],
                    schedule_weekdays=schedule[3],
                )
                assign_channel(conn, group_id, channel_id)
        if name_conflict:
            return _render_edit_channel_page(
                request, conn, session, channel, t, section="delivery",
                workspace_error=t.text("web.admin.task.group_name_taken"),
                workspace_error_field="new-group-name",
                delivery_values=values, status_code=400,
            )
        if not same_arrangement:
            record_admin_action(
                conn, action_type="email_group_created", target_type="email_group",
                target_id=group_id, target_label=new_name, detail={"channel_ids": [channel_id]},
            )
    record_admin_action(
        conn, action_type="channel_updated", target_type="channel",
        target_id=channel_id, target_label=channel["name"],
    )
    if channel["profile"].strip() and list_sources(conn, channel_id):
        advance_channel_setup(conn, channel_id, "review")
    section = "delivery"
    if channel.get("setup_stage"):
        next_stage = get_channel(conn, channel_id)["setup_stage"]
        section = "overview" if next_stage == "review" else next_stage
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?section={section}&saved=1", 303,
    )


@router.post("/channels/{channel_id}/activate")
def activate_channel(
    channel_id: int,
    request: Request,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    channel = _workspace_channel(conn, channel_id)
    try:
        activated = finish_channel_setup(conn, channel_id)
    except ValueError as exc:
        return _render_edit_channel_page(
            request, conn, session, channel, t, section="overview", status_code=400,
            workspace_error=_workspace_error(t, str(exc)),
        )
    if activated:
        request_fetch(conn, [channel_id], datetime.now(timezone.utc))
        record_admin_action(
            conn, action_type="channel_fetch_requested", target_type="channel",
            target_id=channel_id, target_label=channel["name"],
        )
    return RedirectResponse(f"/admin/channels/{channel_id}/edit?activated=1", 303)


@router.post("/channels/{channel_id}/rules/preview")
def preview_channel_rules(
    channel_id: int,
    request: Request,
    profile: str = Form(...),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    channel = _workspace_channel(conn, channel_id)
    try:
        operation = create_ranking_request(conn, channel_id, mode="preview", profile=profile)
    except RankingRequestError as exc:
        return _render_edit_channel_page(
            request, conn, session, {**channel, "profile": profile}, t, effective_channel=channel,
            section="rules", workspace_error=_workspace_error(t, exc.code), status_code=400,
        )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?section=rules&ranking_request={operation['id']}#ranking-result", 303,
    )


@router.post("/channels/{channel_id}/rules/apply")
def apply_channel_rules(
    channel_id: int,
    request: Request,
    confirmation: bool = Form(False),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    channel = _workspace_channel(conn, channel_id)
    if not confirmation:
        return _render_edit_channel_page(
            request, conn, session, channel, t, section="rules", status_code=400,
            workspace_error=t.text("web.admin.task.confirm_required"), force_dirty=False,
        )
    try:
        operation = create_ranking_request(
            conn, channel_id, mode="apply", profile=channel["profile"],
        )
    except RankingRequestError as exc:
        return _render_edit_channel_page(
            request, conn, session, channel, t, section="rules", status_code=400,
            workspace_error=_workspace_error(t, exc.code), force_dirty=False,
        )
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?section=rules&ranking_request={operation['id']}#ranking-result", 303,
    )


def _channel_ranking_request(conn: sqlite3.Connection, channel_id: int, request_id: str) -> dict:
    operation = get_ranking_request(conn, request_id)
    if operation is None or operation["channel_id"] != channel_id:
        raise HTTPException(status_code=404, detail="Rule operation not found")
    return operation


@router.get("/channels/{channel_id}/rules/requests/{request_id}")
def ranking_request_status(
    channel_id: int,
    request_id: str,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    operation = _channel_ranking_request(conn, channel_id, request_id)
    response = request.app.state.templates.TemplateResponse(
        request=request, name="_admin_ranking_result.html",
        context={"ranking_request": _ranking_request_view(operation), "channel": _workspace_channel(conn, channel_id),
                 "csrf_token": session["csrf_token"]},
    )
    return JSONResponse({
        "status": operation["status"], "html": response.body.decode("utf-8"),
        "progress": t.text(
            "web.admin.task.job_progress",
            done=operation["processed_count"] + operation["skipped_count"],
            total=operation["total_count"], skipped=operation["skipped_count"],
        ),
    })


@router.post("/channels/{channel_id}/rules/requests/{request_id}/cancel")
def cancel_channel_ranking(
    channel_id: int,
    request_id: str,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    _channel_ranking_request(conn, channel_id, request_id)
    cancel_ranking_request(conn, request_id)
    return RedirectResponse(
        f"/admin/channels/{channel_id}/edit?section=rules&ranking_request={request_id}#ranking-result", 303,
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
    """Owner recovery for "Fetch now" requests held by a worker that stopped. Clears only
    requests whose lease ran out; a fetch that is really running is left alone."""
    verify_csrf(session, csrf_token)
    if get_channel(conn, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    recovered = clear_stale_fetch_requests(
        conn, datetime.now(timezone.utc), channel_id=channel_id,
    )
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
    channel = get_channel(conn, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.get("setup_stage"):
        raise HTTPException(status_code=409, detail="Complete channel setup before duplicating")
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
