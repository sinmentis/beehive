"""What the jobs worker reports about itself, and how the admin reads it back.

The worker saves one JSON document under app_state["jobs_worker_status"]: when it last checked
in, and for each lane what it is doing since when and how its last job ended. This module has no
heavy imports so the web process can read the status without loading the worker.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.db import app_state

STATUS_KEY = "jobs_worker_status"
LANES = ("manual_fetch", "scheduled_fetch", "deep_read", "digest", "reminders")
# The worker checks in at least every 30 seconds, so three minutes of silence means it stopped.
DOWN_AFTER = timedelta(minutes=3)
# A job running longer than this is worth a warning, well before the lane's hard limit.
SLOW_AFTER = {
    "manual_fetch": timedelta(minutes=30),
    "scheduled_fetch": timedelta(minutes=30),
    "deep_read": timedelta(minutes=20),
    "digest": timedelta(minutes=10),
    "reminders": timedelta(minutes=10),
}


@dataclass(frozen=True)
class LaneStatus:
    name: str
    busy_since: datetime | None
    doing: str | None
    last_finished_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class JobsWorkerStatus:
    seen_at: datetime
    last_sweep_started_at: datetime | None
    lanes: tuple[LaneStatus, ...]

    def is_down(self, now: datetime) -> bool:
        return now - self.seen_at > DOWN_AFTER

    def slow_lanes(self, now: datetime) -> tuple[LaneStatus, ...]:
        return tuple(
            lane for lane in self.lanes
            if lane.busy_since is not None and now - lane.busy_since > SLOW_AFTER[lane.name]
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def save_status(
    conn: sqlite3.Connection,
    *,
    seen_at: datetime,
    last_sweep_started_at: datetime | None,
    lanes: tuple[LaneStatus, ...],
) -> None:
    document = {
        "seen_at": _iso(seen_at),
        "last_sweep_started_at": _iso(last_sweep_started_at),
        "lanes": {
            lane.name: {
                "busy_since": _iso(lane.busy_since),
                "doing": lane.doing,
                "last_finished_at": _iso(lane.last_finished_at),
                "last_error": lane.last_error,
            }
            for lane in lanes
        },
    }
    app_state.set(conn, STATUS_KEY, json.dumps(document, separators=(",", ":")))


def load_status(conn: sqlite3.Connection) -> JobsWorkerStatus | None:
    """The worker's last report, or None when no worker has ever run against this database."""
    raw = app_state.get(conn, STATUS_KEY)
    if raw is None:
        return None
    try:
        document = json.loads(raw)
        seen_at = _parse(document["seen_at"])
        lanes = document.get("lanes") or {}
        if seen_at is None:
            return None
        return JobsWorkerStatus(
            seen_at=seen_at,
            last_sweep_started_at=_parse(document.get("last_sweep_started_at")),
            lanes=tuple(
                LaneStatus(
                    name=name,
                    busy_since=_parse((lanes.get(name) or {}).get("busy_since")),
                    doing=(lanes.get(name) or {}).get("doing"),
                    last_finished_at=_parse((lanes.get(name) or {}).get("last_finished_at")),
                    last_error=(lanes.get(name) or {}).get("last_error"),
                )
                for name in LANES
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None

