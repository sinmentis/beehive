"""Durable previews and explicit reprocessing of a fixed set of existing items.

Only the jobs worker holds claims. Every batch fences its writes on the live claim,
saved profile and original item state, and commits progress with the corresponding
score updates. Recovery therefore never reapplies a completed batch.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from beehive.db.connection import write_transaction
from beehive.db.item_events import mark_item_events_ready, suppress_item_events

PREVIEW_LIMIT = 5
MAX_ATTEMPTS = 3
_PUBLIC_COLUMNS = (
    "id", "channel_id", "mode", "status", "requested_profile", "total_count", "processed_count",
    "skipped_count", "error", "error_code", "created_at", "started_at", "finished_at",
)
_ITEM_SELECT = (
    "SELECT items.*, sources.type AS source_type FROM items "
    "JOIN sources ON sources.id = items.source_id "
    "JOIN channels ON channels.id = sources.channel_id "
)
_ELIGIBLE = (
    "sources.channel_id = ? AND items.superseded_at IS NULL AND "
    "((channels.kind = 'editorial' AND items.is_read = 0) OR "
    "(channels.kind IN ('monitor', 'tracker') AND items.inactive_at IS NULL))"
)
_CONTENT_FIELDS = (
    "source_id", "source_type", "external_id", "title", "url", "body", "created_at",
    "raw_metadata", "ai_score", "ai_summary", "ai_rationale",
)
_STATE_FIELDS = ("is_read", "inactive_at", "superseded_at")


class RankingRequestError(ValueError):
    """A stable code that the admin can localize without importing the worker."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RankingClaim:
    request_id: str
    channel_id: int
    claim_token: str
    attempt: int


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _channel(conn: sqlite3.Connection, channel_id: int) -> dict:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise RankingRequestError("channel_not_found")
    return dict(row)


def _item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["raw_metadata"] = json.loads(item["raw_metadata"])
    return item


def ranking_candidate_count(conn: sqlite3.Connection, channel_id: int) -> int:
    """Count unread Editorial items or current, active listings, irrespective of old scores."""
    _channel(conn, channel_id)
    return conn.execute(
        "SELECT COUNT(*) FROM items JOIN sources ON sources.id = items.source_id "
        "JOIN channels ON channels.id = sources.channel_id WHERE " + _ELIGIBLE,
        (channel_id,),
    ).fetchone()[0]


def create_ranking_request(
    conn: sqlite3.Connection,
    channel_id: int,
    *,
    mode: Literal["preview", "apply"],
    profile: str,
    now: datetime | None = None,
) -> dict:
    """Queue work without AI credentials. Apply must match the saved profile exactly.

    A preview can use draft rules and stores at most five recent candidates. Neither
    mode clears scores or changes items, interactions, sources or events when queued.
    """
    if mode not in ("preview", "apply"):
        raise RankingRequestError("invalid_mode")
    request_id = secrets.token_urlsafe(18)
    with write_transaction(conn):
        channel = _channel(conn, channel_id)
        if mode == "apply" and profile != channel["profile"]:
            raise RankingRequestError("rules_changed")
        if conn.execute(
            "SELECT 1 FROM ranking_requests WHERE channel_id = ? "
            "AND status IN ('queued', 'running')", (channel_id,),
        ).fetchone():
            raise RankingRequestError("request_in_progress")
        limit = f" LIMIT {PREVIEW_LIMIT}" if mode == "preview" else ""
        rows = conn.execute(
            _ITEM_SELECT + "WHERE " + _ELIGIBLE
            + " ORDER BY items.fetched_at DESC, items.id DESC" + limit,
            (channel_id,),
        ).fetchall()
        if not rows:
            raise RankingRequestError("no_candidates")
        conn.execute(
            "INSERT INTO ranking_requests "
            "(id, channel_id, mode, requested_profile, saved_profile, channel_kind, "
            "total_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, channel_id, mode, profile, channel["profile"], channel["kind"],
             len(rows), _now(now).isoformat()),
        )
        conn.executemany(
            "INSERT INTO ranking_request_items (request_id, position, item_id, snapshot) "
            "VALUES (?, ?, ?, ?)",
            [(request_id, position, row["id"], json.dumps(_item(row), sort_keys=True))
             for position, row in enumerate(rows)],
        )
        request = get_ranking_request(conn, request_id)
        assert request is not None
    return request


def get_ranking_request(conn: sqlite3.Connection, request_id: str) -> dict | None:
    columns = ", ".join(f"request.{column}" for column in _PUBLIC_COLUMNS)
    rows = conn.execute(
        f"SELECT {columns}, candidate.result AS candidate_result FROM ranking_requests AS request "
        "LEFT JOIN ranking_request_items AS candidate ON candidate.request_id = request.id "
        "AND candidate.result IS NOT NULL "
        "WHERE request.id = ? ORDER BY candidate.position", (request_id,),
    ).fetchall()
    if not rows:
        return None
    request = {column: rows[0][column] for column in _PUBLIC_COLUMNS}
    request["result"] = [
        json.loads(row["candidate_result"]) for row in rows if row["candidate_result"] is not None
    ]
    return request


def list_ranking_requests_for_channel(
    conn: sqlite3.Connection, channel_id: int, limit: int = 5,
) -> list[dict]:
    ids = conn.execute(
        "SELECT id FROM ranking_requests WHERE channel_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?", (channel_id, max(0, limit)),
    ).fetchall()
    return [request for row in ids if (request := get_ranking_request(conn, row["id"])) is not None]


def cancel_ranking_request(
    conn: sqlite3.Connection, request_id: str, now: datetime | None = None,
) -> bool:
    """Fence subsequent writes immediately; already committed apply batches remain intact."""
    with write_transaction(conn):
        changed = conn.execute(
            "UPDATE ranking_requests SET status = 'cancelled', finished_at = ?, "
            "claim_token = NULL, lease_expires_at = NULL "
            "WHERE id = ? AND status IN ('queued', 'running')",
            (_now(now).isoformat(), request_id),
        ).rowcount
    return changed == 1


def claim_next_ranking_request(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    lease_seconds: float,
    exclude_channel_ids: Iterable[int] = (),
    defer_apply_channel_ids: Iterable[int] = (),
) -> RankingClaim | None:
    """Prefer previews, then rotate applies by their latest claim start.

    Progress breaks timestamp ties so rapid claims or a frozen clock cannot keep
    selecting the same apply. started_at describes the current/last execution slice.
    Deferred Channels yield progressed applies to waiting collection, without
    delaying previews or the first apply slice.
    """
    excluded = tuple(exclude_channel_ids)
    exclusion = (
        f"AND channel_id NOT IN ({','.join('?' for _ in excluded)}) " if excluded else ""
    )
    deferred = tuple(defer_apply_channel_ids)
    deferral = (
        "AND NOT (mode = 'apply' AND processed_count + skipped_count > 0 "
        f"AND channel_id IN ({','.join('?' for _ in deferred)})) " if deferred else ""
    )
    now = _now(now)
    with write_transaction(conn):
        row = conn.execute(
            "SELECT id, channel_id, attempts FROM ranking_requests WHERE status = 'queued' "
            + exclusion + deferral
            + "ORDER BY (mode = 'preview') DESC, COALESCE(started_at, created_at), "
            "processed_count + skipped_count, rowid LIMIT 1", excluded + deferred,
        ).fetchone()
        if row is None:
            return None
        token = secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE ranking_requests SET status = 'running', claim_token = ?, "
            "lease_expires_at = ?, started_at = ?, attempts = attempts + 1 "
            "WHERE id = ?",
            (token, (now + timedelta(seconds=lease_seconds)).isoformat(),
             now.isoformat(), row["id"]),
        )
    return RankingClaim(row["id"], row["channel_id"], token, row["attempts"] + 1)


def _live_request(
    conn: sqlite3.Connection, claim: RankingClaim, now: datetime,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ranking_requests WHERE id = ? AND channel_id = ? AND status = 'running' "
        "AND claim_token = ? AND lease_expires_at > ?",
        (claim.request_id, claim.channel_id, claim.claim_token, _now(now).isoformat()),
    ).fetchone()
    return dict(row) if row is not None else None


def ranking_claim_is_live(
    conn: sqlite3.Connection, claim: RankingClaim, now: datetime,
) -> bool:
    return _live_request(conn, claim, now) is not None


def heartbeat_ranking_request(
    conn: sqlite3.Connection, claim: RankingClaim, now: datetime, *, lease_seconds: float,
) -> bool:
    now = _now(now)
    with write_transaction(conn):
        changed = conn.execute(
            "UPDATE ranking_requests SET lease_expires_at = ? WHERE id = ? "
            "AND status = 'running' AND claim_token = ? AND lease_expires_at > ?",
            ((now + timedelta(seconds=lease_seconds)).isoformat(), claim.request_id,
             claim.claim_token, now.isoformat()),
        ).rowcount
    return changed == 1


def _finish(
    conn: sqlite3.Connection, request_id: str, status: str, now: datetime,
    *, error_code: str | None = None, error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE ranking_requests SET status = ?, finished_at = ?, claim_token = NULL, "
        "lease_expires_at = NULL, error_code = ?, error = ? WHERE id = ?",
        (status, _now(now).isoformat(), error_code, error, request_id),
    )


def _rules_current(
    conn: sqlite3.Connection, request: dict, now: datetime,
) -> dict | None:
    channel = _channel(conn, request["channel_id"])
    if (channel["kind"] != request["channel_kind"]
            or (request["mode"] == "apply" and channel["profile"] != request["saved_profile"])):
        _finish(conn, request["id"], "failed", now, error_code="rules_changed",
                error="The saved rules changed. Start a new request to use them.")
        return None
    return channel


def _skip_reason(conn: sqlite3.Connection, channel_id: int, snapshot: dict) -> str | None:
    row = conn.execute(
        _ITEM_SELECT + "WHERE items.id = ? AND sources.channel_id = ?",
        (snapshot["id"], channel_id),
    ).fetchone()
    if row is None:
        return "item_deleted"
    current = _item(row)
    if any(current[key] != snapshot[key] for key in _STATE_FIELDS):
        return "state_changed"
    if any(current[key] != snapshot[key] for key in _CONTENT_FIELDS):
        return "item_changed"
    return None


def _skip(conn: sqlite3.Connection, request_id: str, item_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE ranking_request_items SET status = 'skipped', skip_reason = ? "
        "WHERE request_id = ? AND item_id = ? AND status = 'pending'",
        (reason, request_id, item_id),
    )
    conn.execute(
        "UPDATE ranking_requests SET skipped_count = skipped_count + 1 WHERE id = ?",
        (request_id,),
    )


def _complete_if_exhausted(
    conn: sqlite3.Connection, request_id: str, now: datetime,
) -> bool:
    pending = conn.execute(
        "SELECT 1 FROM ranking_request_items WHERE request_id = ? AND status = 'pending' LIMIT 1",
        (request_id,),
    ).fetchone()
    if pending is None:
        _finish(conn, request_id, "completed", now)
        return True
    return False


def prepare_ranking_batch(
    conn: sqlite3.Connection, claim: RankingClaim, now: datetime, *, limit: int,
) -> list[dict] | None:
    """Load original candidates outside AI calls; skip known stale apply inputs.

    None ends processing (completed, cancelled, lost lease or changed rules). An
    empty batch means stale inputs were skipped but more original work remains.
    Previews intentionally use their immutable sample even if a live item changed.
    """
    with write_transaction(conn):
        request = _live_request(conn, claim, now)
        if request is None or _rules_current(conn, request, now) is None:
            return None
        rows = conn.execute(
            "SELECT snapshot FROM ranking_request_items WHERE request_id = ? AND status = 'pending' "
            "ORDER BY position LIMIT ?", (claim.request_id, limit),
        ).fetchall()
        batch = []
        for row in rows:
            snapshot = json.loads(row["snapshot"])
            reason = (
                _skip_reason(conn, claim.channel_id, snapshot) if request["mode"] == "apply" else None
            )
            if reason:
                _skip(conn, claim.request_id, snapshot["id"], reason)
            else:
                batch.append(snapshot)
        if _complete_if_exhausted(conn, claim.request_id, now):
            return None
    return batch


def complete_ranking_batch(
    conn: sqlite3.Connection, claim: RankingClaim, results: list[dict], now: datetime,
) -> bool:
    """Save results, AI-only item changes and still-open event decisions atomically."""
    with write_transaction(conn):
        request = _live_request(conn, claim, now)
        if request is None or (channel := _rules_current(conn, request, now)) is None:
            return False
        for ranked in results:
            row = conn.execute(
                "SELECT snapshot FROM ranking_request_items WHERE request_id = ? "
                "AND item_id = ? AND status = 'pending'",
                (claim.request_id, ranked["item_id"]),
            ).fetchone()
            if row is None:
                continue
            snapshot = json.loads(row["snapshot"])
            if request["mode"] == "apply":
                reason = _skip_reason(conn, claim.channel_id, snapshot)
                if reason:
                    _skip(conn, claim.request_id, snapshot["id"], reason)
                    continue
                conn.execute(
                    "UPDATE items SET ai_score = ?, ai_summary = ?, ai_rationale = ? WHERE id = ?",
                    (ranked["score"], ranked["summary"], ranked["rationale"], snapshot["id"]),
                )
                if ranked["score"] >= channel["minimum_score"]:
                    mark_item_events_ready(conn, snapshot["id"], _now(now).isoformat())
                else:
                    suppress_item_events(conn, snapshot["id"], _now(now).isoformat())
            result = {
                "item_id": snapshot["id"], "title": snapshot["title"], "url": snapshot["url"],
                "previous_score": snapshot["ai_score"], "previous_summary": snapshot["ai_summary"],
                "score": ranked["score"], "summary": ranked["summary"], "rationale": ranked["rationale"],
            }
            conn.execute(
                "UPDATE ranking_request_items SET status = 'processed', result = ? "
                "WHERE request_id = ? AND item_id = ?",
                (json.dumps(result), claim.request_id, snapshot["id"]),
            )
            conn.execute(
                "UPDATE ranking_requests SET processed_count = processed_count + 1 WHERE id = ?",
                (claim.request_id,),
            )
        _complete_if_exhausted(conn, claim.request_id, now)
    return True


def fail_ranking_request(
    conn: sqlite3.Connection, claim: RankingClaim, now: datetime, *, error_code: str, error: str,
) -> bool:
    with write_transaction(conn):
        if _live_request(conn, claim, now) is None:
            return False
        _finish(conn, claim.request_id, "failed", now, error_code=error_code, error=error)
    return True


def _requeue(conn: sqlite3.Connection, row: dict, now: datetime, *, failed: bool) -> None:
    if failed and row["attempts"] >= MAX_ATTEMPTS:
        _finish(conn, row["id"], "failed", now, error_code="interrupted",
                error="The worker repeatedly stopped before the request finished.")
    else:
        conn.execute(
            "UPDATE ranking_requests SET status = 'queued', claim_token = NULL, "
            "lease_expires_at = NULL, attempts = MAX(attempts - ?, 0) WHERE id = ?",
            (0 if failed else 1, row["id"]),
        )


def recover_expired_ranking_requests(conn: sqlite3.Connection, now: datetime) -> int:
    with write_transaction(conn):
        rows = conn.execute(
            "SELECT id, attempts FROM ranking_requests WHERE status = 'running' "
            "AND lease_expires_at <= ?", (_now(now).isoformat(),),
        ).fetchall()
        for row in rows:
            _requeue(conn, dict(row), now, failed=True)
    return len(rows)


def requeue_ranking_request(
    conn: sqlite3.Connection, claim: RankingClaim, now: datetime, *, failed: bool,
) -> bool:
    with write_transaction(conn):
        row = conn.execute(
            "SELECT id, attempts FROM ranking_requests WHERE id = ? "
            "AND status = 'running' AND claim_token = ?", (claim.request_id, claim.claim_token),
        ).fetchone()
        if row is None:
            return False
        _requeue(conn, dict(row), now, failed=failed)
    return True
