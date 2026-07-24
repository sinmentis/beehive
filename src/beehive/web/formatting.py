"""Small formatting helpers shared between the public Dashboard/Channel-drilldown pages and the
admin Channel list -- both need to show a Channel's freshness ("Fetched 3 hours ago" / "Not
fetched yet") and, since this task, an exact host-local timestamp for hover tooltips (also used
by the admin login page's "last login" line, moved here from its own former private duplicate).

Every user-facing string here goes through a Localizer (translations/web.py) so the same helper
renders correctly for every supported platform language -- host_local_time_label is the one
exception, since a numeric "YYYY-MM-DD HH:MM" timestamp needs no translation."""
from __future__ import annotations

from datetime import datetime, timezone

from beehive.localization import Localizer
from beehive.scheduling import HOST_TZ, next_channel_fetch_at


def _as_aware_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relative_time(iso_str: str, t: Localizer) -> str:
    dt = _as_aware_utc(iso_str)
    minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if minutes < 1:
        return t.text("web.time.just_now")
    if minutes < 60:
        return t.text("web.time.minutes_ago", count=minutes)
    hours = minutes // 60
    if hours < 24:
        return t.text("web.time.hours_ago", count=hours)
    return t.text("web.time.days_ago", count=hours // 24)


def host_local_time_label(iso_str: str) -> str:
    return _as_aware_utc(iso_str).astimezone(HOST_TZ).strftime("%Y-%m-%d %H:%M %Z")


def freshness_label(sources: list[dict], t: Localizer) -> str:
    fetch_times = [s["last_fetch_at"] for s in sources if s["last_fetch_at"]]
    if not fetch_times:
        return t.text("web.freshness.never_fetched")
    return t.text("web.freshness.fetched", time=relative_time(max(fetch_times), t))


def freshness_exact_time(sources: list[dict]) -> str:
    """The exact host-local timestamp backing freshness_label's relative-time string, for a
    title="" tooltip. Mirrors freshness_label's own "most recent fetch across all of a
    Channel's Sources" convention exactly, so the tooltip always describes the same moment
    the headline text is relative to. Empty string (no tooltip) when nothing has been fetched
    yet, matching how _decorate_item's per-item exact_time handles a missing timestamp."""
    fetch_times = [s["last_fetch_at"] for s in sources if s["last_fetch_at"]]
    return host_local_time_label(max(fetch_times)) if fetch_times else ""


def next_fetch_countdown(
    sources: list[dict],
    fetch_interval_hours: int,
    now: datetime,
    t: Localizer,
) -> str:
    next_fetch_at = next_channel_fetch_at(
        sources,
        fetch_interval_hours,
        now,
    )
    if next_fetch_at is None:
        return ""
    minutes = max(
        0,
        int((next_fetch_at - now.astimezone(timezone.utc)).total_seconds() // 60),
    )
    if minutes < 60:
        return t.text("web.countdown.minutes", count=minutes)
    return t.text("web.countdown.hours", count=minutes // 60)


def fetch_stats_label(sources: list[dict], t: Localizer) -> str:
    raw_counts = [s["last_fetch_raw_count"] for s in sources if s["last_fetch_raw_count"] is not None]
    new_counts = [s["last_fetch_new_count"] for s in sources if s["last_fetch_new_count"] is not None]
    if not raw_counts:
        return ""
    total_raw = sum(raw_counts)
    total_new = sum(new_counts)
    ratio = round(100 * total_new / total_raw) if total_raw else 0
    return t.text(
        "web.fetch_stats.summary",
        total_raw=total_raw,
        total_new=total_new,
        ratio=ratio,
    )
