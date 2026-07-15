"""Global configuration and time bounds for the aggregate Featured feed."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from beehive.db import app_state
from beehive.scheduling import HOST_TZ

DEFAULT_FEATURED_WINDOW_DAYS = 3
MIN_FEATURED_WINDOW_DAYS = 1
MAX_FEATURED_WINDOW_DAYS = 30
FEATURED_WINDOW_DAYS_KEY = "featured_window_days"


class InvalidFeaturedWindowError(ValueError):
    pass


def _validate_featured_window_days(days: int) -> int:
    if not MIN_FEATURED_WINDOW_DAYS <= days <= MAX_FEATURED_WINDOW_DAYS:
        raise InvalidFeaturedWindowError(
            f"Featured window must be between {MIN_FEATURED_WINDOW_DAYS} "
            f"and {MAX_FEATURED_WINDOW_DAYS} days"
        )
    return days


def load_featured_window_days(conn: sqlite3.Connection) -> int:
    stored = app_state.get(
        conn,
        FEATURED_WINDOW_DAYS_KEY,
        default=str(DEFAULT_FEATURED_WINDOW_DAYS),
    )
    try:
        return _validate_featured_window_days(int(stored))
    except (TypeError, ValueError, InvalidFeaturedWindowError):
        print(
            f"[featured] invalid stored window {stored!r}; "
            f"using default {DEFAULT_FEATURED_WINDOW_DAYS}"
        )
        return DEFAULT_FEATURED_WINDOW_DAYS


def save_featured_window_days(conn: sqlite3.Connection, days: int) -> None:
    app_state.set(
        conn,
        FEATURED_WINDOW_DAYS_KEY,
        str(_validate_featured_window_days(days)),
    )


def featured_utc_bounds(now: datetime, days: int) -> tuple[str, str]:
    _validate_featured_window_days(days)
    local_now = now.astimezone(HOST_TZ)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = today_start - timedelta(days=days - 1)
    end_local = today_start + timedelta(days=1)

    def as_naive_utc_iso(boundary: datetime) -> str:
        return (
            boundary.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )

    return as_naive_utc_iso(start_local), as_naive_utc_iso(end_local)
