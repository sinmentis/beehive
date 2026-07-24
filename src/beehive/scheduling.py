from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HOST_TZ = ZoneInfo("Pacific/Auckland")
_FETCH_HOURS = (1, 4, 7, 10, 13, 16, 19, 22)
# Absorbs run-to-run jitter (container boot time, or AI-ranking calls for Channels processed
# earlier in the same fetch cycle -- see collector/run_cycle.py) in the due-time check below.
# Without this, a Channel whose fetch_interval_hours is an exact multiple of the fetch timer's
# 3-hour cadence (e.g. the common once-a-day 24h interval) recomputes its due boundary within a
# couple of seconds of the timer's own trigger instant every single cycle. Jitter of just a
# second or two can then push `now` a hair before `due_at` and skip the whole cycle; the next
# chance is a further interval_hours away, not a few seconds, so repeated near-misses drift the
# source's effective fetch time later day over day until it eventually lands after the daily
# digest send and that Channel reports zero new items despite the source being healthy.
# Confirmed in production: a 24h-interval source's due_at trailed `now` by ~1.2s at one fetch
# cycle, so it was skipped for a full extra day.
_DUE_GRACE = timedelta(minutes=5)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduling needs a timezone-aware datetime")


def _as_aware_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _email_group_checkpoint(group: dict) -> datetime | None:
    checkpoints = [
        _as_aware_utc(value)
        for value in (group.get("last_checked_at"), group.get("last_sent_at"))
        if value
    ]
    return max(checkpoints) if checkpoints else None


def _calendar_schedule(group: dict) -> tuple[ZoneInfo, frozenset[int], int, int]:
    zone = ZoneInfo(group.get("schedule_timezone") or "Pacific/Auckland")
    weekday_text = group.get("schedule_weekdays") or "0,1,2,3,4,5,6"
    weekdays = frozenset(int(value) for value in weekday_text.split(",") if value != "")
    if not weekdays or not weekdays <= frozenset(range(7)):
        raise ValueError("email group schedule needs at least one valid weekday")
    try:
        hour_text, minute_text = (group.get("schedule_time") or "09:00").split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("email group schedule needs a valid HH:MM time") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("email group schedule needs a valid HH:MM time")
    return zone, weekdays, hour, minute


def _calendar_slot(
    group: dict,
    now: datetime,
    *,
    direction: int,
) -> datetime:
    zone, weekdays, hour, minute = _calendar_schedule(group)
    local_now = now.astimezone(zone)
    for day_offset in range(8):
        candidate_date = (
            local_now.date() + timedelta(days=direction * day_offset)
        )
        if candidate_date.weekday() not in weekdays:
            continue
        candidate = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            hour,
            minute,
            tzinfo=zone,
        )
        if direction < 0 and candidate <= local_now:
            return candidate.astimezone(timezone.utc)
        if direction > 0 and candidate > local_now:
            return candidate.astimezone(timezone.utc)
    raise RuntimeError("could not calculate an email group calendar slot")


def source_is_due(source: dict, interval_hours: int, now: datetime) -> bool:
    _require_aware(now)
    last_fetch_at = source.get("last_fetch_at")
    if not last_fetch_at:
        return True
    due_at = _as_aware_utc(last_fetch_at) + timedelta(hours=interval_hours)
    return due_at - _DUE_GRACE <= now.astimezone(timezone.utc)


def email_group_is_due(group: dict, now: datetime) -> bool:
    """Return whether a group has crossed its interval or latest local calendar slot."""
    _require_aware(now)
    checkpoint = _email_group_checkpoint(group)
    if group.get("schedule_mode", "interval") == "calendar":
        if checkpoint is None and group.get("created_at"):
            checkpoint = _as_aware_utc(group["created_at"])
        latest_slot = _calendar_slot(group, now, direction=-1)
        return checkpoint is None or latest_slot > checkpoint
    if group.get("schedule_mode", "interval") != "interval":
        raise ValueError(f"unknown email group schedule mode: {group['schedule_mode']!r}")
    if checkpoint is None:
        return True
    due_at = checkpoint + timedelta(hours=group["send_interval_hours"])
    return due_at - _DUE_GRACE <= now.astimezone(timezone.utc)


def next_email_group_due_at(group: dict, now: datetime) -> datetime:
    _require_aware(now)
    checkpoint = _email_group_checkpoint(group)
    if group.get("schedule_mode", "interval") == "calendar":
        if checkpoint is None and group.get("created_at"):
            checkpoint = _as_aware_utc(group["created_at"])
        latest_slot = _calendar_slot(group, now, direction=-1)
        if checkpoint is None or latest_slot > checkpoint:
            return latest_slot
        return _calendar_slot(group, now, direction=1)
    if group.get("schedule_mode", "interval") != "interval":
        raise ValueError(f"unknown email group schedule mode: {group['schedule_mode']!r}")
    if checkpoint is None:
        return now.astimezone(timezone.utc)
    return checkpoint + timedelta(hours=group["send_interval_hours"])


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
