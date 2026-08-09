from datetime import datetime, timedelta, timezone

import pytest

from beehive.scheduling import (
    ChannelFetchSchedule,
    email_group_is_due,
    next_channel_fetch_at,
    next_email_group_due_at,
    source_is_due,
)

DAILY_5AM = ChannelFetchSchedule.daily(timezone_name="Pacific/Auckland", time_text="05:00")


def _source(last_fetch_at, *, last_scheduled_slot_at=None, **overrides):
    source = {
        "last_fetch_at": last_fetch_at,
        "last_scheduled_slot_at": last_scheduled_slot_at,
        "last_scheduled_error_at": None,
    }
    source.update(overrides)
    return source


def _group(last_sent_at, send_interval_hours=24, last_checked_at=None):
    return {
        "last_sent_at": last_sent_at,
        "last_checked_at": last_checked_at,
        "send_interval_hours": send_interval_hours,
    }


def test_never_fetched_source_is_due():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    assert source_is_due(_source(None), ChannelFetchSchedule.interval(24), now)


def test_recent_source_is_not_due():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=23)).isoformat())
    assert not source_is_due(source, ChannelFetchSchedule.interval(24), now)


def test_source_is_due_at_its_interval_boundary():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=24)).isoformat())
    assert source_is_due(source, ChannelFetchSchedule.interval(24), now)


def test_source_is_due_within_grace_before_its_interval_boundary():
    # Regression: a 24h-interval source's due boundary recurs within a couple of seconds of
    # the fetch timer's own trigger instant every cycle, so run-to-run jitter (container boot
    # time, or slow AI-ranking for Channels processed earlier in the same cycle) can make `now`
    # land a hair before `due_at`. Seen in production: due_at trailed `now` by ~1.2s, so the
    # source was skipped for a full extra day instead of just a few seconds.
    now = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=24) + timedelta(seconds=2)).isoformat())
    assert source_is_due(source, ChannelFetchSchedule.interval(24), now)


def test_source_is_not_due_outside_the_grace_window():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=24) + timedelta(minutes=10)).isoformat())
    assert not source_is_due(source, ChannelFetchSchedule.interval(24), now)


def test_never_sent_email_group_is_due():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    assert email_group_is_due(_group(None), now)


def test_recently_sent_email_group_is_not_due():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    group = _group((now - timedelta(hours=23)).isoformat(), send_interval_hours=24)
    assert not email_group_is_due(group, now)


def test_email_group_is_due_at_its_interval_boundary():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    group = _group((now - timedelta(hours=6)).isoformat(), send_interval_hours=6)
    assert email_group_is_due(group, now)


def test_email_group_is_due_within_grace_before_its_interval_boundary():
    now = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
    group = _group(
        (now - timedelta(hours=24) + timedelta(seconds=2)).isoformat(), send_interval_hours=24)
    assert email_group_is_due(group, now)


def test_email_group_is_not_due_outside_the_grace_window():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    group = _group(
        (now - timedelta(hours=24) + timedelta(minutes=10)).isoformat(), send_interval_hours=24)
    assert not email_group_is_due(group, now)


def test_recent_empty_email_group_check_controls_the_next_due_time():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    group = _group(
        (now - timedelta(days=2)).isoformat(),
        send_interval_hours=24,
        last_checked_at=(now - timedelta(hours=1)).isoformat(),
    )

    assert not email_group_is_due(group, now)


def test_calendar_group_becomes_due_after_its_local_slot():
    group = {
        "schedule_mode": "calendar",
        "schedule_timezone": "Pacific/Auckland",
        "schedule_time": "09:00",
        "schedule_weekdays": "0",
        "created_at": "2026-07-12T20:00:00+00:00",
        "last_checked_at": None,
        "last_sent_at": None,
    }

    assert not email_group_is_due(
        group,
        datetime(2026, 7, 12, 20, 59, tzinfo=timezone.utc),
    )
    assert email_group_is_due(
        group,
        datetime(2026, 7, 12, 21, 1, tzinfo=timezone.utc),
    )


def test_new_calendar_group_does_not_execute_a_slot_before_creation():
    group = {
        "schedule_mode": "calendar",
        "schedule_timezone": "Pacific/Auckland",
        "schedule_time": "09:00",
        "schedule_weekdays": "0",
        "created_at": "2026-07-12T22:00:00+00:00",
        "last_checked_at": None,
        "last_sent_at": None,
    }
    now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)

    assert not email_group_is_due(group, now)
    assert next_email_group_due_at(group, now) == datetime(
        2026,
        7,
        19,
        21,
        0,
        tzinfo=timezone.utc,
    )


def test_calendar_schedule_uses_its_configured_timezone():
    group = {
        "schedule_mode": "calendar",
        "schedule_timezone": "America/New_York",
        "schedule_time": "09:00",
        "schedule_weekdays": "0",
        "created_at": "2026-07-13T12:00:00+00:00",
        "last_checked_at": None,
        "last_sent_at": None,
    }

    assert next_email_group_due_at(
        group,
        datetime(2026, 7, 13, 12, 30, tzinfo=timezone.utc),
    ) == datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc)


def test_next_channel_fetch_uses_earliest_source_due_slot():
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    sources = [
        _source("2026-07-13T07:00:00+00:00"),
        _source("2026-07-13T09:52:00+00:00"),
    ]

    assert next_channel_fetch_at(sources, ChannelFetchSchedule.interval(24), now) == datetime(
        2026, 7, 14, 7, 0, tzinfo=timezone.utc
    )


def test_never_fetched_source_targets_the_next_timer_slot():
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    assert next_channel_fetch_at([_source(None)], ChannelFetchSchedule.interval(24), now) == datetime(
        2026, 7, 13, 10, 0, tzinfo=timezone.utc
    )


def test_due_source_at_a_timer_boundary_keeps_the_current_minute():
    now = datetime(2026, 7, 13, 4, 0, 0, 500000, tzinfo=timezone.utc)
    assert next_channel_fetch_at([_source(None)], ChannelFetchSchedule.interval(24), now) == datetime(
        2026, 7, 13, 4, 0, tzinfo=timezone.utc
    )


def test_due_source_keeps_zero_countdown_for_the_whole_boundary_minute():
    now = datetime(2026, 7, 13, 4, 0, 59, tzinfo=timezone.utc)
    assert next_channel_fetch_at([_source(None)], ChannelFetchSchedule.interval(24), now) == datetime(
        2026, 7, 13, 4, 0, tzinfo=timezone.utc
    )


def test_future_due_time_after_a_boundary_uses_the_following_slot():
    now = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)
    source = _source("2026-07-12T04:00:30+00:00")
    assert next_channel_fetch_at([source], ChannelFetchSchedule.interval(24), now) == datetime(
        2026, 7, 13, 4, 0, tzinfo=timezone.utc
    )


def test_channel_without_sources_has_no_next_fetch():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    assert next_channel_fetch_at([], ChannelFetchSchedule.interval(24), now) is None


def test_paused_sources_do_not_change_the_next_fetch_preview():
    now = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    sources = [
        _source(None, paused_at="2026-07-13T19:00:00+00:00"),
        _source(
            "2026-07-13T17:02:00+00:00",
            last_scheduled_slot_at="2026-07-13T17:00:00+00:00",
        ),
    ]

    assert next_channel_fetch_at(sources, DAILY_5AM, now) == datetime(
        2026, 7, 14, 17, 0, tzinfo=timezone.utc
    )


def test_channel_with_only_paused_sources_has_no_next_fetch():
    now = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    source = _source(None, paused_at="2026-07-13T19:00:00+00:00")

    assert next_channel_fetch_at([source], DAILY_5AM, now) is None


def test_scheduling_rejects_naive_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        source_is_due(
            _source(None), ChannelFetchSchedule.interval(24), datetime(2026, 7, 13, 10, 0)
        )


def test_scheduling_rejects_invalid_stored_timestamp():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        source_is_due(_source("not-a-timestamp"), ChannelFetchSchedule.interval(24), now)


def test_daily_channel_is_due_once_its_local_slot_has_arrived():
    # 05:00 Pacific/Auckland is 17:00 UTC the previous day in July (NZST, UTC+12).
    before_slot = datetime(2026, 7, 13, 16, 59, tzinfo=timezone.utc)
    after_slot = datetime(2026, 7, 13, 17, 1, tzinfo=timezone.utc)
    source = _source(
        "2026-07-12T17:05:00+00:00",
        last_scheduled_slot_at="2026-07-12T17:00:00+00:00",
    )

    assert not source_is_due(source, DAILY_5AM, before_slot)
    assert source_is_due(source, DAILY_5AM, after_slot)


def test_daily_channel_without_a_slot_checkpoint_is_due_immediately():
    now = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)

    assert source_is_due(_source("2026-07-13T02:00:00+00:00"), DAILY_5AM, now)


def test_daily_channel_does_not_drift_when_a_run_finishes_late():
    # The run for the 05:00 slot started late and finished at 06:40 local, but the checkpoint
    # records the slot it served, so the next slot is still 05:00 the following day and not
    # 06:40 + 24h.
    source = _source(
        "2026-07-13T18:40:00+00:00",
        last_scheduled_slot_at="2026-07-13T17:00:00+00:00",
    )

    assert not source_is_due(
        source, DAILY_5AM, datetime(2026, 7, 14, 16, 59, tzinfo=timezone.utc)
    )
    assert source_is_due(source, DAILY_5AM, datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc))


def test_daily_channel_slot_follows_its_configured_timezone():
    tokyo = ChannelFetchSchedule.daily(timezone_name="Asia/Tokyo", time_text="05:00")
    # 05:00 Asia/Tokyo is 20:00 UTC the previous day.
    source = _source(None, last_scheduled_slot_at="2026-07-12T20:00:00+00:00")

    assert not source_is_due(source, tokyo, datetime(2026, 7, 13, 19, 59, tzinfo=timezone.utc))
    assert source_is_due(source, tokyo, datetime(2026, 7, 13, 20, 1, tzinfo=timezone.utc))


def test_daily_channel_catches_up_only_the_latest_missed_slot():
    # The host was offline for three days. The single unserved anchor is yesterday's slot, so one
    # catch-up run clears it; a feed snapshot cannot be rebuilt for the older slots anyway.
    schedule = DAILY_5AM
    source = _source(None, last_scheduled_slot_at="2026-07-10T17:00:00+00:00")
    now = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)

    assert source_is_due(source, schedule, now)
    served = dict(source, last_scheduled_slot_at=schedule.slot_for(now).isoformat())
    assert not source_is_due(served, schedule, now)


def test_slot_for_is_none_in_interval_mode():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)

    assert ChannelFetchSchedule.interval(3).slot_for(now) is None
    assert DAILY_5AM.slot_for(now) == datetime(2026, 7, 12, 17, 0, tzinfo=timezone.utc)


def test_failed_scheduled_run_retries_after_its_backoff_without_moving_the_anchor():
    # A scheduled run failed at 17:05 UTC: the slot checkpoint was never advanced, so the Source
    # stays due, but the backoff stops a 15-minute timer from hammering a broken site.
    failed = _source(
        "2026-07-12T17:05:00+00:00",
        last_scheduled_slot_at="2026-07-12T17:00:00+00:00",
        last_scheduled_error_at="2026-07-13T17:05:00+00:00",
    )

    assert not source_is_due(failed, DAILY_5AM, datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc))
    assert source_is_due(failed, DAILY_5AM, datetime(2026, 7, 13, 18, 10, tzinfo=timezone.utc))


def test_failed_interval_run_also_waits_for_its_backoff():
    schedule = ChannelFetchSchedule.interval(3)
    failed = _source(
        "2026-07-13T06:00:00+00:00",
        last_scheduled_error_at="2026-07-13T09:00:00+00:00",
    )

    assert not source_is_due(failed, schedule, datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc))
    assert source_is_due(failed, schedule, datetime(2026, 7, 13, 10, 5, tzinfo=timezone.utc))


def test_source_without_a_scheduled_error_is_never_held_back_by_the_retry_backoff():
    schedule = ChannelFetchSchedule.interval(3)
    source = _source(
        "2026-07-13T06:00:00+00:00",
    )

    assert source_is_due(source, schedule, datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc))


def test_next_daily_fetch_previews_tomorrows_slot_once_todays_ran():
    source = _source(
        "2026-07-13T17:02:00+00:00",
        last_scheduled_slot_at="2026-07-13T17:00:00+00:00",
    )

    assert next_channel_fetch_at(
        [source], DAILY_5AM, datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    ) == datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)


def test_next_daily_fetch_previews_the_next_timer_tick_when_a_slot_is_pending():
    source = _source(None, last_scheduled_slot_at="2026-07-12T17:00:00+00:00")

    assert next_channel_fetch_at(
        [source], DAILY_5AM, datetime(2026, 7, 13, 17, 3, tzinfo=timezone.utc)
    ) == datetime(2026, 7, 13, 17, 15, tzinfo=timezone.utc)


def test_next_daily_fetch_previews_a_pending_retry_rather_than_tomorrow():
    failed = _source(
        "2026-07-12T17:05:00+00:00",
        last_scheduled_slot_at="2026-07-12T17:00:00+00:00",
        last_scheduled_error_at="2026-07-13T17:05:00+00:00",
    )

    assert next_channel_fetch_at(
        [failed], DAILY_5AM, datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)
    ) == datetime(2026, 7, 13, 18, 15, tzinfo=timezone.utc)


def test_dst_gap_schedule_never_returns_a_future_latest_slot():
    schedule = ChannelFetchSchedule.daily(
        timezone_name="Pacific/Auckland",
        time_text="02:30",
    )
    # Auckland skips from 02:00 to 03:00 on 2026-09-27. The imaginary 02:30 local slot maps to
    # 14:30 UTC, so at 14:00 UTC the latest arrived slot must still be the previous day's.
    now = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)

    assert schedule.slot_for(now) < now


def test_channel_fetch_schedule_reads_its_stored_columns():
    schedule = ChannelFetchSchedule.from_channel(
        {
            "fetch_interval_hours": 6,
            "fetch_schedule_mode": "calendar",
            "fetch_schedule_timezone": "Europe/Berlin",
            "fetch_schedule_time": "07:30",
        }
    )

    assert schedule.calendar is not None
    assert (schedule.calendar.hour, schedule.calendar.minute) == (7, 30)
    assert str(schedule.calendar.zone) == "Europe/Berlin"


def test_channel_fetch_schedule_defaults_to_the_stored_interval():
    schedule = ChannelFetchSchedule.from_channel(
        {
            "fetch_interval_hours": 3,
            "fetch_schedule_mode": "interval",
            "fetch_schedule_timezone": "Pacific/Auckland",
            "fetch_schedule_time": "05:00",
        }
    )

    assert schedule.calendar is None
    assert schedule.interval_hours == 3


def test_channel_fetch_schedule_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="unknown schedule mode"):
        ChannelFetchSchedule.from_channel(
            {"fetch_interval_hours": 3, "fetch_schedule_mode": "cron"}
        )


def test_channel_fetch_schedule_rejects_an_unknown_timezone():
    with pytest.raises(ValueError, match="unknown schedule timezone"):
        ChannelFetchSchedule.daily(timezone_name="Mars/Olympus", time_text="05:00")


def test_channel_fetch_schedule_rejects_an_invalid_time():
    with pytest.raises(ValueError, match="valid HH:MM"):
        ChannelFetchSchedule.daily(timezone_name="Pacific/Auckland", time_text="25:00")
