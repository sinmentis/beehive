"""Helpers shared by more than one admin area: the admin page shell, schedule labels, Source
state and confirmation checks."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from beehive.ai.model_selection import (
    choose_model,
)
from beehive.collector.manual_trigger import (
    list_manual_trigger_states,
)
from beehive.connectors.international_clearance import RETAILER_LABELS
from beehive.db.channels import (
    list_channels,
)
from beehive.db.email_groups import (
    list_email_groups,
)
from beehive.domain.channels import ChannelKind
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    resolve_default_email,
    resolve_group_email,
)
from beehive.localization import (
    Localizer,
)
from beehive.scheduling import (
    HOST_TZ,
    ChannelFetchSchedule,
    ScheduleMode,
    next_channel_fetch_at,
    require_schedule_mode,
)
from beehive.web.formatting import (
    host_local_time_label,
    short_time_label,
)
from beehive.source_labels import derived_source_label, source_display_name


# The admin home is four numbered chapters. The old tab names still resolve, so bookmarks and
# redirects written before the chapters were merged keep landing on the right one.
_ADMIN_CHAPTERS = ("channels", "groups", "settings", "system")


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


# Every value here is a translation key (translations/web_admin.py), not display text -- the
# actual copy always comes from the request's Localizer, so the same English exception message
# renders correctly in any supported platform language rather than being hardcoded to one.
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


def _safe_return_path(value: str | None, fallback: str = "/admin/") -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    return value


def _fetch_interval_label(hours: int, t: Localizer) -> str:
    return (
        t.text("web.fetch_interval.daily")
        if hours >= 24
        else t.text("web.fetch_interval.every_n_hours", hours=hours)
    )


def _channel_fetch_schedule_label(channel: dict, t: Localizer) -> str:
    """"Every 3 hours" or "Daily at 05:00 (Pacific/Auckland)", from the Channel's stored mode."""
    if require_schedule_mode(channel["fetch_schedule_mode"]) is ScheduleMode.INTERVAL:
        return _fetch_interval_label(channel["fetch_interval_hours"], t)
    return t.text(
        "web.admin.channel_schedule.daily_summary",
        time=channel["fetch_schedule_time"],
        timezone=channel["fetch_schedule_timezone"],
    )


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
    return short_time_label(next_fetch_at, now, t) if next_fetch_at else None


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
        "home_href": "/admin/",
        "label": t.text("web.nav.admin"),
        "home_aria": t.text("web.admin.shell.home_aria", product=t.text("common.product_name")),
        "toc_aria": t.text("web.admin.shell.toc_aria"),
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
        {**context, "shell_nav": _admin_nav(request, t)},
        status_code=status_code,
    )


def _channel_kind_label(kind: ChannelKind, t: Localizer) -> str:
    return t.text(f"web.channel.{kind.value}_label")


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


def _admin_source_label(source: dict, t: Localizer) -> str:
    return derived_source_label(source, t)
