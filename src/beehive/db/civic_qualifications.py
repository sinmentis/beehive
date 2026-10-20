"""Durable receipts for the pure `beehive.civic.evidence` qualification engine.

The engine is deterministic, which is only useful if the inputs it saw are still there months
later. This table stores each decision alongside a hash of exactly what produced it and the
version of the engine that produced it, so a published statement can be re-justified -- and, more
immediately, so a worker that comes back to a Candidate can tell the difference between *the same
decision again* and *a different decision it is about to make silently*.

**Replay is by input hash, not by timestamp.** `input_hash` covers the claim, the Candidate's
evidence rows, the ownership registry and the dedup membership; it deliberately excludes the wall
clock, because a resumed worker qualifying the same material an hour later is replaying, not
drifting. A replay whose hash matches returns the stored decision and writes nothing. A replay
whose hash differs raises `CivicQualificationDriftError` -- the inputs behind a recorded decision
changed, and quietly recording the new one would let a Run overturn a qualification without
anyone seeing it happen.

**The research pass writes a new row, it does not overwrite.** `research_attempts_used` (0 before
the single bounded pass, 1 after it) is part of the key, so the decision that justified spending
the attempt stays readable next to the decision that stands. A second pass is impossible: the
column's CHECK stops at 1, matching `beehive.civic.evidence.MAX_RESEARCH_ATTEMPTS`.

**A decision that is already settled cannot be re-opened.** A new row is only accepted when the
latest recorded decision was a `deferred_evidence` -- the one outcome the bounded research pass
exists to close. Anything else (qualified, screened out, duplicate, unsupported) is final, and
attempting to record over it raises rather than downgrading a Candidate other rows already depend
on.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from beehive.civic.evidence import (CivicPublicationDisposition, CivicQualification,
                                    CivicQualificationOutcome, CivicRecordKind)
from beehive.civic.reasons import CivicPolicyReason


class CivicQualificationDriftError(ValueError):
    """The stored decision's inputs are not the inputs being offered now.

    Subclasses ValueError so a caller that only distinguishes "bad request" still handles it, but
    carries both hashes so a caller that wants to explain the drift can."""

    def __init__(self, candidate_id: int, stored_hash: str, offered_hash: str) -> None:
        super().__init__(
            f"Civic Candidate {candidate_id}: qualification inputs changed since the recorded "
            f"decision (stored {stored_hash[:12]}, offered {offered_hash[:12]})")
        self.candidate_id = candidate_id
        self.stored_hash = stored_hash
        self.offered_hash = offered_hash


@dataclass(frozen=True)
class CivicQualificationRecord:
    """One recorded decision, with everything needed to reproduce and audit it."""

    id: int | None
    candidate_id: int
    run_id: int
    research_attempts_used: int
    engine_version: str
    input_hash: str
    output_hash: str
    outcome: CivicQualificationOutcome
    disposition: CivicPublicationDisposition
    reasons: tuple[CivicPolicyReason, ...]
    evidence_used: tuple[str, ...]
    evidence_excluded: tuple[str, ...]
    ownership_groups: tuple[str, ...]
    decided_at: datetime
    dedup_reason: CivicPolicyReason | None = None
    matched_candidate_id: int | None = None
    matched_batch_id: int | None = None
    # The PartyMap record kind the decision was taken about. Kept here because the Civic Batch
    # seal has to queue this Candidate through beehive.civic.batch_policy, which refuses a record
    # kind outside the automated publication scope, and the seal no longer holds the claim.
    record_kind: CivicRecordKind | None = None
    ai_revision: int = 0
    refresh_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("a recorded Civic qualification needs at least one reason")
        if not self.engine_version.strip():
            raise ValueError("a recorded Civic qualification needs an engine_version")

    @property
    def is_publishable(self) -> bool:
        return self.disposition is CivicPublicationDisposition.PUBLISHABLE

    @property
    def is_research_retryable(self) -> bool:
        return self.outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split(",") if part)


def _row_to_record(row: sqlite3.Row) -> CivicQualificationRecord:
    return CivicQualificationRecord(
        id=row["id"],
        candidate_id=row["candidate_id"],
        run_id=row["run_id"],
        research_attempts_used=row["research_attempts_used"],
        engine_version=row["engine_version"],
        input_hash=row["input_hash"],
        output_hash=row["output_hash"],
        outcome=CivicQualificationOutcome(row["outcome"]),
        disposition=CivicPublicationDisposition(row["disposition"]),
        reasons=tuple(CivicPolicyReason(code) for code in _split(row["reasons"])),
        evidence_used=_split(row["evidence_used"]),
        evidence_excluded=_split(row["evidence_excluded"]),
        ownership_groups=_split(row["ownership_groups"]),
        decided_at=datetime.fromisoformat(row["decided_at"]),
        dedup_reason=(
            CivicPolicyReason(row["dedup_reason"]) if row["dedup_reason"] else None),
        matched_candidate_id=row["matched_candidate_id"],
        matched_batch_id=row["matched_batch_id"],
        record_kind=(
            CivicRecordKind(row["record_kind"]) if row["record_kind"] else None),
        ai_revision=row["ai_revision"], refresh_reason=row["refresh_reason"])


def record_qualification(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    run_id: int,
    qualification: CivicQualification,
    engine_version: str,
    input_hash: str,
    output_hash: str,
    research_attempts_used: int,
    now: datetime,
    record_kind: CivicRecordKind | None = None,
) -> CivicQualificationRecord:
    """Writes one decision, or replays the identical one already recorded. Caller holds the
    transaction.

    Not transactional itself: the decision and the Candidate settlement it drives must commit
    together, so this is only ever called from inside an open BEGIN IMMEDIATE (see
    `beehive.civic.pipeline`).

    * Same (candidate, active revision, attempt) and input_hash -> the stored row is untouched.
    * Same revision/attempt with changed inputs -> `CivicQualificationDriftError`.
    * A new attempt after a decision that was not `deferred_evidence` -> ValueError. Only an
      evidence deferral is re-openable within a revision. A bounded unsealed AI refresh starts
      a new revision and records a new decision without rewriting earlier receipts."""
    if research_attempts_used not in (0, 1):
        raise ValueError("research_attempts_used must be 0 or 1")
    candidate = conn.execute(
        "SELECT ai_revision, ai_refresh_reason FROM civic_candidates WHERE id = ? AND run_id = ?",
        (candidate_id, run_id)).fetchone()
    if candidate is None:
        raise ValueError(f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
    existing = conn.execute(
        "SELECT * FROM civic_qualifications "
        "WHERE candidate_id = ? AND research_attempts_used = ? AND ai_revision = ?",
        (candidate_id, research_attempts_used, candidate["ai_revision"])).fetchone()
    if existing is not None:
        if existing["input_hash"] != input_hash:
            raise CivicQualificationDriftError(
                candidate_id, existing["input_hash"], input_hash)
        if existing["engine_version"] != engine_version:
            raise CivicQualificationDriftError(
                candidate_id, existing["engine_version"], engine_version)
        return _row_to_record(existing)

    latest = get_latest_qualification(conn, candidate_id)
    if latest is not None:
        if research_attempts_used <= latest.research_attempts_used:
            raise ValueError(
                f"Civic Candidate {candidate_id}: research attempt "
                f"{research_attempts_used} is not after the recorded attempt "
                f"{latest.research_attempts_used}")
        if not latest.is_research_retryable:
            raise ValueError(
                f"Civic Candidate {candidate_id}: a {latest.outcome.value} qualification is "
                "final and cannot be re-decided")
    elif research_attempts_used != 0:
        raise ValueError(
            f"Civic Candidate {candidate_id}: a research-pass qualification needs the first "
            "qualification on record")

    if conn.execute(
            "SELECT 1 FROM civic_candidates WHERE id = ? AND run_id = ?",
            (candidate_id, run_id)).fetchone() is None:
        raise ValueError(
            f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")

    dedup = qualification.dedup
    cur = conn.execute(
        "INSERT INTO civic_qualifications (candidate_id, run_id, research_attempts_used, "
        "engine_version, input_hash, output_hash, outcome, disposition, reasons, evidence_used, "
        "evidence_excluded, ownership_groups, dedup_reason, matched_candidate_id, "
        "matched_batch_id, record_kind, decided_at, ai_revision, refresh_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, run_id, research_attempts_used, engine_version, input_hash, output_hash,
         qualification.outcome.value, qualification.disposition.value,
         ",".join(reason.value for reason in qualification.reasons),
         ",".join(qualification.evidence_used),
         ",".join(qualification.evidence_excluded),
         ",".join(qualification.ownership_groups),
         dedup.reason.value if dedup is not None else None,
         _matched_candidate_id(qualification),
         _matched_batch_id(qualification),
         record_kind.value if record_kind is not None else None,
         now.isoformat(), candidate["ai_revision"], candidate["ai_refresh_reason"]))
    return _row_to_record(conn.execute(
        "SELECT * FROM civic_qualifications WHERE id = ?", (cur.lastrowid,)).fetchone())


def _matched_candidate_id(qualification: CivicQualification) -> int | None:
    """The duplicate's own row id, when the match is a Candidate this database holds.

    The engine speaks in opaque candidate id strings because it is pure; only a decision that
    actually excluded the Candidate as a duplicate names a row, and only a numeric id is a row
    here."""
    dedup = qualification.dedup
    if (dedup is None or not dedup.is_duplicate
            or qualification.outcome is not CivicQualificationOutcome.DUPLICATE):
        return None
    matched = dedup.matched_candidate_id
    return int(matched) if matched is not None and matched.isdigit() else None


def _matched_batch_id(qualification: CivicQualification) -> int | None:
    dedup = qualification.dedup
    if (dedup is None or not dedup.is_duplicate
            or qualification.outcome is not CivicQualificationOutcome.DUPLICATE):
        return None
    matched = dedup.matched_batch_id
    return int(matched) if matched is not None and matched.isdigit() else None


def get_qualification(
    conn: sqlite3.Connection, candidate_id: int, research_attempts_used: int,
) -> CivicQualificationRecord | None:
    row = conn.execute(
        "SELECT q.* FROM civic_qualifications q JOIN civic_candidates c ON c.id = q.candidate_id "
        "WHERE q.candidate_id = ? AND q.research_attempts_used = ? AND q.ai_revision = c.ai_revision",
        (candidate_id, research_attempts_used)).fetchone()
    return _row_to_record(row) if row else None


def get_latest_qualification(
    conn: sqlite3.Connection, candidate_id: int,
) -> CivicQualificationRecord | None:
    """The decision that currently stands for this Candidate."""
    row = conn.execute(
        "SELECT q.* FROM civic_qualifications q JOIN civic_candidates c ON c.id = q.candidate_id "
        "WHERE q.candidate_id = ? AND q.ai_revision = c.ai_revision "
        "ORDER BY q.research_attempts_used DESC LIMIT 1", (candidate_id,)).fetchone()
    return _row_to_record(row) if row else None


def list_qualifications(
    conn: sqlite3.Connection, candidate_id: int,
) -> list[CivicQualificationRecord]:
    rows = conn.execute(
        "SELECT * FROM civic_qualifications WHERE candidate_id = ? "
        "ORDER BY ai_revision, research_attempts_used", (candidate_id,)).fetchall()
    return [_row_to_record(row) for row in rows]


def list_run_qualifications(
    conn: sqlite3.Connection, run_id: int,
) -> list[CivicQualificationRecord]:
    rows = conn.execute(
        "SELECT * FROM civic_qualifications WHERE run_id = ? "
        "ORDER BY candidate_id, research_attempts_used", (run_id,)).fetchall()
    return [_row_to_record(row) for row in rows]


__all__ = [
    "CivicQualificationDriftError",
    "CivicQualificationRecord",
    "get_latest_qualification",
    "get_qualification",
    "list_qualifications",
    "list_run_qualifications",
    "record_qualification",
]
