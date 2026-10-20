"""Civic Candidate and Civic Evidence persistence.

**Cross-day deduplication is a database invariant, not a heuristic.** dedupe_key is unique per
Watch (UNIQUE(watch_id, dedupe_key)), not per Run, so the same underlying development can only
ever enter one Civic Batch however many days it keeps being reported. `create_candidate` returns
the existing Candidate instead of raising when its key is already known, which is what makes a
re-run of the same slot -- or a crash mid-batch -- converge rather than duplicate.

**Evidence is append-and-idempotent.** UNIQUE(candidate_id, external_key) means re-capturing the
same source is a no-op that returns the existing row, so a resumed Run cannot inflate a
Candidate's corroboration count by recording the same article twice. Evidence may only be added
while the Candidate is unsettled: attaching a source to an already-published or already-rejected
statement would silently change the basis of a decision that has already been made.

**Publication requires evidence, checked here rather than assumed.**
`mark_candidate_verified` refuses outright unless, under the same BEGIN IMMEDIATE:
  * the Candidate's own evidence satisfies domain.civic.evidence_supports_publication (one
    official/party-primary supporting source, or two independent publishers, and no
    contradicting source at all), and
  * a Civic Verification with a SUPPORTED verdict exists for it (db/civic_verifications.py).
Both are re-read inside the lock, so evidence recorded or a verdict written a moment earlier is
always seen, and a contradicting source landing concurrently cannot be raced past.

Adding a Candidate is fenced on its Batch still being BUILDING: a sealed envelope's content_hash
has already been computed, so admitting one more statement afterwards would make the hash a lie.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from beehive.domain.civic import (CivicBatchStatus, CivicCandidate, CivicCandidateStatus,
                                   CivicEvidence, CivicEvidenceStance, CivicEvidenceTier,
                                   evidence_supports_publication, is_terminal_candidate_status,
                                   require_candidate_transition)

STALE_DESTINATION_CARRYOVER_REJECTION_CODE = "destination_scope_v1_stale"


def _row_to_candidate(row: sqlite3.Row) -> CivicCandidate:
    return CivicCandidate(
        id=row["id"],
        watch_id=row["watch_id"],
        run_id=row["run_id"],
        batch_id=row["batch_id"],
        dedupe_key=row["dedupe_key"],
        title=row["title"],
        statement=row["statement"],
        status=CivicCandidateStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        research_attempt_count=row["research_attempt_count"],
        settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
        sealed_into_batch_at=(
            datetime.fromisoformat(row["sealed_into_batch_at"])
            if row["sealed_into_batch_at"] else None),
        rejection_code=row["rejection_code"],
        superseded_by_candidate_id=row["superseded_by_candidate_id"],
        ai_revision=row["ai_revision"], ai_refresh_reason=row["ai_refresh_reason"])


def _row_to_evidence(row: sqlite3.Row) -> CivicEvidence:
    return CivicEvidence(
        id=row["id"],
        candidate_id=row["candidate_id"],
        source_ref=row["source_ref"],
        external_key=row["external_key"],
        url=row["url"],
        title=row["title"],
        tier=CivicEvidenceTier(row["tier"]),
        stance=CivicEvidenceStance(row["stance"]),
        captured_at=datetime.fromisoformat(row["captured_at"]),
        publisher=row["publisher"],
        excerpt=row["excerpt"],
        published_at=(
            datetime.fromisoformat(row["published_at"]) if row["published_at"] else None),
        publisher_id=row["publisher_id"],
        source_kind=row["source_kind"],
        party_slug=row["party_slug"],
        subject_id=row["subject_id"],
        claim_category=row["claim_category"],
        event_date=date.fromisoformat(row["event_date"]) if row["event_date"] else None,
        canonical_url=row["canonical_url"],
        accessible_full_text=bool(row["accessible_full_text"]),
        paywalled=bool(row["paywalled"]),
        anonymous_source_dependency=bool(row["anonymous_source_dependency"]))


def _fetch(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM civic_candidates WHERE id = ?", (candidate_id,)).fetchone()


def create_candidate_locked(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    watch_id: int,
    run_id: int,
    batch_status: str,
    dedupe_key: str,
    title: str,
    statement: str,
    now: datetime,
) -> tuple[CivicCandidate, bool]:
    """The insert half of `create_candidate`, without the transaction. Caller holds the lock.

    Exists so a caller that must create the Candidate and claim the source items that produced it
    in one transaction (`beehive.civic.pipeline`) runs the same rules as a standalone create
    rather than a second copy of them. Returns (candidate, created)."""
    if not dedupe_key.strip():
        raise ValueError("Civic Candidate needs a dedupe_key")
    existing = conn.execute(
        "SELECT * FROM civic_candidates WHERE watch_id = ? AND dedupe_key = ?",
        (watch_id, dedupe_key)).fetchone()
    if existing is not None:
        return _row_to_candidate(existing), False
    if batch_status != CivicBatchStatus.BUILDING.value:
        raise ValueError(
            f"cannot add a Civic Candidate to Civic Batch {batch_id}: status is "
            f"{batch_status!r}, not 'building'")
    cur = conn.execute(
        "INSERT INTO civic_candidates (watch_id, run_id, batch_id, dedupe_key, title, "
        "statement, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'drafted', ?)",
        (watch_id, run_id, batch_id, dedupe_key, title, statement, now.isoformat()))
    return _row_to_candidate(_fetch(conn, cur.lastrowid)), True


def create_candidate(
    conn: sqlite3.Connection,
    batch_id: int,
    dedupe_key: str,
    title: str,
    statement: str,
    now: datetime,
) -> CivicCandidate:
    """Adds one Candidate to a BUILDING Batch, or returns the Watch's existing Candidate for the
    same dedupe_key.

    Deduplication is Watch-wide, so a development already published (or already rejected) on an
    earlier day returns that earlier Candidate untouched and nothing new is written -- the caller
    can tell it is a duplicate because the returned Candidate's batch_id is not the one it asked
    for. Raises ValueError if the Batch does not exist or is no longer BUILDING: a sealed
    envelope's content_hash is already computed, so a late addition would make it a lie."""
    if not dedupe_key.strip():
        raise ValueError("Civic Candidate needs a dedupe_key")
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute(
            "SELECT run_id, watch_id, status FROM civic_batches WHERE id = ?",
            (batch_id,)).fetchone()
        if batch is None:
            raise ValueError(f"no Civic Batch with id={batch_id}")
        candidate, _created = create_candidate_locked(
            conn, batch_id=batch_id, watch_id=batch["watch_id"], run_id=batch["run_id"],
            batch_status=batch["status"], dedupe_key=dedupe_key, title=title,
            statement=statement, now=now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return candidate


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> CivicCandidate | None:
    row = _fetch(conn, candidate_id)
    return _row_to_candidate(row) if row else None


def get_candidate_by_dedupe_key(
    conn: sqlite3.Connection, watch_id: int, dedupe_key: str,
) -> CivicCandidate | None:
    row = conn.execute(
        "SELECT * FROM civic_candidates WHERE watch_id = ? AND dedupe_key = ?",
        (watch_id, dedupe_key)).fetchone()
    return _row_to_candidate(row) if row else None


def list_batch_candidates(conn: sqlite3.Connection, batch_id: int) -> list[CivicCandidate]:
    rows = conn.execute(
        "SELECT * FROM civic_candidates WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    return [_row_to_candidate(row) for row in rows]


def list_run_candidates(
    conn: sqlite3.Connection, run_id: int, status: CivicCandidateStatus | None = None,
) -> list[CivicCandidate]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM civic_candidates WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM civic_candidates WHERE run_id = ? AND status = ? ORDER BY id",
            (run_id, status.value)).fetchall()
    return [_row_to_candidate(row) for row in rows]


def add_candidate_evidence(
    conn: sqlite3.Connection,
    candidate_id: int,
    source_ref: str,
    external_key: str,
    url: str,
    title: str,
    tier: CivicEvidenceTier,
    stance: CivicEvidenceStance,
    now: datetime,
    *,
    publisher: str = "",
    excerpt: str = "",
    published_at: datetime | None = None,
    publisher_id: str = "",
    source_kind: str | None = None,
    party_slug: str | None = None,
    subject_id: str = "",
    claim_category: str | None = None,
    event_date: date | None = None,
    canonical_url: str = "",
    accessible_full_text: bool = False,
    paywalled: bool = False,
    anonymous_source_dependency: bool = False,
    source_metadata: Mapping[str, Any] | None = None,
) -> CivicEvidence:
    """Records one source for a Candidate, idempotently.

    Re-capturing the same (candidate_id, external_key) returns its current capture projection
    rather than inserting a second copy or overwriting the original -- so a resumed Run cannot inflate a
    Candidate's corroboration count, and a stance recorded once is never silently flipped by a
    later pass. That write-once rule is also what makes the qualification-policy inputs
    (publisher_id, ownership-bearing source_kind, subject, category, event date and the
    accessibility flags) immutable in the original row. Source corrections use the separate
    append-only capture API and invalidate unsealed AI work. Full initial source metadata is
    stored with the new row, never used to silently supersede an existing capture.
    Raises ValueError if the Candidate does not exist or is already
    settled: attaching a source to a published or rejected statement would change the basis of a
    decision that has already been made."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        evidence = _add_candidate_evidence_locked(
            conn, candidate_id, source_ref, external_key, url, title, tier, stance, now,
            publisher=publisher, excerpt=excerpt, published_at=published_at,
            publisher_id=publisher_id, source_kind=source_kind, party_slug=party_slug,
            subject_id=subject_id, claim_category=claim_category, event_date=event_date,
            canonical_url=canonical_url, accessible_full_text=accessible_full_text,
            paywalled=paywalled,
            anonymous_source_dependency=anonymous_source_dependency, source_metadata=source_metadata)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return evidence


def _add_candidate_evidence_locked(
    conn: sqlite3.Connection,
    candidate_id: int,
    source_ref: str,
    external_key: str,
    url: str,
    title: str,
    tier: CivicEvidenceTier,
    stance: CivicEvidenceStance,
    now: datetime,
    *,
    publisher: str = "",
    excerpt: str = "",
    published_at: datetime | None = None,
    publisher_id: str = "",
    source_kind: str | None = None,
    party_slug: str | None = None,
    subject_id: str = "",
    claim_category: str | None = None,
    event_date: date | None = None,
    canonical_url: str = "",
    accessible_full_text: bool = False,
    paywalled: bool = False,
    anonymous_source_dependency: bool = False,
    source_metadata: Mapping[str, Any] | None = None,
) -> CivicEvidence:
    """Insert/replay one evidence row while the caller owns the write transaction."""
    row = conn.execute(
        "SELECT status FROM civic_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"no Civic Candidate with id={candidate_id}")
    if is_terminal_candidate_status(CivicCandidateStatus(row["status"])):
        raise ValueError(
            f"cannot add Civic Evidence to a {row['status']} Civic Candidate {candidate_id}")
    existing = conn.execute(
        "SELECT * FROM civic_candidate_evidence WHERE candidate_id = ? AND external_key = ?",
        (candidate_id, external_key)).fetchone()
    if existing is not None:
        from beehive.db.civic_evidence_captures import project_current_evidence

        return project_current_evidence(conn, _row_to_evidence(existing))
    cur = conn.execute(
        "INSERT INTO civic_candidate_evidence (candidate_id, source_ref, external_key, "
        "url, title, publisher, excerpt, tier, stance, published_at, captured_at, "
        "publisher_id, source_kind, party_slug, subject_id, claim_category, event_date, "
        "canonical_url, accessible_full_text, paywalled, anonymous_source_dependency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, source_ref, external_key, url, title, publisher, excerpt,
         tier.value, stance.value,
         published_at.isoformat() if published_at else None, now.isoformat(),
         publisher_id, source_kind, party_slug, subject_id, claim_category,
         event_date.isoformat() if event_date else None, canonical_url,
         int(accessible_full_text), int(paywalled), int(anonymous_source_dependency)))
    evidence = _row_to_evidence(conn.execute(
        "SELECT * FROM civic_candidate_evidence WHERE id = ?",
        (cur.lastrowid,)).fetchone())
    if source_metadata is not None:
        from beehive.db.civic_evidence_captures import record_initial_capture_locked

        record_initial_capture_locked(conn, evidence, source_metadata)
    return evidence


def add_candidate_evidence_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    source_ref: str,
    external_key: str,
    url: str,
    title: str,
    tier: CivicEvidenceTier,
    stance: CivicEvidenceStance,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    publisher: str = "",
    excerpt: str = "",
    published_at: datetime | None = None,
    publisher_id: str = "",
    source_kind: str | None = None,
    party_slug: str | None = None,
    subject_id: str = "",
    claim_category: str | None = None,
    event_date: date | None = None,
    canonical_url: str = "",
    accessible_full_text: bool = False,
    paywalled: bool = False,
    anonymous_source_dependency: bool = False,
    source_metadata: Mapping[str, Any] | None = None,
) -> CivicEvidence | None:
    """Persist evidence only while the Candidate's Run claim, lease and deadline are live."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT deadline_at, lease_expires_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        candidate = conn.execute(
            "SELECT run_id FROM civic_candidates WHERE id = ?",
            (candidate_id,)).fetchone()
        if (run is None or candidate is None or candidate["run_id"] != run_id
                or run["lease_expires_at"] is None
                or now >= datetime.fromisoformat(run["lease_expires_at"])
                or (run["deadline_at"] is not None
                    and now >= datetime.fromisoformat(run["deadline_at"]))):
            evidence = None
        else:
            evidence = _add_candidate_evidence_locked(
                conn, candidate_id, source_ref, external_key, url, title, tier, stance, now,
                publisher=publisher, excerpt=excerpt, published_at=published_at,
                publisher_id=publisher_id, source_kind=source_kind, party_slug=party_slug,
                subject_id=subject_id, claim_category=claim_category, event_date=event_date,
                canonical_url=canonical_url, accessible_full_text=accessible_full_text,
                paywalled=paywalled,
                anonymous_source_dependency=anonymous_source_dependency,
                source_metadata=source_metadata)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return evidence


def list_candidate_evidence(conn: sqlite3.Connection, candidate_id: int) -> list[CivicEvidence]:
    from beehive.db.civic_evidence_captures import project_current_evidence

    rows = conn.execute(
        "SELECT * FROM civic_candidate_evidence WHERE candidate_id = ? ORDER BY id",
        (candidate_id,)).fetchall()
    return [project_current_evidence(conn, _row_to_evidence(row)) for row in rows]


def candidate_has_publishable_evidence(conn: sqlite3.Connection, candidate_id: int) -> bool:
    """Whether the Candidate's own evidence alone would permit publication (before any verifier
    verdict is consulted). Pure read; the rule itself lives in domain.civic."""
    return evidence_supports_publication(list_candidate_evidence(conn, candidate_id))


def set_candidate_status(
    conn: sqlite3.Connection,
    candidate_id: int,
    target: CivicCandidateStatus,
    now: datetime,
) -> CivicCandidate:
    """Moves a Candidate between non-settling states (DRAFTED/RESEARCHING/VERIFYING), validating
    the move under the same BEGIN IMMEDIATE that performs the write.

    Settling transitions have their own functions, because each carries a required field the
    schema enforces: VERIFIED needs evidence and a supporting verdict, REJECTED needs a rejection
    code, SUPERSEDED needs the Candidate that replaced it."""
    if is_terminal_candidate_status(target):
        raise ValueError(
            f"use the dedicated function to settle a Civic Candidate as {target.value}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise ValueError(f"no Civic Candidate with id={candidate_id}")
        require_candidate_transition(CivicCandidateStatus(row["status"]), target)
        conn.execute(
            "UPDATE civic_candidates SET status = ? WHERE id = ?", (target.value, candidate_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, candidate_id)


def mark_candidate_verified(
    conn: sqlite3.Connection, candidate_id: int, now: datetime,
) -> CivicCandidate:
    """Publishes a Candidate -- the one transition that can make a statement public.

    Refuses, under the same lock, unless BOTH gates are satisfied:
      * the Candidate's own evidence satisfies domain.civic.evidence_supports_publication, and
      * a Civic Verification with a SUPPORTED verdict already exists for it.
    Re-reading both inside the transaction is what closes the race where a contradicting source
    or a superseding verdict lands between a caller's own check and this write."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _mark_candidate_verified_locked(conn, candidate_id, now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, candidate_id)


def _mark_candidate_verified_locked(
    conn: sqlite3.Connection, candidate_id: int, now: datetime,
) -> None:
    row = conn.execute(
        "SELECT status FROM civic_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"no Civic Candidate with id={candidate_id}")
    require_candidate_transition(
        CivicCandidateStatus(row["status"]), CivicCandidateStatus.VERIFIED)
    evidence = [_row_to_evidence(item) for item in conn.execute(
        "SELECT * FROM civic_candidate_evidence WHERE candidate_id = ? ORDER BY id",
        (candidate_id,)).fetchall()]
    if not evidence_supports_publication(evidence):
        raise ValueError(
            f"Civic Candidate {candidate_id} does not have publishable Civic Evidence")
    supported = conn.execute(
        "SELECT 1 FROM civic_verifications "
        "WHERE candidate_id = ? AND verdict = 'supported'", (candidate_id,)).fetchone()
    if supported is None:
        raise ValueError(
            f"Civic Candidate {candidate_id} has no supporting Civic Verification")
    conn.execute(
        "UPDATE civic_candidates SET status = 'verified', settled_at = ? WHERE id = ?",
        (now.isoformat(), candidate_id))


def mark_candidate_verified_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicCandidate | None:
    """Verify only while the Candidate's current Run claim/deadline is live."""
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
            verified = None
        else:
            _mark_candidate_verified_locked(conn, candidate_id, now)
            verified = candidate_id
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, verified) if verified is not None else None


def reject_candidate(
    conn: sqlite3.Connection, candidate_id: int, rejection_code: str, now: datetime,
) -> CivicCandidate:
    """Settles a Candidate as REJECTED. The code is required (and enforced by the schema) so a
    rejection is always explainable rather than an unexplained absence."""
    if not rejection_code.strip():
        raise ValueError("a rejected Civic Candidate needs a rejection_code")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _reject_candidate_locked(conn, candidate_id, rejection_code, now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, candidate_id)


def _reject_candidate_locked(
    conn: sqlite3.Connection,
    candidate_id: int,
    rejection_code: str,
    now: datetime,
) -> None:
    row = conn.execute(
        "SELECT status FROM civic_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"no Civic Candidate with id={candidate_id}")
    require_candidate_transition(
        CivicCandidateStatus(row["status"]), CivicCandidateStatus.REJECTED)
    conn.execute(
        "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, "
        "rejection_code = ? WHERE id = ?", (now.isoformat(), rejection_code, candidate_id))


def reject_candidate_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    rejection_code: str,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicCandidate | None:
    """Reject only while the Candidate's current Run claim/deadline is live."""
    if not rejection_code.strip():
        raise ValueError("a rejected Civic Candidate needs a rejection_code")
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
            rejected = None
        else:
            _reject_candidate_locked(conn, candidate_id, rejection_code, now)
            rejected = candidate_id
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, rejected) if rejected is not None else None


def reject_stale_verified_capacity_carryover_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicCandidate | None:
    """Reject only an adopted, unsealed VERIFIED capacity carryover in this claimed Batch.

    This intentionally does not widen the domain transition table. A normal VERIFIED Candidate
    may only be superseded or deferred for capacity; the exceptional rejection exists solely to
    stop an old destination qualification/subject from poisoning every future daily Run.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = conn.execute(
            "SELECT deadline_at FROM civic_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if (run is None or (
                run["deadline_at"] is not None
                and now >= datetime.fromisoformat(run["deadline_at"]))):
            rejected = None
        else:
            candidate = conn.execute(
                "SELECT c.status AS status, c.batch_id AS batch_id, "
                "c.sealed_into_batch_at AS sealed_into_batch_at, b.status AS batch_status "
                "FROM civic_candidates c "
                "JOIN civic_batches b ON b.id = c.batch_id AND b.run_id = c.run_id "
                "WHERE c.id = ? AND c.run_id = ?",
                (candidate_id, run_id),
            ).fetchone()
            if candidate is None:
                raise ValueError(
                    f"Civic Candidate {candidate_id} is not in Civic Run {run_id}")
            if (candidate["status"] != CivicCandidateStatus.VERIFIED.value
                    or candidate["batch_status"] != CivicBatchStatus.BUILDING.value
                    or candidate["sealed_into_batch_at"] is not None):
                raise ValueError(
                    f"Civic Candidate {candidate_id} is not an unsealed VERIFIED Candidate "
                    "in the current BUILDING Batch")
            adopted_capacity = conn.execute(
                "SELECT 1 FROM civic_capacity_deferrals "
                "WHERE candidate_id = ? AND to_run_id = ? AND to_batch_id = ? "
                "AND adopted_at IS NOT NULL "
                "AND reason NOT IN ('ai_budget_exhausted', 'ai_transient_failed')",
                (candidate_id, run_id, candidate["batch_id"]),
            ).fetchone()
            if adopted_capacity is None:
                raise ValueError(
                    f"Civic Candidate {candidate_id} is not an adopted capacity carryover")
            cur = conn.execute(
                "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, "
                "rejection_code = ? WHERE id = ? AND run_id = ? AND batch_id = ? "
                "AND status = 'verified' AND sealed_into_batch_at IS NULL",
                (
                    now.isoformat(),
                    STALE_DESTINATION_CARRYOVER_REJECTION_CODE,
                    candidate_id,
                    run_id,
                    candidate["batch_id"],
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"Civic Candidate {candidate_id} changed during stale carryover rejection")
            rejected = candidate_id
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, rejected) if rejected is not None else None


def supersede_candidate(
    conn: sqlite3.Connection, candidate_id: int, replacement_candidate_id: int, now: datetime,
) -> CivicCandidate:
    """Publishes a correction: the earlier VERIFIED statement is marked SUPERSEDED and points at
    the Candidate that replaced it, rather than being edited or deleted, so the published record
    stays append-only. Rejects self-supersession and an unknown replacement."""
    if candidate_id == replacement_candidate_id:
        raise ValueError("a Civic Candidate cannot supersede itself")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM civic_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise ValueError(f"no Civic Candidate with id={candidate_id}")
        if conn.execute(
                "SELECT 1 FROM civic_candidates WHERE id = ?",
                (replacement_candidate_id,)).fetchone() is None:
            raise ValueError(f"no Civic Candidate with id={replacement_candidate_id}")
        require_candidate_transition(
            CivicCandidateStatus(row["status"]), CivicCandidateStatus.SUPERSEDED)
        conn.execute(
            "UPDATE civic_candidates SET status = 'superseded', settled_at = ?, "
            "superseded_by_candidate_id = ? WHERE id = ?",
            (now.isoformat(), replacement_candidate_id, candidate_id))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_candidate(conn, candidate_id)
