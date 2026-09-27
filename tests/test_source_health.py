from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beehive.connectors.base import TruncatedSnapshotError
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind
from beehive.source_health import classify_fetch_error, is_stale, retry_backoff

NOW = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)


def test_classifies_truncation_http_kinds_and_everything_else():
    assert classify_fetch_error(TruncatedSnapshotError("cap")) == "truncated"
    assert classify_fetch_error(
        ConnectorHttpError(ConnectorHttpErrorKind.ACCESS_DENIED, "403")) == "access_denied"
    assert classify_fetch_error(ValueError("GraphQL error")) == "error"


@pytest.mark.parametrize(
    ("failures", "hours"),
    [(0, 1), (1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 24), (40, 24)],
)
def test_retry_backoff_doubles_and_settles_at_a_day(failures, hours):
    assert retry_backoff(failures) == timedelta(hours=hours)


def _calendar_channel():
    return {"fetch_schedule_mode": "calendar", "fetch_interval_hours": 24}


def test_a_daily_source_is_stale_once_it_misses_a_whole_scheduled_fetch():
    channel = _calendar_channel()
    fresh = {"last_fetch_at": (NOW - timedelta(hours=47)).isoformat()}
    stale = {"last_fetch_at": (NOW - timedelta(hours=49)).isoformat()}

    assert not is_stale(fresh, channel, NOW)
    assert is_stale(stale, channel, NOW)


def test_an_interval_source_goes_stale_after_two_intervals():
    channel = {"fetch_schedule_mode": "interval", "fetch_interval_hours": 3}

    assert not is_stale({"last_fetch_at": (NOW - timedelta(hours=5)).isoformat()}, channel, NOW)
    assert is_stale({"last_fetch_at": (NOW - timedelta(hours=7)).isoformat()}, channel, NOW)


def test_a_source_that_never_succeeded_has_no_stale_data():
    assert not is_stale({"last_fetch_at": None}, _calendar_channel(), NOW)


def test_a_naive_legacy_timestamp_is_read_as_utc():
    stale = {"last_fetch_at": (NOW - timedelta(hours=49)).replace(tzinfo=None).isoformat()}

    assert is_stale(stale, _calendar_channel(), NOW)
