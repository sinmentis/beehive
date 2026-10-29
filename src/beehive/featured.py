"""The shared rolling time window for current Editorial news."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from beehive.db import app_state
DEFAULT_FEATURED_WINDOW_DAYS = 1
MIN_FEATURED_WINDOW_DAYS = 1
MAX_FEATURED_WINDOW_DAYS = 30
FEATURED_WINDOW_DAYS_KEY = "featured_window_days"


class InvalidFeaturedWindowError(ValueError):
    pass


def _validate_featured_window_days(days: int) -> int:
    if not MIN_FEATURED_WINDOW_DAYS <= days <= MAX_FEATURED_WINDOW_DAYS:
        raise InvalidFeaturedWindowError(
            f"News window must be between {MIN_FEATURED_WINDOW_DAYS} "
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
    utc_now = now.astimezone(timezone.utc)
    start = utc_now - timedelta(days=days)
    # Queries use second-precision timestamps and an exclusive upper bound.
    end = utc_now + timedelta(seconds=1)

    def as_naive_utc_iso(boundary: datetime) -> str:
        return (
            boundary.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )

    return as_naive_utc_iso(start), as_naive_utc_iso(end)
