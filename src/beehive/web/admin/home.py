"""The admin home: its four chapters (Channels, Email groups, Settings, System), the global
settings forms and Undo."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from beehive.ai.model_catalog import (
    BUILTIN_MODELS,
    RefreshState,
    RefreshStatus,
    load_catalog,
    load_refresh_state,
    request_refresh,
)
from beehive.ai.model_selection import (
    UnsupportedModelError,
    choose_model,
    save_model,
)
from beehive.channels import require_channel_kind
from beehive.collector.manual_trigger import (
    list_manual_trigger_states,
)
from beehive.db.admin_actions import (
    list_admin_actions,
    record_admin_action,
    undo_admin_action,
)
from beehive.db.channels import (
    get_channel,
    list_channels,
)
from beehive.db.email_groups import (
    get_channel_group,
    list_email_groups,
    list_member_channels,
)
from beehive.db.sources import (
    get_source,
    list_by_channel as list_sources,
)
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    get_stored_default_email,
    resolve_group_email,
    set_stored_default_email,
    validate_email,
)
from beehive.featured import (
    InvalidFeaturedWindowError,
    load_featured_window_days,
    save_featured_window_days,
)
from beehive.localization import (
    SUPPORTED_LANGUAGES,
    Localizer,
    UnsupportedLanguageError,
    save_language,
)
from beehive.scheduling import (
    next_email_group_due_at,
)
from beehive.web.deps import (
    get_db,
    get_localizer,
    require_admin_session,
    verify_csrf,
)
from beehive.web.formatting import (
    fetch_stats_label,
    format_count,
    freshness_exact_time,
    freshness_label,
    host_local_time_label,
    relative_time,
    short_time_label,
)
from beehive.source_labels import source_display_name
from beehive.web.admin.common import (
    _ADMIN_CHAPTERS,
    _CLEAR_DEFAULT_WITHOUT_ENV_ERROR,
    _build_system_health_rows,
    _channel_attention_items,
    _channel_fetch_schedule_label,
    _channel_kind_label,
    _channel_next_fetch_label,
    _channel_next_fetch_short,
    _channel_schedule_short,
    _email_error_message,
    _email_group_schedule_label,
    _render_admin,
    _resolve_default_for_admin,
    _safe_return_path,
    _source_summary,
)

router = APIRouter()


_ADMIN_TAB_ALIASES = {"ai": "settings", "delivery": "groups"}


def _admin_chapter(tab: str | None) -> str:
    chapter = _ADMIN_TAB_ALIASES.get(tab or "", tab or "")
    return chapter if chapter in _ADMIN_CHAPTERS else "channels"


def _build_admin_channel_rows(
    conn: sqlite3.Connection,
    t: Localizer,
    data_dir: str,
) -> list[dict]:
    manual_states = list_manual_trigger_states(data_dir)
    now = datetime.now(timezone.utc)
    channels = []
    for channel in list_channels(conn):
        kind = require_channel_kind(channel["kind"])
        sources = list_sources(conn, channel["id"])
        manual_state = manual_states.get(channel["id"])
        if manual_state == "running":
            fetch_status_kind = "running"
            fetch_status_label = t.text("web.admin.settings.fetch_running")
        elif manual_state == "queued":
            fetch_status_kind = "queued"
            fetch_status_label = t.text("web.admin.settings.fetch_queued")
        elif manual_state == "stale":
            fetch_status_kind = "stale"
            fetch_status_label = t.text("web.admin.settings.fetch_stale")
        else:
            fetch_status_kind = None
            fetch_status_label = None
        source_state, source_summary = _source_summary(sources, t)
        fetch_times = [source["last_fetch_at"] for source in sources if source["last_fetch_at"]]
        raw_counts = [
            source["last_fetch_raw_count"]
            for source in sources
            if source["last_fetch_raw_count"] is not None
        ]
        group = get_channel_group(conn, channel["id"])
        channels.append(
            {
                "id": channel["id"],
                "name": channel["name"],
                "kind": kind.value,
                "kind_label": _channel_kind_label(kind, t),
                "source_count": len(sources),
                "source_state": source_state,
                "source_summary": source_summary,
                "fetch_interval_label": _channel_fetch_schedule_label(channel, t),
                "schedule_short": _channel_schedule_short(channel, t),
                "next_fetch_label": _channel_next_fetch_label(channel, sources, now),
                "next_fetch_short": _channel_next_fetch_short(channel, sources, now, t),
                "freshness_label": freshness_label(sources, t),
                "freshness_exact_label": freshness_exact_time(sources),
                "last_fetch_relative": relative_time(max(fetch_times), t) if fetch_times else None,
                "fetch_counts_label": (
                    t.text(
                        "web.admin.channels.fetch_counts",
                        raw=format_count(sum(raw_counts), t.code),
                        new=format_count(
                            sum(
                                source["last_fetch_new_count"] or 0
                                for source in sources
                                if source["last_fetch_raw_count"] is not None
                            ),
                            t.code,
                        ),
                    )
                    if raw_counts
                    else None
                ),
                "fetch_stats_label": fetch_stats_label(sources, t),
                "fetch_status_kind": fetch_status_kind,
                "fetch_status_label": fetch_status_label,
                "group_id": group["id"] if group else None,
                "group_name": group["name"] if group else None,
            }
        )
    return channels


def _build_admin_email_group_rows(
    conn: sqlite3.Connection,
    default_recipient: ResolvedRecipient,
    t: Localizer,
) -> list[dict]:
    groups = []
    now = datetime.now(timezone.utc)
    for group in list_email_groups(conn):
        try:
            recipient = resolve_group_email(group, default_recipient).address
        except EmailConfigurationError:
            recipient = None
        groups.append(
            {
                "id": group["id"],
                "name": group["name"],
                "subject_template": group["subject_template"],
                "frequency_label": _email_group_schedule_label(group, t),
                "schedule_short": _email_group_schedule_label(group, t, short=True),
                "member_count": len(list_member_channels(conn, group["id"])),
                "effective_email": recipient,
                "uses_default": not group["recipient_email"],
                "needs_recipient": recipient is None,
                "last_checked_label": (
                    host_local_time_label(group["last_checked_at"])
                    if group["last_checked_at"]
                    else None
                ),
                "last_sent_label": (
                    host_local_time_label(group["last_sent_at"])
                    if group["last_sent_at"]
                    else None
                ),
                "last_sent_short": (
                    short_time_label(group["last_sent_at"], now, t) if group["last_sent_at"] else None
                ),
                "last_error": group["last_error"],
                "last_error_label": (
                    host_local_time_label(group["last_error_at"])
                    if group["last_error_at"]
                    else None
                ),
                "next_due_label": host_local_time_label(
                    next_email_group_due_at(group, now).isoformat()
                ),
                "next_due_short": short_time_label(next_email_group_due_at(group, now), now, t),
            }
        )
    return groups


def _action_target(conn: sqlite3.Connection, action: dict, t: Localizer) -> tuple[str, str | None]:
    """(label, context) for an audit row's target. A Source that still exists is shown by its
    current name, with its Channel as context: older rows stored the connector type (for example
    "shopify_collection") as the label, which identified nothing."""
    label = action["target_label"]
    if action["target_type"] != "source" or action["target_id"] is None:
        return label, None
    source = get_source(conn, action["target_id"])
    if source is None:
        return label, None
    channel = get_channel(conn, source["channel_id"])
    return source_display_name(source, t), channel["name"] if channel else None


def _build_admin_action_rows(
    conn: sqlite3.Connection,
    t: Localizer,
) -> list[dict]:
    rows = []
    for action in list_admin_actions(conn):
        detail = action["detail"]
        content_keys = {"sources", "items", "votes", "deep_reads", "watches", "events"}
        if action["action_type"] == "source_tested" and detail.get("success"):
            impact = t.text(
                "web.admin.source_test.success",
                count=detail.get("items", 0),
                duration=detail.get("duration_ms", 0),
            )
        elif not detail or not (content_keys & detail.keys() or "channels" in detail):
            impact = None
        elif action["target_type"] == "email_group" or (
            "channels" in detail and not content_keys & detail.keys()
        ):
            impact = t.text(
                "web.admin.activity.group_impact",
                channels=detail.get("channels", 0),
            )
        else:
            impact = t.text(
                "web.admin.activity.content_impact",
                sources=detail.get("sources", 0),
                items=detail.get("items", 0),
                votes=detail.get("votes", 0),
                deep_reads=detail.get("deep_reads", 0),
                watches=detail.get("watches", 0),
                events=detail.get("events", 0),
            )
        target_label, target_context = _action_target(conn, action, t)
        rows.append(
            {
                **action,
                "action_label": t.text(
                    f"web.admin.activity.{action['action_type']}",
                    target=target_label,
                ),
                "target_context": target_context,
                "impact_label": impact,
                "created_label": host_local_time_label(action["created_at"]),
            }
        )
    return rows


def _model_refresh_phase(state: RefreshState | None, now: datetime) -> str:
    """Where the latest model-list refresh stands: idle, pending, waiting (queued, but the Research
    worker has not picked it up), done or failed. The settings page polls it and reloads when it
    changes."""
    if state is None:
        return "idle"
    if state.status is RefreshStatus.QUEUED:
        return "waiting" if state.is_waiting(now) else "pending"
    if state.status is RefreshStatus.RUNNING:
        return "failed" if state.is_stale(now) else "pending"
    return "done" if state.status is RefreshStatus.DONE else "failed"


def _model_list_view(conn: sqlite3.Connection, t: Localizer, now: datetime) -> dict:
    catalog = load_catalog(conn)
    models = catalog.models if catalog is not None else BUILTIN_MODELS
    choice = choose_model(conn, models)
    state = load_refresh_state(conn)
    phase = _model_refresh_phase(state, now)
    view = {
        "models": models,
        "current_model": choice.model_id,
        "from_copilot": catalog is not None,
        "count": len(models),
        "refreshed": short_time_label(catalog.refreshed_at, now, t) if catalog else None,
        "refreshed_exact": (
            host_local_time_label(catalog.refreshed_at.isoformat()) if catalog else None
        ),
        "phase": phase,
        "busy": phase in {"pending", "waiting"},
        "unavailable": choice.unavailable,
        "default_name": choice.display_name,
        "requested": None,
        "failed_at": None,
        "error": None,
        "error_detail": None,
        "added": 0,
        "removed": 0,
    }
    if state is None:
        return view
    if phase == "waiting":
        view["requested"] = short_time_label(state.requested_at, now, t)
    elif phase == "failed":
        kind = (
            state.error.value
            if state.error is not None
            else "interrupted" if state.status is RefreshStatus.RUNNING else "failed"
        )
        view["failed_at"] = short_time_label(
            state.finished_at or state.started_at or now, now, t)
        view["error"] = t.text(f"web.admin.model.refresh_error.{kind}")
        view["error_detail"] = state.error_detail
    elif phase == "done":
        view["added"] = len(state.added)
        view["removed"] = len(state.removed)
    return view


def _render_admin_home_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    t: Localizer,
    *,
    submitted_email: str | None = None,
    error: str | None = None,
    saved: bool = False,
    triggered: int | None = None,
    language_saved: bool = False,
    language_error: str | None = None,
    model_saved: bool = False,
    model_error: str | None = None,
    model_refreshed: bool = False,
    featured_saved: bool = False,
    featured_error: str | None = None,
    submitted_featured_window_days: int | None = None,
    active_tab: str = "channels",
    triggered_count: int | None = None,
    bulk_error: str | None = None,
    action_id: int | None = None,
    undone: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    effective, default_error = _resolve_default_for_admin(conn, t)
    stored = get_stored_default_email(conn)
    data_dir = os.path.dirname(request.app.state.db_path)
    chapter = _admin_chapter(active_tab)
    channels = _build_admin_channel_rows(conn, t, data_dir) if chapter == "channels" else []
    group_rows = _build_admin_email_group_rows(conn, effective, t) if chapter == "groups" else []
    ungrouped = (
        [channel for channel in list_channels(conn) if get_channel_group(conn, channel["id"]) is None]
        if chapter == "groups"
        else []
    )
    return _render_admin(
        request,
        t,
        "admin_settings.html",
        {
            "csrf_token": session["csrf_token"],
            "submitted_email": (stored or "")
            if submitted_email is None
            else submitted_email,
            "effective_email": effective.address,
            "effective_source": effective.source,
            "environment_email": os.environ.get("DIGEST_EMAIL_TO"),
            "error": error,
            "default_error": default_error,
            "saved": saved,
            "triggered": triggered,
            "channels": channels,
            "attention_items": (
                _channel_attention_items(conn, t, data_dir) if chapter == "channels" else []
            ),
            "ungrouped_channels": ungrouped,
            "email_groups": group_rows,
            "languages": SUPPORTED_LANGUAGES,
            "current_language": t.code,
            "language_saved": language_saved,
            "language_error": language_error,
            "model_list": (
                _model_list_view(conn, t, datetime.now(timezone.utc))
                if chapter == "settings"
                else None
            ),
            "model_saved": model_saved,
            "model_error": model_error,
            "model_refreshed": model_refreshed,
            "featured_window_days": (
                load_featured_window_days(conn)
                if submitted_featured_window_days is None
                else submitted_featured_window_days
            ),
            "featured_saved": featured_saved,
            "featured_error": featured_error,
            "active_tab": chapter,
            "triggered_count": triggered_count,
            "bulk_error": bulk_error,
            "recent_actions": _build_admin_action_rows(conn, t) if chapter == "system" else [],
            "system_health": (
                _build_system_health_rows(conn, t, data_dir, effective) if chapter == "system" else []
            ),
            "action_id": action_id,
            "undone": undone,
        },
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    tab: str = "channels",
    saved: int | None = None,
    triggered: int | None = None,
    triggered_count: int | None = None,
    language_saved: int | None = None,
    model_saved: int | None = None,
    model_refreshed: int | None = None,
    featured_saved: int | None = None,
    action: int | None = None,
    undone: int | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    return _render_admin_home_page(
        request,
        conn,
        session,
        t,
        saved=saved == 1,
        triggered=triggered,
        triggered_count=triggered_count,
        language_saved=language_saved == 1,
        model_saved=model_saved == 1,
        model_refreshed=model_refreshed == 1,
        featured_saved=featured_saved == 1,
        action_id=action,
        undone=undone == 1,
        active_tab=tab,
    )


@router.post("/", response_class=HTMLResponse)
def admin_settings_submit(
    request: Request,
    default_digest_email: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    submitted = default_digest_email.strip()
    try:
        if submitted:
            set_stored_default_email(conn, submitted)
        else:
            environment_email = os.environ.get("DIGEST_EMAIL_TO")
            if not environment_email:
                raise EmailConfigurationError(_CLEAR_DEFAULT_WITHOUT_ENV_ERROR)
            validate_email(environment_email)
            set_stored_default_email(conn, None)
    except EmailConfigurationError as exc:
        return _render_admin_home_page(
            request,
            conn,
            session,
            t,
            submitted_email=default_digest_email,
            error=_email_error_message(exc, t),
            active_tab="groups",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="delivery_settings_updated",
        target_type="settings",
        target_id=None,
        target_label=t.text("web.admin.tabs.delivery"),
    )
    return RedirectResponse("/admin/?tab=groups&saved=1", status_code=303)


@router.post("/actions/{action_id}/undo")
def undo_admin_action_submit(
    action_id: int,
    csrf_token: str = Form(...),
    return_url: str = Form("/admin/?tab=system"),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    try:
        undo_admin_action(conn, action_id)
    except (sqlite3.IntegrityError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target = _safe_return_path(return_url, "/admin/?tab=system")
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}undone=1", status_code=303)


@router.post("/language", response_class=HTMLResponse)
def save_language_submit(
    request: Request,
    language: str = Form(...),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    """A deliberately separate form/route from the digest-email settings form above: saving the
    platform language must never run (or be blocked by) email validation, and vice versa. Existing
    per-Item AI summaries/rationale were generated in whatever language was active when the AI
    ranked them -- switching the platform language only changes the web UI's own copy going
    forward; it never retroactively re-summarizes already-stored content."""
    verify_csrf(session, csrf_token)
    try:
        save_language(conn, language)
    except UnsupportedLanguageError:
        return _render_admin_home_page(
            request,
            conn,
            session,
            t,
            language_error=t.text("web.admin.language.invalid"),
            active_tab="settings",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="language_updated",
        target_type="settings",
        target_id=None,
        target_label=language,
    )
    return RedirectResponse("/admin/?tab=settings&language_saved=1", status_code=303)


@router.post("/model", response_class=HTMLResponse)
def save_model_submit(
    request: Request,
    model: str = Form(...),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    """Save the model independently from language and email settings.

    Collector and deep-read processes load this value when they begin future LLM work. Existing
    generated content remains unchanged.
    """
    verify_csrf(session, csrf_token)
    try:
        save_model(conn, model)
    except UnsupportedModelError:
        return _render_admin_home_page(
            request,
            conn,
            session,
            t,
            model_error=t.text("web.admin.model.invalid"),
            active_tab="settings",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="model_updated",
        target_type="settings",
        target_id=None,
        target_label=model,
    )
    return RedirectResponse("/admin/?tab=settings&model_saved=1", status_code=303)


@router.post("/model/refresh")
def refresh_model_list_submit(
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Queue a model-list refresh. The web container has no Copilot token, so the Research
    worker runs it within a few seconds (ai/model_catalog.py); the settings page polls
    /admin/model/refresh-status and reloads when it is done."""
    verify_csrf(session, csrf_token)
    request_refresh(conn, datetime.now(timezone.utc))
    return RedirectResponse("/admin/?tab=settings#interface-ai", status_code=303)


@router.get("/model/refresh-status")
def model_refresh_status(
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    phase = _model_refresh_phase(load_refresh_state(conn), datetime.now(timezone.utc))
    return JSONResponse({"phase": phase})


@router.post("/featured-window", response_class=HTMLResponse)
def save_featured_window_submit(
    request: Request,
    featured_window_days: int = Form(...),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    try:
        save_featured_window_days(conn, featured_window_days)
    except InvalidFeaturedWindowError:
        return _render_admin_home_page(
            request,
            conn,
            session,
            t,
            featured_error=t.text("web.admin.featured.invalid"),
            submitted_featured_window_days=featured_window_days,
            active_tab="settings",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="featured_window_updated",
        target_type="settings",
        target_id=None,
        target_label=str(featured_window_days),
    )
    return RedirectResponse(
        "/admin/?tab=settings&featured_saved=1",
        status_code=303,
    )
