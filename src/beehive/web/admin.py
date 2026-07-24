"""Admin routes (Slice 3): password login/logout, plus Channel/Source CRUD added in later
tasks — all gated by require_admin_session except /admin/login itself (that's how a session gets
created in the first place)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from beehive.ai.model_selection import (
    SUPPORTED_MODELS,
    UnsupportedModelError,
    load_model,
    save_model,
)
from beehive.auth.passwords import verify_password
from beehive.auth.rate_limit import is_locked_out
from beehive.auth.tokens import generate_session_id, sign_session_id
from beehive.channels import all_definitions, require_channel_kind
from beehive.channels.source_policy import connector_supports_kind, source_types_for_kind
from beehive.collector.manual_trigger import (
    clear_stale_manual_triggers,
    list_manual_trigger_states,
    request_channel_fetch,
    request_channel_fetch_batch,
)
from beehive.connectors import (  # noqa: F401 (registers the connectors)
    all_about_auctions,
    google_news,
    hackernews,
    land_sea_collection,
    official_feeds,
    reddit,
    shopify_collection,
)
from beehive.connectors.base import PreviewSourceConnector
from beehive.connectors.registry import get as get_connector
from beehive.db import app_state
from beehive.db.admin_actions import (
    clear_channel_with_undo,
    delete_channel_with_undo,
    delete_email_group_with_undo,
    delete_source_with_undo,
    list_admin_actions,
    record_admin_action,
    undo_admin_action,
)
from beehive.db.admin_login_attempts import get_most_recent_attempt, record_attempt
from beehive.db.channels import (
    channel_impact_counts,
    create_channel,
    duplicate_channel,
    get_channel,
    list_channels,
    update_channel,
)
from beehive.db.email_groups import (
    assign_channel,
    create_email_group,
    get_channel_group,
    get_email_group,
    list_email_groups,
    list_member_channels,
    unassign_channel,
    update_email_group,
)
from beehive.db.sessions import create_session, delete_session
from beehive.db.sources import (
    create_source,
    find_duplicate_source,
    get_source,
    list_by_channel as list_sources,
    set_source_paused,
    source_impact_counts,
    update_source,
)
from beehive.digest.send import build_email_group_digest_preview
from beehive.domain.channels import ChannelKind
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    get_stored_default_email,
    resolve_channel_email,
    resolve_default_email,
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
from beehive.notify import build_notifier
from beehive.scheduling import next_email_group_due_at
from beehive.web.deps import (
    SESSION_COOKIE_NAME,
    get_db,
    get_localizer,
    get_optional_session,
    require_admin_session,
    verify_csrf,
)
from beehive.web.formatting import (
    fetch_stats_label,
    freshness_exact_time,
    freshness_label,
    host_local_time_label,
    relative_time,
)
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.link_safety import safe_external_href
from beehive.web.official_feed_labels import official_feed_icon, official_feed_label

router = APIRouter(prefix="/admin")

_PASSWORD_HASH_KEY = "admin_password_hash"
_SESSION_LIFETIME_DAYS = 30
_ADMIN_TABS = frozenset({"channels", "ai", "delivery", "system", "groups"})
_EMAIL_TIMEZONES = (
    "Pacific/Auckland",
    "Australia/Sydney",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "UTC",
)
_EMAIL_WEEKDAYS = (
    (0, "web.weekday.monday"),
    (1, "web.weekday.tuesday"),
    (2, "web.weekday.wednesday"),
    (3, "web.weekday.thursday"),
    (4, "web.weekday.friday"),
    (5, "web.weekday.saturday"),
    (6, "web.weekday.sunday"),
)

_CLEAR_DEFAULT_WITHOUT_ENV_ERROR = (
    "Cannot clear default recipient because DIGEST_EMAIL_TO is not configured"
)

# Every value here is a translations/web.py key, not display text -- the actual copy always
# comes from the request's Localizer, so the same English exception message renders correctly
# in any supported platform language rather than being hardcoded to one.
_EMAIL_ERROR_KEYS = {
    "Email address is required": "web.email_error.required",
    "Email address cannot contain whitespace": "web.email_error.no_whitespace",
    "Only one email address is supported": "web.email_error.single_address",
    "Email address must contain one @": "web.email_error.at_symbol",
    "Email address needs a local part and domain": "web.email_error.local_and_domain",
    "Email address contains an invalid dot": "web.email_error.invalid_dot",
    "Email domain must contain a valid dot": "web.email_error.invalid_domain_dot",
    _CLEAR_DEFAULT_WITHOUT_ENV_ERROR: "web.email_error.clear_without_env",
}


def _email_error_message(error: EmailConfigurationError, t: Localizer) -> str:
    message = str(error)
    key = _EMAIL_ERROR_KEYS.get(message)
    return t.text(key) if key is not None else message


def _client_ip(request: Request) -> str:
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else "unknown"
    )


def _client_country(request: Request) -> str | None:
    return request.headers.get("CF-IPCountry")


def _safe_return_path(value: str | None, fallback: str = "/admin/") -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    return value


def _render_login_page(
    request: Request,
    conn: sqlite3.Connection,
    t: Localizer,
    error: str | None,
    next_path: str = "/admin/",
    expired: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    latest = get_most_recent_attempt(conn)
    last_login = None
    if latest:
        last_login = {
            "time": host_local_time_label(latest["attempted_at"]),
            "ip": latest["ip"] or "unknown",
            "country": latest["country"] or t.text("web.admin.login.unknown_region"),
            "success": bool(latest["success"]),
        }
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {
            "error": error,
            "last_login": last_login,
            "next_path": _safe_return_path(next_path),
            "expired": expired,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next: str = "/admin/",
    expired: int | None = None,
    session: dict | None = Depends(get_optional_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    # The public header's admin link always points here (Slice 2 Task 10) -- an already-logged-in
    # owner following it must land on the settings home, not be shown the password form again
    # just because their session is still valid (that previously looked exactly like an
    # unexpectedly-short session).
    if session is not None:
        return RedirectResponse(_safe_return_path(next), status_code=303)
    return _render_login_page(
        request,
        conn,
        t,
        error=None,
        next_path=next,
        expired=expired == 1 or "reauth=1" in next,
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/admin/"),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    ip = _client_ip(request)
    country = _client_country(request)
    now = datetime.now(timezone.utc)

    if is_locked_out(conn, ip, now):
        return _render_login_page(
            request,
            conn,
            t,
            error=t.text("web.admin.login.rate_limited"),
            next_path=next,
            status_code=429,
        )

    stored_hash = app_state.get(conn, _PASSWORD_HASH_KEY)
    success = stored_hash is not None and verify_password(stored_hash, password)
    record_attempt(conn, ip, country, success, now.isoformat())

    if not success:
        return _render_login_page(
            request,
            conn,
            t,
            error=t.text("web.admin.login.wrong_password"),
            next_path=next,
            status_code=401,
        )

    session_id = generate_session_id()
    csrf_token = (
        generate_session_id()
    )  # same high-entropy generator; distinct value/purpose
    expires_at = (now + timedelta(days=_SESSION_LIFETIME_DAYS)).isoformat()
    create_session(conn, session_id, csrf_token, expires_at)

    response = RedirectResponse(_safe_return_path(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        sign_session_id(session_id, request.app.state.session_secret),
        max_age=_SESSION_LIFETIME_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    delete_session(conn, session["session_id"])
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="lax"
    )
    return response


def _fetch_interval_label(hours: int, t: Localizer) -> str:
    return (
        t.text("web.fetch_interval.daily")
        if hours >= 24
        else t.text("web.fetch_interval.every_n_hours", hours=hours)
    )


def _email_group_frequency_label(hours: int, t: Localizer) -> str:
    """Unlike _fetch_interval_label above (a fixed {3, 6, 24} dropdown where "24+" is always
    "daily"), a group's send_interval_hours is a free-form number -- only exactly 24 should read
    as "Once a day"; anything else (including 48, 168, etc.) must show its actual hour count."""
    return (
        t.text("web.fetch_interval.daily")
        if hours == 24
        else t.text("web.fetch_interval.every_n_hours", hours=hours)
    )


def _email_weekday_rows(t: Localizer, selected: set[int]) -> list[dict]:
    return [
        {
            "value": value,
            "label": t.text(label_key),
            "selected": value in selected,
        }
        for value, label_key in _EMAIL_WEEKDAYS
    ]


def _normalize_email_schedule(
    *,
    schedule_mode: str,
    schedule_timezone: str,
    schedule_time: str,
    schedule_weekdays: list[int] | None,
) -> tuple[str, str, str, str]:
    if schedule_mode not in {"interval", "calendar"}:
        raise ValueError("web.admin.email_group.schedule_error_mode")
    timezone_name = schedule_timezone.strip() or "Pacific/Auckland"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("web.admin.email_group.schedule_error_timezone") from exc
    try:
        parsed_time = datetime.strptime(schedule_time.strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError("web.admin.email_group.schedule_error_time") from exc
    normalized_time = parsed_time.strftime("%H:%M")
    weekdays = sorted(set(schedule_weekdays or []))
    if schedule_mode == "calendar" and (
        not weekdays or any(day not in range(7) for day in weekdays)
    ):
        raise ValueError("web.admin.email_group.schedule_error_days")
    if not weekdays:
        weekdays = list(range(7))
    return (
        schedule_mode,
        timezone_name,
        normalized_time,
        ",".join(str(day) for day in weekdays),
    )


def _email_group_schedule_label(group: dict, t: Localizer) -> str:
    if group.get("schedule_mode") != "calendar":
        return _email_group_frequency_label(group["send_interval_hours"], t)
    selected = {
        int(day)
        for day in (group.get("schedule_weekdays") or "").split(",")
        if day
    }
    if selected == set(range(7)):
        day_label = t.text("web.admin.email_group.every_day")
    elif selected == set(range(5)):
        day_label = t.text("web.admin.email_group.weekdays")
    else:
        label_keys = dict(_EMAIL_WEEKDAYS)
        day_label = ", ".join(t.text(label_keys[day]) for day in sorted(selected))
    return t.text(
        "web.admin.email_group.calendar_summary",
        days=day_label,
        time=group.get("schedule_time") or "09:00",
        timezone=group.get("schedule_timezone") or "Pacific/Auckland",
    )


def _resolve_default_for_admin(
    conn: sqlite3.Connection,
    t: Localizer,
) -> tuple[ResolvedRecipient, str | None]:
    try:
        return (
            resolve_default_email(conn, os.environ.get("DIGEST_EMAIL_TO")),
            None,
        )
    except EmailConfigurationError as exc:
        return ResolvedRecipient(None, "missing"), _email_error_message(exc, t)


def _channel_kind_label(kind: ChannelKind, t: Localizer) -> str:
    return t.text(f"web.channel.{kind.value}_label")


def _build_admin_channel_rows(
    conn: sqlite3.Connection,
    t: Localizer,
    data_dir: str,
) -> list[dict]:
    manual_states = list_manual_trigger_states(data_dir)
    channels = []
    for channel in list_channels(conn):
        kind = require_channel_kind(channel["kind"])
        sources = list_sources(conn, channel["id"])
        manual_state = manual_states.get(channel["id"])
        fetch_errors = [
            source["last_fetch_error"]
            for source in sources
            if source["last_fetch_error"] and not source["paused_at"]
        ]
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
        fetch_error_label = (
            t.text("web.admin.settings.fetch_failed", error=fetch_errors[0])
            if fetch_errors
            else None
        )
        channels.append(
            {
                "id": channel["id"],
                "name": channel["name"],
                "kind": kind.value,
                "kind_label": _channel_kind_label(kind, t),
                "source_count": len(sources),
                "fetch_interval_label": _fetch_interval_label(
                    channel["fetch_interval_hours"], t
                ),
                "freshness_label": freshness_label(sources, t),
                "freshness_exact_label": freshness_exact_time(sources),
                "fetch_stats_label": fetch_stats_label(sources, t),
                "fetch_status_kind": fetch_status_kind,
                "fetch_status_label": fetch_status_label,
                "fetch_error_label": fetch_error_label,
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
                "member_count": len(list_member_channels(conn, group["id"])),
                "effective_email": recipient,
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
                "last_error": group["last_error"],
                "last_error_label": (
                    host_local_time_label(group["last_error_at"])
                    if group["last_error_at"]
                    else None
                ),
                "next_due_label": host_local_time_label(
                    next_email_group_due_at(group, now).isoformat()
                ),
            }
        )
    return groups


def _build_group_channel_rows(
    conn: sqlite3.Connection,
    t: Localizer,
    *,
    group_id: int | None,
    selected_ids: set[int] | None = None,
) -> list[dict]:
    """Powers the channel-assignment checklist on both the new and edit group pages. When
    selected_ids is given (re-rendering after a validation error) it reflects the user's
    just-submitted, not-yet-saved checkbox state instead of the DB's current membership --
    group_id is still used to decide whether a channel's *other* group membership should be
    flagged (a channel already in *this* group is never "other")."""
    if selected_ids is not None:
        member_ids = selected_ids
    elif group_id is not None:
        member_ids = {c["id"] for c in list_member_channels(conn, group_id)}
    else:
        member_ids = set()
    rows = []
    for channel in list_channels(conn):
        kind = require_channel_kind(channel["kind"])
        other_group = get_channel_group(conn, channel["id"])
        other_group_name = None
        if other_group is not None and other_group["id"] != group_id:
            other_group_name = other_group["name"]
        rows.append(
            {
                "id": channel["id"],
                "name": channel["name"],
                "kind": kind.value,
                "kind_label": _channel_kind_label(kind, t),
                "is_member": channel["id"] in member_ids,
                "other_group_name": other_group_name,
            }
        )
    return rows


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
            impact = t.text("web.admin.activity.no_impact")
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
        rows.append(
            {
                **action,
                "action_label": t.text(
                    f"web.admin.activity.{action['action_type']}",
                    target=action["target_label"],
                ),
                "impact_label": impact,
                "created_label": host_local_time_label(action["created_at"]),
            }
        )
    return rows


def _build_system_health_rows(
    conn: sqlite3.Connection,
    t: Localizer,
    data_dir: str,
    default_recipient: ResolvedRecipient,
) -> list[dict]:
    source_counts = dict(
        conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN paused_at IS NOT NULL THEN 1 ELSE 0 END) AS paused,
                SUM(CASE
                    WHEN paused_at IS NULL AND last_fetch_error IS NOT NULL THEN 1
                    ELSE 0
                END) AS failed
            FROM sources
            """
        ).fetchone()
    )
    manual_states = list_manual_trigger_states(data_dir)
    stale_fetches = sum(state == "stale" for state in manual_states.values())
    active_fetches = sum(
        state in {"queued", "running"} for state in manual_states.values()
    )

    groups = list_email_groups(conn)
    delivery_failures = sum(bool(group["last_error"]) for group in groups)
    missing_recipients = 0
    for group in groups:
        try:
            if resolve_group_email(group, default_recipient).address is None:
                missing_recipients += 1
        except EmailConfigurationError:
            missing_recipients += 1

    reminder_counts = dict(
        conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN claim_token IS NOT NULL THEN 1 ELSE 0 END) AS processing
            FROM auction_watches
            """
        ).fetchone()
    )
    research_counts = dict(
        conn.execute(
            """
            SELECT
                SUM(CASE WHEN status IN ('pending', 'processing') THEN 1 ELSE 0 END)
                    AS active,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE
                    WHEN status = 'processing' AND lease_expires_at <= ? THEN 1
                    ELSE 0
                END) AS stale
            FROM research_runs
            """,
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchone()
    )

    def row(
        key: str,
        *,
        status: str,
        detail: str,
        href: str,
    ) -> dict:
        return {
            "heading": t.text(f"web.admin.health.{key}_heading"),
            "summary": t.text(f"web.admin.health.{key}_summary"),
            "status": status,
            "status_label": t.text(f"web.admin.health.status_{status}"),
            "detail": detail,
            "href": href,
            "action_label": t.text(f"web.admin.health.{key}_action"),
        }

    source_failed = int(source_counts["failed"] or 0)
    source_status = "error" if source_failed or stale_fetches else "ok"
    delivery_status = (
        "error" if delivery_failures else "warning" if missing_recipients else "ok"
    )
    reminder_failed = int(reminder_counts["failed"] or 0)
    reminder_status = "error" if reminder_failed else "ok"
    research_failed = int(research_counts["failed"] or 0)
    research_stale = int(research_counts["stale"] or 0)
    research_status = "error" if research_stale else "warning" if research_failed else "ok"
    return [
        row(
            "sources",
            status=source_status,
            detail=t.text(
                "web.admin.health.sources_detail",
                total=int(source_counts["total"] or 0),
                paused=int(source_counts["paused"] or 0),
                failed=source_failed,
                active=active_fetches,
                stale=stale_fetches,
            ),
            href="/admin/?tab=channels",
        ),
        row(
            "delivery",
            status=delivery_status,
            detail=t.text(
                "web.admin.health.delivery_detail",
                total=len(groups),
                failed=delivery_failures,
                missing=missing_recipients,
            ),
            href="/admin/?tab=groups",
        ),
        row(
            "reminders",
            status=reminder_status,
            detail=t.text(
                "web.admin.health.reminders_detail",
                total=int(reminder_counts["total"] or 0),
                failed=reminder_failed,
                processing=int(reminder_counts["processing"] or 0),
            ),
            href="/watchlist",
        ),
        row(
            "research",
            status=research_status,
            detail=t.text(
                "web.admin.health.research_detail",
                active=int(research_counts["active"] or 0),
                failed=research_failed,
                stale=research_stale,
            ),
            href="/research",
        ),
    ]


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
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
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
            "channels": _build_admin_channel_rows(
                conn,
                t,
                data_dir,
            ),
            "email_groups": _build_admin_email_group_rows(conn, effective, t),
            "languages": SUPPORTED_LANGUAGES,
            "current_language": t.code,
            "language_saved": language_saved,
            "language_error": language_error,
            "models": SUPPORTED_MODELS,
            "current_model": load_model(conn),
            "model_saved": model_saved,
            "model_error": model_error,
            "featured_window_days": (
                load_featured_window_days(conn)
                if submitted_featured_window_days is None
                else submitted_featured_window_days
            ),
            "featured_saved": featured_saved,
            "featured_error": featured_error,
            "active_tab": active_tab if active_tab in _ADMIN_TABS else "channels",
            "triggered_count": triggered_count,
            "bulk_error": bulk_error,
            "recent_actions": _build_admin_action_rows(conn, t),
            "system_health": _build_system_health_rows(
                conn,
                t,
                data_dir,
                effective,
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
            active_tab="delivery",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="delivery_settings_updated",
        target_type="settings",
        target_id=None,
        target_label=t.text("web.admin.tabs.delivery"),
    )
    return RedirectResponse("/admin/?tab=delivery&saved=1", status_code=303)


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
            active_tab="ai",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="language_updated",
        target_type="settings",
        target_id=None,
        target_label=language,
    )
    return RedirectResponse("/admin/?tab=ai&language_saved=1", status_code=303)


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
            active_tab="ai",
            status_code=400,
        )
    record_admin_action(
        conn,
        action_type="model_updated",
        target_type="settings",
        target_id=None,
        target_label=model,
    )
    return RedirectResponse("/admin/?tab=ai&model_saved=1", status_code=303)


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
            active_tab="system",
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
        "/admin/?tab=system&featured_saved=1",
        status_code=303,
    )


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
    CSS/hint toggling convention (`kind-<value>` / `.kind-only-<value>`) in beehive.css."""
    return tuple(
        {
            "value": definition.kind.value,
            "input_id": f"kind-{definition.kind.value}",
            "icon": definition.kind.value[0].upper(),
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
        "profile_hint": profile_hint,
        "highlight_count_hint": highlight_hint,
        "minimum_score_hint": minimum_hint,
    }


@router.get("/channels/new", response_class=HTMLResponse)
def new_channel_form(
    request: Request,
    session: dict = Depends(require_admin_session),
    t: Localizer = Depends(get_localizer),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_new_channel.html",
        {
            "csrf_token": session["csrf_token"],
            "kind_options": _channel_kind_options(t),
            "selected_kind": ChannelKind.EDITORIAL.value,
        },
    )


@router.post("/channels/new")
def new_channel_submit(
    name: str = Form(...),
    profile: str = Form(...),
    fetch_interval_hours: int = Form(...),
    highlight_count: int = Form(8, ge=1, le=50),
    minimum_score: int = Form(0, ge=0, le=100),
    kind: Literal["editorial", "monitor", "tracker"] = Form("editorial"),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    channel_id = create_channel(
        conn,
        name,
        profile,
        fetch_interval_hours=fetch_interval_hours,
        highlight_count=highlight_count,
        minimum_score=minimum_score,
        kind=kind,
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


_SOURCE_TYPE_ICONS = {
    "reddit_subreddit": "📍",
    "google_news_query": "📰",
    "hackernews_stories": "🟧",
    "hackernews_query": "🟧",
    "rbnz_news": official_feed_icon("rbnz_news"),
    "nz_government_news": official_feed_icon("nz_government_news"),
    "federal_reserve_news": official_feed_icon("federal_reserve_news"),
    "shopify_collection": "🛍️",
    "land_sea_collection": "🌊",
    "all_about_auctions": "AA",
}


def _source_type_options(t: Localizer) -> tuple[dict, ...]:
    """Built per-request (not module-level) since every label is a translated string --
    Reddit/Google News/Hacker News/RBNZ/Federal Reserve stay in their own proper names in every
    language; only the descriptor after the em dash (e.g. "Subreddit", "Keyword query") changes
    per locale. Mirrors the input_id/icon convention _admin_source_icon relies on below."""
    return (
        {
            "type_key": "reddit_subreddit",
            "input_id": "type-reddit",
            "icon": _SOURCE_TYPE_ICONS["reddit_subreddit"],
            "label": t.text("web.source_type.reddit_subreddit"),
        },
        {
            "type_key": "google_news_query",
            "input_id": "type-google",
            "icon": _SOURCE_TYPE_ICONS["google_news_query"],
            "label": t.text("web.source_type.google_news_query"),
        },
        {
            "type_key": "hackernews_stories",
            "input_id": "type-hn-stories",
            "icon": _SOURCE_TYPE_ICONS["hackernews_stories"],
            "label": t.text("web.source_type.hackernews_stories"),
        },
        {
            "type_key": "hackernews_query",
            "input_id": "type-hn-query",
            "icon": _SOURCE_TYPE_ICONS["hackernews_query"],
            "label": t.text("web.source_type.hackernews_query"),
        },
        {
            "type_key": "rbnz_news",
            "input_id": "type-rbnz",
            "icon": _SOURCE_TYPE_ICONS["rbnz_news"],
            "label": t.text("web.source_type.rbnz_news"),
        },
        {
            "type_key": "nz_government_news",
            "input_id": "type-nz-gov",
            "icon": _SOURCE_TYPE_ICONS["nz_government_news"],
            "label": t.text("web.source_type.nz_government_news"),
        },
        {
            "type_key": "federal_reserve_news",
            "input_id": "type-fed",
            "icon": _SOURCE_TYPE_ICONS["federal_reserve_news"],
            "label": t.text("web.source_type.federal_reserve_news"),
        },
        {
            "type_key": "shopify_collection",
            "input_id": "type-shopify",
            "icon": _SOURCE_TYPE_ICONS["shopify_collection"],
            "label": t.text("web.source_type.shopify_collection"),
        },
        {
            "type_key": "land_sea_collection",
            "input_id": "type-land-sea",
            "icon": _SOURCE_TYPE_ICONS["land_sea_collection"],
            "label": t.text("web.source_type.land_sea_collection"),
        },
        {
            "type_key": "all_about_auctions",
            "input_id": "type-all-about-auctions",
            "icon": _SOURCE_TYPE_ICONS["all_about_auctions"],
            "label": t.text("web.source_type.all_about_auctions"),
        },
    )


def _admin_source_label(source: dict, t: Localizer) -> str:
    config = json.loads(source["config"])
    if source["type"] == "reddit_subreddit":
        return f"r/{config['subreddit']}"
    if source["type"] == "google_news_query":
        return f'"{config["query"]}"'
    if source["type"] == "all_about_auctions":
        return "All About Auctions"
    if source["type"] in {"shopify_collection", "land_sea_collection"}:
        # Both connectors store the same {"collection_url": ...} config shape.
        url = config.get("collection_url", "")
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}" if parsed.netloc else url
    official_label = official_feed_label(source["type"])
    if official_label is not None:
        return official_label
    hackernews_label = hackernews_source_label(source["type"], config, t)
    return hackernews_label if hackernews_label is not None else source["type"]


def _admin_source_copy_value(source: dict, label: str) -> str:
    """Full, untruncated value for the "copy" button -- unlike `_admin_source_label`, this
    keeps any query string/fragment (e.g. Shopify vendor filters) so it can be pasted straight
    back into a new source. Falls back to the display label for source types that have nothing
    to truncate in the first place."""
    if source["type"] in {"shopify_collection", "land_sea_collection"}:
        config = json.loads(source["config"])
        return config.get("collection_url") or label
    return label


def _admin_source_icon(source: dict) -> str:
    """Mirrors the icons admin_add_source.html uses for each type, so a source's icon in the
    Channel edit page's source list always matches the icon the admin picked it by."""
    return _SOURCE_TYPE_ICONS.get(source["type"], "🔗")


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
) -> HTMLResponse:
    sources = []
    for source in list_sources(conn, channel["id"]):
        label = _admin_source_label(source, t)
        sources.append(
            {
                "id": source["id"],
                "label": label,
                "confirmation_value": source["name"] or source["type"],
                "icon": _admin_source_icon(source),
                "copy_value": _admin_source_copy_value(source, label),
                "impact": source_impact_counts(conn, source["id"]),
                **_source_observability(source, t),
            }
        )
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    # The effective hint reflects what a *saved* value would resolve to, so a rejected
    # override (kept only in the display `channel` for the field) must not poison it --
    # resolve it from the unmodified existing row instead.
    source_channel = effective_channel if effective_channel is not None else channel
    try:
        effective = resolve_channel_email(source_channel, default_recipient).address
    except EmailConfigurationError:
        effective = None
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_edit_channel.html",
        {
            "channel": channel,
            "kind_display": _channel_kind_display(require_channel_kind(channel["kind"]), t),
            "sources": sources,
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
    fetch_interval_hours: int = Form(...),
    highlight_count: int | None = Form(None, ge=1, le=50),
    minimum_score: int | None = Form(None, ge=0, le=100),
    digest_email: str = Form(""),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    existing = get_channel(conn, channel_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    submitted_email = digest_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        channel = {
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
        }
        return _render_edit_channel_page(
            request,
            conn,
            session,
            channel,
            t,
            effective_channel=existing,
            error=_email_error_message(exc, t),
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


def _render_new_email_group_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    t: Localizer,
    *,
    name: str = "",
    subject_template: str = "",
    recipient_email: str = "",
    send_interval_hours: int = 24,
    schedule_mode: str = "interval",
    schedule_timezone: str = "Pacific/Auckland",
    schedule_time: str = "09:00",
    schedule_weekdays: set[int] | None = None,
    selected_channel_ids: set[int] | None = None,
    error: str | None = None,
    schedule_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_new_email_group.html",
        {
            "csrf_token": session["csrf_token"],
            "name": name,
            "subject_template": subject_template,
            "recipient_email": recipient_email,
            "send_interval_hours": send_interval_hours,
            "schedule_mode": schedule_mode,
            "schedule_timezone": schedule_timezone,
            "schedule_time": schedule_time,
            "schedule_timezones": _EMAIL_TIMEZONES,
            "schedule_weekdays": _email_weekday_rows(
                t,
                set(range(7)) if schedule_weekdays is None else schedule_weekdays,
            ),
            "effective_email": default_recipient.address,
            "default_error": default_error,
            "error": error,
            "schedule_error": schedule_error,
            "channel_rows": _build_group_channel_rows(
                conn, t, group_id=None, selected_ids=selected_channel_ids
            ),
        },
        status_code=status_code,
    )


@router.get("/email-groups/new", response_class=HTMLResponse)
def new_email_group_form(
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    return _render_new_email_group_page(request, conn, session, t)


@router.post("/email-groups/new")
def new_email_group_submit(
    request: Request,
    name: str = Form(...),
    subject_template: str = Form(...),
    recipient_email: str = Form(""),
    send_interval_hours: int = Form(24, ge=1),
    schedule_mode: str = Form("interval"),
    schedule_timezone: str = Form("Pacific/Auckland"),
    schedule_time: str = Form("09:00"),
    schedule_weekdays: list[int] | None = Form(None),
    channel_ids: list[int] | None = Form(None),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    submitted_email = recipient_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        return _render_new_email_group_page(
            request,
            conn,
            session,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            error=_email_error_message(exc, t),
            status_code=400,
        )
    try:
        normalized_schedule = _normalize_email_schedule(
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=schedule_weekdays,
        )
    except ValueError as exc:
        return _render_new_email_group_page(
            request,
            conn,
            session,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            schedule_error=t.text(str(exc)),
            status_code=400,
        )
    group_id = create_email_group(
        conn,
        name,
        subject_template,
        normalized_email,
        send_interval_hours,
        schedule_mode=normalized_schedule[0],
        schedule_timezone=normalized_schedule[1],
        schedule_time=normalized_schedule[2],
        schedule_weekdays=normalized_schedule[3],
    )
    for channel_id in dict.fromkeys(channel_ids or []):
        assign_channel(conn, group_id, channel_id)
    record_admin_action(
        conn,
        action_type="email_group_created",
        target_type="email_group",
        target_id=group_id,
        target_label=name,
        detail={"channels": len(set(channel_ids or []))},
    )
    return RedirectResponse(
        f"/admin/email-groups/{group_id}/edit?created=1",
        status_code=303,
    )


def _render_edit_email_group_page(
    request: Request,
    conn: sqlite3.Connection,
    session: dict,
    group: dict,
    t: Localizer,
    *,
    name: str | None = None,
    subject_template: str | None = None,
    recipient_email: str | None = None,
    send_interval_hours: int | None = None,
    schedule_mode: str | None = None,
    schedule_timezone: str | None = None,
    schedule_time: str | None = None,
    schedule_weekdays: set[int] | None = None,
    selected_channel_ids: set[int] | None = None,
    error: str | None = None,
    schedule_error: str | None = None,
    saved: bool = False,
    created: bool = False,
    test_sent: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    channel_rows = _build_group_channel_rows(
        conn, t, group_id=group["id"], selected_ids=selected_channel_ids
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_edit_email_group.html",
        {
            "group": group,
            "csrf_token": session["csrf_token"],
            "name": group["name"] if name is None else name,
            "subject_template": (
                group["subject_template"]
                if subject_template is None
                else subject_template
            ),
            "recipient_email": (
                (group["recipient_email"] or "")
                if recipient_email is None
                else recipient_email
            ),
            "send_interval_hours": (
                group["send_interval_hours"]
                if send_interval_hours is None
                else send_interval_hours
            ),
            "schedule_mode": (
                group["schedule_mode"] if schedule_mode is None else schedule_mode
            ),
            "schedule_timezone": (
                group["schedule_timezone"]
                if schedule_timezone is None
                else schedule_timezone
            ),
            "schedule_time": (
                group["schedule_time"] if schedule_time is None else schedule_time
            ),
            "schedule_timezones": _EMAIL_TIMEZONES,
            "schedule_weekdays": _email_weekday_rows(
                t,
                (
                    {
                        int(value)
                        for value in group["schedule_weekdays"].split(",")
                        if value
                    }
                    if schedule_weekdays is None
                    else schedule_weekdays
                ),
            ),
            "effective_email": default_recipient.address,
            "default_error": default_error,
            "error": error,
            "schedule_error": schedule_error,
            "saved": saved,
            "created": created,
            "test_sent": test_sent,
            "channel_rows": channel_rows,
            "member_count": sum(row["is_member"] for row in channel_rows),
        },
        status_code=status_code,
    )


@router.get("/email-groups/{group_id}/edit", response_class=HTMLResponse)
def edit_email_group_form(
    group_id: int,
    request: Request,
    saved: int | None = None,
    created: int | None = None,
    test_sent: int | None = None,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    return _render_edit_email_group_page(
        request,
        conn,
        session,
        group,
        t,
        saved=saved == 1,
        created=created == 1,
        test_sent=test_sent == 1,
    )


@router.post("/email-groups/{group_id}/edit")
def edit_email_group_submit(
    group_id: int,
    request: Request,
    name: str = Form(...),
    subject_template: str = Form(...),
    recipient_email: str = Form(""),
    send_interval_hours: int = Form(..., ge=1),
    schedule_mode: str = Form("interval"),
    schedule_timezone: str = Form("Pacific/Auckland"),
    schedule_time: str = Form("09:00"),
    schedule_weekdays: list[int] | None = Form(None),
    channel_ids: list[int] | None = Form(None),
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    submitted_email = recipient_email.strip()
    try:
        normalized_email = validate_email(submitted_email) if submitted_email else None
    except EmailConfigurationError as exc:
        return _render_edit_email_group_page(
            request,
            conn,
            session,
            group,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            error=_email_error_message(exc, t),
            status_code=400,
        )
    try:
        normalized_schedule = _normalize_email_schedule(
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=schedule_weekdays,
        )
    except ValueError as exc:
        return _render_edit_email_group_page(
            request,
            conn,
            session,
            group,
            t,
            name=name,
            subject_template=subject_template,
            recipient_email=recipient_email,
            send_interval_hours=send_interval_hours,
            schedule_mode=schedule_mode,
            schedule_timezone=schedule_timezone,
            schedule_time=schedule_time,
            schedule_weekdays=set(schedule_weekdays or []),
            selected_channel_ids=set(channel_ids or []),
            schedule_error=t.text(str(exc)),
            status_code=400,
        )
    update_email_group(
        conn,
        group_id,
        name,
        subject_template,
        normalized_email,
        send_interval_hours,
        schedule_mode=normalized_schedule[0],
        schedule_timezone=normalized_schedule[1],
        schedule_time=normalized_schedule[2],
        schedule_weekdays=normalized_schedule[3],
    )
    selected_ids = set(dict.fromkeys(channel_ids or []))
    current_member_ids = {c["id"] for c in list_member_channels(conn, group_id)}
    for channel_id in selected_ids - current_member_ids:
        assign_channel(conn, group_id, channel_id)
    for channel_id in current_member_ids - selected_ids:
        unassign_channel(conn, channel_id)
    record_admin_action(
        conn,
        action_type="email_group_updated",
        target_type="email_group",
        target_id=group_id,
        target_label=name,
        detail={"channels": len(selected_ids)},
    )
    return RedirectResponse(
        f"/admin/email-groups/{group_id}/edit?saved=1",
        status_code=303,
    )


@router.get("/email-groups/{group_id}/preview", response_class=HTMLResponse)
def preview_email_group(
    group_id: int,
    request: Request,
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    preview = build_email_group_digest_preview(conn, group, t)
    default_recipient, default_error = _resolve_default_for_admin(conn, t)
    try:
        recipient = resolve_group_email(group, default_recipient).address
    except EmailConfigurationError as exc:
        recipient = None
        default_error = _email_error_message(exc, t)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_email_group_preview.html",
        {
            "group": group,
            "preview": preview,
            "recipient": recipient,
            "recipient_error": default_error,
            "csrf_token": session["csrf_token"],
        },
    )


@router.post("/email-groups/{group_id}/test-send")
def test_send_email_group(
    group_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
    t: Localizer = Depends(get_localizer),
):
    verify_csrf(session, csrf_token)
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    preview = build_email_group_digest_preview(conn, group, t)
    if preview is None:
        raise HTTPException(status_code=409, detail="Email group has no content to preview")
    default_recipient = resolve_default_email(
        conn,
        os.environ.get("DIGEST_EMAIL_TO"),
    )
    recipient = resolve_group_email(group, default_recipient)
    if recipient.address is None:
        raise HTTPException(status_code=409, detail="Email recipient is not configured")
    notifier = build_notifier(os.environ, default_to_addr=default_recipient.address)
    notifier.send(
        f"[Test] {preview.subject}",
        preview.plain_text,
        preview.html,
        to_addr=recipient.address,
    )
    record_admin_action(
        conn,
        action_type="email_group_test_sent",
        target_type="email_group",
        target_id=group_id,
        target_label=group["name"],
        detail={
            "channels": preview.channel_count,
            "events": preview.event_count,
        },
    )
    return RedirectResponse(
        f"/admin/email-groups/{group_id}/edit?test_sent=1",
        status_code=303,
    )


@router.post("/email-groups/{group_id}/delete")
def delete_email_group_submit(
    group_id: int,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
):
    verify_csrf(session, csrf_token)
    group = get_email_group(conn, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Email group not found")
    if confirmation.strip() != group["name"]:
        raise HTTPException(status_code=409, detail="Email group confirmation did not match")
    action_id = delete_email_group_with_undo(
        conn,
        group_id,
        target_label=group["name"],
    )
    return RedirectResponse(
        f"/admin/?tab=system&action={action_id}",
        status_code=303,
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
        page_eyebrow = "Edit input"
        page_heading = t.text("web.admin.source_edit.heading")
        page_lede = t.text("web.admin.source_edit.lede", channel=channel["name"])
        submit_label = t.text("web.admin.source_edit.submit")
    else:
        page_eyebrow = "New input"
        page_heading = t.text("web.admin.source_new.heading")
        page_lede = t.text("web.admin.source_new.lede", channel=channel["name"])
        submit_label = t.text("web.admin.source_new.submit")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
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
            "page_eyebrow": page_eyebrow,
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
        target_label=source_name.strip() or type,
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
        target_label=source_name.strip() or type,
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
):
    verify_csrf(session, csrf_token)
    source = get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    channel_id = source["channel_id"]
    label = source["name"] or source["type"]
    if confirmation.strip() != label:
        raise HTTPException(status_code=409, detail="Source confirmation did not match")
    action_id = delete_source_with_undo(
        conn,
        source_id,
        target_label=label,
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
        target_label=source["name"] or source["type"],
        detail={"items": len(raw_items), "duration_ms": duration_ms, "success": error is None},
    )
    return request.app.state.templates.TemplateResponse(
        request,
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
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
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
        target_label=source["name"] or source["type"],
        detail={"channel_id": source["channel_id"]},
    )
    return RedirectResponse(
        f"/admin/channels/{source['channel_id']}/edit", status_code=303
    )


@router.post("/sources/{source_id}/resume")
def resume_source_submit(
    source_id: int,
    csrf_token: str = Form(...),
    session: dict = Depends(require_admin_session),
    conn: sqlite3.Connection = Depends(get_db),
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
        target_label=source["name"] or source["type"],
        detail={"channel_id": source["channel_id"]},
    )
    return RedirectResponse(
        f"/admin/channels/{source['channel_id']}/edit", status_code=303
    )
