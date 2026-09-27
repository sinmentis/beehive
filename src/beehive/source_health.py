"""Source health: how fetch outcomes turn into retry timing and operator-facing state, so the
scheduler, the collector log, and the digest warning all agree.

A failing Source used to be retried a fixed hour after every automatic failure, forever. An
upstream that blocks a bot keeps blocking it, and hourly retries can prolong that block, so the
backoff now doubles with each automatic failure in a row and settles at once a day. Any success,
manual or automatic, resets the streak. Manual "fetch now" failures are shown but never counted,
so running a Source by hand cannot postpone its schedule."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beehive.connectors.base import TruncatedSnapshotError
from beehive.connectors.http import ConnectorHttpError

RETRY_BACKOFF_BASE = timedelta(hours=1)
RETRY_BACKOFF_CAP = timedelta(hours=24)

# Stable kinds stored in sources.last_fetch_error_kind. ConnectorHttpError contributes its own
# ConnectorHttpErrorKind values (transient, access_denied, not_found, ...).
TRUNCATED = "truncated"
OTHER = "error"


def classify_fetch_error(exc: BaseException) -> str:
    """A stable kind for a failed fetch, for storage and logs."""
    if isinstance(exc, TruncatedSnapshotError):
        return TRUNCATED
    if isinstance(exc, ConnectorHttpError):
        return exc.kind.value
    return OTHER


def retry_backoff(consecutive_failures: int) -> timedelta:
    """How long after an automatic failure the Source may be retried: 1h after the first failure,
    doubling per further failure in a row, capped at 24h."""
    doublings = min(max(consecutive_failures, 1) - 1, 16)
    return min(RETRY_BACKOFF_BASE * (2**doublings), RETRY_BACKOFF_CAP)


def expected_fetch_period(channel: dict) -> timedelta:
    """How often the Channel is meant to fetch: daily in calendar mode, else its interval."""
    if channel.get("fetch_schedule_mode") == "calendar":
        return timedelta(days=1)
    return timedelta(hours=max(1, int(channel.get("fetch_interval_hours") or 1)))


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_stale(source: dict, channel: dict, now: datetime) -> bool:
    """Whether the Source's current data is too old to present as current state: its last
    successful fetch is more than two expected fetch periods ago, so at least one whole scheduled
    fetch has been missed. A Source that has never succeeded has no data to be stale."""
    last_success = source.get("last_fetch_at")
    if not last_success:
        return False
    return now - _as_utc(last_success) > 2 * expected_fetch_period(channel)
