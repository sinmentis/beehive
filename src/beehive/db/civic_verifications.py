"""Civic Research Attempt and Civic Verification persistence -- the two append-only records that
stand behind every published civic statement.

Both are numbered per Candidate with MAX(attempt_number)+1 allocated under BEGIN IMMEDIATE, so
two workers racing on the same Candidate get two distinct attempts rather than one silently
overwriting the other, and neither is ever edited after it is written. That is what makes "what
was already tried, and what was concluded" answerable for a statement published months ago.

**The research budget is spent durably, before the work runs.**
`reserve_research_attempt` increments the Candidate's own research_attempt_count inside the same
transaction that checks it against the cap, and returns the reserved attempt number. A worker
that dies mid-pass therefore leaks exactly one reservation and can never quietly buy itself
another -- the same reserve-then-do-the-I/O discipline db/research_runs.py uses for deep fetches.
`record_research_attempt` then writes the outcome against that reserved number.

**A verifier verdict cannot contradict its own counts.** CivicVerification's invariants and the
matching CHECK constraints on civic_verifications make SUPPORTED structurally impossible without
supporting evidence, and structurally impossible alongside contradicting evidence -- so a
verifier record can never claim more than its evidence allows. `record_verification` additionally
recounts the Candidate's live evidence under the lock rather than trusting the counts a caller
passed, so a verdict is always written against what the database actually holds."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from beehive.domain.civic import (CivicEvidenceStance, CivicResearchAttempt,
                                   CivicResearchOutcome, CivicVerification,
                                   CivicVerificationVerdict)

# How many extra research passes one Civic Candidate may ever consume. Deliberately small: an
# under-evidenced statement is meant to be dropped, not researched indefinitely.
MAX_RESEARCH_ATTEMPTS = 2


def _row_to_attempt(row: sqlite3.Row) -> CivicResearchAttempt:
    return CivicResearchAttempt(
        id=row["id"],
        candidate_id=row["candidate_id"],
        attempt_number=row["attempt_number"],
        outcome=CivicResearchOutcome(row["outcome"]),
        evidence_added=row["evidence_added"],
        started_at=datetime.fromisoformat(row["started_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]),
        notes=row["notes"],
        error_code=row["error_code"])


def _row_to_verification(row: sqlite3.Row) -> CivicVerification:
    return CivicVerification(
        id=row["id"],
        candidate_id=row["candidate_id"],
        attempt_number=row["attempt_number"],
        verdict=CivicVerificationVerdict(row["verdict"]),
        supporting_evidence_count=row["supporting_evidence_count"],
        contradicting_evidence_count=row["contradicting_evidence_count"],
        model=row["model"],
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
        rationale=row["rationale"],
        error_code=row["error_code"])


def _count_evidence(conn: sqlite3.Connection, candidate_id: int) -> tuple[int, int]:
    """(supporting, contradicting) counts read straight from civic_candidate_evidence.
    SUPPORTING and CORROBORATING both count as supporting; CONTEXT counts as neither."""
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN stance IN ('supporting', 'corroborating') THEN 1 ELSE 0 END) AS supporting,"
        " SUM(CASE WHEN stance = ? THEN 1 ELSE 0 END) AS contradicting "
        "FROM civic_candidate_evidence WHERE candidate_id = ?",
        (CivicEvidenceStance.CONTRADICTING.value, candidate_id)).fetchone()
    return int(row["supporting"] or 0), int(row["contradicting"] or 0)


def reserve_research_attempt_locked(
    conn: sqlite3.Connection, candidate_id: int, *, max_attempts: int = MAX_RESEARCH_ATTEMPTS,
) -> int | None:
    """The reservation itself, without the transaction. Caller holds the lock.

    Exists so a caller that must reserve the attempt and move the Candidate into RESEARCHING in
    one transaction (`beehive.civic.pipeline`) runs this exact counter rather than a second copy
    of it -- two counters that could disagree about how much budget is left is the one thing a
    spend-before-work design cannot tolerate."""
    if max_attempts < 0:
        raise ValueError("max_attempts must not be negative")
    if max_attempts > MAX_RESEARCH_ATTEMPTS:
        raise ValueError(
            f"max_attempts {max_attempts} exceeds the repository ceiling of "
            f"{MAX_RESEARCH_ATTEMPTS}")
    row = conn.execute(
        "SELECT research_attempt_count FROM civic_candidates WHERE id = ?",
        (candidate_id,)).fetchone()
    if row is None or row["research_attempt_count"] >= max_attempts:
        return None
    cur = conn.execute(
        "UPDATE civic_candidates SET research_attempt_count = research_attempt_count + 1 "
        "WHERE id = ? AND research_attempt_count < ?", (candidate_id, max_attempts))
    return row["research_attempt_count"] + 1 if cur.rowcount else None


def reserve_research_attempt(
    conn: sqlite3.Connection, candidate_id: int, *, max_attempts: int = MAX_RESEARCH_ATTEMPTS,
) -> int | None:
    """Reserves the next research pass for a Candidate, returning its attempt number, or None if
    the budget is already spent.

    The increment and the cap check happen in one BEGIN IMMEDIATE, so two workers racing on the
    same Candidate can never both be granted the last remaining pass. The spend is durable before
    any I/O runs, so a crash mid-pass costs the attempt rather than being silently refunded.

    `max_attempts` lets a caller impose a tighter bound than this repository's ceiling -- the
    Civic evidence pipeline allows exactly one extra pass (`beehive.civic.evidence.
    MAX_RESEARCH_ATTEMPTS`) -- without a second counter that could disagree with this one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        reserved = reserve_research_attempt_locked(
            conn, candidate_id, max_attempts=max_attempts)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return reserved


def pending_research_attempt(conn: sqlite3.Connection, candidate_id: int) -> int | None:
    """The attempt number that was reserved but whose outcome was never recorded, if any.

    This is what a worker resuming after a crash needs: `reserve_research_attempt` spends the
    budget durably *before* the pass runs, so a process that died mid-pass leaves the counter
    incremented and no `civic_research_attempts` row behind. Without this read the resumed worker
    would either buy itself a pass it has already spent or leave the Candidate stuck waiting for
    an outcome nobody is going to write. Returns None when every reserved attempt is recorded."""
    row = conn.execute(
        "SELECT c.research_attempt_count AS reserved, "
        "(SELECT COUNT(*) FROM civic_research_attempts a WHERE a.candidate_id = c.id) AS recorded"
        " FROM civic_candidates c WHERE c.id = ?", (candidate_id,)).fetchone()
    if row is None or row["recorded"] >= row["reserved"]:
        return None
    return int(row["recorded"]) + 1


def record_research_attempt(
    conn: sqlite3.Connection,
    candidate_id: int,
    attempt_number: int,
    outcome: CivicResearchOutcome,
    started_at: datetime,
    completed_at: datetime,
    *,
    evidence_added: int = 0,
    notes: str = "",
    error_code: str | None = None,
) -> CivicResearchAttempt:
    """Writes the outcome of one reserved research pass. Append-only: recording the same
    attempt_number twice raises rather than overwriting the first record, so a retried worker
    cannot rewrite history."""
    attempt = CivicResearchAttempt(
        id=None, candidate_id=candidate_id, attempt_number=attempt_number, outcome=outcome,
        evidence_added=evidence_added, started_at=started_at, completed_at=completed_at,
        notes=notes, error_code=error_code)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
                "SELECT 1 FROM civic_candidates WHERE id = ?",
                (candidate_id,)).fetchone() is None:
            raise ValueError(f"no Civic Candidate with id={candidate_id}")
        if conn.execute(
                "SELECT 1 FROM civic_research_attempts "
                "WHERE candidate_id = ? AND attempt_number = ?",
                (candidate_id, attempt_number)).fetchone() is not None:
            raise ValueError(
                f"Civic Research Attempt {attempt_number} for Candidate {candidate_id} is "
                "already recorded")
        cur = conn.execute(
            "INSERT INTO civic_research_attempts (candidate_id, attempt_number, outcome, "
            "evidence_added, notes, started_at, completed_at, error_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, attempt_number, attempt.outcome.value, evidence_added, notes,
             started_at.isoformat(), completed_at.isoformat(), error_code))
        attempt_id = cur.lastrowid
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_attempt(conn.execute(
        "SELECT * FROM civic_research_attempts WHERE id = ?", (attempt_id,)).fetchone())


def record_research_attempt_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    attempt_number: int,
    outcome: CivicResearchOutcome,
    started_at: datetime,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    evidence_added: int = 0,
    notes: str = "",
    error_code: str | None = None,
) -> CivicResearchAttempt | None:
    """Record a research result only while its Run claim/deadline still authorizes it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT deadline_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        candidate = conn.execute(
            "SELECT run_id FROM civic_candidates WHERE id = ?",
            (candidate_id,)).fetchone()
        if (run is None or candidate is None or candidate["run_id"] != run_id
                or (run["deadline_at"] is not None
                    and now >= datetime.fromisoformat(run["deadline_at"]))):
            result = None
        else:
            attempt = CivicResearchAttempt(
                id=None,
                candidate_id=candidate_id,
                attempt_number=attempt_number,
                outcome=outcome,
                evidence_added=evidence_added,
                started_at=started_at,
                completed_at=now,
                notes=notes,
                error_code=error_code,
            )
            existing = conn.execute(
                "SELECT 1 FROM civic_research_attempts "
                "WHERE candidate_id = ? AND attempt_number = ?",
                (candidate_id, attempt_number)).fetchone()
            if existing is not None:
                raise ValueError(
                    f"Civic Research Attempt {attempt_number} for Candidate {candidate_id} is "
                    "already recorded")
            cur = conn.execute(
                "INSERT INTO civic_research_attempts (candidate_id, attempt_number, outcome, "
                "evidence_added, notes, started_at, completed_at, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, attempt_number, attempt.outcome.value, evidence_added, notes,
                 started_at.isoformat(), now.isoformat(), error_code))
            result = _row_to_attempt(conn.execute(
                "SELECT * FROM civic_research_attempts WHERE id = ?",
                (cur.lastrowid,)).fetchone())
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def list_research_attempts(
    conn: sqlite3.Connection, candidate_id: int,
) -> list[CivicResearchAttempt]:
    rows = conn.execute(
        "SELECT * FROM civic_research_attempts WHERE candidate_id = ? ORDER BY attempt_number",
        (candidate_id,)).fetchall()
    return [_row_to_attempt(row) for row in rows]


def record_verification(
    conn: sqlite3.Connection,
    candidate_id: int,
    verdict: CivicVerificationVerdict,
    model: str,
    now: datetime,
    *,
    rationale: str = "",
    error_code: str | None = None,
) -> CivicVerification:
    """Appends one verifier record, numbered MAX+1 for this Candidate.

    The evidence counts are recounted from civic_candidate_evidence under this call's own lock
    rather than taken from the caller, so a verdict is always recorded against what the database
    actually holds at that instant -- a contradicting source that landed a moment earlier is seen,
    and a SUPPORTED verdict written beside it is rejected by CivicVerification's own invariants
    instead of being persisted. An ERROR verdict skips the counts (there is no judgment to
    record) and requires an error_code."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        verification_id = _record_verification_locked(
            conn, candidate_id, verdict, model, now,
            rationale=rationale, error_code=error_code)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return _row_to_verification(conn.execute(
        "SELECT * FROM civic_verifications WHERE id = ?", (verification_id,)).fetchone())


def _record_verification_locked(
    conn: sqlite3.Connection,
    candidate_id: int,
    verdict: CivicVerificationVerdict,
    model: str,
    now: datetime,
    *,
    rationale: str = "",
    error_code: str | None = None,
) -> int:
    if conn.execute(
            "SELECT 1 FROM civic_candidates WHERE id = ?",
            (candidate_id,)).fetchone() is None:
        raise ValueError(f"no Civic Candidate with id={candidate_id}")
    if verdict is CivicVerificationVerdict.ERROR:
        supporting, contradicting = 0, 0
    else:
        supporting, contradicting = _count_evidence(conn, candidate_id)
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) AS current FROM civic_verifications "
        "WHERE candidate_id = ?", (candidate_id,)).fetchone()
    attempt_number = row["current"] + 1
    CivicVerification(
        id=None, candidate_id=candidate_id, attempt_number=attempt_number, verdict=verdict,
        supporting_evidence_count=supporting, contradicting_evidence_count=contradicting,
        model=model, recorded_at=now, rationale=rationale, error_code=error_code)
    cur = conn.execute(
        "INSERT INTO civic_verifications (candidate_id, attempt_number, verdict, "
        "supporting_evidence_count, contradicting_evidence_count, rationale, model, "
        "recorded_at, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, attempt_number, verdict.value, supporting, contradicting, rationale,
         model, now.isoformat(), error_code))
    return cur.lastrowid


def record_verification_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    verdict: CivicVerificationVerdict,
    model: str,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    rationale: str = "",
    error_code: str | None = None,
) -> CivicVerification | None:
    """Append a verifier verdict only under the live Run claim/deadline."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT deadline_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        candidate = conn.execute(
            "SELECT run_id FROM civic_candidates WHERE id = ?",
            (candidate_id,)).fetchone()
        if (run is None or candidate is None or candidate["run_id"] != run_id
                or (run["deadline_at"] is not None
                    and now >= datetime.fromisoformat(run["deadline_at"]))):
            verification_id = None
        else:
            verification_id = _record_verification_locked(
                conn, candidate_id, verdict, model, now,
                rationale=rationale, error_code=error_code)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    if verification_id is None:
        return None
    return _row_to_verification(conn.execute(
        "SELECT * FROM civic_verifications WHERE id = ?",
        (verification_id,)).fetchone())


def list_verifications(conn: sqlite3.Connection, candidate_id: int) -> list[CivicVerification]:
    rows = conn.execute(
        "SELECT * FROM civic_verifications WHERE candidate_id = ? ORDER BY attempt_number",
        (candidate_id,)).fetchall()
    return [_row_to_verification(row) for row in rows]


def get_latest_verification(
    conn: sqlite3.Connection, candidate_id: int,
) -> CivicVerification | None:
    row = conn.execute(
        "SELECT * FROM civic_verifications WHERE candidate_id = ? "
        "ORDER BY attempt_number DESC LIMIT 1", (candidate_id,)).fetchone()
    return _row_to_verification(row) if row else None


def has_supporting_verification(conn: sqlite3.Connection, candidate_id: int) -> bool:
    """Whether any recorded verification permits publication. Deliberately "any", not "latest":
    a SUPPORTED verdict is a fact that was established, and a later ERROR (a transient model
    failure) must not silently retract it."""
    return conn.execute(
        "SELECT 1 FROM civic_verifications WHERE candidate_id = ? AND verdict = 'supported'",
        (candidate_id,)).fetchone() is not None
