from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from beehive.db import app_state
from beehive.db.connection import connect, init_schema
from beehive.featured import (
    DEFAULT_FEATURED_WINDOW_DAYS,
    InvalidFeaturedWindowError,
    featured_utc_bounds,
    load_featured_window_days,
    save_featured_window_days,
)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    return connection


def test_featured_window_defaults_to_one_rolling_day(conn):
    assert DEFAULT_FEATURED_WINDOW_DAYS == 1
    assert load_featured_window_days(conn) == 1


def test_featured_window_round_trips_through_app_state(conn):
    save_featured_window_days(conn, 7)

    assert load_featured_window_days(conn) == 7
    assert app_state.get(conn, "featured_window_days") == "7"


@pytest.mark.parametrize("days", [0, 31])
def test_featured_window_rejects_out_of_range_values(conn, days):
    with pytest.raises(InvalidFeaturedWindowError):
        save_featured_window_days(conn, days)


def test_featured_bounds_cover_a_rolling_period_including_the_current_second():
    now = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)

    assert featured_utc_bounds(now, 3) == (
        "2026-07-11T12:30:00",
        "2026-07-14T12:30:01",
    )


@pytest.mark.parametrize("now,expected", [
    (
        datetime(2026, 9, 27, 4, 30, tzinfo=ZoneInfo("Pacific/Auckland")),
        ("2026-09-25T15:30:00", "2026-09-26T15:30:01"),
    ),
    (
        datetime(2026, 4, 5, 4, 30, tzinfo=ZoneInfo("Pacific/Auckland")),
        ("2026-04-03T16:30:00", "2026-04-04T16:30:01"),
    ),
])
def test_one_day_is_24_hours_across_daylight_saving_changes(now, expected):
    assert featured_utc_bounds(now, 1) == expected
