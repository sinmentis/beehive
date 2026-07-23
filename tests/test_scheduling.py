from datetime import datetime, timedelta, timezone

import pytest

from beehive.scheduling import email_group_is_due, next_channel_fetch_at, source_is_due


def _source(last_fetch_at):
    return {"last_fetch_at": last_fetch_at}


def _group(last_sent_at, send_interval_hours=24, last_checked_at=None):
    return {
        "last_sent_at": last_sent_at,
        "last_checked_at": last_checked_at,
        "send_interval_hours": send_interval_hours,
    }


def test_never_fetched_source_is_due():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    assert source_is_due(_source(None), 24, now)


def test_recent_source_is_not_due():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=23)).isoformat())
    assert not source_is_due(source, 24, now)


def test_source_is_due_at_its_interval_boundary():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=24)).isoformat())
    assert source_is_due(source, 24, now)


def test_source_is_due_within_grace_before_its_interval_boundary():
    # Regression: a 24h-interval source's due boundary recurs within a couple of seconds of
    # the fetch timer's own trigger instant every cycle, so run-to-run jitter (container boot
    # time, or slow AI-ranking for Channels processed earlier in the same cycle) can make `now`
    # land a hair before `due_at`. Seen in production: due_at trailed `now` by ~1.2s, so the
    # source was skipped for a full extra day instead of just a few seconds.
    now = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=24) + timedelta(seconds=2)).isoformat())
    assert source_is_due(source, 24, now)


def test_source_is_not_due_outside_the_grace_window():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    source = _source((now - timedelta(hours=24) + timedelta(minutes=10)).isoformat())
    assert not source_is_due(source, 24, now)


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


def test_next_channel_fetch_uses_earliest_source_due_slot():
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    sources = [
        _source("2026-07-13T07:00:00+00:00"),
        _source("2026-07-13T09:52:00+00:00"),
    ]

    assert next_channel_fetch_at(sources, 24, now) == datetime(
        2026, 7, 14, 7, 0, tzinfo=timezone.utc
    )


def test_never_fetched_source_targets_the_next_timer_slot():
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    assert next_channel_fetch_at([_source(None)], 24, now) == datetime(
        2026, 7, 13, 10, 0, tzinfo=timezone.utc
    )


def test_due_source_at_a_timer_boundary_keeps_the_current_minute():
    now = datetime(2026, 7, 13, 4, 0, 0, 500000, tzinfo=timezone.utc)
    assert next_channel_fetch_at([_source(None)], 24, now) == datetime(
        2026, 7, 13, 4, 0, tzinfo=timezone.utc
    )


def test_due_source_keeps_zero_countdown_for_the_whole_boundary_minute():
    now = datetime(2026, 7, 13, 4, 0, 59, tzinfo=timezone.utc)
    assert next_channel_fetch_at([_source(None)], 24, now) == datetime(
        2026, 7, 13, 4, 0, tzinfo=timezone.utc
    )


def test_future_due_time_after_a_boundary_uses_the_following_slot():
    now = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)
    source = _source("2026-07-12T04:00:30+00:00")
    assert next_channel_fetch_at([source], 24, now) == datetime(
        2026, 7, 13, 7, 0, tzinfo=timezone.utc
    )


def test_channel_without_sources_has_no_next_fetch():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    assert next_channel_fetch_at([], 24, now) is None


def test_scheduling_rejects_naive_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        source_is_due(_source(None), 24, datetime(2026, 7, 13, 10, 0))


def test_scheduling_rejects_invalid_stored_timestamp():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        source_is_due(_source("not-a-timestamp"), 24, now)
