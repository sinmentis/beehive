from datetime import datetime, timezone

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


def test_featured_window_defaults_to_three_days(conn):
    assert DEFAULT_FEATURED_WINDOW_DAYS == 3
    assert load_featured_window_days(conn) == 3


def test_featured_window_round_trips_through_app_state(conn):
    save_featured_window_days(conn, 7)

    assert load_featured_window_days(conn) == 7
    assert app_state.get(conn, "featured_window_days") == "7"


@pytest.mark.parametrize("days", [0, 31])
def test_featured_window_rejects_out_of_range_values(conn, days):
    with pytest.raises(InvalidFeaturedWindowError):
        save_featured_window_days(conn, days)


def test_featured_bounds_cover_three_auckland_calendar_days():
    now = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)

    assert featured_utc_bounds(now, 3) == (
        "2026-07-12T12:00:00",
        "2026-07-15T12:00:00",
    )
