"""Immutable evidence capture versions and shared, durable hydration authorization."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from beehive.civic.documents import CivicDocument
from beehive.domain.civic import CivicBatch, CivicCandidate, CivicEvidence, CivicEvidenceTier

if TYPE_CHECKING:
    from beehive.civic.evidence_refresh import EvidenceCaptureInput, EvidenceRefreshPlan, PreparedEvidenceRefresh

MAX_RUN_HYDRATIONS = 25
MAX_REFRESH_FETCH_ATTEMPTS = 3

# A source hold is not a judgment on the news, but it must end: a Candidate whose current capture
# has been attempted for this long without becoming usable is settled rejected
# (`source_hold_expired`) instead of being re-adopted and held by every daily Run.
SOURCE_HOLD_LIFETIME = timedelta(days=14)
# Every source is an outlet whose articles are never captured, so no later Run can date it.
# Settled at once instead of held for SOURCE_HOLD_LIFETIME.
UNCAPTURABLE_SOURCE_REASON = "source_refresh_uncapturable"


class CivicCaptureConflictError(ValueError):
    """The proposed capture no longer describes the claimed current evidence."""


class CivicCaptureIntegrityError(ValueError):
    """An immutable stored capture/result no longer matches its recorded hash."""


class CivicHydrationBudgetError(ValueError):
    """The shared hydration budget cannot authorize the proposed fetches."""


class CivicSourceRefreshBlocked(ValueError):
    """Source work needs an explicit hold, not an unsafe metadata fallback."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CivicHydrationReservation:
    id: int
    run_id: int
    candidate_id: int | None
    work_key: str
    claim_token: str
    units: int
    purpose: str
    expected_ai_revision: int | None
    capture_fingerprint: str | None
    plan_hash: str | None
    status: str
    result_json: str | None
    result_hash: str | None
    error_code: str | None


@dataclass(frozen=True)
class CivicEvidenceApplyResult:
    status: str
    candidate_id: int
    ai_revision: int
    capture_ids: tuple[int, ...] = ()
    reason: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class CivicEvidenceCapture:
    id: int
    evidence_id: int
    candidate_id: int
    run_id: int
    ai_revision: int
    capture_index: int
    predecessor_id: int | None
    evidence: CivicEvidence
    source_metadata: Mapping[str, Any]
    content_hash: str
    reason: str


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported capture value: {type(value).__name__}")


def _encode(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=_json_default)


def _hash(value: Any) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _current_row(conn: sqlite3.Connection, evidence_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM civic_evidence_captures WHERE evidence_id = ? "
        "ORDER BY capture_index DESC LIMIT 1", (evidence_id,)).fetchone()


def _project(base: CivicEvidence, row: sqlite3.Row) -> CivicEvidence:
    # Capture existence selects the entire overlay. None is authoritative, never a fallback
    # to the legacy row's potentially lastmod-derived publication/event date.
    return replace(
        base, title=row["title"], excerpt=row["excerpt"], canonical_url=row["canonical_url"],
        published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
        event_date=date.fromisoformat(row["event_date"]) if row["event_date"] else None,
        captured_at=datetime.fromisoformat(row["captured_at"]),
        accessible_full_text=bool(row["accessible_full_text"]), paywalled=bool(row["paywalled"]))


def project_current_evidence(conn: sqlite3.Connection, base: CivicEvidence) -> CivicEvidence:
    row = _current_row(conn, base.id)
    if row is None:
        return base
    evidence = _project(base, row)
    if _hash({"evidence": asdict(evidence), "source_metadata": json.loads(row["source_metadata_json"])}) != row["content_hash"]:
        raise CivicCaptureIntegrityError(f"Civic Evidence {base.id} capture hash changed")
    return evidence


def list_evidence_capture_inputs(
    conn: sqlite3.Connection, candidate_id: int,
) -> tuple[EvidenceCaptureInput, ...]:
    from beehive.civic.evidence_refresh import EvidenceCaptureInput
    from beehive.db.civic_candidates import list_candidate_evidence

    result = []
    for evidence in list_candidate_evidence(conn, candidate_id):
        row = _current_row(conn, evidence.id)
        metadata = json.loads(row["source_metadata_json"]) if row is not None else {}
        result.append(EvidenceCaptureInput(evidence, metadata))
    return tuple(result)


def candidate_source_plan(
    conn: sqlite3.Connection, candidate_id: int, *, remaining_hydrations: int | None = None,
) -> EvidenceRefreshPlan:
    """Authoritative readiness/refresh plan for every unsealed Candidate, not only new ones."""
    from beehive.civic.evidence_refresh import (
        EvidenceRefreshPlan, EvidenceRefreshStatus, capture_fingerprint,
        captured_refreshable_documents, has_capturable_evidence, plan_evidence_refresh,
    )
    from beehive.civic.production import event_date_for_document
    from beehive.db.civic_batches import get_civic_batch
    from beehive.db.civic_candidates import get_candidate
    from beehive.db.civic_watches import get_civic_watch

    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise ValueError(f"no Civic Candidate with id={candidate_id}")
    batch = get_civic_batch(conn, candidate.batch_id)
    if batch is None:
        raise CivicCaptureIntegrityError("source Candidate has no Batch")
    inputs = list_evidence_capture_inputs(conn, candidate_id)
    if remaining_hydrations is None:
        remaining_hydrations = max(0, MAX_RUN_HYDRATIONS - count_run_hydrations(conn, candidate.run_id))
    if not 0 <= remaining_hydrations <= MAX_RUN_HYDRATIONS:
        raise ValueError("remaining hydration budget must be between 0 and 25")
    watch = get_civic_watch(conn, candidate.watch_id)
    if watch is None:
        raise CivicCaptureIntegrityError("source Candidate has no Watch")
    try:
        plan = plan_evidence_refresh(
            candidate, batch, inputs, remaining_hydrations=remaining_hydrations)
        if plan.status is EvidenceRefreshStatus.CURRENT and not any(
            event_date_for_document(document, watch) is not None
            for document in captured_refreshable_documents(inputs)
        ):
            reason = ("source_refresh_date_unavailable" if has_capturable_evidence(inputs)
                      else UNCAPTURABLE_SOURCE_REASON)
            return replace(plan, status=EvidenceRefreshStatus.DEFERRED, reason=reason)
    except ValueError as exc:
        return EvidenceRefreshPlan(
            candidate.id, batch.id, candidate.run_id, candidate.ai_revision,
            capture_fingerprint(inputs), EvidenceRefreshStatus.DEFERRED,
            reason=f"source_refresh_invalid_capture:{type(exc).__name__}")
    return plan


def require_current_source_capture(
    conn: sqlite3.Connection, candidate_id: int, *, publication_only: bool = False,
) -> None:
    """Call before qualification/AI and again at final admission; never on historical replay."""
    from beehive.civic.evidence_refresh import EvidenceRefreshStatus

    if publication_only:
        from beehive.civic.scheduling import PARTYMAP_GLOBAL_WATCH_SUBJECT

        scope = conn.execute(
            "SELECT w.subject FROM civic_candidates c JOIN civic_watches w ON w.id=c.watch_id "
            "WHERE c.id=?", (candidate_id,)).fetchone()
        if scope is None:
            raise ValueError(f"no Civic Candidate with id={candidate_id}")
        if scope["subject"] != PARTYMAP_GLOBAL_WATCH_SUBJECT:
            return
    plan = candidate_source_plan(conn, candidate_id)
    if plan.status is not EvidenceRefreshStatus.CURRENT:
        raise CivicSourceRefreshBlocked(plan.reason or "source_refresh_required")


def list_evidence_capture_history(
    conn: sqlite3.Connection, evidence_id: int,
) -> list[CivicEvidenceCapture]:
    from beehive.db.civic_candidates import _row_to_evidence

    base_row = conn.execute(
        "SELECT * FROM civic_candidate_evidence WHERE id = ?", (evidence_id,)).fetchone()
    if base_row is None:
        raise ValueError(f"no Civic Evidence with id={evidence_id}")
    base = _row_to_evidence(base_row)
    return [
        CivicEvidenceCapture(
            id=row["id"], evidence_id=evidence_id, candidate_id=row["candidate_id"],
            run_id=row["run_id"], ai_revision=row["ai_revision"],
            capture_index=row["capture_index"], predecessor_id=row["predecessor_id"],
            evidence=_project(base, row), source_metadata=json.loads(row["source_metadata_json"]),
            content_hash=row["content_hash"], reason=row["reason"])
        for row in conn.execute(
            "SELECT * FROM civic_evidence_captures WHERE evidence_id = ? ORDER BY capture_index",
            (evidence_id,))]


def append_capture_locked(
    conn: sqlite3.Connection, *, evidence: CivicEvidence, source_metadata: Mapping[str, Any],
    run_id: int, ai_revision: int, reason: str,
) -> int:
    """Append one validated overlay while the caller holds the application transaction."""
    predecessor = _current_row(conn, evidence.id)
    capture_index = predecessor["capture_index"] + 1 if predecessor is not None else 0
    payload = {"evidence": asdict(evidence), "source_metadata": dict(source_metadata)}
    cur = conn.execute(
        "INSERT INTO civic_evidence_captures (evidence_id, candidate_id, run_id, ai_revision, "
        "capture_index, predecessor_id, title, excerpt, canonical_url, published_at, event_date, "
        "captured_at, accessible_full_text, paywalled, source_metadata_json, content_hash, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence.id, evidence.candidate_id, run_id, ai_revision, capture_index,
         predecessor["id"] if predecessor is not None else None, evidence.title, evidence.excerpt,
         evidence.canonical_url, evidence.published_at.isoformat() if evidence.published_at else None,
         evidence.event_date.isoformat() if evidence.event_date else None,
         evidence.captured_at.isoformat(), int(evidence.accessible_full_text),
         int(evidence.paywalled), _encode(source_metadata), _hash(payload), reason))
    return cur.lastrowid


def record_initial_capture_locked(
    conn: sqlite3.Connection, evidence: CivicEvidence, source_metadata: Mapping[str, Any],
) -> None:
    candidate = conn.execute(
        "SELECT run_id, ai_revision FROM civic_candidates WHERE id = ?",
        (evidence.candidate_id,)).fetchone()
    if candidate is None:
        raise ValueError(f"no Civic Candidate with id={evidence.candidate_id}")
    append_capture_locked(
        conn, evidence=evidence, source_metadata=source_metadata, run_id=candidate["run_id"],
        ai_revision=candidate["ai_revision"], reason="initial_source_capture")


def _reservation(row: sqlite3.Row) -> CivicHydrationReservation:
    return CivicHydrationReservation(**{
        name: row[name] for name in CivicHydrationReservation.__dataclass_fields__})


def _run_claim(
    conn: sqlite3.Connection, run_id: int, claim_token: str, now: datetime,
) -> sqlite3.Row | None:
    if now.tzinfo is None:
        raise ValueError("source capture operations require timezone-aware time")
    row = conn.execute(
        "SELECT * FROM civic_runs WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (run_id, claim_token)).fetchone()
    if row is None:
        return None
    if (row["lease_expires_at"] is None
            or now >= datetime.fromisoformat(row["lease_expires_at"])
            or (row["deadline_at"] is not None
                and now >= datetime.fromisoformat(row["deadline_at"]))):
        return None
    return row


def count_run_hydrations(conn: sqlite3.Connection, run_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM civic_hydration_attempts WHERE run_id = ?",
        (run_id,)).fetchone()[0]


def list_hydration_attempts(
    conn: sqlite3.Connection, run_id: int,
) -> tuple[CivicHydrationReservation, ...]:
    return tuple(_reservation(row) for row in conn.execute(
        "SELECT * FROM civic_hydration_attempts WHERE run_id = ? ORDER BY id", (run_id,)))


def _reserve_locked(
    conn: sqlite3.Connection, *, run_id: int, claim_token: str, work_key: str,
    units: int, now: datetime, candidate_id: int | None = None,
    expected_ai_revision: int | None = None, capture_fingerprint: str | None = None,
    plan_hash: str | None = None,
) -> CivicHydrationReservation | None:
    purpose = "refresh" if candidate_id is not None else "fresh"
    existing = conn.execute(
        "SELECT * FROM civic_hydration_attempts WHERE run_id = ? AND work_key = ? "
        "AND status IN ('reserved', 'completed') ORDER BY id DESC LIMIT 1",
        (run_id, work_key)).fetchone()
    if existing is not None:
        if (existing["units"] != units or existing["purpose"] != purpose
                or existing["candidate_id"] != candidate_id
                or existing["capture_fingerprint"] != capture_fingerprint
                or existing["plan_hash"] != plan_hash):
            raise CivicCaptureConflictError("hydration work key was reused for different inputs")
        if existing["status"] == "completed":
            return _reservation(existing)
        if existing["claim_token"] == claim_token:
            return None
        conn.execute(
            "UPDATE civic_hydration_attempts SET status = 'failed', "
            "error_code = 'reservation_abandoned', completed_at = ? WHERE id = ?",
            (now.isoformat(), existing["id"]))
    if candidate_id is not None:
        attempts = conn.execute(
            "SELECT COUNT(*) FROM civic_hydration_attempts WHERE candidate_id = ? "
            "AND capture_fingerprint = ? AND units > 0", (candidate_id, capture_fingerprint),
        ).fetchone()[0]
        if attempts >= MAX_REFRESH_FETCH_ATTEMPTS:
            raise CivicSourceRefreshBlocked("source_refresh_retry_exhausted")
    if count_run_hydrations(conn, run_id) + units > MAX_RUN_HYDRATIONS:
        raise CivicHydrationBudgetError("source_refresh_hydration_limit")
    cur = conn.execute(
        "INSERT INTO civic_hydration_attempts (run_id, candidate_id, work_key, claim_token, "
        "units, purpose, expected_ai_revision, capture_fingerprint, plan_hash, status, reserved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)",
        (run_id, candidate_id, work_key, claim_token, units, purpose, expected_ai_revision,
         capture_fingerprint, plan_hash, now.isoformat()))
    return _reservation(conn.execute(
        "SELECT * FROM civic_hydration_attempts WHERE id = ?", (cur.lastrowid,)).fetchone())


def reserve_hydration_if_claimed(
    conn: sqlite3.Connection, *, run_id: int, claim_token: str, work_key: str,
    units: int, now: datetime, now_fn: Callable[[], datetime] | None = None,
    documents: Sequence[CivicDocument] | None = None,
) -> CivicHydrationReservation | None:
    """Authorize fresh detail requests before fetching. Completed results are explicit replays."""
    if not work_key.strip() or not 1 <= units <= MAX_RUN_HYDRATIONS:
        raise ValueError("hydration needs a work key and 1..25 reserved requests")
    identities = (
        [(document.dedupe_key, document.canonical_url) for document in documents]
        if documents is not None else None)
    if identities is not None and (len(identities) != units or len(set(identities)) != units):
        raise ValueError("fresh hydration identities must match the reserved request count")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        result = (
            _reserve_locked(
                conn, run_id=run_id, claim_token=claim_token, work_key=work_key, units=units,
                now=now, plan_hash=_hash(identities) if identities is not None else None)
            if _run_claim(conn, run_id, claim_token, now) is not None else None)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def _candidate_for_plan(
    conn: sqlite3.Connection, plan: EvidenceRefreshPlan,
    *, allow_deferred: bool = False,
) -> tuple[CivicCandidate, CivicBatch] | None:
    from beehive.db.civic_batches import get_civic_batch
    from beehive.db.civic_candidates import get_candidate
    from beehive.domain.civic import CivicBatchStatus, CivicCandidateStatus

    candidate = get_candidate(conn, plan.candidate_id)
    batch = get_civic_batch(conn, plan.batch_id)
    if (candidate is None or batch is None
            or (candidate.batch_id, candidate.run_id, candidate.watch_id)
            != (batch.id, plan.run_id, batch.watch_id)
            or batch.run_id != plan.run_id or batch.status is not CivicBatchStatus.BUILDING
            or candidate.sealed_into_batch_at is not None
            or not (candidate.status in {
                CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
                CivicCandidateStatus.VERIFYING, CivicCandidateStatus.VERIFIED,
            } or (allow_deferred and candidate.status is CivicCandidateStatus.DEFERRED_AI))):
        return None
    if candidate.status is CivicCandidateStatus.DEFERRED_AI:
        deferral = conn.execute(
            "SELECT reason FROM civic_capacity_deferrals "
            "WHERE candidate_id = ? AND from_batch_id = ?",
            (candidate.id, batch.id)).fetchone()
        if deferral is None or deferral["reason"] != "source_refresh_required":
            return None
    return candidate, batch


def _pending_revision(conn: sqlite3.Connection, candidate: CivicCandidate) -> bool:
    from beehive.db.civic_batches import _pending_ai_refresh

    return _pending_ai_refresh(conn, candidate)


def reserve_evidence_refresh_if_claimed(
    conn: sqlite3.Connection, *, plan: EvidenceRefreshPlan, claim_token: str,
    now: datetime, now_fn: Callable[[], datetime] | None = None,
) -> CivicHydrationReservation | None:
    from beehive.civic.evidence_refresh import (
        EvidenceRefreshStatus, capture_fingerprint, plan_evidence_refresh,
    )
    from beehive.db.civic_batches import MAX_AI_DEFERRALS_PER_CANDIDATE

    if plan.status is not EvidenceRefreshStatus.PLANNED or not 1 <= len(plan.targets) <= MAX_RUN_HYDRATIONS:
        raise ValueError("only a bounded PLANNED source refresh can reserve network requests")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        state = _candidate_for_plan(conn, plan)
        if _run_claim(conn, plan.run_id, claim_token, now) is None or state is None:
            result = None
        else:
            candidate, _batch = state
            if (candidate.ai_revision != plan.expected_ai_revision
                    and not (candidate.ai_revision == plan.expected_ai_revision + 1
                             and _pending_revision(conn, candidate))):
                raise CivicCaptureConflictError("source refresh AI revision changed")
            inputs = list_evidence_capture_inputs(conn, candidate.id)
            if capture_fingerprint(inputs) != plan.expected_capture_fingerprint:
                raise CivicCaptureConflictError("source refresh active evidence changed")
            if (candidate.ai_revision >= MAX_AI_DEFERRALS_PER_CANDIDATE
                    and not _pending_revision(conn, candidate)):
                raise CivicSourceRefreshBlocked("source_refresh_ai_revision_limit")
            expected = plan_evidence_refresh(
                candidate, _batch, inputs, remaining_hydrations=MAX_RUN_HYDRATIONS)
            if expected.status is not EvidenceRefreshStatus.PLANNED or expected.targets != plan.targets:
                raise CivicCaptureConflictError("source refresh plan omitted or changed required captures")
            result = _reserve_locked(
                conn, run_id=plan.run_id, claim_token=claim_token,
                work_key=f"candidate:{candidate.id}:{_hash(asdict(plan))}",
                units=len(plan.targets), now=now, candidate_id=candidate.id,
                expected_ai_revision=plan.expected_ai_revision,
                capture_fingerprint=plan.expected_capture_fingerprint, plan_hash=_hash(asdict(plan)))
            if result is not None and result.status == "reserved":
                conn.execute(
                    "UPDATE civic_candidates SET ai_refresh_reason = "
                    "'source_refresh:in_progress' WHERE id = ?", (candidate.id,))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def _settle_hydration_locked(
    conn: sqlite3.Connection, reservation: CivicHydrationReservation,
    *, now: datetime, result: Any = None, error_code: str | None = None,
) -> None:
    conn.execute(
        "UPDATE civic_hydration_attempts SET status = ?, completed_at = ?, result_json = ?, "
        "result_hash = ?, error_code = ? WHERE id = ? AND status = 'reserved'",
        ("failed" if error_code else "completed", now.isoformat(),
         _encode(result) if error_code is None else None,
         _hash(result) if error_code is None else None, error_code, reservation.id))


def _live_reservation(
    conn: sqlite3.Connection, reservation: CivicHydrationReservation, now: datetime,
) -> CivicHydrationReservation | None:
    if _run_claim(conn, reservation.run_id, reservation.claim_token, now) is None:
        return None
    row = conn.execute(
        "SELECT * FROM civic_hydration_attempts WHERE id = ? AND claim_token = ? "
        "AND status = 'reserved'", (reservation.id, reservation.claim_token)).fetchone()
    if row is None:
        return None
    stored = _reservation(row)
    if _authorization(stored) != _authorization(reservation):
        raise CivicCaptureConflictError("hydration reservation authorization changed")
    return stored


def _authorization(reservation: CivicHydrationReservation) -> tuple:
    return (
        reservation.id, reservation.run_id, reservation.candidate_id, reservation.work_key,
        reservation.claim_token, reservation.units, reservation.purpose,
        reservation.expected_ai_revision, reservation.capture_fingerprint, reservation.plan_hash,
    )


def _completed_payload(
    conn: sqlite3.Connection, reservation: CivicHydrationReservation,
) -> Any | None:
    row = conn.execute(
        "SELECT * FROM civic_hydration_attempts WHERE id = ? AND status = 'completed'",
        (reservation.id,)).fetchone()
    if row is None:
        return None
    stored = _reservation(row)
    if _authorization(stored) != _authorization(reservation):
        raise CivicCaptureConflictError("hydration replay authorization changed")
    payload = json.loads(stored.result_json)
    if _hash(payload) != stored.result_hash:
        raise CivicCaptureIntegrityError("stored hydration result hash changed")
    return payload


def _document_payload(document: CivicDocument) -> dict:
    return asdict(document)


def hydration_documents(reservation: CivicHydrationReservation) -> tuple[CivicDocument, ...]:
    if reservation.status != "completed" or reservation.purpose != "fresh":
        raise ValueError("only completed fresh hydration work contains reusable documents")
    payload = json.loads(reservation.result_json)
    if _hash(payload) != reservation.result_hash:
        raise CivicCaptureIntegrityError("stored hydration result hash changed")
    documents = []
    for data in payload:
        data = dict(data)
        data["tier"] = CivicEvidenceTier(data["tier"])
        for field in ("first_seen_at", "published_at", "updated_at"):
            data[field] = datetime.fromisoformat(data[field]) if data[field] is not None else None
        documents.append(CivicDocument(**data))
    return tuple(documents)


def complete_hydration_if_claimed(
    conn: sqlite3.Connection, *, reservation: CivicHydrationReservation,
    documents: Sequence[CivicDocument], now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicHydrationReservation | None:
    from beehive.civic.source_extraction import has_current_article_capture

    if reservation.purpose != "fresh" or len(documents) != reservation.units:
        raise ValueError("hydration completion must match the reserved fresh document count")
    if reservation.plan_hash is None or reservation.plan_hash != _hash([
            (document.dedupe_key, document.canonical_url) for document in documents]):
        raise CivicCaptureConflictError("fresh hydration completion changed its reserved identities")
    if not all(has_current_article_capture(document) for document in documents):
        raise ValueError("hydration completion needs full current capture provenance")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        payload = [_document_payload(document) for document in documents]
        completed = _completed_payload(conn, reservation)
        if completed is not None:
            if _encode(completed) != _encode(payload):
                raise CivicCaptureConflictError("completed hydration was replayed with different documents")
            conn.commit()
            return _reservation(conn.execute(
                "SELECT * FROM civic_hydration_attempts WHERE id = ?", (reservation.id,)).fetchone())
        live = _live_reservation(conn, reservation, now)
        if live is None:
            result = None
        else:
            if _run_claim(conn, live.run_id, live.claim_token,
                          now_fn() if now_fn is not None else now) is None:
                conn.rollback()
                return None
            _settle_hydration_locked(
                conn, live, now=now, result=payload)
            result = _reservation(conn.execute(
                "SELECT * FROM civic_hydration_attempts WHERE id = ?", (live.id,)).fetchone())
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def fail_hydration_if_claimed(
    conn: sqlite3.Connection, *, reservation: CivicHydrationReservation,
    reason: str, now: datetime, now_fn: Callable[[], datetime] | None = None,
) -> bool:
    if not reason.strip():
        raise ValueError("failed hydration needs a reason")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        live = _live_reservation(conn, reservation, now)
        if live is not None:
            _settle_hydration_locked(conn, live, now=now, error_code=reason)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return live is not None


def _replacement_evidence(original: CivicEvidence, document: CivicDocument, now: datetime) -> CivicEvidence:
    from beehive.civic.source_extraction import document_publication, has_current_article_capture

    if (document.dedupe_key != original.external_key
            or document.source_key != original.source_ref
            or document.canonical_url != (original.canonical_url or original.url)
            or document.tier is not original.tier or document.publisher != original.publisher):
        raise CivicCaptureConflictError("replacement capture changed source identity")
    if not has_current_article_capture(document):
        raise ValueError("replacement capture lacks current full-article/publication provenance")
    captured = document.raw_metadata["source_capture"].get("captured_at")
    if not isinstance(captured, str):
        raise ValueError("replacement capture needs its actual capture timestamp")
    captured_at = datetime.fromisoformat(captured)
    if captured_at.tzinfo is None or captured_at > now:
        raise ValueError("replacement capture timestamp is not valid at apply time")
    witness = document_publication(document)
    published_at = witness.value if witness is not None else None
    event_date = published_at.astimezone(ZoneInfo("Pacific/Auckland")).date() if published_at else None
    provenance = document.raw_metadata.get("event_date_provenance")
    if original.source_kind not in {"party-primary", "independent-news"} and provenance is not None:
        event_at = datetime.fromisoformat(provenance["value"])
        if event_at.tzinfo is None:
            raise ValueError("official event provenance must be timezone-aware")
        event_date = event_at.astimezone(ZoneInfo("Pacific/Auckland")).date()
        explicit = document.raw_metadata.get("event_date")
        if explicit is not None and explicit != event_date.isoformat():
            raise ValueError("official event date does not match its recorded provenance")
    return replace(
        original, title=document.title, excerpt=document.excerpt, canonical_url=document.canonical_url,
        published_at=published_at, event_date=event_date, captured_at=captured_at,
        accessible_full_text=True, paywalled=False)


def _hold_source_locked(conn, candidate, batch, reason: str, now: datetime) -> None:
    from beehive.civic.batch_policy import CivicRunMode, published_cap
    from beehive.domain.civic import require_candidate_source_hold

    require_candidate_source_hold(candidate, batch)
    conn.execute(
        "UPDATE civic_candidates SET status = 'deferred_ai', settled_at = ?, "
        "ai_refresh_reason = ? WHERE id = ?",
        (now.isoformat(), f"source_refresh:{reason}", candidate.id))
    mode = CivicRunMode(conn.execute(
        "SELECT run_mode FROM civic_runs WHERE id = ?", (batch.run_id,)).fetchone()["run_mode"])
    conn.execute(
        "INSERT OR IGNORE INTO civic_capacity_deferrals (watch_id, candidate_id, from_run_id, "
        "from_batch_id, published_cap, run_mode, reason, deferred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'source_refresh_required', ?)",
        (candidate.watch_id, candidate.id, candidate.run_id, candidate.batch_id,
         published_cap(mode), mode.value, now.isoformat()))


def _holdable(candidate: CivicCandidate) -> bool:
    from beehive.domain.civic import CivicCandidateStatus

    # Deferred and verified Candidates keep the old hold; the next adopted pass settles them.
    return candidate.status in {
        CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
        CivicCandidateStatus.VERIFYING,
    }


def _source_hold_expired(
    conn: sqlite3.Connection, candidate: CivicCandidate, fingerprint: str, now: datetime,
) -> bool:

    if not _holdable(candidate):
        return False
    started = conn.execute(
        "SELECT MIN(reserved_at) FROM civic_hydration_attempts "
        "WHERE candidate_id = ? AND capture_fingerprint = ?",
        (candidate.id, fingerprint)).fetchone()[0]
    return started is not None and now - datetime.fromisoformat(started) >= SOURCE_HOLD_LIFETIME


def _expire_source_hold_locked(conn, candidate, batch, reason: str, now: datetime,
                               *, rejection_code: str = "source_hold_expired") -> None:
    from beehive.domain.civic import (CivicCandidateStatus, require_candidate_source_hold,
                                      require_candidate_transition)

    require_candidate_source_hold(candidate, batch)
    require_candidate_transition(candidate.status, CivicCandidateStatus.REJECTED)
    conn.execute(
        "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, "
        "rejection_code = ?, ai_refresh_reason = ? WHERE id = ?",
        (now.isoformat(), rejection_code, f"source_refresh:{reason}", candidate.id))


def defer_evidence_refresh_if_claimed(
    conn: sqlite3.Connection, *, plan: EvidenceRefreshPlan, claim_token: str, reason: str,
    now: datetime, reservation: CivicHydrationReservation | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicEvidenceApplyResult | None:
    """Record a source hold without judging news or spending another AI revision/lifetime.

    The hold itself is bounded by `SOURCE_HOLD_LIFETIME`: past it, the Candidate is settled
    rejected (`source_hold_expired`) and the result's status is "rejected"."""
    from beehive.civic.evidence_refresh import capture_fingerprint

    if not reason.strip():
        raise ValueError("source hold needs an explicit reason")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        state = _candidate_for_plan(conn, plan, allow_deferred=True)
        if _run_claim(conn, plan.run_id, claim_token, now) is None or state is None:
            result = None
        else:
            candidate, batch = state
            if (candidate.ai_revision != plan.expected_ai_revision
                    and not (candidate.ai_revision == plan.expected_ai_revision + 1
                             and _pending_revision(conn, candidate))):
                raise CivicCaptureConflictError("source hold AI revision changed")
            if capture_fingerprint(list_evidence_capture_inputs(conn, candidate.id)) != plan.expected_capture_fingerprint:
                raise CivicCaptureConflictError("source hold would overwrite a newer capture decision")
            if reservation is not None:
                if (reservation.purpose != "refresh" or reservation.candidate_id != candidate.id
                        or reservation.plan_hash != _hash(asdict(plan))):
                    raise CivicCaptureConflictError("source hold reservation does not match its plan")
                live = _live_reservation(conn, reservation, now)
                if live is None:
                    conn.rollback()
                    return None
                _settle_hydration_locked(conn, live, now=now, error_code=reason)
            else:
                key = f"hold:{candidate.id}:{_hash((asdict(plan), reason))}"
                existing = conn.execute(
                    "SELECT 1 FROM civic_hydration_attempts WHERE run_id = ? AND work_key = ? "
                    "AND status = 'blocked'", (plan.run_id, key)).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO civic_hydration_attempts (run_id, candidate_id, work_key, "
                        "claim_token, units, purpose, expected_ai_revision, capture_fingerprint, "
                        "plan_hash, status, reserved_at, completed_at, error_code) "
                        "VALUES (?, ?, ?, ?, 0, 'refresh', ?, ?, ?, 'blocked', ?, ?, ?)",
                        (plan.run_id, candidate.id, key, claim_token, candidate.ai_revision,
                         plan.expected_capture_fingerprint, _hash(asdict(plan)),
                         now.isoformat(), now.isoformat(), reason))
            if reason == UNCAPTURABLE_SOURCE_REASON and _holdable(candidate):
                _expire_source_hold_locked(conn, candidate, batch, reason, now,
                                           rejection_code="source_uncapturable")
                result = CivicEvidenceApplyResult(
                    "rejected", candidate.id, candidate.ai_revision, reason=reason)
            elif _source_hold_expired(conn, candidate, plan.expected_capture_fingerprint, now):
                _expire_source_hold_locked(conn, candidate, batch, reason, now)
                result = CivicEvidenceApplyResult(
                    "rejected", candidate.id, candidate.ai_revision, reason=reason)
            else:
                _hold_source_locked(conn, candidate, batch, reason, now)
                result = CivicEvidenceApplyResult(
                    "deferred", candidate.id, candidate.ai_revision, reason=reason)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def hold_capture_integrity_if_claimed(
    conn: sqlite3.Connection, *, candidate_id: int, run_id: int, claim_token: str,
    now: datetime, now_fn: Callable[[], datetime] | None = None,
) -> CivicEvidenceApplyResult | None:
    """Hold a corrupt unsealed capture without repairing it or blocking unrelated work."""
    from beehive.db.civic_batches import get_civic_batch
    from beehive.db.civic_candidates import get_candidate

    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        candidate = get_candidate(conn, candidate_id)
        batch = get_civic_batch(conn, candidate.batch_id) if candidate is not None else None
        if (_run_claim(conn, run_id, claim_token, now) is None or candidate is None
                or candidate.run_id != run_id or batch is None):
            result = None
        else:
            try:
                list_evidence_capture_inputs(conn, candidate_id)
            except CivicCaptureIntegrityError:
                reason = "source_capture_integrity_error"
                _hold_source_locked(conn, candidate, batch, reason, now)
                result = CivicEvidenceApplyResult(
                    "deferred", candidate.id, candidate.ai_revision, reason=reason)
            else:
                result = CivicEvidenceApplyResult(
                    "recheck", candidate.id, candidate.ai_revision)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def apply_evidence_refresh_if_claimed(
    conn: sqlite3.Connection, *, reservation: CivicHydrationReservation,
    prepared: PreparedEvidenceRefresh, now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicEvidenceApplyResult | None:
    from beehive.civic.evidence_refresh import EvidenceRefreshStatus, capture_fingerprint
    from beehive.db.civic_batches import MAX_AI_DEFERRALS_PER_CANDIDATE, _refresh_candidate_for_ai_locked

    plan = prepared.plan
    if (reservation.purpose != "refresh" or reservation.candidate_id != plan.candidate_id
            or reservation.run_id != plan.run_id or reservation.plan_hash != _hash(asdict(plan))):
        raise CivicCaptureConflictError("prepared source capture does not match its reservation")
    if prepared.status is not EvidenceRefreshStatus.PREPARED:
        raise ValueError("only a fully PREPARED source refresh may be applied")
    wanted = {target.evidence_id for target in plan.targets}
    if (len(prepared.captures) != len(wanted)
            or {capture.supersedes_evidence_id for capture in prepared.captures} != wanted):
        raise CivicCaptureConflictError("prepared source capture set changed")
    if ({attempt.evidence_id for attempt in prepared.attempts} != wanted
            or len(prepared.attempts) != len(wanted)
            or any(attempt.error_code is not None for attempt in prepared.attempts)):
        raise CivicCaptureConflictError("prepared source refresh lacks successful reserved attempts")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        completed = _completed_payload(conn, reservation)
        if completed is not None:
            if completed["prepared_hash"] != _hash(asdict(prepared)):
                raise CivicCaptureConflictError("source refresh replay changed the prepared capture")
            stored = completed["application"]
            conn.commit()
            return CivicEvidenceApplyResult(
                stored["status"], stored["candidate_id"], stored["ai_revision"],
                tuple(stored["capture_ids"]), stored["reason"], replayed=True)
        live = _live_reservation(conn, reservation, now)
        state = _candidate_for_plan(conn, plan)
        if live is None or state is None:
            result = None
        else:
            candidate, batch = state
            inputs = list_evidence_capture_inputs(conn, candidate.id)
            by_evidence_id = {capture.evidence.id: capture for capture in inputs}
            replacement_fingerprints_match = all(
                capture.supersedes_evidence_id in by_evidence_id
                and capture.expected_evidence_fingerprint == capture_fingerprint(
                    (by_evidence_id[capture.supersedes_evidence_id],))
                for capture in prepared.captures
            )
            if (capture_fingerprint(inputs) != live.capture_fingerprint
                    or not replacement_fingerprints_match
                    or (candidate.ai_revision != plan.expected_ai_revision
                        and not (candidate.ai_revision == plan.expected_ai_revision + 1
                                 and _pending_revision(conn, candidate)))):
                _settle_hydration_locked(conn, live, now=now, error_code="source_refresh_conflict")
                result = CivicEvidenceApplyResult(
                    "stale", candidate.id, candidate.ai_revision, reason="source_refresh_conflict")
            else:
                replacements = [
                    (_replacement_evidence(
                        by_evidence_id[capture.supersedes_evidence_id].evidence, capture.document, now),
                     capture.document.raw_metadata)
                    for capture in prepared.captures]
                if not _pending_revision(conn, candidate):
                    if candidate.ai_revision >= MAX_AI_DEFERRALS_PER_CANDIDATE:
                        raise CivicSourceRefreshBlocked("source_refresh_ai_revision_limit")
                candidate = _refresh_candidate_for_ai_locked(
                    conn, candidate=candidate, batch=batch, reason="source_capture_changed",
                    expected_revision=plan.expected_ai_revision, now=now, defer=False,
                    source_refresh=True)
                capture_ids = tuple(append_capture_locked(
                    conn, evidence=evidence, source_metadata=metadata, run_id=plan.run_id,
                    ai_revision=candidate.ai_revision, reason="source_capture_changed",
                ) for evidence, metadata in replacements)
                result = CivicEvidenceApplyResult(
                    "applied", candidate.id, candidate.ai_revision, capture_ids)
                readiness = candidate_source_plan(conn, candidate.id)
                if readiness.reason == "source_refresh_date_unavailable":
                    _hold_source_locked(conn, candidate, batch, readiness.reason, now)
                    result = replace(result, status="deferred", reason=readiness.reason)
                _settle_hydration_locked(
                    conn, live, now=now,
                    result={"application": asdict(result), "prepared_hash": _hash(asdict(prepared))})
                if _run_claim(conn, reservation.run_id, reservation.claim_token,
                              now_fn() if now_fn is not None else now) is None:
                    conn.rollback()
                    return None
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result
