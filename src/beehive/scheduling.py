from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HOST_TZ = ZoneInfo("Pacific/Auckland")
_FETCH_HOURS = (1, 4, 7, 10, 13, 16, 19, 22)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduling needs a timezone-aware datetime")


def _as_aware_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def source_is_due(source: dict, interval_hours: int, now: datetime) -> bool:
    _require_aware(now)
    last_fetch_at = source.get("last_fetch_at")
    if not last_fetch_at:
        return True
    due_at = _as_aware_utc(last_fetch_at) + timedelta(hours=interval_hours)
    return due_at <= now.astimezone(timezone.utc)


def _next_timer_slot_at_or_after(
    target: datetime,
    *,
    include_current_minute: bool,
) -> datetime:
    local_target = target.astimezone(HOST_TZ)
    if (
        include_current_minute
        and local_target.hour in _FETCH_HOURS
        and local_target.minute == 0
    ):
        return local_target.replace(second=0, microsecond=0).astimezone(timezone.utc)

    for day_offset in range(2):
        candidate_day = (local_target + timedelta(days=day_offset)).date()
        for hour in _FETCH_HOURS:
            candidate = datetime(
                candidate_day.year,
                candidate_day.month,
                candidate_day.day,
                hour,
                tzinfo=HOST_TZ,
            )
            if candidate >= local_target:
                return candidate.astimezone(timezone.utc)
    raise RuntimeError("could not calculate the next fetch timer slot")


def next_channel_fetch_at(
    sources: list[dict],
    interval_hours: int,
    now: datetime,
) -> datetime | None:
    _require_aware(now)
    if not sources:
        return None

    utc_now = now.astimezone(timezone.utc)
    due_times = [
        utc_now
        if not source.get("last_fetch_at")
        else _as_aware_utc(source["last_fetch_at"]) + timedelta(hours=interval_hours)
        for source in sources
    ]
    earliest_due = min(due_times)
    target = max(earliest_due, utc_now)
    return _next_timer_slot_at_or_after(
        target,
        include_current_minute=earliest_due <= utc_now,
    )
