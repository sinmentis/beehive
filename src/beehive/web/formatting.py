"""Small formatting helpers shared between the public Dashboard/Channel-drilldown pages and the
admin Channel list -- both need to show a Channel's freshness ("上次抓取: 3小时前" / "尚未抓取")
and, since this task, an exact host-local timestamp for hover tooltips (also used by the admin
login page's "last login" line, moved here from its own former private duplicate)."""
from __future__ import annotations

from datetime import datetime, timezone

from beehive.scheduling import HOST_TZ, next_channel_fetch_at


def _as_aware_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relative_time(iso_str: str) -> str:
    dt = _as_aware_utc(iso_str)
    minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def host_local_time_label(iso_str: str) -> str:
    return _as_aware_utc(iso_str).astimezone(HOST_TZ).strftime("%Y-%m-%d %H:%M")


def freshness_label(sources: list[dict]) -> str:
    fetch_times = [s["last_fetch_at"] for s in sources if s["last_fetch_at"]]
    if not fetch_times:
        return "尚未抓取"
    return f"{relative_time(max(fetch_times))}抓取"


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
        return f"{minutes}分钟后抓取"
    return f"{minutes // 60}小时后抓取"


def fetch_stats_label(sources: list[dict]) -> str:
    raw_counts = [s["last_fetch_raw_count"] for s in sources if s["last_fetch_raw_count"] is not None]
    new_counts = [s["last_fetch_new_count"] for s in sources if s["last_fetch_new_count"] is not None]
    if not raw_counts:
        return ""
    total_raw = sum(raw_counts)
    total_new = sum(new_counts)
    ratio = round(100 * total_new / total_raw) if total_raw else 0
    return f"上次抓取 {total_raw} 条，新增 {total_new} 条 ({ratio}%)"
