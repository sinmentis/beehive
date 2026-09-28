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
    international_clearance,
    land_sea_collection,
    official_feeds,
    reddit,
    shopify_collection,
)
from beehive.connectors.base import PreviewSourceConnector
from beehive.connectors.international_clearance import RETAILER_LABELS
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
from beehive.scheduling import (
    DEFAULT_CHANNEL_FETCH_TIME,
    DEFAULT_SCHEDULE_TIMEZONE,
    HOST_TZ,
    ChannelFetchSchedule,
    ScheduleMode,
    next_channel_fetch_at,
    next_email_group_due_at,
    require_schedule_mode,
)
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
    format_count,
    freshness_exact_time,
    freshness_label,
    host_local_time_label,
    relative_time,
)
from beehive.web.client_ip import resolve_client_ip
from beehive.web.link_safety import safe_external_href
from beehive.web.source_labels import derived_source_label, source_display_name

router = APIRouter(prefix="/admin")

_PASSWORD_HASH_KEY = "admin_password_hash"
_SESSION_LIFETIME_DAYS = 30
# The admin home is four numbered chapters. The old tab names still resolve, so bookmarks and
# redirects written before the chapters were merged keep landing on the right one.
_ADMIN_CHAPTERS = ("channels", "groups", "settings", "system")
_ADMIN_TAB_ALIASES = {"ai": "settings", "delivery": "groups"}
# Interval mode is for frequent polling only; a once-a-day Channel belongs in calendar mode, where
# it gets a real wall-clock time instead of "24 hours after the last success".
_FETCH_INTERVAL_CHOICES = (3, 6)
# The IANA timezones offered by both schedule builders (Email Group delivery and Channel fetch).
_SCHEDULE_TIMEZONES = (
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
    return resolve_client_ip(
        request.client.host if request.client else None,
        request.headers,
        getattr(request.app.state, "trusted_proxies", ()),
    )


def _client_country(request: Request) -> str | None:
    if not getattr(request.app.state, "trusted_proxies", ()):
        return None
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


def _channel_fetch_schedule_label(channel: dict, t: Localizer) -> str:
    """"Every 3 hours" or "Daily at 05:00 (Pacific/Auckland)", from the Channel's stored mode."""
    if require_schedule_mode(channel["fetch_schedule_mode"]) is ScheduleMode.INTERVAL:
        return _fetch_interval_label(channel["fetch_interval_hours"], t)
    return t.text(
        "web.admin.channel_schedule.daily_summary",
        time=channel["fetch_schedule_time"],
        timezone=channel["fetch_schedule_timezone"],
    )


def _short_time_label(value: str | datetime, now: datetime, t: Localizer) -> str:
    """"Today 05:00", "Tomorrow 23:00" or "2026-10-02 09:00" in the host's time zone."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(HOST_TZ)
    clock = local.strftime("%H:%M")
    days_away = (local.date() - now.astimezone(HOST_TZ).date()).days
    if days_away == 0:
        return t.text("web.admin.time.today", time=clock)
    if days_away == 1:
        return t.text("web.admin.time.tomorrow", time=clock)
    if days_away == -1:
        return t.text("web.admin.time.yesterday", time=clock)
    return f"{local.date().isoformat()} {clock}"


def _channel_schedule_short(channel: dict, t: Localizer) -> str:
    if (
        require_schedule_mode(channel["fetch_schedule_mode"]) is ScheduleMode.CALENDAR
        and channel["fetch_schedule_timezone"] == HOST_TZ.key
    ):
        return t.text("web.admin.channel_schedule.daily_short", time=channel["fetch_schedule_time"])
    return _channel_fetch_schedule_label(channel, t)


def _channel_next_fetch_short(
    channel: dict, sources: list[dict], now: datetime, t: Localizer
) -> str | None:
    next_fetch_at = next_channel_fetch_at(
        sources, ChannelFetchSchedule.from_channel(channel), now
    )
    return _short_time_label(next_fetch_at, now, t) if next_fetch_at else None


def _channel_next_fetch_label(
    channel: dict,
    sources: list[dict],
    now: datetime,
) -> str | None:
    """The host-local timestamp of this Channel's next automatic fetch, or None when it has no
    Source to fetch. Mirrors the Email Group list's next_due_label so both schedules read the
    same way."""
    next_fetch_at = next_channel_fetch_at(
        sources, ChannelFetchSchedule.from_channel(channel), now
    )
    if next_fetch_at is None:
        return None
    return host_local_time_label(next_fetch_at.isoformat())


def _email_group_frequency_label(hours: int, t: Localizer) -> str:
    """Unlike _fetch_interval_label above (a small fixed dropdown where "24+" is always "daily"),
    a group's send_interval_hours is a free-form number -- only exactly 24 should read
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
            "short_label": t.text(f"web.admin.weekday_short.{label_key.rsplit('.', 1)[1]}"),
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


def _email_group_schedule_label(group: dict, t: Localizer, *, short: bool = False) -> str:
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
    timezone_name = group.get("schedule_timezone") or "Pacific/Auckland"
    if short and timezone_name == HOST_TZ.key:
        time = group.get("schedule_time") or "09:00"
        if selected == set(range(7)):
            return t.text("web.admin.channel_schedule.daily_short", time=time)
        if selected != set(range(5)):
            label_keys = dict(_EMAIL_WEEKDAYS)
            day_label = t.text("web.admin.list_separator").join(
                t.text(f"web.admin.weekday_short.{label_keys[day].rsplit('.', 1)[1]}")
                for day in sorted(selected)
            )
        return t.text("web.admin.email_group.calendar_short", days=day_label, time=time)
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


# Stable kinds stored in sources.last_fetch_error_kind (see beehive.source_health). Anything else,
# including rows written before kinds were recorded, reads as the generic "error".
_FETCH_ERROR_KINDS = frozenset(
    {"transient", "unsafe_url", "not_found", "access_denied", "too_large", "protocol", "truncated", "error"}
)
_URL_SOURCE_TYPES = frozenset({"shopify_collection", "land_sea_collection"})
_ATTENTION_ORDER = {"failed": 0, "stale": 1, "paused": 2}


def _admin_chapter(tab: str | None) -> str:
    chapter = _ADMIN_TAB_ALIASES.get(tab or "", tab or "")
    return chapter if chapter in _ADMIN_CHAPTERS else "channels"


def _source_state(source: dict) -> str:
    """One word for a Source's current condition, shared by every admin table and note."""
    if source["paused_at"]:
        return "paused"
    if source["last_fetch_error"]:
        return "error"
    if source["last_fetch_at"]:
        return "ok"
    return "never"


_RATE_LIMIT_MARKERS = ("too many requests", "too_many_requests", "rate limit", "rate-limit", "http 429", "error 429")


def _fetch_error_kind(source: dict) -> str:
    kind = source.get("last_fetch_error_kind") or "error"
    if kind not in _FETCH_ERROR_KINDS:
        kind = "error"
    message = (source.get("last_fetch_error") or "").lower()
    if kind in {"error", "transient", "protocol"} and any(m in message for m in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    return kind


def _fetch_error_kind_label(source: dict, t: Localizer) -> str:
    return t.text(f"web.admin.error_kind.{_fetch_error_kind(source)}")


def _source_summary(sources: list[dict], t: Localizer) -> tuple[str, str]:
    """(state, label) for a Channel's Sources as a whole: the worst condition wins, so a single
    failing or paused Source is never hidden behind a healthy count."""
    states = [_source_state(source) for source in sources]
    total = len(states)
    if not total:
        return "never", t.text("web.admin.sources_summary.none")
    if "error" in states:
        return "error", t.text(
            "web.admin.sources_summary.failed", count=total, failed=states.count("error")
        )
    if "paused" in states:
        return "paused", t.text(
            "web.admin.sources_summary.paused", count=total, paused=states.count("paused")
        )
    if "never" in states:
        return "never", t.text(
            "web.admin.sources_summary.never", count=total, pending=states.count("never")
        )
    return "ok", t.text("web.admin.sources_summary.ok", count=total)


def _source_confirmation_value(source: dict, t: Localizer) -> str:
    """What the Owner types to confirm removing a Source: its own name when it has one, otherwise
    the shortest thing that still identifies it (a shop's domain, a subreddit, a query). Typing a
    connector type such as "shopify_collection" identified nothing and read as jargon."""
    name = (source.get("name") or "").strip()
    if name:
        return name
    try:
        config = json.loads(source["config"])
    except (TypeError, ValueError):
        config = {}
    source_type = source["type"]
    if source_type in _URL_SOURCE_TYPES:
        host = urlparse(config.get("collection_url") or "").netloc
        if host:
            return host.removeprefix("www.")
    elif source_type == "reddit_subreddit" and config.get("subreddit"):
        return f"r/{config['subreddit']}"
    elif source_type in {"google_news_query", "hackernews_query"} and config.get("query"):
        return str(config["query"])
    elif source_type == "international_clearance" and config.get("retailer"):
        return RETAILER_LABELS.get(config["retailer"], str(config["retailer"]))
    elif source_type == "hackernews_stories":
        # The feed's display name is translated; the confirmation must not change with language.
        return f"hn/{config.get('feed') or 'top'}"
    return source_display_name(source, t)


def _confirmation_matches(submitted: str, expected: str) -> bool:
    return submitted.strip().casefold() == expected.strip().casefold()


def _split_source_label(source: dict, label: str) -> tuple[str, str]:
    """A collection URL label split into its bold host and quieter path; other labels stay whole."""
    if source["type"] in _URL_SOURCE_TYPES and "/" in label:
        host, _, path = label.partition("/")
        return host, f"/{path}"
    return label, ""


def _channel_attention_items(conn: sqlite3.Connection, t: Localizer, data_dir: str) -> list[dict]:
    """Everything about collection that needs the Owner, worst first: failing Sources, stuck manual
    fetches, then paused Sources. The admin contents rail counts this same list."""
    channels = {channel["id"]: channel for channel in list_channels(conn)}
    items = []
    for channel_id, state in list_manual_trigger_states(data_dir).items():
        if state == "stale" and channel_id in channels:
            items.append(
                {
                    "kind": "stale",
                    "channel_id": channel_id,
                    "channel_name": channels[channel_id]["name"],
                }
            )
    rows = conn.execute(
        """
        SELECT * FROM sources
        WHERE paused_at IS NOT NULL OR last_fetch_error IS NOT NULL
        ORDER BY channel_id, id
        """
    ).fetchall()
    for row in rows:
        source = dict(row)
        channel = channels.get(source["channel_id"])
        if channel is None:
            continue
        items.append(
            {
                "kind": "paused" if source["paused_at"] else "failed",
                "source_id": source["id"],
                "source_label": source_display_name(source, t),
                "channel_id": channel["id"],
                "channel_name": channel["name"],
                "error": source["last_fetch_error"],
                "error_kind_label": (
                    _fetch_error_kind_label(source, t) if source["last_fetch_error"] else None
                ),
                "failures": int(source["consecutive_failures"] or 0),
            }
        )
    items.sort(key=lambda item: _ATTENTION_ORDER[item["kind"]])
    return items


def _email_group_needs_attention(group: dict, default_recipient: ResolvedRecipient) -> bool:
    if group["last_error"]:
        return True
    try:
        return resolve_group_email(group, default_recipient).address is None
    except EmailConfigurationError:
        return True


def _admin_nav(request: Request, t: Localizer) -> dict:
    """The contents rail and running-head clock shared by every signed-in admin page. Each
    problem is counted once, in the chapter where the Owner fixes it."""
    conn = request.state.db
    data_dir = os.path.dirname(request.app.state.db_path)
    default_recipient, _ = _resolve_default_for_admin(conn, t)
    health = _build_system_health_rows(conn, t, data_dir, default_recipient)
    attention = {
        "channels": len(_channel_attention_items(conn, t, data_dir)),
        "groups": sum(
            _email_group_needs_attention(group, default_recipient)
            for group in list_email_groups(conn)
        ),
        "settings": int(choose_model(conn).unavailable is not None),
        "system": sum(
            row["status"] != "ok" for row in health if row["key"] in {"reminders", "research"}
        ),
    }
    return {
        "chapters": [
            {
                "key": key,
                "number": number,
                "label": t.text(f"web.admin.chapter.{key}"),
                "href": f"/admin/?tab={key}",
                "attention": attention[key],
            }
            for number, key in enumerate(_ADMIN_CHAPTERS, start=1)
        ],
        "clock": t.text(
            "web.admin.shell.clock",
            time=host_local_time_label(datetime.now(timezone.utc).isoformat()),
        ),
    }


def _render_admin(
    request: Request,
    t: Localizer,
    template: str,
    context: dict,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a signed-in admin page inside the shared admin shell."""
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {**context, "admin_nav": _admin_nav(request, t)},
        status_code=status_code,
    )


def _channel_kind_label(kind: ChannelKind, t: Localizer) -> str:
    return t.text(f"web.channel.{kind.value}_label")


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
                    _short_time_label(group["last_sent_at"], now, t) if group["last_sent_at"] else None
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
                "next_due_short": _short_time_label(next_email_group_due_at(group, now), now, t),
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
            "key": key,
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
        "refreshed": _short_time_label(catalog.refreshed_at, now, t) if catalog else None,
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
        view["requested"] = _short_time_label(state.requested_at, now, t)
    elif phase == "failed":
        kind = (
            state.error.value
            if state.error is not None
            else "interrupted" if state.status is RefreshStatus.RUNNING else "failed"
        )
        view["failed_at"] = _short_time_label(
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


def _admin_source_label(source: dict, t: Localizer) -> str:
    return derived_source_label(source, t)


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
    return _render_admin(
        request,
        t,
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
            "schedule_timezones": _SCHEDULE_TIMEZONES,
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
    return _render_admin(
        request,
        t,
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
            "schedule_timezones": _SCHEDULE_TIMEZONES,
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
            "schedule_label": _email_group_schedule_label(group, t),
            "schedule_short": _email_group_schedule_label(group, t, short=True),
            "last_sent_short": (
                _short_time_label(group["last_sent_at"], datetime.now(timezone.utc), t)
                if group["last_sent_at"]
                else None
            ),
            "next_due_short": _short_time_label(
                next_email_group_due_at(group, datetime.now(timezone.utc)),
                datetime.now(timezone.utc),
                t,
            ),
            "unsaved_submission": status_code >= 400,
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
    return _render_admin(
        request,
        t,
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
