"""Claim-fenced clock for Civic source outage holds.

One row per source with an open outage episode. An episode starts at the first held
observation. It survives healthy fetches until the source has stayed healthy for
``OUTAGE_RECOVERY_AFTER`` after its last held observation, so a flapping site cannot reset its
own escalation clock. It is discarded as untrustworthy when no Run has observed the source for
``OUTAGE_OBSERVATION_GAP``, for example while an older image that does not maintain it ran.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

OUTAGE_RECOVERY_AFTER = timedelta(days=7)
OUTAGE_OBSERVATION_GAP = timedelta(days=3)


@dataclass(frozen=True)
class CivicSourceOutage:
    source_key: str
    unavailable_since: datetime
    last_unavailable_at: datetime
    observed_at: datetime
    detail: str


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _row_to_outage(row: sqlite3.Row) -> CivicSourceOutage:
    return CivicSourceOutage(
        source_key=row["source_key"],
        unavailable_since=datetime.fromisoformat(row["unavailable_since"]),
        last_unavailable_at=datetime.fromisoformat(row["last_unavailable_at"]),
        observed_at=datetime.fromisoformat(row["observed_at"]),
        detail=row["detail"],
    )


def get_source_outage(conn: sqlite3.Connection, source_key: str) -> CivicSourceOutage | None:
    row = conn.execute(
        "SELECT * FROM civic_source_outages WHERE source_key = ?", (source_key,)).fetchone()
    return _row_to_outage(row) if row else None


def _claim_is_live(conn: sqlite3.Connection, run_id: int, claim_token: str, now: datetime) -> bool:
    run = conn.execute(
        "SELECT deadline_at, lease_expires_at FROM civic_runs "
        "WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (run_id, claim_token)).fetchone()
    return not (
        run is None
        or run["lease_expires_at"] is None
        or now >= datetime.fromisoformat(run["lease_expires_at"])
        or (run["deadline_at"] is not None
            and now >= datetime.fromisoformat(run["deadline_at"])))


def observe_source_outages_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    observations: Mapping[str, str | None],
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, CivicSourceOutage] | None:
    """Record one Run's source observations while its claim, lease and deadline are live.

    ``observations`` maps a source key to its outage detail when the source was held, or to None
    when it was fetched successfully. A source that failed for any other reason is not an
    observation. Returns the open episode of every held source, or None, writing nothing, when
    the claim is gone.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        if not _claim_is_live(conn, run_id, claim_token, now):
            conn.rollback()
            return None
        held: dict[str, CivicSourceOutage] = {}
        for source_key, detail in sorted(observations.items()):
            outage = get_source_outage(conn, source_key)
            if outage is not None and now - outage.observed_at > OUTAGE_OBSERVATION_GAP:
                conn.execute(
                    "DELETE FROM civic_source_outages WHERE source_key = ?", (source_key,))
                outage = None
            if detail is not None:
                if outage is None:
                    conn.execute(
                        "INSERT INTO civic_source_outages (source_key, unavailable_since, "
                        "last_unavailable_at, observed_at, detail) VALUES (?, ?, ?, ?, ?)",
                        (source_key, _stamp(now), _stamp(now), _stamp(now), detail[:500]))
                else:
                    conn.execute(
                        "UPDATE civic_source_outages SET last_unavailable_at = ?, "
                        "observed_at = ?, detail = ? WHERE source_key = ?",
                        (_stamp(now), _stamp(now), detail[:500], source_key))
                held[source_key] = get_source_outage(conn, source_key)
            elif outage is not None:
                if now - outage.last_unavailable_at >= OUTAGE_RECOVERY_AFTER:
                    conn.execute(
                        "DELETE FROM civic_source_outages WHERE source_key = ?", (source_key,))
                else:
                    conn.execute(
                        "UPDATE civic_source_outages SET observed_at = ? WHERE source_key = ?",
                        (_stamp(now), source_key))
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return held


__all__ = [
    "OUTAGE_OBSERVATION_GAP",
    "OUTAGE_RECOVERY_AFTER",
    "CivicSourceOutage",
    "get_source_outage",
    "observe_source_outages_if_claimed",
]
