"""Research Chat Request persistence (ADR-0009): the durable worker's queue and lease for one
Owner message's in-flight reply-generation task. Mirrors db/deep_reads.py's claim_token +
lease_expires_at lease shape.

pinned_evidence_state_revision_id/pinned_synthesis_id/pinned_memory_version freeze exactly
which evidence state, synthesis, and conversation memory version the reply is generated
against at request time, so a reply that takes a while to generate remains reproducible even
if the Owner changes curation or a new synthesis lands while it is in flight -- it never
silently reads a moving target.

submit_chat_request is the ONE atomic entry point research.conversation.py's submission phase
uses (see that module's docstring): it appends the Owner's Conversation Message AND enqueues its
chat request -- including every pin above -- in a single BEGIN IMMEDIATE transaction, so the
owner message can never be committed without a chat request to process it, or vice versa.
Calling research_messages.append_message and enqueue_chat_request as two separate transactions
would leave exactly that kind of orphan message if the second transaction ever failed;
submit_chat_request is what closes that gap. enqueue_chat_request remains available as the
lower-level primitive (used directly by tests, and by anything that has already inserted its own
owner message some other way).

complete_chat_request_with_reply is the one atomic function ADR-0009/0010 require for finishing
a chat turn: inside a single transaction, fenced on (request_id, claim_token,
status='processing') plus session_id/owner_message_id/pinned_memory_version, it writes the
assistant's reply message (and its citations), marks the chat request completed, and bumps
Conversation Memory -- all three happen together or not at all, so a crash can never leave a
completed request with no reply, or a bumped memory version with no corresponding reply on
record."""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from beehive.db.evidence_state import get_latest_evidence_state_revision
from beehive.db.research_conversation_memory import _upsert_memory, get_conversation_memory
from beehive.db.research_messages import (_insert_message, _insert_message_citations,
                                           get_message)
from beehive.db.research_sessions import _is_session_active
from beehive.db.research_syntheses import get_latest_synthesis
from beehive.domain.research import (ConversationMessage, ConversationMessageStatus,
                                      ConversationRole, EvidenceCitation)

_MAX_PROCESSING_CHAT_REQUESTS = 3


class ChatRequestStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


_TERMINAL_STATUSES = frozenset({ChatRequestStatus.COMPLETED, ChatRequestStatus.FAILED})


@dataclass(frozen=True)
class ChatRequest:
    id: int
    session_id: int
    owner_message_id: int
    status: ChatRequestStatus
    claim_token: str | None
    lease_expires_at: str | None
    pinned_evidence_state_revision_id: int
    pinned_synthesis_id: int | None
    pinned_memory_version: int
    reply_message_id: int | None
    requested_at: str
    started_at: str | None
    completed_at: str | None
    error_code: str | None
    error_detail: str | None


def _row_to_request(row: sqlite3.Row) -> ChatRequest:
    return ChatRequest(
        id=row["id"],
        session_id=row["session_id"],
        owner_message_id=row["owner_message_id"],
        status=ChatRequestStatus(row["status"]),
        claim_token=row["claim_token"],
        lease_expires_at=row["lease_expires_at"],
        pinned_evidence_state_revision_id=row["pinned_evidence_state_revision_id"],
        pinned_synthesis_id=row["pinned_synthesis_id"],
        pinned_memory_version=row["pinned_memory_version"],
        reply_message_id=row["reply_message_id"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_code=row["error_code"],
        error_detail=row["error_detail"])


def _fetch(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM research_chat_requests WHERE id = ?", (request_id,)).fetchone()


def get_active_chat_request(conn: sqlite3.Connection, session_id: int) -> ChatRequest | None:
    row = conn.execute(
        "SELECT * FROM research_chat_requests WHERE session_id = ? "
        "AND status IN ('pending', 'processing')",
        (session_id,)).fetchone()
    return _row_to_request(row) if row else None


def enqueue_chat_request(conn: sqlite3.Connection, session_id: int, owner_message_id: int,
                          pinned_evidence_state_revision_id: int,
                          pinned_synthesis_id: int | None, pinned_memory_version: int,
                          now: datetime) -> ChatRequest:
    """Creates a new pending chat request. Raises ValueError if the Research Session is not
    'active' (archived or nonexistent), or if the session already has a pending/processing
    chat request -- both checked explicitly under BEGIN IMMEDIATE so the caller gets a clear
    error, with the partial unique index on (session_id) WHERE status IN
    ('pending','processing') as defense in depth if the active-request check were ever
    bypassed."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _is_session_active(conn, session_id):
            raise ValueError(
                f"cannot enqueue a chat request for a non-active Research Session {session_id}")
        if get_active_chat_request(conn, session_id) is not None:
            raise ValueError(
                f"Research Session {session_id} already has an active chat request")
        cur = conn.execute(
            "INSERT INTO research_chat_requests (session_id, owner_message_id, status, "
            "pinned_evidence_state_revision_id, pinned_synthesis_id, pinned_memory_version, "
            "requested_at) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
            (session_id, owner_message_id, pinned_evidence_state_revision_id,
             pinned_synthesis_id, pinned_memory_version, now.isoformat()))
        request_id = cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_request(_fetch(conn, request_id))


def submit_chat_request(
        conn: sqlite3.Connection, session_id: int, question: str,
        now: datetime) -> tuple[ConversationMessage, ChatRequest]:
    """The one atomic entry point for starting a new chat turn (research.conversation.py's
    submission phase calls this, and nothing lower-level, for exactly this reason): appends the
    Owner's question as a Conversation Message AND enqueues its chat request in a SINGLE BEGIN
    IMMEDIATE transaction, so the owner message can never be committed without a chat request to
    process it, or vice versa -- unlike append_message followed by enqueue_chat_request as two
    separate transactions, which could leave an orphan owner message behind if the second
    transaction ever failed.

    Performs every check enqueue_chat_request performs (active session, no other pending/
    processing chat request for this session), plus the pinning decisions only this higher-level
    entry point can make:
    - the Research Session must already have a current, non-empty Evidence State Revision (a chat
      turn cannot be requested before evidence has been collected or after all evidence is
      excluded)
    - the Research Session must already have a current Research Synthesis -- CONTEXT.md's
      Research Synthesis is what every reply is grounded against, so a session's FIRST chat turn
      can never be submitted before at least one exists. (pinned_synthesis_id stays nullable at
      the schema/enqueue_chat_request level for flexibility, but this entry point never actually
      leaves it NULL once this check has passed.)
    - that Research Synthesis must be pinned to the SAME Evidence State Revision as the one just
      looked up above -- i.e. it must actually be current, not merely exist. Curation
      (exclude/restore) always builds a fresh Evidence State Revision immediately, before any
      new Research Synthesis is generated against it, so an Owner can otherwise be looking at the
      latest Research Synthesis while the latest Evidence State Revision has already moved past
      it. Submitting in that window would pin a synthesis/revision pair that never actually
      coexisted -- never falls back to the synthesis's own (now-superseded) revision instead.

    Pins the CURRENT (latest) Evidence State Revision, Research Synthesis, and Conversation
    Memory version at the moment of submission -- exactly what keeps a reply generated later
    reproducible even if the Owner curates evidence or a new Research Synthesis lands while this
    request is in flight.

    Raises ValueError -- with zero rows written in every case -- for a non-active session, an
    already-active chat request, a missing/empty Evidence State Revision, a missing Research
    Synthesis, or a Research Synthesis that is not pinned to the current Evidence State
    Revision."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _is_session_active(conn, session_id):
            raise ValueError(
                f"cannot submit a chat request for a non-active Research Session {session_id}")
        if get_active_chat_request(conn, session_id) is not None:
            raise ValueError(
                f"Research Session {session_id} already has an active chat request")
        revision = get_latest_evidence_state_revision(conn, session_id)
        if revision is None:
            raise ValueError(
                f"Research Session {session_id} has no Evidence State Revision yet -- a chat "
                "reply cannot be requested before evidence has been collected")
        if not revision.evidence_item_ids:
            raise ValueError(
                f"Research Session {session_id} has no active Evidence Items -- a chat reply "
                "cannot be requested while all evidence is excluded")
        synthesis = get_latest_synthesis(conn, session_id)
        if synthesis is None:
            raise ValueError(
                f"Research Session {session_id} has no Research Synthesis yet -- the first "
                "chat turn requires a completed Research Synthesis")
        if synthesis.evidence_state_revision_id != revision.id:
            raise ValueError(
                f"Research Session {session_id}'s latest Research Synthesis is pinned to "
                f"Evidence State Revision {synthesis.evidence_state_revision_id}, not the "
                f"current Evidence State Revision {revision.id} -- a new Research Synthesis is "
                "needed before a chat reply can be requested")
        memory = get_conversation_memory(conn, session_id)
        pinned_memory_version = memory.version if memory is not None else 0

        message_id = _insert_message(
            conn, session_id, ConversationRole.OWNER, ConversationMessageStatus.READY,
            question, now)
        cur = conn.execute(
            "INSERT INTO research_chat_requests (session_id, owner_message_id, status, "
            "pinned_evidence_state_revision_id, pinned_synthesis_id, pinned_memory_version, "
            "requested_at) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
            (session_id, message_id, revision.id, synthesis.id, pinned_memory_version,
             now.isoformat()))
        request_id = cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_message(conn, message_id), _row_to_request(_fetch(conn, request_id))


def get_chat_request(conn: sqlite3.Connection, request_id: int) -> ChatRequest | None:
    row = _fetch(conn, request_id)
    return _row_to_request(row) if row else None


def list_chat_requests(conn: sqlite3.Connection, session_id: int) -> list[ChatRequest]:
    rows = conn.execute(
        "SELECT * FROM research_chat_requests WHERE session_id = ? ORDER BY id",
        (session_id,)).fetchall()
    return [_row_to_request(r) for r in rows]


def list_pending_chat_requests(conn: sqlite3.Connection, limit: int = 20) -> list[ChatRequest]:
    rows = conn.execute(
        "SELECT * FROM research_chat_requests WHERE status = 'pending' "
        "ORDER BY requested_at ASC, id ASC LIMIT ?",
        (limit,)).fetchall()
    return [_row_to_request(r) for r in rows]


def count_active_processing_chat_requests(conn: sqlite3.Connection, now: datetime) -> int:
    """Count unexpired processing claims for the fleet-wide chat concurrency limit."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM research_chat_requests "
        "WHERE status = 'processing' AND lease_expires_at > ?",
        (now.isoformat(),)).fetchone()
    return row["n"]


def claim_chat_request(conn: sqlite3.Connection, request_id: int, now: datetime,
                        lease_seconds: int) -> ChatRequest | None:
    """Claim one pending request without exceeding the fleet-wide three-chat limit."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if count_active_processing_chat_requests(conn, now) >= _MAX_PROCESSING_CHAT_REQUESTS:
            result = None
        else:
            claim_token = secrets.token_urlsafe(32)
            now_iso = now.isoformat()
            lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            cur = conn.execute(
                "UPDATE research_chat_requests SET status = 'processing', claim_token = ?, "
                "lease_expires_at = ?, started_at = COALESCE(started_at, ?) "
                "WHERE id = ? AND status = 'pending'",
                (claim_token, lease_expires_at, now_iso, request_id))
            result = _row_to_request(_fetch(conn, request_id)) if cur.rowcount else None
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def heartbeat_chat_request(conn: sqlite3.Connection, request_id: int, claim_token: str,
                            now: datetime, lease_seconds: int) -> bool:
    lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
    cur = conn.execute(
        "UPDATE research_chat_requests SET lease_expires_at = ? "
        "WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (lease_expires_at, request_id, claim_token))
    conn.commit()
    return cur.rowcount > 0


def fail_chat_request(conn: sqlite3.Connection, request_id: int, claim_token: str,
                       error_code: str, error_detail: str | None, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE research_chat_requests SET status = 'failed', error_code = ?, "
        "error_detail = ?, claim_token = NULL, lease_expires_at = NULL, completed_at = ? "
        "WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (error_code, error_detail, now.isoformat(), request_id, claim_token))
    conn.commit()
    return cur.rowcount > 0


def requeue_chat_request(conn: sqlite3.Connection, request_id: int, claim_token: str) -> bool:
    """Voluntarily gives back an active claim before its lease expires, exactly like
    db/deep_reads.py's requeue_deep_read: the durable worker (a later todo) rearms an in-flight
    chat request on a graceful shutdown instead of making it wait out the full lease. Guarded
    like heartbeat_chat_request/fail_chat_request: only succeeds if (request_id, claim_token) is
    still the active 'processing' claim."""
    cur = conn.execute(
        "UPDATE research_chat_requests SET status = 'pending', claim_token = NULL, "
        "lease_expires_at = NULL WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (request_id, claim_token))
    conn.commit()
    return cur.rowcount > 0


def recover_expired_chat_requests(conn: sqlite3.Connection, now: datetime) -> int:
    """Reconciliation sweep, exactly like db/deep_reads.py's recover_expired_deep_reads: any
    'processing' request whose lease expired goes back to 'pending' with its claim cleared."""
    cur = conn.execute(
        "UPDATE research_chat_requests SET status = 'pending', claim_token = NULL, "
        "lease_expires_at = NULL WHERE status = 'processing' AND lease_expires_at < ?",
        (now.isoformat(),))
    conn.commit()
    return cur.rowcount


def complete_chat_request_with_reply(
    conn: sqlite3.Connection, request_id: int, claim_token: str, session_id: int,
    owner_message_id: int, reply_content: str, reply_citations: tuple[EvidenceCitation, ...],
    memory_content: str, memory_covers_through_message_id: int | None, now: datetime,
) -> tuple[ChatRequest, ConversationMessage] | None:
    """Atomically finishes a chat turn: writes the assistant's reply Conversation Message (and
    citations), marks this chat request completed with reply_message_id set, and bumps
    Conversation Memory -- all inside one transaction fenced on FOUR things, not just the
    original (request_id, claim_token, status='processing'):

    - (request_id, claim_token, status='processing') as before -- a stale worker whose lease was
      recovered and reclaimed by someone else gets None back (rolling back nothing was written),
      exactly as before.
    - session_id and owner_message_id must match what THIS request row actually has on file.
      These are caller-supplied only for defense in depth (the row already knows both) -- a
      mismatch here can only mean the caller passed the wrong parameters (a programming error,
      never a normal race), so it raises ValueError loudly instead of silently completing the
      request into the wrong Research Session or against the wrong owner message.
    - the session's CURRENT Conversation Memory version must still equal this request's own
      pinned_memory_version (no row at all counts as version 0). The one-pending-request-per-
      session invariant should make a mismatch here impossible in practice -- this is cheap,
      load-bearing defense in depth against ever silently clobbering a newer memory version than
      the one this reply was generated against, since a memory update, once committed, has no
      history to recover from.

    Every mismatch above raises ValueError and rolls back the whole transaction -- nothing is
    ever partially written."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT session_id, owner_message_id, pinned_memory_version "
            "FROM research_chat_requests "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (request_id, claim_token)).fetchone()
        if row is None:
            result = None
        else:
            if row["session_id"] != session_id:
                raise ValueError(
                    f"chat request {request_id} belongs to Research Session "
                    f"{row['session_id']}, not {session_id}")
            if row["owner_message_id"] != owner_message_id:
                raise ValueError(
                    f"chat request {request_id} is pinned to owner message "
                    f"{row['owner_message_id']}, not {owner_message_id}")
            current_memory = get_conversation_memory(conn, session_id)
            current_version = current_memory.version if current_memory is not None else 0
            if current_version != row["pinned_memory_version"]:
                raise ValueError(
                    f"chat request {request_id}'s pinned Conversation Memory version "
                    f"{row['pinned_memory_version']} no longer matches session {session_id}'s "
                    f"current version {current_version}")

            message_id = _insert_message(
                conn, session_id, ConversationRole.ASSISTANT, ConversationMessageStatus.READY,
                reply_content, now)
            _insert_message_citations(conn, message_id, reply_citations)
            _upsert_memory(conn, session_id, memory_content, memory_covers_through_message_id,
                            now)
            conn.execute(
                "UPDATE research_chat_requests SET status = 'completed', "
                "claim_token = NULL, lease_expires_at = NULL, reply_message_id = ?, "
                "completed_at = ? WHERE id = ? AND claim_token = ? AND status = 'processing'",
                (message_id, now.isoformat(), request_id, claim_token))
            result = (_row_to_request(_fetch(conn, request_id)), get_message(conn, message_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result
