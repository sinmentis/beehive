"""Civic Batch persistence: the content-hashed envelope of one Civic Run's publishable
Candidates, and the delivery lease that carries it downstream exactly once.

**Exactly one Batch per Run** (UNIQUE(run_id)). `get_or_create_batch_if_claimed` is therefore
resume-safe: a worker that crashed mid-run and reclaimed its Run finds the same envelope rather
than minting a second one, which is the same invariant research_snapshots enforces for Evidence
Snapshots.

**Sealing freezes the envelope, and only settled work may be sealed.** `seal_batch_if_claimed`
refuses outright while any Candidate is still unsettled, then stamps `sealed_into_batch_at` on
the VERIFIED Candidates and computes a SHA-256 content_hash over exactly those rows -- their
dedupe keys, titles and statements, in id order. REJECTED Candidates are excluded from both the
hash and candidate_count. From that moment nothing may be added. Every state afterwards
(DELIVERING, a retry back to SEALED, DELIVERED, QUARANTINED) reads those exact bytes, which is
what lets a redelivery be *proven* identical rather than merely assumed to be.
`recompute_content_hash` exists only to verify that claim; it never writes, and it reads the same
immutable `sealed_into_batch_at` membership seal itself hashed, so tomorrow's correction moving a
published Candidate to SUPERSEDED cannot make an already-delivered envelope look tampered with.

**PartyMap seals freeze the actual validated publication, not just Candidate membership.**
Every verified Candidate passes the shared publication preflight before capacity selection.
Invalid content becomes an explicit unsealed publication rejection. Deployment drift starts a
bounded AI revision instead, retaining source ownership and prior decisions. Neither uses a
fake capacity transition. The exact v1 bytes and payload hash commit with membership.
Generic Civic Batches remain available to non-publication callers, but cannot be spooled as new
PartyMap publications. Legacy sealed Batches are never backfilled or re-evaluated.

**Delivery is claimed, leased, retried, and fenced**, the same shape as a Run's worker lease:
  * `claim_batch_for_delivery` moves SEALED -> DELIVERING under BEGIN IMMEDIATE, mints a
    claim_token, and grants a bounded lease. A Batch inside its retry backoff (next_attempt_at
    in the future) is not offered at all.
  * Every later write is fenced on (batch_id, claim_token, status='delivering'), so a delivery
    worker that lost its lease to a recovery sweep can never mark the envelope delivered.
  * `release_batch_claim` returns the envelope to SEALED with a backoff, or -- once
    attempt_count reaches max_attempts -- terminalizes it FAILED, so a permanently broken
    downstream cannot retry forever.
  * `quarantine_batch` is terminal and never auto-retried: it means an integrity problem an
    Owner has to look at.

The idempotency_key is minted at insert, before any content exists, and never derived from
mutable content -- so it is stable across a rebuild of the same envelope and is what a Civic
Delivery Receipt is keyed on (db/civic_receipts.py)."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from beehive.civic.batch_policy import (CivicRankedCandidate, CivicRunMode, published_cap,
                                        select_batch)
from beehive.civic.evidence import SUPPORTED_RECORD_KINDS, CivicRecordKind
from beehive.civic.limits import MAX_EVIDENCE_TEXT_CHARS
from beehive.civic.novelty import (
    MAX_NOVELTY_HISTORY,
    NOVELTY_WINDOW,
    CivicPriorEvent,
    find_duplicate_event,
    identify_event,
)
from beehive.civic.publication import (
    SCHEMA_VERSION,
    CivicPublicationCandidateError,
    CivicPublicationIntegrityError,
    CivicPublicationRefreshRequired,
    freeze_publication_envelope,
    preflight_candidate,
    publication_stamp,
    read_publication_envelope,
)
from beehive.db.civic_candidates import get_candidate, list_candidate_evidence
from beehive.domain.civic import (CivicBatch, CivicBatchStatus, CivicCandidate, CivicCandidateStatus,
                                   CivicRunStatus, is_terminal_batch_status,
                                   require_batch_transition, require_candidate_transition,
                                   require_candidate_publication_rejection,
                                   require_candidate_ai_refresh, require_candidate_ai_deferral,
                                   require_candidate_refresh_adoption,
                                   require_candidate_source_refresh)

_DEFAULT_MAX_ATTEMPTS = 5

# A Candidate id, rendered so that beehive.civic.batch_policy's (rank, candidate_id) tie-break
# sorts the way a reader expects. select_batch compares candidate ids as strings, so "10" would
# otherwise sort before "9" and the envelope's membership would depend on how many Candidates the
# Watch had happened to create.
_ID_WIDTH = 12

# The record kind a Candidate with no qualification receipt is queued under. Scope is decided at
# qualification (an out-of-scope record kind defers as unsupported and never reaches VERIFIED),
# so this only ever applies to a Candidate verified outside the evidence pipeline, and it exists
# purely so the capacity policy has a value to validate rather than to assert anything about what
# the statement is.
_DEFAULT_RECORD_KIND = CivicRecordKind.TIMELINE


class CivicPayloadHashMismatchError(ValueError):
    """A sealed Batch was replayed with bytes different from its first spool projection."""

    def __init__(self, batch_id: int, stored_hash: str, offered_hash: str) -> None:
        super().__init__(
            f"Civic Batch {batch_id} payload changed after it was bound "
            f"(stored {stored_hash[:12]}, offered {offered_hash[:12]})")
        self.batch_id = batch_id
        self.stored_hash = stored_hash
        self.offered_hash = offered_hash


def _policy_id(candidate_id: int) -> str:
    return f"{candidate_id:0{_ID_WIDTH}d}"


@dataclass(frozen=True)
class CivicCapacityDeferral:
    """One VERIFIED Candidate the daily cap held back, and where it went afterwards."""

    id: int | None
    watch_id: int
    candidate_id: int
    from_run_id: int
    from_batch_id: int
    published_cap: int
    run_mode: CivicRunMode
    deferred_at: datetime
    reason: str = "batch_cap_reached"
    to_run_id: int | None = None
    to_batch_id: int | None = None
    adopted_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.adopted_at is None


# Why a Run gave a Candidate back to the queue instead of publishing it. Keyed by the Run's own
# terminal state, so the audit says what actually happened rather than "something went wrong".
# 'run_completed_unsealed' is the defensive one: a Run has no business reaching COMPLETED without
# sealing, and if it ever does the Candidates must still come back rather than vanish quietly.
RELEASE_REASON_BY_RUN_STATUS: dict[CivicRunStatus, str] = {
    CivicRunStatus.FAILED: "run_failed",
    CivicRunStatus.CANCELLED: "run_cancelled",
    CivicRunStatus.COMPLETED: "run_completed_unsealed",
}
DEADLINE_RELEASE_REASON = "run_deadline_exceeded"


@dataclass(frozen=True)
class CivicReleaseResult:
    """What a Run that ended without sealing handed back.

    `released_candidate_ids` were publishable and are now waiting for a slot again.
    `rejected_candidate_ids` never got a verdict at all: a Run that died is not a judgment, but
    leaving them unsettled inside an envelope that can never seal would strand them somewhere no
    later Run could reach, so they are settled with the Run's own terminal reason."""

    run_id: int
    batch_id: int | None = None
    released_candidate_ids: tuple[int, ...] = ()
    rejected_candidate_ids: tuple[int, ...] = ()

    @property
    def released_count(self) -> int:
        return len(self.released_candidate_ids)


@dataclass(frozen=True)
class CivicAdoptionResult:
    """What one Run took over from earlier slots that ran out of room."""

    batch_id: int
    adopted_candidate_ids: tuple[int, ...] = ()

    @property
    def adopted_count(self) -> int:
        return len(self.adopted_candidate_ids)


def _row_to_deferral(row: sqlite3.Row) -> CivicCapacityDeferral:
    return CivicCapacityDeferral(
        id=row["id"],
        watch_id=row["watch_id"],
        candidate_id=row["candidate_id"],
        from_run_id=row["from_run_id"],
        from_batch_id=row["from_batch_id"],
        published_cap=row["published_cap"],
        run_mode=CivicRunMode(row["run_mode"]),
        deferred_at=datetime.fromisoformat(row["deferred_at"]),
        reason=row["reason"],
        to_run_id=row["to_run_id"],
        to_batch_id=row["to_batch_id"],
        adopted_at=(
            datetime.fromisoformat(row["adopted_at"]) if row["adopted_at"] else None))


@dataclass(frozen=True)
class CivicBatchLease:
    """A CivicBatch paired with the delivery-worker bookkeeping outside the pure domain
    contract: the lease expiry and the attempt this claim represents."""
    batch: CivicBatch
    lease_expires_at: str
    attempt_count: int


@dataclass(frozen=True)
class CivicBatchRecoveryResult:
    released_count: int
    failed_count: int


def _row_to_batch(row: sqlite3.Row) -> CivicBatch:
    return CivicBatch(
        id=row["id"],
        run_id=row["run_id"],
        watch_id=row["watch_id"],
        status=CivicBatchStatus(row["status"]),
        idempotency_key=row["idempotency_key"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        created_at=datetime.fromisoformat(row["created_at"]),
        content_hash=row["content_hash"],
        payload_hash=row["payload_hash"],
        candidate_count=row["candidate_count"],
        sealed_at=datetime.fromisoformat(row["sealed_at"]) if row["sealed_at"] else None,
        claim_token=row["claim_token"],
        settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
        next_attempt_at=(
            datetime.fromisoformat(row["next_attempt_at"]) if row["next_attempt_at"] else None),
        error_code=row["error_code"],
        error_detail=row["error_detail"])


def _fetch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM civic_batches WHERE id = ?", (batch_id,)).fetchone()


def _compute_content_hash(conn: sqlite3.Connection, batch_id: int) -> tuple[str, int]:
    """SHA-256 over the Batch's SEALED-IN Candidates in id order, plus their count.

    Membership is read from `sealed_into_batch_at`, never from `status`, and that is the whole
    point: seal stamps the column onto the Candidates that were VERIFIED at that instant, and
    nothing clears it afterwards. Reading status here instead would make the recomputed hash of
    an already-delivered envelope change the moment tomorrow's correction moved one of its
    Candidates from VERIFIED to SUPERSEDED -- a tamper check that fires on legitimate, expected
    editorial activity is worse than no tamper check at all.

    Seal itself calls this function AFTER stamping, so `recompute_content_hash` is not merely
    "equivalent to" seal's own computation, it is literally the same code reading the same
    immutable column. Only the fields that are actually published are hashed, so purely internal
    bookkeeping never moves the hash."""
    rows = conn.execute(
        "SELECT dedupe_key, title, statement FROM civic_candidates "
        "WHERE batch_id = ? AND sealed_into_batch_at IS NOT NULL ORDER BY id",
        (batch_id,)).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{row['dedupe_key']}\x1f{row['title']}\x1f{row['statement']}\x1e".encode())
    return digest.hexdigest(), len(rows)


def get_civic_batch(conn: sqlite3.Connection, batch_id: int) -> CivicBatch | None:
    row = _fetch(conn, batch_id)
    return _row_to_batch(row) if row else None


def get_frozen_publication(conn: sqlite3.Connection, batch_id: int) -> bytes | None:
    """Read and verify a frozen artifact. None means a legacy/non-publication seal, not rebuild."""
    row = conn.execute(
        "SELECT b.*, p.envelope_bytes, p.frozen_at, r.slot_at "
        "FROM civic_batches b JOIN civic_runs r ON r.id = b.run_id "
        "LEFT JOIN civic_batch_publications p ON p.batch_id = b.id WHERE b.id = ?",
        (batch_id,)).fetchone()
    if row is None:
        raise CivicPublicationIntegrityError(f"no Civic Batch with id={batch_id}")
    if row["publication_schema_version"] is None:
        if row["envelope_bytes"] is not None:
            raise CivicPublicationIntegrityError(
                f"Civic Batch {batch_id} has an unbound frozen publication")
        return None
    data = row["envelope_bytes"]
    if not isinstance(data, bytes):
        raise CivicPublicationIntegrityError(
            f"Civic Batch {batch_id} is missing its frozen publication")
    envelope = read_publication_envelope(data)
    expected = {
        "idempotency_key": row["idempotency_key"],
        "batch_content_hash": row["content_hash"],
        "payload_hash": row["payload_hash"],
        "slot_at": publication_stamp(datetime.fromisoformat(row["slot_at"]), "batch slot_at"),
        "sealed_at": row["sealed_at"],
    }
    if (envelope["batch"] != expected
            or envelope["schema_version"] != row["publication_schema_version"]
            or row["frozen_at"] != row["sealed_at"]):
        raise CivicPublicationIntegrityError(
            f"Civic Batch {batch_id} frozen publication metadata changed")
    content_hash, candidate_count = _compute_content_hash(conn, batch_id)
    keys = [
        candidate["dedupe_key"] for candidate in conn.execute(
            "SELECT dedupe_key FROM civic_candidates WHERE batch_id = ? "
            "AND sealed_into_batch_at IS NOT NULL ORDER BY id", (batch_id,))]
    if (content_hash != row["content_hash"] or candidate_count != row["candidate_count"]
            or keys != [item["record_key"] for item in envelope["items"]]):
        raise CivicPublicationIntegrityError(
            f"Civic Batch {batch_id} frozen publication membership changed")
    return data


@dataclass(frozen=True)
class CivicPublicationRejection:
    candidate_id: int
    batch_id: int
    reason: str
    detail: str
    rejected_at: datetime


def list_publication_rejections(
    conn: sqlite3.Connection, batch_id: int,
) -> list[CivicPublicationRejection]:
    return [
        CivicPublicationRejection(
            candidate_id=row["candidate_id"], batch_id=row["batch_id"],
            reason=row["reason"], detail=row["detail"],
            rejected_at=datetime.fromisoformat(row["rejected_at"]))
        for row in conn.execute(
            "SELECT * FROM civic_publication_rejections WHERE batch_id = ? "
            "ORDER BY candidate_id", (batch_id,))]


def get_batch_for_run(conn: sqlite3.Connection, run_id: int) -> CivicBatch | None:
    row = conn.execute("SELECT * FROM civic_batches WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_batch(row) if row else None


def get_batch_by_idempotency_key(
    conn: sqlite3.Connection, idempotency_key: str,
) -> CivicBatch | None:
    row = conn.execute(
        "SELECT * FROM civic_batches WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return _row_to_batch(row) if row else None


def get_or_create_batch_if_claimed(
    conn: sqlite3.Connection,
    run_id: int,
    claim_token: str,
    now: datetime,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicBatch | None:
    """Returns the Run's single Batch, creating it if this is the first time. Fenced on
    (run_id, claim_token, status='processing'): a worker that has lost its Run claim gets None
    and writes nothing, so a stale worker can never mint or resume an envelope. Resume-safe by
    construction -- a reclaimed Run finds its existing envelope in whatever state it was left."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT watch_id, deadline_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if (run is None or (
                run["deadline_at"] is not None
                and now >= datetime.fromisoformat(run["deadline_at"]))):
            batch = None
        else:
            existing = conn.execute(
                "SELECT * FROM civic_batches WHERE run_id = ?", (run_id,)).fetchone()
            if existing is not None:
                batch = _row_to_batch(existing)
            else:
                cur = conn.execute(
                    "INSERT INTO civic_batches (run_id, watch_id, status, idempotency_key, "
                    "max_attempts, created_at) VALUES (?, ?, 'building', ?, ?, ?)",
                    (run_id, run["watch_id"], secrets.token_urlsafe(24), max_attempts,
                     now.isoformat()))
                batch = _row_to_batch(_fetch(conn, cur.lastrowid))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return batch


def _queue_for_selection(
    conn: sqlite3.Connection, batch_id: int,
) -> tuple[list[CivicRankedCandidate], dict[str, int]]:
    """This Batch's VERIFIED Candidates as beehive.civic.batch_policy's own queue type.

    Rank is the Candidate's position in id order, which is the order clustering created them in
    and therefore most-recent-development first. It is a total order (ids are unique), so the
    queue never depends on how rows came back and `select_batch`'s (rank, candidate_id)
    tie-break never has to decide anything.

    `record_kind` comes from the Candidate's own qualification receipt, and scope is *rechecked*
    here rather than assumed: a receipt naming a record kind outside the automated publication
    scope on a Candidate that nonetheless reached VERIFIED is an integrity fault (qualification
    should have deferred it as unsupported), and it raises instead of being published. A
    Candidate with no receipt at all -- verified through the repository directly, as an operator
    tool or an older Run may have done -- is queued under the neutral default, because scope was
    already decided upstream and refusing it here would strand a fully verified statement in no
    envelope at all.

    Publication-scoped seals run the shared item preflight before this capacity-only selection.
    A generic Civic Batch is not a PartyMap publication and cannot be spooled as one."""
    rows = conn.execute(
        "SELECT c.id AS id, c.dedupe_key AS dedupe_key, q.record_kind AS record_kind "
        "FROM civic_candidates c "
        "LEFT JOIN civic_qualifications q ON q.candidate_id = c.id "
        "AND q.ai_revision = c.ai_revision "
        "AND q.research_attempts_used = ("
        "  SELECT MAX(research_attempts_used) FROM civic_qualifications "
        "  WHERE candidate_id = c.id AND ai_revision = c.ai_revision"
        ") "
        "WHERE c.batch_id = ? AND c.status = 'verified' AND c.sealed_into_batch_at IS NULL "
        "ORDER BY c.id",
        (batch_id,)).fetchall()
    queue: list[CivicRankedCandidate] = []
    by_policy_id: dict[str, int] = {}
    rank = 0
    for row in rows:
        rank += 1
        policy_id = _policy_id(row["id"])
        by_policy_id[policy_id] = row["id"]
        queue.append(CivicRankedCandidate(
            candidate_id=policy_id, rank=rank, fingerprint=row["dedupe_key"],
            record_kind=_queueable_record_kind(row["id"], row["record_kind"])))
    return queue, by_policy_id


def _preflight_publication(
    conn: sqlite3.Connection, batch_id: int, now: datetime,
) -> dict[int, dict]:
    """Settle expected candidate failures and retain the exact valid items, under seal's lock."""
    from beehive.db.civic_evidence_captures import (
        CivicCaptureIntegrityError, CivicSourceRefreshBlocked, _hold_source_locked,
    )

    items: dict[int, dict] = {}
    batch = get_civic_batch(conn, batch_id)
    if batch is None:
        raise CivicPublicationIntegrityError(f"no Civic Batch with id={batch_id}")
    rows = conn.execute(
        "SELECT id FROM civic_candidates WHERE batch_id = ? AND status = 'verified' "
        "AND sealed_into_batch_at IS NULL ORDER BY id", (batch_id,)).fetchall()
    for row in rows:
        candidate_id = row["id"]
        try:
            items[candidate_id] = preflight_candidate(conn, candidate_id)
        except CivicCaptureIntegrityError:
            candidate = get_candidate(conn, candidate_id)
            _hold_source_locked(conn, candidate, batch, "source_capture_integrity_error", now)
        except CivicSourceRefreshBlocked as exc:
            candidate = get_candidate(conn, candidate_id)
            _hold_source_locked(conn, candidate, batch, exc.reason, now)
        except CivicPublicationRefreshRequired as exc:
            candidate = get_candidate(conn, candidate_id)
            _refresh_candidate_for_ai_locked(
                conn, candidate=candidate, batch=batch, reason=exc.reason,
                expected_revision=candidate.ai_revision, now=now, defer=True)
        except CivicPublicationCandidateError as exc:
            _reject_publication_candidate(conn, batch, candidate_id, exc.reason, exc.detail, now)
    return items


def _reject_publication_candidate(
    conn: sqlite3.Connection, batch: CivicBatch, candidate_id: int,
    reason: str, detail: str, now: datetime,
) -> None:
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise CivicPublicationIntegrityError(
            f"Civic Candidate {candidate_id} disappeared during publication preflight")
    require_candidate_publication_rejection(candidate, batch)
    rejected = conn.execute(
        "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, rejection_code = ? "
        "WHERE id = ? AND batch_id = ? AND status = 'verified' AND sealed_into_batch_at IS NULL",
        (now.isoformat(), reason, candidate_id, batch.id))
    if rejected.rowcount != 1:
        raise CivicPublicationIntegrityError(
            f"Civic Candidate {candidate_id} moved during publication preflight")
    conn.execute(
        "INSERT INTO civic_publication_rejections "
        "(candidate_id, batch_id, reason, detail, rejected_at) VALUES (?, ?, ?, ?, ?)",
        (candidate_id, batch.id, reason, detail, now.isoformat()))


def _cited_texts(conn: sqlite3.Connection, candidate_id: int, item: dict) -> tuple[str, ...]:
    keys = {citation["document_key"] for citation in item["citations"]}
    return tuple(
        f"{evidence.title}\n{evidence.excerpt[:MAX_EVIDENCE_TEXT_CHARS]}"
        for evidence in list_candidate_evidence(conn, candidate_id)
        if evidence.external_key in keys)


def _prior_publication_events(
    conn: sqlite3.Connection, batch: CivicBatch, now: datetime,
) -> list[CivicPriorEvent]:
    """Read immutable final outputs; never rebuild legacy snapshots or revalidate old policy."""
    from beehive.civic.ai_gateway import CivicAiStage
    from beehive.db.civic_ai_stages import get_stage_receipt, output_hash_for

    rows = conn.execute(
        "SELECT c.id, c.batch_id, c.dedupe_key FROM civic_candidates c "
        "JOIN civic_batches b ON b.id = c.batch_id "
        "WHERE b.watch_id = ? AND b.id != ? AND c.sealed_into_batch_at IS NOT NULL "
        "AND b.status IN ('sealed', 'delivering', 'delivered') "
        "AND julianday(b.sealed_at) >= julianday(?) "
        "ORDER BY b.sealed_at DESC, c.id DESC LIMIT ?",
        (batch.watch_id, batch.id, (now - NOVELTY_WINDOW).isoformat(),
         MAX_NOVELTY_HISTORY + 1)).fetchall()
    if len(rows) > MAX_NOVELTY_HISTORY:
        raise CivicPublicationIntegrityError(
            "publication novelty history exceeds its bounded comparison window")
    projections: dict[int, dict | None] = {}
    records = []
    for row in rows:
        prior_batch = row["batch_id"]
        if prior_batch not in projections:
            data = get_frozen_publication(conn, prior_batch)
            projections[prior_batch] = (
                {item["record_key"]: item for item in json.loads(data)["items"]}
                if data is not None else None)
        projected = projections[prior_batch]
        if projected is not None:
            item = projected[row["dedupe_key"]]
            sources = _cited_texts(conn, row["id"], item)
        else:
            receipt = get_stage_receipt(conn, row["id"], CivicAiStage.GENERATE)
            if receipt is None:
                continue
            try:
                output = receipt.output
            except json.JSONDecodeError as exc:
                raise CivicPublicationIntegrityError(
                    f"historical Candidate {row['id']} generation artifact is not JSON") from exc
            if output_hash_for(output) != receipt.output_hash:
                raise CivicPublicationIntegrityError(
                    f"historical Candidate {row['id']} generation artifact hash changed")
            entry = output.get("entry") if isinstance(output, dict) else None
            english = entry.get("en") if isinstance(entry, dict) else None
            if not isinstance(english, dict) or not all(
                    isinstance(english.get(field), str) for field in ("title", "summary", "event_date")):
                continue
            entities = english.get("entities")
            if not isinstance(entities, list) or len(entities) != 1 or not isinstance(entities[0], str):
                continue
            item = {
                "subject": {"id": entities[0]}, "event_date": english["event_date"],
                "locales": {"en": english},
            }
            # Old aliases are not reconstructed under today's evidence ordering/filter rules.
            sources = ()
        identity = identify_event(item, source_texts=sources)
        if identity is not None:
            records.append(CivicPriorEvent(row["id"], prior_batch, identity))
    return records


def _filter_novel_publication_queue(
    conn: sqlite3.Connection, batch: CivicBatch, queue: list[CivicRankedCandidate],
    by_policy_id: dict[str, int], items: dict[int, dict], mode: CivicRunMode, now: datetime,
) -> list[CivicRankedCandidate]:
    if not queue:
        return queue
    prior = _prior_publication_events(conn, batch, now)
    kept = []
    admitted = 0
    cap = published_cap(mode)
    for queued in queue:
        candidate_id = by_policy_id[queued.candidate_id]
        item = items[candidate_id]
        identity = identify_event(item, source_texts=_cited_texts(conn, candidate_id, item))
        duplicate = find_duplicate_event(identity, prior)
        if duplicate is not None:
            _reject_publication_candidate(
                conn, batch, candidate_id, "publication_duplicate_event",
                f"Same {identity.stage} event as Candidate {duplicate.candidate_id} "
                f"in Batch {duplicate.batch_id}: {' '.join(identity.entity)}", now)
            continue
        kept.append(queued)
        if admitted < cap:
            admitted += 1
            # An unsealed overflow Candidate cannot suppress another deferred peer.
            if identity is not None:
                prior.append(CivicPriorEvent(candidate_id, batch.id, identity))
    return kept


def _queueable_record_kind(candidate_id: int, value: str | None) -> CivicRecordKind:
    if not value:
        return _DEFAULT_RECORD_KIND
    try:
        kind = CivicRecordKind(value)
    except ValueError as exc:
        raise ValueError(
            f"Civic Candidate {candidate_id} was verified under an unknown record kind "
            f"{value!r}") from exc
    if kind not in SUPPORTED_RECORD_KINDS:
        raise ValueError(
            f"Civic Candidate {candidate_id} was verified under record kind {value!r}, which is "
            "outside the automated publication scope and should have deferred as unsupported")
    return kind


def _defer_for_capacity(
    conn: sqlite3.Connection,
    *,
    watch_id: int,
    run_id: int,
    batch_id: int,
    candidate_ids: list[int],
    mode: CivicRunMode,
    cap: int,
    now: datetime,
) -> None:
    """Hold Candidates back for capacity, inside the caller's seal transaction.

    Everything except run/batch membership is left exactly as it is: the evidence, the
    verification, the qualification receipt and the `civic_consumed_items` rows all still belong
    to this Candidate, so a later Run adopts a decision that has already been made rather than
    re-making it. The Candidate keeps its `settled_at` -- it settled when it was verified, and
    the cap did not un-verify it."""
    for candidate_id in candidate_ids:
        require_candidate_transition(
            CivicCandidateStatus.VERIFIED, CivicCandidateStatus.DEFERRED_CAPACITY)
        conn.execute(
            "UPDATE civic_candidates SET status = 'deferred_capacity' "
            "WHERE id = ? AND status = 'verified' AND sealed_into_batch_at IS NULL",
            (candidate_id,))
        conn.execute(
            "INSERT OR IGNORE INTO civic_capacity_deferrals (watch_id, candidate_id, "
            "from_run_id, from_batch_id, published_cap, run_mode, reason, deferred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'batch_cap_reached', ?)",
            (watch_id, candidate_id, run_id, batch_id, cap, mode.value, now.isoformat()))


def seal_batch_if_claimed(
    conn: sqlite3.Connection,
    batch_id: int,
    run_id: int,
    claim_token: str,
    now: datetime,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicBatch | None:
    """Freezes a BUILDING envelope, all under one BEGIN IMMEDIATE so a reader can never observe a
    sealed Batch whose hash does not match its own membership.

    A ``partymap`` Watch always uses the publication-v1 preflight and frozen-byte store, including
    low-level/operator callers. No caller can disable its gate. Other Watches retain generic
    content-only sealing and cannot be projected as new PartyMap publications.

    **Every Candidate must be settled first.** A Batch holding even one DRAFTED / RESEARCHING /
    VERIFYING Candidate raises ValueError: sealing then would either publish a statement nobody
    had finished verifying, or silently drop one that was still on its way to being verified.
    Both are exactly the failure a durable pipeline exists to prevent, so the seal waits instead
    of guessing.

    **Only VERIFIED Candidates enter the envelope, and only as many as the day may publish.**
    Once everything is settled, `beehive.civic.batch_policy.select_batch` takes the highest
    ranked Candidates up to this Run's cap -- six normally, ten in an election period, decided by
    the Run's own `run_mode` rather than by anything a source said. The selected Candidates are
    stamped with `sealed_into_batch_at` and are precisely the rows hashed and counted. REJECTED
    Candidates -- and any Candidate already SUPERSEDED before the seal, meaning this same Run
    replaced it -- are excluded from both the hash and candidate_count, while staying in the
    table as the record of what was considered and why it was dropped.

    **Overflow is carried, not lost.** Anything above the cap moves to DEFERRED_CAPACITY, keeps
    its evidence, verification, qualification receipt and consumed-item ownership, and is
    recorded in `civic_capacity_deferrals` with the Batch it was held back from. It never enters
    this envelope's hash and never receives the stamp, so today's publication is unaffected; a
    later Run with room adopts it (`adopt_capacity_deferrals`) without requalifying it and
    without creating a second Candidate for the same development. The receiving Run does not have
    to exist yet -- a Batch that has to defer cannot be made to wait for tomorrow.

    A Batch whose Candidates were all rejected seals with candidate_count 0 and the empty-set
    hash, identical to a Batch that never had any: a quiet civic day publishes nothing, which is
    a valid, hashable envelope rather than an error. Two such envelopes sharing a content_hash is
    harmless -- a Civic Delivery Receipt is keyed on the Batch's own unique idempotency_key, never
    on the hash.

    **Idempotent.** Sealing a Batch that is already SEALED returns it completely unchanged --
    same content_hash, same candidate_count, same sealed_at, nothing rewritten, nothing
    re-stamped and nothing re-deferred. That makes the crash-and-resume path safe: a worker that
    sealed, died before advancing its phase, and was reclaimed simply calls this again. A Batch
    that has moved PAST sealed (delivering/delivered/quarantined/failed) still raises, since
    re-sealing would overwrite a hash a delivery may already have been receipted against --
    DELIVERING -> SEALED is a legal *release* transition, but releasing a failed delivery and
    sealing an envelope are different operations and only the former may use it.

    Fenced on the Run's claim and live lease/deadline: a new seal returns None, writing nothing,
    if its authorization expires. Reading an already sealed envelope never re-evaluates it."""
    from beehive.civic.scheduling import PARTYMAP_GLOBAL_WATCH_SUBJECT

    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT civic_batches.status AS status, civic_batches.watch_id AS watch_id, "
            "civic_runs.run_mode AS run_mode, civic_runs.deadline_at AS deadline_at, "
            "civic_runs.lease_expires_at AS lease_expires_at, "
            "civic_runs.slot_at AS slot_at, civic_watches.subject AS watch_subject "
            "FROM civic_batches "
            "JOIN civic_runs ON civic_runs.id = civic_batches.run_id "
            "JOIN civic_watches ON civic_watches.id = civic_batches.watch_id "
            "WHERE civic_batches.id = ? AND civic_runs.id = ? AND civic_runs.claim_token = ? "
            "AND civic_runs.status = 'processing'",
            (batch_id, run_id, claim_token)).fetchone()
        if row is None:
            sealed = None
        elif CivicBatchStatus(row["status"]) is CivicBatchStatus.SEALED:
            # Already sealed: hand back the identical envelope rather than re-hashing it.
            sealed = _row_to_batch(_fetch(conn, batch_id))
        elif (row["lease_expires_at"] is None
              or now >= datetime.fromisoformat(row["lease_expires_at"])
              or (row["deadline_at"] is not None
                  and now >= datetime.fromisoformat(row["deadline_at"]))):
            sealed = None
        elif CivicBatchStatus(row["status"]) is not CivicBatchStatus.BUILDING:
            # DELIVERING -> SEALED is a legal *release* transition, so the domain table alone
            # would wave a re-seal through. Sealing is a different operation and only ever
            # applies to a Batch that has not been sealed yet.
            raise ValueError(
                f"cannot seal Civic Batch {batch_id}: status is {row['status']!r}, not "
                "'building'")
        else:
            require_batch_transition(CivicBatchStatus.BUILDING, CivicBatchStatus.SEALED)
            unsettled = conn.execute(
                "SELECT COUNT(*) AS n FROM civic_candidates "
                "WHERE batch_id = ? AND status IN ('drafted', 'researching', 'verifying')",
                (batch_id,)).fetchone()
            if unsettled["n"]:
                raise ValueError(
                    f"cannot seal Civic Batch {batch_id}: {unsettled['n']} Civic Candidate(s) "
                    "are still unsettled")
            now_iso = now.isoformat()
            mode = CivicRunMode(row["run_mode"])
            publication_scoped = row["watch_subject"] == PARTYMAP_GLOBAL_WATCH_SUBJECT
            items = _preflight_publication(conn, batch_id, now) if publication_scoped else {}
            queue, by_policy_id = _queue_for_selection(conn, batch_id)
            if publication_scoped:
                queue = _filter_novel_publication_queue(
                    conn, get_civic_batch(conn, batch_id), queue, by_policy_id, items, mode, now)
            selection = select_batch(queue, mode=mode)
            selected = [by_policy_id[key] for key in selection.published_candidate_ids]
            deferred = [by_policy_id[key] for key in selection.deferred_candidate_ids]
            for candidate_id in selected:
                conn.execute(
                    "UPDATE civic_candidates SET sealed_into_batch_at = ? "
                    "WHERE id = ? AND status = 'verified' AND sealed_into_batch_at IS NULL",
                    (now_iso, candidate_id))
            _defer_for_capacity(
                conn, watch_id=row["watch_id"], run_id=run_id, batch_id=batch_id,
                candidate_ids=deferred, mode=mode, cap=selection.published_cap, now=now)
            content_hash, candidate_count = _compute_content_hash(conn, batch_id)
            payload_hash = None
            publication_schema_version = None
            if publication_scoped:
                data = freeze_publication_envelope(
                    idempotency_key=_fetch(conn, batch_id)["idempotency_key"],
                    content_hash=content_hash,
                    slot_at=datetime.fromisoformat(row["slot_at"]),
                    sealed_at=now,
                    items=[items[candidate_id] for candidate_id in selected])
                payload_hash = read_publication_envelope(data)["batch"]["payload_hash"]
                publication_schema_version = SCHEMA_VERSION
                conn.execute(
                    "INSERT INTO civic_batch_publications "
                    "(batch_id, envelope_bytes, frozen_at) VALUES (?, ?, ?)",
                    (batch_id, data, now_iso))
            if now_fn is not None:
                final_now = now_fn()
                if (final_now >= datetime.fromisoformat(row["lease_expires_at"])
                        or (row["deadline_at"] is not None
                            and final_now >= datetime.fromisoformat(row["deadline_at"]))):
                    conn.rollback()
                    return None
            conn.execute(
                "UPDATE civic_batches SET status = 'sealed', content_hash = ?, "
                "candidate_count = ?, sealed_at = ?, payload_hash = ?, "
                "publication_schema_version = ? WHERE id = ? AND status = 'building'",
                (content_hash, candidate_count, now_iso, payload_hash,
                 publication_schema_version, batch_id))
            sealed = _row_to_batch(_fetch(conn, batch_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return sealed


def adopt_capacity_deferrals(
    conn: sqlite3.Connection,
    batch_id: int,
    run_id: int,
    claim_token: str,
    now: datetime,
    *,
    limit: int | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicAdoptionResult | None:
    """Take over the Candidates earlier slots ran out of room for, into this Run's BUILDING Batch.

    This is the other half of the cap. A Candidate held back yesterday is already VERIFIED, still
    holds its evidence, its verification and its qualification receipt, and still owns the source
    items it was built from -- so adoption moves *only* the run/batch-scoped fields (`run_id`,
    `batch_id`, and the status back to VERIFIED) and touches nothing that would amount to
    deciding it again. No requalification, and no second Candidate for a development that already
    has one.

    Called unconditionally at the start of a Run, including a Run whose connectors returned
    nothing: an empty news day is exactly when there is room for yesterday's overflow, so
    adoption must not be reachable only through the ingest path.

    Idempotent and concurrency-safe. The whole pass runs under one BEGIN IMMEDIATE fenced on
    (run_id, claim_token, status='processing'), the live lease/deadline and the Batch still BUILDING,
    and each row is
    moved with a status-guarded UPDATE, so of two workers racing on the same carryover exactly
    one adopts it and the other simply finds nothing left. Re-running it after it has already
    adopted everything returns an empty result rather than doing anything twice.

    Deferral history is closed, not overwritten: the open `civic_capacity_deferrals` row gets its
    to_run_id/to_batch_id/adopted_at filled in, so "carried from Monday's Batch into Wednesday's"
    stays readable. A Candidate deferred again by this Run's own seal opens a fresh row rather
    than reusing the closed one.

    Returns None -- writing nothing -- when the claim expires or no longer matches, or the Batch
    is no longer BUILDING. now_fn samples time under the lock and before commit."""
    if limit is not None and limit < 0:
        raise ValueError("limit must not be negative")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        row = conn.execute(
            "SELECT civic_batches.status AS status, civic_batches.watch_id AS watch_id, "
            "civic_runs.lease_expires_at, civic_runs.deadline_at "
            "FROM civic_batches JOIN civic_runs ON civic_runs.id = civic_batches.run_id "
            "WHERE civic_batches.id = ? AND civic_runs.id = ? AND civic_runs.claim_token = ? "
            "AND civic_runs.status = 'processing'",
            (batch_id, run_id, claim_token)).fetchone()
        expires_at = None
        if (row is not None and row["lease_expires_at"] is not None
                and row["deadline_at"] is not None):
            expires_at = min(
                datetime.fromisoformat(row["lease_expires_at"]),
                datetime.fromisoformat(row["deadline_at"]))
        if (row is None or row["status"] != CivicBatchStatus.BUILDING.value
                or expires_at is None or now >= expires_at):
            result = None
        else:
            adopted = _adopt_locked(
                conn, watch_id=row["watch_id"], run_id=run_id, batch_id=batch_id, now=now,
                limit=limit)
            if now_fn is not None and now_fn() >= expires_at:
                conn.rollback()
                return None
            result = CivicAdoptionResult(
                batch_id=batch_id, adopted_candidate_ids=tuple(adopted))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


@dataclass(frozen=True)
class CivicAiDeferralResult:
    """What one AI deferral did: carried the Candidate over, found it already carried, or reached
    the end of its bounded lifetime and settled it."""

    candidate_id: int
    reason: str
    deferred: bool
    exhausted: bool
    deferral_count: int


def _adopt_locked(
    conn: sqlite3.Connection,
    *,
    watch_id: int,
    run_id: int,
    batch_id: int,
    now: datetime,
    limit: int | None,
) -> list[int]:
    """The adoption itself, inside the caller's transaction. Oldest carryover first, so a
    Candidate held back twice is never overtaken by one deferred today.

    Two kinds of carryover are adopted, and they resume in different states because they were
    held back for different reasons. A DEFERRED_CAPACITY Candidate was already VERIFIED and only
    the daily cap kept it out, so it comes back VERIFIED and is ready to seal. A DEFERRED_AI
    Candidate never finished generating -- the Run ran out of AI budget, or a transient model
    failure outlived its bounded retry -- so it comes back VERIFYING, resumes at the first stage
    with no receipt, and still has to pass the independent verifier. Adopting it straight to
    VERIFIED would publish a statement no verifier ever saw."""
    sql = (
        "SELECT id, status, ai_revision, ai_refresh_reason FROM civic_candidates "
        "WHERE watch_id = ? AND status IN ('deferred_capacity', 'deferred_ai') "
        "AND batch_id != ? ORDER BY id")
    params: tuple = (watch_id, batch_id)
    if limit is not None:
        sql += " LIMIT ?"
        params = (watch_id, batch_id, limit)
    now_iso = now.isoformat()
    adopted: list[int] = []
    for candidate in conn.execute(sql, params).fetchall():
        candidate_id = candidate["id"]
        source = CivicCandidateStatus(candidate["status"])
        target = (CivicCandidateStatus.VERIFYING
                  if source is CivicCandidateStatus.DEFERRED_AI
                  else CivicCandidateStatus.VERIFIED)
        if source is CivicCandidateStatus.DEFERRED_AI and (
                candidate["ai_revision"] > 0
                or (candidate["ai_refresh_reason"] or "").startswith("source_refresh:")):
            from beehive.db.civic_verifications import pending_research_attempt

            qualification = conn.execute(
                "SELECT outcome FROM civic_qualifications WHERE candidate_id = ? "
                "AND ai_revision = ? ORDER BY research_attempts_used DESC LIMIT 1",
                (candidate_id, candidate["ai_revision"])).fetchone()
            if pending_research_attempt(conn, candidate_id) is not None:
                target = CivicCandidateStatus.RESEARCHING
            elif qualification is None:
                target = CivicCandidateStatus.DRAFTED
            elif qualification["outcome"] == "deferred_evidence":
                target = CivicCandidateStatus.RESEARCHING
        if target in {CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING}:
            require_candidate_refresh_adoption(get_candidate(conn, candidate_id), target)
        else:
            require_candidate_transition(source, target)
        settled_at = "NULL" if target in {
            CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
            CivicCandidateStatus.VERIFYING} else "settled_at"
        moved = conn.execute(
            f"UPDATE civic_candidates SET status = ?, settled_at = {settled_at}, run_id = ?, "
            "batch_id = ? WHERE id = ? AND status = ? AND sealed_into_batch_at IS NULL",
            (target.value, run_id, batch_id, candidate_id, source.value))
        if not moved.rowcount:
            continue
        conn.execute(
            "UPDATE civic_capacity_deferrals SET to_run_id = ?, to_batch_id = ?, adopted_at = ? "
            "WHERE candidate_id = ? AND adopted_at IS NULL AND from_batch_id != ?",
            (run_id, batch_id, now_iso, candidate_id, batch_id))
        adopted.append(candidate_id)
    return adopted


def release_terminal_run_holds_locked(
    conn: sqlite3.Connection, *, run_id: int, reason: str, now: datetime,
) -> CivicReleaseResult:
    """Give back everything a Run was holding when it ended without sealing. Caller holds the
    transaction.

    A Run that goes terminal before its seal leaves two kinds of Candidate stranded, and both are
    the same bug seen from different ends: their `run_id` and `batch_id` point at an envelope that
    can never be sealed, while the source items that produced them stay permanently consumed. The
    next Run therefore skips those documents -- `civic_consumed_items` is deliberately never
    released -- so without this the material is not delayed, it is gone.

      * **VERIFIED and unstamped** -- an adopted carryover the cap moved here, or a Candidate this
        Run verified itself; the two are indistinguishable and neither should be lost. Each goes
        back to DEFERRED_CAPACITY and opens a fresh deferral row from *this* Batch, so a later
        same-Watch Run adopts it exactly as it would any other carryover. Nothing else moves: the
        evidence, the verification, the qualification receipt and the consumed-item claims are
        all still the Candidate's, so it is republished on the decision that was already taken
        rather than being decided again.
      * **Refreshing work** -- an unsealed AI revision keeps its progress in DEFERRED_AI under
        the same bounded deferral policy, including when the Run fails before requalification.
      * **Other unsettled work** (DRAFTED / RESEARCHING / VERIFYING) -- never verified, so there is nothing
        publishable to preserve, but leaving it unsettled would park it in an envelope no seal
        can ever close. It is settled REJECTED with the Run's own terminal reason as its code, so
        the record says a Run ended rather than implying the material was judged and refused.

    Deliberately *not* called when a Run is requeued: a Run whose lease expired but whose deadline
    is still ahead resumes with its work intact, and handing its Candidates back would turn a
    retry into a redistribution.

    Idempotent. Every write is guarded on the state it is changing, and the deferral insert is
    guarded by UNIQUE(candidate_id, from_batch_id), so a second call -- a repeated failure, a
    recovery sweep after an explicit completion -- finds nothing left to do and writes nothing."""
    batch = conn.execute(
        "SELECT b.id AS id, b.watch_id AS watch_id, r.run_mode AS run_mode "
        "FROM civic_batches b JOIN civic_runs r ON r.id = b.run_id WHERE b.run_id = ?",
        (run_id,)).fetchone()
    if batch is None:
        return CivicReleaseResult(run_id=run_id)
    mode = CivicRunMode(batch["run_mode"])
    cap = published_cap(mode)
    now_iso = now.isoformat()

    released: list[int] = []
    for row in conn.execute(
            "SELECT id FROM civic_candidates WHERE batch_id = ? AND status = 'verified' "
            "AND sealed_into_batch_at IS NULL ORDER BY id", (batch["id"],)).fetchall():
        require_candidate_transition(
            CivicCandidateStatus.VERIFIED, CivicCandidateStatus.DEFERRED_CAPACITY)
        moved = conn.execute(
            "UPDATE civic_candidates SET status = 'deferred_capacity' "
            "WHERE id = ? AND status = 'verified' AND sealed_into_batch_at IS NULL",
            (row["id"],))
        if not moved.rowcount:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO civic_capacity_deferrals (watch_id, candidate_id, "
            "from_run_id, from_batch_id, published_cap, run_mode, reason, deferred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch["watch_id"], row["id"], run_id, batch["id"], cap, mode.value, reason,
             now_iso))
        released.append(row["id"])

    rejected: list[int] = []
    for row in conn.execute(
            "SELECT id, status, ai_revision, ai_refresh_reason FROM civic_candidates WHERE batch_id = ? "
            "AND status IN ('drafted', 'researching', 'verifying') ORDER BY id",
            (batch["id"],)).fetchall():
        if (row["ai_refresh_reason"] or "").startswith("source_refresh:"):
            from beehive.db.civic_evidence_captures import _hold_source_locked

            _hold_source_locked(
                conn, get_candidate(conn, row["id"]), get_civic_batch(conn, batch["id"]),
                "source_refresh_interrupted", now)
            released.append(row["id"])
            continue
        if row["ai_revision"] > 0:
            result = _defer_candidate_for_ai_locked(
                conn, candidate=get_candidate(conn, row["id"]),
                batch=get_civic_batch(conn, batch["id"]),
                reason="ai_provenance_stale", now=now,
                max_deferrals=MAX_AI_DEFERRALS_PER_CANDIDATE)
            (rejected if result.exhausted else released).append(row["id"])
            continue
        require_candidate_transition(
            CivicCandidateStatus(row["status"]), CivicCandidateStatus.REJECTED)
        settled = conn.execute(
            "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, "
            "rejection_code = ? WHERE id = ? "
            "AND status IN ('drafted', 'researching', 'verifying')",
            (now_iso, reason, row["id"]))
        if settled.rowcount:
            rejected.append(row["id"])

    return CivicReleaseResult(
        run_id=run_id, batch_id=batch["id"], released_candidate_ids=tuple(released),
        rejected_candidate_ids=tuple(rejected))


def release_terminal_run_holds(
    conn: sqlite3.Connection, run_id: int, now: datetime, *, reason: str | None = None,
) -> CivicReleaseResult | None:
    """Standalone reconciliation for a Run that is already terminal.

    Every terminal path in `db/civic_runs.py` already calls the locked form inside the same
    transaction that ends the Run, so this exists for the sweep that finds an older Run nothing
    ever released -- one failed before this behavior existed, say. Refuses a Run that is still
    live (returns None, writing nothing): handing back the Candidates of a Run that can still
    resume would take work away from a worker that is entitled to finish it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            result = None
        else:
            status = CivicRunStatus(row["status"])
            if status not in RELEASE_REASON_BY_RUN_STATUS:
                result = None
            else:
                result = release_terminal_run_holds_locked(
                    conn, run_id=run_id,
                    reason=reason or RELEASE_REASON_BY_RUN_STATUS[status], now=now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


# How many times one Candidate may be carried to a later slot for an AI reason before the
# pipeline gives up on it. Bounded on purpose: a Candidate that has been deferred this often is
# not being held back by luck, and an unbounded carryover would keep consuming a Run's budget
# forever while its evidence quietly aged out of relevance.
MAX_AI_DEFERRALS_PER_CANDIDATE = 3

# The reasons an AI deferral can carry. None is an evidence finding or the daily
# cap, which is exactly why they are their own values: recording an exhausted call budget as
# `batch_cap_reached` would claim the Candidate was ready to publish, and recording it as a
# rejection would destroy it.
AI_DEFERRAL_REASONS = ("ai_budget_exhausted", "ai_transient_failed", "ai_provenance_stale")


def count_ai_deferrals(conn: sqlite3.Connection, candidate_id: int) -> int:
    """How many times this Candidate has already been carried over for an AI reason."""
    placeholders = ", ".join("?" for _ in AI_DEFERRAL_REASONS)
    return int(conn.execute(
        f"SELECT COUNT(*) AS n FROM civic_capacity_deferrals "
        f"WHERE candidate_id = ? AND reason IN ({placeholders})",
        (candidate_id, *AI_DEFERRAL_REASONS)).fetchone()["n"])


def _defer_candidate_for_ai_locked(
    conn: sqlite3.Connection, *, candidate: CivicCandidate, batch: CivicBatch,
    reason: str, now: datetime, max_deferrals: int,
) -> CivicAiDeferralResult:
    used = count_ai_deferrals(conn, candidate.id)
    if candidate.status is CivicCandidateStatus.DEFERRED_AI:
        return CivicAiDeferralResult(candidate.id, reason, False, False, used)
    if used >= max_deferrals:
        require_candidate_transition(candidate.status, CivicCandidateStatus.REJECTED)
        conn.execute(
            "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, rejection_code = ? "
            "WHERE id = ?", (now.isoformat(), f"{reason}_lifetime_exhausted", candidate.id))
        return CivicAiDeferralResult(candidate.id, reason, False, True, used)
    require_candidate_ai_deferral(candidate, batch)
    conn.execute(
        "UPDATE civic_candidates SET status = 'deferred_ai', settled_at = ? WHERE id = ?",
        (now.isoformat(), candidate.id))
    mode = CivicRunMode(conn.execute(
        "SELECT run_mode FROM civic_runs WHERE id = ?", (batch.run_id,)).fetchone()["run_mode"])
    conn.execute(
        "INSERT OR IGNORE INTO civic_capacity_deferrals (watch_id, candidate_id, from_run_id, "
        "from_batch_id, published_cap, run_mode, reason, deferred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (batch.watch_id, candidate.id, batch.run_id, batch.id, published_cap(mode),
         mode.value, reason, now.isoformat()))
    return CivicAiDeferralResult(candidate.id, reason, True, False, used + 1)


def _pending_ai_refresh(conn: sqlite3.Connection, candidate: CivicCandidate) -> bool:
    return (
        candidate.ai_revision > 0
        and candidate.ai_refresh_reason is not None
        and candidate.status in {
            CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
            CivicCandidateStatus.DEFERRED_AI,
        }
        and conn.execute(
            "SELECT 1 FROM civic_qualifications WHERE candidate_id = ? AND ai_revision = ?",
            (candidate.id, candidate.ai_revision)).fetchone() is None
        and conn.execute(
            "SELECT 1 FROM civic_ai_stages WHERE candidate_id = ? AND ai_revision = ?",
            (candidate.id, candidate.ai_revision)).fetchone() is None
    )


def _refresh_candidate_for_ai_locked(
    conn: sqlite3.Connection, *, candidate: CivicCandidate, batch: CivicBatch,
    reason: str, expected_revision: int, now: datetime, defer: bool,
    source_refresh: bool = False,
) -> CivicCandidate:
    if source_refresh:
        require_candidate_source_refresh(candidate, batch)
    if candidate.ai_revision >= expected_revision and _pending_ai_refresh(conn, candidate):
        if source_refresh:
            conn.execute(
                "UPDATE civic_candidates SET ai_refresh_reason = ? WHERE id = ?",
                (reason, candidate.id))
        if defer:
            _defer_candidate_for_ai_locked(
                conn, candidate=candidate, batch=batch, reason="ai_provenance_stale", now=now,
                max_deferrals=MAX_AI_DEFERRALS_PER_CANDIDATE)
        return get_candidate(conn, candidate.id)
    if candidate.ai_revision > expected_revision:
        return candidate
    if candidate.ai_revision != expected_revision:
        raise ValueError("AI refresh revision does not match the Candidate")
    if not source_refresh:
        require_candidate_ai_refresh(candidate, batch)
    if candidate.ai_revision >= MAX_AI_DEFERRALS_PER_CANDIDATE:
        if candidate.status is CivicCandidateStatus.VERIFIED:
            require_candidate_publication_rejection(candidate, batch)
        else:
            require_candidate_transition(candidate.status, CivicCandidateStatus.REJECTED)
        conn.execute(
            "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, "
            "rejection_code = 'ai_refresh_lifetime_exhausted' WHERE id = ?",
            (now.isoformat(), candidate.id))
        return get_candidate(conn, candidate.id)
    target = CivicCandidateStatus.DRAFTED
    if source_refresh:
        from beehive.db.civic_verifications import pending_research_attempt

        if pending_research_attempt(conn, candidate.id) is not None:
            target = CivicCandidateStatus.RESEARCHING
    conn.execute(
        "UPDATE civic_candidates SET ai_revision = ai_revision + 1, ai_refresh_reason = ?, "
        "status = ?, settled_at = NULL WHERE id = ?",
        (reason, target.value, candidate.id))
    conn.execute(
        "UPDATE civic_ai_stages SET status = 'failed', error_code = 'revision_superseded', "
        "error_detail = ?, completed_at = ? "
        "WHERE candidate_id = ? AND ai_revision = ? AND status = 'reserved'",
        (reason, now.isoformat(), candidate.id, candidate.ai_revision))
    if defer:
        _defer_candidate_for_ai_locked(
            conn, candidate=get_candidate(conn, candidate.id), batch=batch,
            reason="ai_provenance_stale", now=now,
            max_deferrals=MAX_AI_DEFERRALS_PER_CANDIDATE)
    return get_candidate(conn, candidate.id)


def refresh_candidate_for_ai_if_claimed(
    conn: sqlite3.Connection, *, candidate_id: int, run_id: int, claim_token: str,
    expected_revision: int, reason: str, now: datetime,
    now_fn: Callable[[], datetime] | None = None, defer: bool = False,
) -> CivicCandidate | None:
    """Start or reuse a pending revision, without touching evidence or prior successes.

    Source and policy causes can share a revision before qualification or any AI reservation.
    This does not authorize evidence mutation after work in that revision has started.
    """
    if not reason.strip():
        raise ValueError("AI refresh needs a reason")
    if expected_revision < 0:
        raise ValueError("expected_revision must not be negative")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT deadline_at FROM civic_runs WHERE id = ? AND claim_token = ? "
            "AND status = 'processing'", (run_id, claim_token)).fetchone()
        candidate = get_candidate(conn, candidate_id)
        batch = get_civic_batch(conn, candidate.batch_id) if candidate is not None else None
        if (run is None or candidate is None or candidate.run_id != run_id or batch is None
                or batch.status is not CivicBatchStatus.BUILDING
                or candidate.sealed_into_batch_at is not None
                or (run["deadline_at"] is not None
                    and now >= datetime.fromisoformat(run["deadline_at"]))):
            refreshed = None
        else:
            refreshed = _refresh_candidate_for_ai_locked(
                conn, candidate=candidate, batch=batch, reason=reason,
                expected_revision=expected_revision, now=now, defer=defer)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return refreshed


def defer_candidate_for_ai(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    run_id: int,
    claim_token: str,
    reason: str,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    max_deferrals: int = MAX_AI_DEFERRALS_PER_CANDIDATE,
) -> CivicAiDeferralResult | None:
    """End this slot's attempt at generating a Candidate without judging it.

    The Candidate keeps everything: its evidence, its qualification receipt, the AI stage
    receipts it already earned, and the consumed-item claims that produced it. It moves to
    DEFERRED_AI with an open carryover row naming the reason, so a later same-Watch Run adopts it
    back to VERIFYING and resumes at the first stage with no receipt.

    Returns None -- writing nothing -- when the Run's claim has moved on. Returns a result with
    `exhausted=True` when the Candidate has already been carried this way `max_deferrals` times:
    the carryover has a bounded lifetime, and a Candidate that reaches it is settled REJECTED so
    it cannot circle between Runs forever.

    Idempotent: a Candidate already sitting in DEFERRED_AI writes nothing and reports the
    deferral it already has, so a retried worker cannot open a second row for one slot."""
    if reason not in AI_DEFERRAL_REASONS:
        raise ValueError(f"unknown AI deferral reason: {reason!r}")
    if not 0 <= max_deferrals <= MAX_AI_DEFERRALS_PER_CANDIDATE:
        raise ValueError("max_deferrals exceeds the bounded AI deferral policy")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT r.id AS id, r.watch_id AS watch_id, r.run_mode AS run_mode, "
            "b.id AS batch_id, r.deadline_at FROM civic_runs r "
            "JOIN civic_batches b ON b.run_id = r.id "
            "WHERE r.id = ? AND r.claim_token = ? AND r.status = 'processing'",
            (run_id, claim_token)).fetchone()
        candidate = get_candidate(conn, candidate_id)
        batch = get_civic_batch(conn, candidate.batch_id) if candidate is not None else None
        if (run is None or candidate is None or candidate.run_id != run_id or batch is None
                or batch.status is not CivicBatchStatus.BUILDING
                or candidate.sealed_into_batch_at is not None
                or (run["deadline_at"] is not None
                    and now >= datetime.fromisoformat(run["deadline_at"]))):
            result = None
        else:
            result = _defer_candidate_for_ai_locked(
                conn, candidate=candidate, batch=batch, reason=reason,
                now=now, max_deferrals=max_deferrals)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def count_pending_ai_deferrals(conn: sqlite3.Connection, watch_id: int) -> int:
    """How many Candidates this Watch is holding with generation still unfinished."""
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM civic_candidates "
        "WHERE watch_id = ? AND status = 'deferred_ai'", (watch_id,)).fetchone()["n"])


def count_pending_capacity_deferrals(conn: sqlite3.Connection, watch_id: int) -> int:
    """How many Candidates this Watch is holding for a slot with room.

    A Run whose connectors found nothing still has work to do when this is non-zero, which is
    what stops an empty news day from skipping yesterday's overflow -- and, equally, what lets a
    Run with neither new Candidates nor carryovers publish nothing at all."""
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM civic_candidates "
        "WHERE watch_id = ? AND status = 'deferred_capacity'", (watch_id,)).fetchone()["n"])


def list_pending_capacity_deferrals(
    conn: sqlite3.Connection, watch_id: int,
) -> list[CivicCapacityDeferral]:
    rows = conn.execute(
        "SELECT * FROM civic_capacity_deferrals "
        "WHERE watch_id = ? AND adopted_at IS NULL ORDER BY id", (watch_id,)).fetchall()
    return [_row_to_deferral(row) for row in rows]


def list_candidate_capacity_deferrals(
    conn: sqlite3.Connection, candidate_id: int,
) -> list[CivicCapacityDeferral]:
    """One Candidate's full carry history, oldest first: which Batch held it back each time and
    which one eventually published it."""
    rows = conn.execute(
        "SELECT * FROM civic_capacity_deferrals WHERE candidate_id = ? ORDER BY id",
        (candidate_id,)).fetchall()
    return [_row_to_deferral(row) for row in rows]


def list_batch_capacity_deferrals(
    conn: sqlite3.Connection, batch_id: int,
) -> list[CivicCapacityDeferral]:
    """What this Batch had to hold back. The audit answer to "why is this envelope only six?"."""
    rows = conn.execute(
        "SELECT * FROM civic_capacity_deferrals WHERE from_batch_id = ? ORDER BY id",
        (batch_id,)).fetchall()
    return [_row_to_deferral(row) for row in rows]


def recompute_content_hash(conn: sqlite3.Connection, batch_id: int) -> str:
    """The hash the Batch's sealed-in Candidates produce right now -- for verifying a sealed
    envelope has not been tampered with. Never writes.

    Literally the same computation seal performed, over the same immutable `sealed_into_batch_at`
    membership, so a mismatch always means the rows were edited outside this module and never
    means the pipeline legitimately moved on. On a still-BUILDING Batch nothing is stamped yet,
    so this returns the empty-set hash."""
    content_hash, _ = _compute_content_hash(conn, batch_id)
    return content_hash


def bind_batch_payload_hash(
    conn: sqlite3.Connection,
    batch_id: int,
    payload_hash: str,
    now: datetime,
) -> CivicBatch:
    """Bind the canonical v1 envelope hash once.

    A matching replay is a no-op. A different replay is an integrity failure and atomically
    quarantines a SEALED/DELIVERING Batch before raising, so the changed bytes can never be
    offered for delivery. A terminal Batch cannot be moved again and is still rejected loudly.
    """
    if len(payload_hash) != 64 or any(
            char not in "0123456789abcdef" for char in payload_hash):
        raise ValueError("payload_hash must be a lowercase SHA-256 hex digest")

    mismatch: CivicPayloadHashMismatchError | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, payload_hash FROM civic_batches WHERE id = ?",
            (batch_id,)).fetchone()
        if row is None:
            raise ValueError(f"no Civic Batch with id={batch_id}")
        status = CivicBatchStatus(row["status"])
        if status is CivicBatchStatus.BUILDING:
            raise ValueError(
                f"cannot bind a payload hash to building Civic Batch {batch_id}")
        if row["payload_hash"] is None:
            if status is not CivicBatchStatus.SEALED:
                raise ValueError(
                    f"cannot first bind a payload hash to {status.value} Civic Batch "
                    f"{batch_id}")
            conn.execute(
                "UPDATE civic_batches SET payload_hash = ? "
                "WHERE id = ? AND status = 'sealed' AND payload_hash IS NULL",
                (payload_hash, batch_id))
        elif row["payload_hash"] != payload_hash:
            mismatch = CivicPayloadHashMismatchError(
                batch_id, row["payload_hash"], payload_hash)
            if status in {CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING}:
                require_batch_transition(status, CivicBatchStatus.QUARANTINED)
                conn.execute(
                    "UPDATE civic_batches SET status = 'quarantined', claim_token = NULL, "
                    "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                    "error_code = 'payload_hash_mismatch', error_detail = ? WHERE id = ?",
                    (now.isoformat(), str(mismatch)[:500], batch_id))
        bound = _row_to_batch(_fetch(conn, batch_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    if mismatch is not None:
        raise mismatch
    return bound


def list_sealed_candidate_ids(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    """The ids the envelope actually published, in the order they were hashed. Stable across a
    later correction, because membership is `sealed_into_batch_at`, not `status`."""
    rows = conn.execute(
        "SELECT id FROM civic_candidates "
        "WHERE batch_id = ? AND sealed_into_batch_at IS NOT NULL ORDER BY id",
        (batch_id,)).fetchall()
    return [row["id"] for row in rows]


def list_deliverable_batches(
    conn: sqlite3.Connection, now: datetime, limit: int = 20, *, after_id: int = 0,
) -> list[CivicBatch]:
    """SEALED envelopes whose retry backoff (if any) has elapsed, oldest first. A Batch still
    inside its backoff is deliberately invisible here rather than being claimed and immediately
    released. ``after_id`` is a keyset cursor: a page never hides Batches behind an unsettled
    prefix, it only bounds one read."""
    rows = conn.execute(
        "SELECT * FROM civic_batches WHERE status = 'sealed' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) AND id > ? ORDER BY id LIMIT ?",
        (now.isoformat(), after_id, limit)).fetchall()
    return [_row_to_batch(row) for row in rows]


def iter_deliverable_batches(
    conn: sqlite3.Connection, now: datetime, *, page_size: int = 100,
) -> Iterator[CivicBatch]:
    """Every deliverable Batch, oldest first, read in bounded keyset pages."""
    after_id = 0
    while True:
        page = list_deliverable_batches(conn, now, page_size, after_id=after_id)
        yield from page
        if len(page) < page_size:
            return
        after_id = page[-1].id


def claim_batch_for_delivery(
    conn: sqlite3.Connection,
    batch_id: int,
    now: datetime,
    lease_seconds: int,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicBatchLease | None:
    """Claims one SEALED envelope for delivery, incrementing attempt_count so the retry budget is
    spent *before* the I/O runs -- a worker that dies mid-delivery leaks at most one attempt and
    can never retry forever.

    Returns None when the Batch is not SEALED, is still inside its retry backoff, or has already
    exhausted max_attempts (in which case it is atomically terminalized FAILED rather than left
    looking claimable). `now_fn`, when given, is sampled only after the write lock is held."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        now_iso = now.isoformat()
        row = conn.execute(
            "SELECT attempt_count, max_attempts, next_attempt_at FROM civic_batches "
            "WHERE id = ? AND status = 'sealed'", (batch_id,)).fetchone()
        if row is None:
            result = None
        elif (row["next_attempt_at"] is not None
                and datetime.fromisoformat(row["next_attempt_at"]) > now):
            result = None
        elif row["attempt_count"] >= row["max_attempts"]:
            conn.execute(
                "UPDATE civic_batches SET status = 'failed', settled_at = ?, "
                "error_code = 'max_attempts_exhausted' WHERE id = ? AND status = 'sealed'",
                (now_iso, batch_id))
            result = None
        else:
            claim_token = secrets.token_urlsafe(32)
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            cur = conn.execute(
                "UPDATE civic_batches SET status = 'delivering', claim_token = ?, "
                "lease_expires_at = ?, attempt_count = attempt_count + 1, next_attempt_at = NULL "
                "WHERE id = ? AND status = 'sealed'",
                (claim_token, lease_expires_at.isoformat(), batch_id))
            if cur.rowcount == 0:
                result = None
            else:
                claimed = _fetch(conn, batch_id)
                result = CivicBatchLease(
                    batch=_row_to_batch(claimed),
                    lease_expires_at=claimed["lease_expires_at"],
                    attempt_count=claimed["attempt_count"])
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def heartbeat_batch_delivery(
    conn: sqlite3.Connection, batch_id: int, claim_token: str, now: datetime,
    lease_seconds: int,
) -> bool:
    """Extends a live delivery claim. Refuses to revive a lease that has already expired
    (returns False, writes nothing) for the same reason a Civic Run heartbeat does: the envelope
    may already have been released to another worker, and resurrecting this claim on top of it
    could deliver twice."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT lease_expires_at FROM civic_batches "
            "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
            (batch_id, claim_token)).fetchone()
        if row is None or datetime.fromisoformat(row["lease_expires_at"]) <= now:
            ok = False
        else:
            conn.execute(
                "UPDATE civic_batches SET lease_expires_at = ? "
                "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), batch_id, claim_token))
            ok = True
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ok


def mark_batch_delivered(
    conn: sqlite3.Connection, batch_id: int, claim_token: str, now: datetime,
) -> bool:
    """Terminalizes a DELIVERING envelope as DELIVERED, fenced on the claim.

    Deliberately does NOT write the receipt: db/civic_receipts.py owns that, and it is what
    callers use to make delivery idempotent. This call only records that the envelope reached
    its terminal state."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_batches "
            "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
            (batch_id, claim_token)).fetchone()
        if row is None:
            ok = False
        else:
            require_batch_transition(
                CivicBatchStatus(row["status"]), CivicBatchStatus.DELIVERED)
            cur = conn.execute(
                "UPDATE civic_batches SET status = 'delivered', claim_token = NULL, "
                "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                "error_code = NULL, error_detail = NULL "
                "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
                (now.isoformat(), batch_id, claim_token))
            ok = cur.rowcount > 0
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ok


def release_batch_claim(
    conn: sqlite3.Connection,
    batch_id: int,
    claim_token: str,
    now: datetime,
    *,
    retry_after_seconds: int,
    error_code: str,
    error_detail: str | None = None,
) -> CivicBatch | None:
    """Hands a failed delivery back, fenced on the claim.

    Returns the envelope to SEALED with a backoff while attempts remain, and terminalizes it
    FAILED once attempt_count has reached max_attempts -- a permanently broken downstream retries
    a bounded number of times, never forever. The envelope's content_hash is untouched either
    way, so a later retry delivers exactly the same bytes.

    Returns None (writing nothing) when the claim no longer matches."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, attempt_count, max_attempts FROM civic_batches "
            "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
            (batch_id, claim_token)).fetchone()
        if row is None:
            released = None
        else:
            exhausted = row["attempt_count"] >= row["max_attempts"]
            target = CivicBatchStatus.FAILED if exhausted else CivicBatchStatus.SEALED
            require_batch_transition(CivicBatchStatus(row["status"]), target)
            now_iso = now.isoformat()
            if exhausted:
                conn.execute(
                    "UPDATE civic_batches SET status = 'failed', claim_token = NULL, "
                    "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                    "error_code = ?, error_detail = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
                    (now_iso, error_code, error_detail, batch_id, claim_token))
            else:
                next_attempt_at = now + timedelta(seconds=retry_after_seconds)
                conn.execute(
                    "UPDATE civic_batches SET status = 'sealed', claim_token = NULL, "
                    "lease_expires_at = NULL, next_attempt_at = ?, error_code = ?, "
                    "error_detail = ? "
                    "WHERE id = ? AND claim_token = ? AND status = 'delivering'",
                    (next_attempt_at.isoformat(), error_code, error_detail, batch_id,
                     claim_token))
            released = _row_to_batch(_fetch(conn, batch_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return released


def quarantine_batch(
    conn: sqlite3.Connection, batch_id: int, now: datetime, error_code: str,
    error_detail: str | None = None,
) -> CivicBatch | None:
    """Pulls a SEALED or DELIVERING envelope out of the delivery queue permanently. Terminal and
    never auto-retried: quarantine means an integrity problem an Owner has to look at, so an
    automated sweep must never quietly return it to circulation."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            quarantined = None
        else:
            require_batch_transition(
                CivicBatchStatus(row["status"]), CivicBatchStatus.QUARANTINED)
            conn.execute(
                "UPDATE civic_batches SET status = 'quarantined', claim_token = NULL, "
                "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
                "error_code = ?, error_detail = ? WHERE id = ?",
                (now.isoformat(), error_code, error_detail, batch_id))
            quarantined = _row_to_batch(_fetch(conn, batch_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return quarantined


def recover_expired_batch_deliveries(
    conn: sqlite3.Connection, now: datetime, *, retry_after_seconds: int = 0,
) -> CivicBatchRecoveryResult:
    """Reconciliation sweep over DELIVERING envelopes whose lease has expired (lease_expires_at
    <= now -- equality IS expired). An envelope with attempts remaining returns to SEALED with a
    backoff; one that has exhausted max_attempts is terminalized FAILED. Both are single UPDATEs
    under one BEGIN IMMEDIATE, so an envelope can never be both released and failed by two racing
    sweeps.

    attempt_count is NOT incremented here: it was already spent at claim time, so an abandoned
    delivery consumes exactly one attempt and no more."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now_iso = now.isoformat()
        failed = conn.execute(
            "UPDATE civic_batches SET status = 'failed', claim_token = NULL, "
            "lease_expires_at = NULL, settled_at = ?, next_attempt_at = NULL, "
            "error_code = 'delivery_lease_expired' "
            "WHERE status = 'delivering' AND lease_expires_at <= ? "
            "AND attempt_count >= max_attempts",
            (now_iso, now_iso))
        released = conn.execute(
            "UPDATE civic_batches SET status = 'sealed', claim_token = NULL, "
            "lease_expires_at = NULL, next_attempt_at = ?, "
            "error_code = 'delivery_lease_expired' "
            "WHERE status = 'delivering' AND lease_expires_at <= ? "
            "AND attempt_count < max_attempts",
            ((now + timedelta(seconds=retry_after_seconds)).isoformat(), now_iso))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return CivicBatchRecoveryResult(
        released_count=released.rowcount, failed_count=failed.rowcount)


def is_terminal(batch: CivicBatch) -> bool:
    return is_terminal_batch_status(batch.status)
