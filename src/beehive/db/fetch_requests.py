"""The "Fetch now" queue: one `fetch_requests` row per Channel while a manual fetch waits for the
worker or runs there.

The admin adds rows. The worker's fetch lane claims one at a time with a lease it renews while
the fetch runs, and deletes the row when the fetch ends, whether it succeeded or not. A request
made while its Channel is already being fetched (say, right after editing a Source) is kept as a
rerun, so the Channel is fetched once more when the running fetch ends.

A claim whose lease ran out belonged to a worker that stopped, and a claim whose fetch ran past
the worker's limit was handed back: both count as a failed try. The request is tried again until
it has had MAX_ATTEMPTS tries, and then dropped, because the next scheduled sweep fetches the
Channel anyway. Every write that ends a claim checks its token, so a worker that lost its claim
cannot finish or requeue a request someone else now holds.
"""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.db.connection import write_transaction

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class FetchClaim:
    channel_id: int
    claim_token: str
    attempt: int


@dataclass(frozen=True)
class RecoveredFetchRequests:
    requeued: tuple[int, ...]
    dropped: tuple[int, ...]


def request_fetch(conn: sqlite3.Connection, channel_ids: Iterable[int], now: datetime) -> None:
    """Queues a fetch for each Channel. A Channel already queued keeps its place; one being
    fetched right now is marked to run once more afterwards."""
    now_iso = now.isoformat()
    conn.executemany(
        "INSERT INTO fetch_requests (channel_id, requested_at) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET rerun = 1 "
        "WHERE fetch_requests.claim_token IS NOT NULL",
        [(channel_id, now_iso) for channel_id in dict.fromkeys(channel_ids)],
    )
    conn.commit()


def fetch_request_states(conn: sqlite3.Connection, now: datetime) -> dict[int, str]:
    """Each Channel with a request, as "queued", "running", or "stale" (its worker stopped)."""
    now_iso = now.isoformat()
    states = {}
    for row in conn.execute(
        "SELECT channel_id, claim_token, lease_expires_at FROM fetch_requests"
    ):
        if row["claim_token"] is None:
            states[row["channel_id"]] = "queued"
        elif row["lease_expires_at"] > now_iso:
            states[row["channel_id"]] = "running"
        else:
            states[row["channel_id"]] = "stale"
    return states


_QUEUE_AGAIN = (
    "UPDATE fetch_requests SET claim_token = NULL, lease_expires_at = NULL, started_at = NULL "
    "WHERE channel_id = ?"
)
# A rerun is a request the Owner made after this fetch started, so it starts afresh.
_QUEUE_RERUN = (
    "UPDATE fetch_requests SET claim_token = NULL, lease_expires_at = NULL, started_at = NULL, "
    "attempts = 0, rerun = 0, requested_at = ? WHERE channel_id = ?"
)


def recover_expired_fetch_requests(
    conn: sqlite3.Connection, now: datetime
) -> RecoveredFetchRequests:
    """Requeues every claim whose lease ran out, or drops it once it has had MAX_ATTEMPTS tries."""
    now_iso = now.isoformat()
    requeued: list[int] = []
    dropped: list[int] = []
    with write_transaction(conn):
        expired = conn.execute(
            "SELECT channel_id, attempts, rerun FROM fetch_requests "
            "WHERE claim_token IS NOT NULL AND lease_expires_at <= ?",
            (now_iso,),
        ).fetchall()
        for row in expired:
            requeued_or_dropped = _end_failed_try(conn, row, now_iso)
            (requeued if requeued_or_dropped else dropped).append(row["channel_id"])
    return RecoveredFetchRequests(requeued=tuple(requeued), dropped=tuple(dropped))


def _end_failed_try(conn: sqlite3.Connection, row: sqlite3.Row, now_iso: str) -> bool:
    """Puts a request whose try failed back in the queue, or drops it after MAX_ATTEMPTS tries.
    Returns True when it was requeued."""
    if row["rerun"]:
        conn.execute(_QUEUE_RERUN, (now_iso, row["channel_id"]))
        return True
    if row["attempts"] >= MAX_ATTEMPTS:
        conn.execute("DELETE FROM fetch_requests WHERE channel_id = ?", (row["channel_id"],))
        return False
    conn.execute(_QUEUE_AGAIN, (row["channel_id"],))
    return True


def claim_next_fetch_request(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    lease_seconds: float,
    exclude_channel_ids: Iterable[int] = (),
) -> FetchClaim | None:
    """Claims the oldest waiting request, skipping `exclude_channel_ids` (Channels a fetch is
    already running for), or returns None when nothing else is waiting."""
    token = secrets.token_urlsafe(24)
    excluded = tuple(exclude_channel_ids)
    not_excluded = f"AND channel_id NOT IN ({', '.join('?' * len(excluded))}) " if excluded else ""
    with write_transaction(conn):
        row = conn.execute(
            "SELECT channel_id, attempts FROM fetch_requests WHERE claim_token IS NULL "
            f"{not_excluded}ORDER BY requested_at, channel_id LIMIT 1",
            excluded,
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE fetch_requests SET claim_token = ?, started_at = ?, lease_expires_at = ?, "
            "attempts = attempts + 1 WHERE channel_id = ?",
            (
                token,
                now.isoformat(),
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                row["channel_id"],
            ),
        )
    return FetchClaim(channel_id=row["channel_id"], claim_token=token, attempt=row["attempts"] + 1)


def heartbeat_fetch_request(
    conn: sqlite3.Connection, claim: FetchClaim, now: datetime, *, lease_seconds: float
) -> bool:
    """Extends a live claim's lease. False means the claim is gone and the fetch lost its slot."""
    cur = conn.execute(
        "UPDATE fetch_requests SET lease_expires_at = ? "
        "WHERE channel_id = ? AND claim_token = ?",
        (
            (now + timedelta(seconds=lease_seconds)).isoformat(),
            claim.channel_id,
            claim.claim_token,
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def finish_fetch_request(conn: sqlite3.Connection, claim: FetchClaim, now: datetime) -> bool:
    """Ends the request once its fetch has ended, whatever the outcome: removes it, or queues it
    again when the Owner asked for another fetch while this one ran."""
    with write_transaction(conn):
        row = conn.execute(
            "SELECT rerun FROM fetch_requests WHERE channel_id = ? AND claim_token = ?",
            (claim.channel_id, claim.claim_token),
        ).fetchone()
        if row is None:
            return False
        if row["rerun"]:
            conn.execute(_QUEUE_RERUN, (now.isoformat(), claim.channel_id))
        else:
            conn.execute("DELETE FROM fetch_requests WHERE channel_id = ?", (claim.channel_id,))
    return True


def requeue_fetch_request(
    conn: sqlite3.Connection, claim: FetchClaim, now: datetime, *, failed: bool
) -> bool:
    """Hands a claimed request back before its fetch ended. `failed` is a try that went wrong
    (it ran past the worker's limit): it counts against the request's attempts, and the request
    is dropped after MAX_ATTEMPTS. Otherwise (the worker is stopping) the try does not count."""
    with write_transaction(conn):
        row = conn.execute(
            "SELECT channel_id, attempts, rerun FROM fetch_requests "
            "WHERE channel_id = ? AND claim_token = ?",
            (claim.channel_id, claim.claim_token),
        ).fetchone()
        if row is None:
            return False
        if failed:
            _end_failed_try(conn, row, now.isoformat())
        else:
            conn.execute(
                "UPDATE fetch_requests SET claim_token = NULL, lease_expires_at = NULL, "
                "started_at = NULL, attempts = MAX(attempts - 1, 0) WHERE channel_id = ?",
                (claim.channel_id,),
            )
    return True


def clear_stale_fetch_requests(conn: sqlite3.Connection, now: datetime) -> int:
    """The Owner's recovery for requests whose worker stopped: removes every expired claim."""
    cur = conn.execute(
        "DELETE FROM fetch_requests WHERE claim_token IS NOT NULL AND lease_expires_at <= ?",
        (now.isoformat(),),
    )
    conn.commit()
    return cur.rowcount
