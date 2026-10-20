"""Calendar-slot scheduling for recurring Civic Watches."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable

from beehive.db.civic_runs import (
    enqueue_civic_run,
    get_active_civic_run,
    get_civic_run_for_slot,
)
from beehive.db.civic_vocabulary import get_published_vocabulary_version
from beehive.db.civic_watches import list_schedulable_civic_watches
from beehive.domain.civic import CivicRun, CivicRunStatus, CivicWatch
from beehive.scheduling import CalendarSchedule

DEFAULT_CIVIC_SCHEDULE_TIME = "06:30"
PARTYMAP_GLOBAL_WATCH_TIMEZONE = "Pacific/Auckland"
PARTYMAP_GLOBAL_WATCH_SUBJECT = "partymap"


class CivicGlobalWatchConfigurationError(ValueError):
    pass


class CivicScheduleDisposition(StrEnum):
    ENQUEUED = "enqueued"
    EXISTING = "existing"
    BLOCKED_ACTIVE_RUN = "blocked_active_run"
    MISSING_VOCABULARY = "missing_vocabulary"
    NOT_DUE = "not_due"


@dataclass(frozen=True)
class CivicScheduleResult:
    watch: CivicWatch
    slot_at: datetime
    disposition: CivicScheduleDisposition
    run: CivicRun | None = None
    blocking_run: CivicRun | None = None


def schedule_due_civic_runs(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    now_fn: Callable[[], datetime] | None = None,
    run_mode: str = "normal",
) -> tuple[CivicScheduleResult, ...]:
    """Enqueue each Watch's latest arrived slot once.

    An older active Run is an explicit backlog blocker. Once it settles, the next tick enqueues
    the latest arrived slot, not one synthetic fetch per missed day; source snapshots cannot be
    reconstructed retroactively.
    """
    now = now_fn() if now_fn is not None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Civic scheduling needs a timezone-aware datetime")
    return tuple(
        _schedule_watch(conn, watch, now, run_mode=run_mode)
        for watch in list_schedulable_civic_watches(conn)
    )


def _schedule_watch(
    conn: sqlite3.Connection,
    watch: CivicWatch,
    now: datetime,
    *,
    run_mode: str,
) -> CivicScheduleResult:
    if watch.created_at.tzinfo is None or watch.created_at.utcoffset() is None:
        raise ValueError(f"Civic Watch {watch.id} created_at must be timezone-aware")
    calendar = CalendarSchedule.parse(
        timezone_name=watch.schedule_timezone,
        time_text=watch.schedule_time,
        weekdays_text=None,
        default_time=DEFAULT_CIVIC_SCHEDULE_TIME,
    )
    slot_at = calendar.latest_slot_at_or_before(now)
    if slot_at < watch.created_at.astimezone(slot_at.tzinfo):
        return CivicScheduleResult(
            watch=watch,
            slot_at=slot_at,
            disposition=CivicScheduleDisposition.NOT_DUE,
        )
    existing = get_civic_run_for_slot(conn, watch.id, slot_at)
    if existing is not None:
        return CivicScheduleResult(
            watch=watch,
            slot_at=slot_at,
            disposition=CivicScheduleDisposition.EXISTING,
            run=existing,
        )
    active = get_active_civic_run(conn, watch.id)
    if active is not None:
        return CivicScheduleResult(
            watch=watch,
            slot_at=slot_at,
            disposition=CivicScheduleDisposition.BLOCKED_ACTIVE_RUN,
            blocking_run=active,
        )
    vocabulary = get_published_vocabulary_version(conn, watch.id)
    if vocabulary is None:
        return CivicScheduleResult(
            watch=watch,
            slot_at=slot_at,
            disposition=CivicScheduleDisposition.MISSING_VOCABULARY,
        )
    run = enqueue_civic_run(
        conn,
        watch.id,
        slot_at,
        now,
        vocabulary_version_id=vocabulary.id,
        run_mode=run_mode,
    )
    return CivicScheduleResult(
        watch=watch,
        slot_at=slot_at,
        disposition=CivicScheduleDisposition.ENQUEUED,
        run=run,
    )


def require_partymap_global_watch(
    conn: sqlite3.Connection,
    *,
    subject: str = PARTYMAP_GLOBAL_WATCH_SUBJECT,
) -> CivicWatch:
    """Require the one global, active, publication-ready PartyMap Watch.

    Requiring exactly one active Watch prevents a party-per-Watch deployment from producing
    several Runs/Batches for the same Auckland day.
    """
    active = list_schedulable_civic_watches(conn)
    if len(active) != 1:
        raise CivicGlobalWatchConfigurationError(
            f"PartyMap Civic requires exactly one active Watch; found {len(active)}")
    watch = active[0]
    if watch.subject != subject:
        raise CivicGlobalWatchConfigurationError(
            f"the active PartyMap Civic Watch subject must be {subject!r}, "
            f"not {watch.subject!r}")
    if (watch.schedule_timezone != PARTYMAP_GLOBAL_WATCH_TIMEZONE
            or watch.schedule_time != DEFAULT_CIVIC_SCHEDULE_TIME):
        raise CivicGlobalWatchConfigurationError(
            "the PartyMap Civic Watch schedule must be 06:30 Pacific/Auckland")
    if get_published_vocabulary_version(conn, watch.id) is None:
        raise CivicGlobalWatchConfigurationError(
            f"PartyMap Civic Watch {watch.id} has no published vocabulary")
    return watch


def schedule_partymap_civic_run(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    now_fn: Callable[[], datetime] | None = None,
    run_mode: str = "normal",
    subject: str = PARTYMAP_GLOBAL_WATCH_SUBJECT,
) -> tuple[CivicScheduleResult, ...]:
    """Validate the global Watch before allowing the generic scheduler to enqueue anything."""
    watch = require_partymap_global_watch(conn, subject=subject)
    now = now_fn() if now_fn is not None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Civic scheduling needs a timezone-aware datetime")
    return (_schedule_watch(conn, watch, now, run_mode=run_mode),)


def enqueue_partymap_catch_up(
    conn: sqlite3.Connection, slot_at: datetime, now: datetime,
) -> CivicRun:
    """An explicit recovery slot preserves the failed daily Run and its fixed deadline."""
    if any(value.tzinfo is None or value.utcoffset() is None for value in (slot_at, now)):
        raise ValueError("Civic catch-up needs timezone-aware timestamps")
    slot_at = slot_at.astimezone(timezone.utc)
    if slot_at > now:
        raise ValueError("Civic catch-up cannot use a future slot")
    watch = require_partymap_global_watch(conn)
    if slot_at < watch.created_at:
        raise ValueError("Civic catch-up cannot predate its Watch")
    existing = get_civic_run_for_slot(conn, watch.id, slot_at)
    if existing is not None:
        return existing
    calendar = CalendarSchedule.parse(
        timezone_name=watch.schedule_timezone, time_text=watch.schedule_time,
        weekdays_text=None, default_time=DEFAULT_CIVIC_SCHEDULE_TIME)
    daily = get_civic_run_for_slot(conn, watch.id, calendar.latest_slot_at_or_before(now))
    if (daily is None or daily.status is not CivicRunStatus.FAILED
            or daily.completed_at is None or slot_at <= daily.completed_at):
        raise ValueError("Civic catch-up requires a failed daily Run before the recovery slot")
    vocabulary = get_published_vocabulary_version(conn, watch.id)
    if vocabulary is None:
        raise CivicGlobalWatchConfigurationError("Civic catch-up requires published vocabulary")
    return enqueue_civic_run(
        conn, watch.id, slot_at, now,
        vocabulary_version_id=vocabulary.id, run_mode=daily.run_mode)


__all__ = [
    "DEFAULT_CIVIC_SCHEDULE_TIME",
    "PARTYMAP_GLOBAL_WATCH_SUBJECT",
    "PARTYMAP_GLOBAL_WATCH_TIMEZONE",
    "CivicGlobalWatchConfigurationError",
    "CivicScheduleDisposition",
    "CivicScheduleResult",
    "schedule_due_civic_runs",
    "schedule_partymap_civic_run",
    "require_partymap_global_watch",
]
