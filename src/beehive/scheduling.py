"""When recurring work is due, for both Channel fetching and Email Group delivery.

Two schedule shapes exist, and both surfaces share them:

* **interval** -- "every N hours", anchored on a checkpoint (the Source's last successful fetch,
  the group's last check/send). Elapsed-duration scheduling, not wall-clock.
* **calendar** -- "at HH:MM in an IANA timezone, on these weekdays". The anchor is the intended
  slot itself, never the moment the previous run finished, so a late or slow run cannot push
  tomorrow's slot later (the drift that made a 24h "daily" interval creep across the day).

A calendar schedule is due when the latest slot at or before `now` is newer than the checkpoint
recording the last slot that was actually carried out. For a Channel that checkpoint is
`sources.last_scheduled_slot_at`, which is deliberately distinct from `last_fetch_at`:
`last_fetch_at` stays the freshness watermark ("when did this Source last succeed"), while the slot
checkpoint answers "which calendar slot has already been served". A manual force-fetch writes the
former and never the latter, so running a Channel by hand does not move its automatic schedule.

Only the latest missed slot is ever caught up: an offline host that missed three days of slots
fetches once when it comes back, not three times -- a feed snapshot cannot be reconstructed
retroactively, so replaying older slots would only duplicate work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The host's display timezone, used by every "when did/will this happen" label in the web UI and
# email. Distinct from a schedule's own timezone, which each Channel and Email Group configures.
HOST_TZ = ZoneInfo("Pacific/Auckland")
DEFAULT_SCHEDULE_TIMEZONE = "Pacific/Auckland"
DEFAULT_CHANNEL_FETCH_TIME = "05:00"
_ALL_WEEKDAYS = frozenset(range(7))
# The fetch timer's polling cadence (deploy/quadlet/beehive-fetch.timer). Kept much finer than the
# smallest schedule an Owner can pick so the polling granularity is never visible in the product:
# a calendar slot runs at the first tick at or after its wall-clock time, within this window.
TIMER_INTERVAL = timedelta(minutes=15)
# Absorbs run-to-run jitter (container boot time, or AI-ranking calls for Channels processed
# earlier in the same fetch cycle -- see collector/run_cycle.py) in the interval due-time check.
# Without this, a Source whose interval is an exact multiple of the fetch timer's cadence
# recomputes its due boundary within a couple of seconds of the timer's own trigger instant every
# cycle. Jitter of just a second or two can then push `now` a hair before `due_at` and skip the
# whole cycle. Confirmed in production on the old 3-hour timer: a 24h-interval source's due_at
# trailed `now` by ~1.2s at one fetch cycle, so it was skipped for a full extra day.
_DUE_GRACE = timedelta(minutes=5)
# How long a Source that failed its last attempt waits before the scheduler offers it again.
# The fetch timer used to run every 3 hours, so the timer itself bounded retries of a broken
# Source; at a 15-minute cadence it no longer does, and a Source failing all day would be
# re-attempted 96 times. Retry is deliberately independent of the schedule (it never moves the
# calendar anchor) -- it only stops a failing Source from hammering an upstream site.
FAILED_ATTEMPT_RETRY_BACKOFF = timedelta(hours=1)
_FETCH_STATUS_ERROR = "error"


class ScheduleMode(StrEnum):
    INTERVAL = "interval"
    CALENDAR = "calendar"


def require_schedule_mode(value: str) -> ScheduleMode:
    try:
        return ScheduleMode(value)
    except ValueError as exc:
        raise ValueError(f"unknown schedule mode: {value!r}") from exc


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduling needs a timezone-aware datetime")


def _as_aware_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CalendarSchedule:
    """A wall-clock recurrence: HH:MM in one IANA timezone on a set of weekdays.

    Shared by Email Groups (which pick their own weekdays) and Channels (which are daily, so they
    pass every weekday), so both surfaces resolve slots through exactly the same arithmetic.
    """

    zone: ZoneInfo
    weekdays: frozenset[int]
    hour: int
    minute: int

    @classmethod
    def parse(
        cls,
        *,
        timezone_name: str | None,
        time_text: str | None,
        weekdays_text: str | None,
        default_time: str,
    ) -> CalendarSchedule:
        try:
            zone = ZoneInfo(timezone_name or DEFAULT_SCHEDULE_TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown schedule timezone: {timezone_name!r}") from exc
        weekday_text = weekdays_text or "0,1,2,3,4,5,6"
        weekdays = frozenset(int(value) for value in weekday_text.split(",") if value != "")
        if not weekdays or not weekdays <= _ALL_WEEKDAYS:
            raise ValueError("a calendar schedule needs at least one valid weekday")
        try:
            hour_text, minute_text = (time_text or default_time).split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("a calendar schedule needs a valid HH:MM time") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("a calendar schedule needs a valid HH:MM time")
        return cls(zone=zone, weekdays=weekdays, hour=hour, minute=minute)

    def _slot(self, now: datetime, *, direction: int) -> datetime:
        local_now = now.astimezone(self.zone)
        for day_offset in range(8):
            candidate_date = local_now.date() + timedelta(days=direction * day_offset)
            if candidate_date.weekday() not in self.weekdays:
                continue
            candidate = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                self.hour,
                self.minute,
                tzinfo=self.zone,
            )
            if direction < 0 and candidate <= local_now:
                return candidate.astimezone(timezone.utc)
            if direction > 0 and candidate > local_now:
                return candidate.astimezone(timezone.utc)
        raise RuntimeError("could not calculate a calendar slot")

    def latest_slot_at_or_before(self, now: datetime) -> datetime:
        """The most recent slot that has already arrived -- the schedule's anchor."""
        return self._slot(now, direction=-1)

    def next_slot_after(self, now: datetime) -> datetime:
        return self._slot(now, direction=1)


@dataclass(frozen=True)
class ChannelFetchSchedule:
    """A Channel's fetch cadence, resolved from its stored columns once per cycle or render.

    Building it validates the stored mode, timezone and time, so a corrupt row fails loudly at the
    edge instead of silently falling back to some other cadence.
    """

    mode: ScheduleMode
    interval_hours: int
    calendar: CalendarSchedule | None

    @classmethod
    def interval(cls, interval_hours: int) -> ChannelFetchSchedule:
        if interval_hours < 1:
            raise ValueError("an interval schedule needs at least one hour")
        return cls(mode=ScheduleMode.INTERVAL, interval_hours=interval_hours, calendar=None)

    @classmethod
    def daily(
        cls,
        *,
        timezone_name: str,
        time_text: str,
        interval_hours: int = 24,
    ) -> ChannelFetchSchedule:
        return cls(
            mode=ScheduleMode.CALENDAR,
            interval_hours=interval_hours,
            calendar=CalendarSchedule.parse(
                timezone_name=timezone_name,
                time_text=time_text,
                weekdays_text=None,
                default_time=DEFAULT_CHANNEL_FETCH_TIME,
            ),
        )

    @classmethod
    def from_channel(cls, channel: dict) -> ChannelFetchSchedule:
        mode = require_schedule_mode(
            channel.get("fetch_schedule_mode") or ScheduleMode.INTERVAL.value
        )
        interval_hours = int(channel["fetch_interval_hours"])
        if mode is ScheduleMode.INTERVAL:
            return cls.interval(interval_hours)
        return cls.daily(
            timezone_name=channel.get("fetch_schedule_timezone") or DEFAULT_SCHEDULE_TIMEZONE,
            time_text=channel.get("fetch_schedule_time") or DEFAULT_CHANNEL_FETCH_TIME,
            interval_hours=interval_hours,
        )

    def slot_for(self, now: datetime) -> datetime | None:
        """The calendar slot a run starting at `now` is serving, or None in interval mode."""
        _require_aware(now)
        if self.calendar is None:
            return None
        return self.calendar.latest_slot_at_or_before(now)


def _retry_backoff_until(source: dict) -> datetime | None:
    """When a Source whose last attempt failed may be attempted again, or None if it did not."""
    if source.get("last_fetch_status") != _FETCH_STATUS_ERROR:
        return None
    last_attempt_at = source.get("last_attempt_at")
    if not last_attempt_at:
        return None
    return _as_aware_utc(last_attempt_at) + FAILED_ATTEMPT_RETRY_BACKOFF


def _calendar_of(schedule: ChannelFetchSchedule) -> CalendarSchedule:
    if schedule.calendar is None:
        raise ValueError("a calendar fetch schedule needs calendar settings")
    return schedule.calendar


def source_is_due(
    source: dict,
    schedule: ChannelFetchSchedule,
    now: datetime,
) -> bool:
    """Whether the scheduler should fetch this Source now.

    Calendar mode compares the latest arrived slot with `last_scheduled_slot_at`, so a slot that
    has not been served yet stays due -- a failed scheduled run therefore remains eligible on a
    later tick (subject to the retry backoff) without its anchor moving. Interval mode is
    unchanged: `last_fetch_at + interval_hours`, with the jitter grace above.
    """
    _require_aware(now)
    utc_now = now.astimezone(timezone.utc)
    backoff_until = _retry_backoff_until(source)
    if backoff_until is not None and backoff_until > utc_now:
        return False
    if schedule.mode is ScheduleMode.CALENDAR:
        calendar = _calendar_of(schedule)
        checkpoint = source.get("last_scheduled_slot_at")
        if not checkpoint:
            return True
        return calendar.latest_slot_at_or_before(utc_now) > _as_aware_utc(checkpoint)
    last_fetch_at = source.get("last_fetch_at")
    if not last_fetch_at:
        return True
    due_at = _as_aware_utc(last_fetch_at) + timedelta(hours=schedule.interval_hours)
    return due_at - _DUE_GRACE <= utc_now


def _email_group_checkpoint(group: dict) -> datetime | None:
    checkpoints = [
        _as_aware_utc(value)
        for value in (group.get("last_checked_at"), group.get("last_sent_at"))
        if value
    ]
    return max(checkpoints) if checkpoints else None


def _email_group_calendar(group: dict) -> CalendarSchedule:
    return CalendarSchedule.parse(
        timezone_name=group.get("schedule_timezone"),
        time_text=group.get("schedule_time"),
        weekdays_text=group.get("schedule_weekdays"),
        default_time="09:00",
    )


def email_group_is_due(group: dict, now: datetime) -> bool:
    """Return whether a group has crossed its interval or latest local calendar slot."""
    _require_aware(now)
    checkpoint = _email_group_checkpoint(group)
    mode = require_schedule_mode(group.get("schedule_mode") or ScheduleMode.INTERVAL.value)
    if mode is ScheduleMode.CALENDAR:
        if checkpoint is None and group.get("created_at"):
            checkpoint = _as_aware_utc(group["created_at"])
        latest_slot = _email_group_calendar(group).latest_slot_at_or_before(now)
        return checkpoint is None or latest_slot > checkpoint
    if checkpoint is None:
        return True
    due_at = checkpoint + timedelta(hours=group["send_interval_hours"])
    return due_at - _DUE_GRACE <= now.astimezone(timezone.utc)


def next_email_group_due_at(group: dict, now: datetime) -> datetime:
    _require_aware(now)
    checkpoint = _email_group_checkpoint(group)
    mode = require_schedule_mode(group.get("schedule_mode") or ScheduleMode.INTERVAL.value)
    if mode is ScheduleMode.CALENDAR:
        calendar = _email_group_calendar(group)
        if checkpoint is None and group.get("created_at"):
            checkpoint = _as_aware_utc(group["created_at"])
        latest_slot = calendar.latest_slot_at_or_before(now)
        if checkpoint is None or latest_slot > checkpoint:
            return latest_slot
        return calendar.next_slot_after(now)
    if checkpoint is None:
        return now.astimezone(timezone.utc)
    return checkpoint + timedelta(hours=group["send_interval_hours"])


def _next_timer_slot_at_or_after(
    target: datetime,
    *,
    include_current_minute: bool,
) -> datetime:
    """Round a wanted start time up to the fetch timer's next polling tick.

    The timer only wakes on TIMER_INTERVAL boundaries, so this is when the work will really begin.
    include_current_minute keeps a countdown at zero for the whole minute a tick fires in, rather
    than jumping a full interval ahead the instant the boundary passes.

    The tick grid is computed in UTC even though systemd fires on host-local wall-clock minutes:
    every real UTC offset is a whole number of 15 minutes, so the boundaries are the same instants
    either way, and staying in UTC keeps the arithmetic correct across DST transitions.
    """
    utc_target = target.astimezone(timezone.utc)
    interval_minutes = int(TIMER_INTERVAL.total_seconds() // 60)
    tick = utc_target.replace(
        minute=(utc_target.minute // interval_minutes) * interval_minutes,
        second=0,
        microsecond=0,
    )
    if tick == utc_target:
        return tick
    if include_current_minute and utc_target.minute % interval_minutes == 0:
        return tick
    return tick + TIMER_INTERVAL


def _source_next_eligible_at(
    source: dict,
    schedule: ChannelFetchSchedule,
    utc_now: datetime,
) -> datetime:
    """The earliest moment the scheduler would accept this Source, ignoring the timer's cadence.

    A pending retry counts: a Source whose slot is still unserved but is inside its failure
    backoff is next eligible when that backoff expires, not at tomorrow's slot.
    """
    if schedule.mode is ScheduleMode.CALENDAR:
        calendar = _calendar_of(schedule)
        checkpoint = source.get("last_scheduled_slot_at")
        latest_slot = calendar.latest_slot_at_or_before(utc_now)
        if not checkpoint or latest_slot > _as_aware_utc(checkpoint):
            eligible_at = utc_now
        else:
            eligible_at = calendar.next_slot_after(utc_now)
    else:
        last_fetch_at = source.get("last_fetch_at")
        eligible_at = (
            utc_now
            if not last_fetch_at
            else _as_aware_utc(last_fetch_at) + timedelta(hours=schedule.interval_hours)
        )
    backoff_until = _retry_backoff_until(source)
    if backoff_until is not None and backoff_until > eligible_at:
        return backoff_until
    return eligible_at


def next_channel_fetch_at(
    sources: list[dict],
    schedule: ChannelFetchSchedule,
    now: datetime,
) -> datetime | None:
    """When this Channel's Sources will next actually be fetched, or None without Sources.

    Always expressed as a real timer tick, so the preview matches what the host will do rather
    than the abstract schedule (a 05:07 daily slot runs at the 05:15 tick).
    """
    _require_aware(now)
    if not sources:
        return None

    utc_now = now.astimezone(timezone.utc)
    earliest_eligible = min(
        _source_next_eligible_at(source, schedule, utc_now) for source in sources
    )
    return _next_timer_slot_at_or_after(
        max(earliest_eligible, utc_now),
        include_current_minute=earliest_eligible <= utc_now,
    )
