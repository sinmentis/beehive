"""Research Synthesis persistence (versioned, append-only). Citations are deliberately kept
in a separate concrete table (research_synthesis_citations) rather than embedded as raw
evidence_item_id references inside claims_json -- this is what makes them FK-validated against
research_evidence_items and queryable ("which syntheses cite evidence item X") without ever
parsing JSON, and it is why this module is not a "polymorphic citations" table: see
research_messages.py's research_message_citations for the sibling concrete table used for
Conversation Messages instead of one shared parent_type/parent_id table.

create_synthesis_if_claimed is the claim-, deadline-, AND cancellation-fenced entry point
research.synthesis.generate_synthesis uses while writing a Research Synthesis on behalf of an
actively-processing Research Run: it is create_synthesis's version-allocation/insert/citation
logic plus a (run_id, claim_token, status='processing') check, a `cancel_requested` check, AND a
`deadline_at > now` check, all in the same BEGIN IMMEDIATE transaction, exactly mirroring
db.research_finalization.finalize_snapshot_if_claimed's own claim+deadline(+cancellation) fence.
A stale worker whose lease was recovered and reclaimed by someone else can never insert a
synthesis after losing its claim; a still-active claim whose run's fixed deadline_at has -- by
the time this transaction actually acquires the write lock -- already arrived persists nothing
either; and NEITHER can a still-active, not-yet-past-deadline claim whose `cancel_requested` has,
by that same instant, already been set. All three cases atomically write nothing (no version
consumed, no research_syntheses row, no research_synthesis_citations row) and return a
`SynthesisPersistResult` with `failure_reason` set, mirroring `SnapshotFinalizationResult`'s own
CLAIM_LOST/DEADLINE_EXCEEDED pair -- extended here with a third, CANCEL_REQUESTED.

Cancellation wins over the deadline check, but is shaped the OPPOSITE way
`finalize_snapshot_if_claimed`'s own cancellation carve-out is: a Research Synthesis is always a
new AI-authored write, never local-only work, so cancellation must never let one persist any
more than the deadline does -- if `cancel_requested` is found set, this call discards the two AI
calls' already-computed `claims` exactly like the DEADLINE_EXCEEDED case does (persisting
nothing), but -- UNLIKE that case -- does NOT fail the run either: a cooperative stop discovered
here is not itself an error, and the claim is left exactly 'processing' so the caller's own
subsequent terminal write (research.orchestrator._synthesize_and_terminate, via
complete_research_run_if_claimed's `honor_cancel=True` default) can still decide CANCELLED from
authoritative state, reporting CANCELLED_WITH_EVIDENCE with no synthesis. This is what closes the
race where an Owner's cancellation commits while the two (potentially slow) AI calls that
produced `claims` were still in flight, discovered only once persistence itself rereads
`cancel_requested` fresh under this lock -- research.orchestrator.py's own upstream admission
check (db.research_syntheses.admit_synthesis_if_claimed) already stops a fresh synthesis from
even being ATTEMPTED once cancellation or the deadline is observed beforehand, but this fence is
what makes both guarantees hold even if that upstream check is ever bypassed or raced. Plain
create_synthesis remains available for callers that are not run-claim-fenced (e.g. tests, or a
future backfill outside a worker's claim, or research.synthesis's own pure curation-overlay
revision rebuild, which is never tied to a run claim at all).

admit_synthesis_if_claimed is the SIBLING atomic gate research.orchestrator._synthesize_and_
terminate calls immediately BEFORE ever starting the two new LLM calls a fresh Research
Synthesis requires (never before REUSING an already-persisted one for the same revision, which
needs no new AI work at all): under its own BEGIN IMMEDIATE, it re-verifies the exact same claim/
session/deadline/cancellation state create_synthesis_if_claimed itself re-verifies at persistence
time, so a claim that is already stale, already past its deadline, or already cancelled never
even reaches the AI calls in the first place -- closing the specific race where an orchestrator's
own local, possibly-stale `cancelled` boolean (captured earlier in the same call, before
finalization even began) would otherwise let a cancellation that committed in the meantime slip
past unnoticed and start a new LLM call regardless."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from beehive.domain.research import (ClaimProvenance, EvidenceCitation, ResearchSynthesis,
                                      SufficiencyState, SynthesisClaim, SynthesisSection)


def _claim_from_raw(
        raw: dict, index: int, citations: tuple[EvidenceCitation, ...]) -> SynthesisClaim:
    """Reconstructs one persisted claim, including its `section` -- validated here, at read
    time, exactly like `provenance` already was: a missing or unknown `section` value raises
    immediately, before `build_document` (or any other reader) ever sees the claim, rather than
    failing later while rendering."""
    if "section" not in raw:
        raise ValueError(
            f"persisted Research Synthesis claim at index {index} is missing 'section': {raw!r}")
    try:
        section = SynthesisSection(raw["section"])
    except ValueError as exc:
        raise ValueError(
            f"persisted Research Synthesis claim at index {index} has an unknown section "
            f"{raw['section']!r}") from exc
    return SynthesisClaim(
        text=raw["text"], section=section, provenance=ClaimProvenance(raw["provenance"]),
        citations=citations)


def _row_to_synthesis(conn: sqlite3.Connection, row: sqlite3.Row) -> ResearchSynthesis:
    raw_claims = json.loads(row["claims_json"])
    citation_rows = conn.execute(
        "SELECT claim_index, evidence_item_id, citation_number "
        "FROM research_synthesis_citations WHERE synthesis_id = ? "
        "ORDER BY claim_index, evidence_item_id",
        (row["id"],)).fetchall()
    citations_by_claim: dict[int, list[EvidenceCitation]] = defaultdict(list)
    for c in citation_rows:
        citations_by_claim[c["claim_index"]].append(
            EvidenceCitation(
                evidence_item_id=c["evidence_item_id"], citation_number=c["citation_number"]))
    claims = tuple(
        _claim_from_raw(raw, index, tuple(citations_by_claim.get(index, ())))
        for index, raw in enumerate(raw_claims))
    return ResearchSynthesis(
        id=row["id"],
        session_id=row["session_id"],
        version=row["version"],
        evidence_state_revision_id=row["evidence_state_revision_id"],
        sufficiency_state=SufficiencyState(row["sufficiency_state"]),
        claims=claims,
        created_at=datetime.fromisoformat(row["created_at"]),
        model=row["model"],
        language_code=row["language_code"])


def _claims_to_json(claims: tuple[SynthesisClaim, ...]) -> str:
    return json.dumps([
        {"text": claim.text, "section": claim.section.value, "provenance": claim.provenance.value}
        for claim in claims
    ])


def create_synthesis(conn: sqlite3.Connection, session_id: int, evidence_state_revision_id: int,
                      sufficiency_state: SufficiencyState, claims: tuple[SynthesisClaim, ...],
                      model: str, language_code: str, now: datetime) -> ResearchSynthesis:
    """Writes a new append-only Research Synthesis version. version is allocated as
    MAX(version)+1 for the session under BEGIN IMMEDIATE, and the synthesis row, its
    claims_json (text/section/provenance only), and every claim's citations are written in the
    same transaction so a reader can never observe claims_json without its matching citation
    rows."""
    if not claims:
        raise ValueError("Research Synthesis must contain at least one claim")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
            "FROM research_syntheses WHERE session_id = ?",
            (session_id,)).fetchone()
        version = row["next_version"]
        claims_json = _claims_to_json(claims)
        cur = conn.execute(
            "INSERT INTO research_syntheses (session_id, version, "
            "evidence_state_revision_id, sufficiency_state, claims_json, model, "
            "language_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, version, evidence_state_revision_id, sufficiency_state.value,
             claims_json, model, language_code, now.isoformat()))
        synthesis_id = cur.lastrowid
        citation_rows = [
            (synthesis_id, claim_index, citation.evidence_item_id, citation.citation_number)
            for claim_index, claim in enumerate(claims)
            for citation in claim.citations
        ]
        if citation_rows:
            conn.executemany(
                "INSERT INTO research_synthesis_citations "
                "(synthesis_id, claim_index, evidence_item_id, citation_number) "
                "VALUES (?, ?, ?, ?)",
                citation_rows)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return get_synthesis(conn, synthesis_id)


class SynthesisPersistFailureReason(str, Enum):
    """Why `create_synthesis_if_claimed` wrote nothing. All three are reported purely for
    observability -- a caller must treat them exactly the same way as far as this call's own
    side effects go (nothing was persisted), mirroring `db.research_finalization.
    FinalizationFailureReason`'s own CLAIM_LOST/DEADLINE_EXCEEDED pair for the same class of
    race, extended here with CANCEL_REQUESTED. CLAIM_LOST means (run_id, claim_token,
    status='processing') no longer matched anything at all by the time this call's own BEGIN
    IMMEDIATE acquired the write lock -- the worker's claim was stolen (its lease expired, was
    recovered, and the run was reclaimed by another worker) or the run already reached a terminal
    state some other way. CANCEL_REQUESTED means this exact claim WAS still active at that
    lock-held instant, and `cancel_requested` was found set -- checked BEFORE the deadline below,
    so cancellation wins over it: this call persists nothing (the two AI calls' output is
    discarded) but, unlike the other two reasons, does NOT fail the run either -- the claim is
    left exactly 'processing' for the caller's own subsequent terminal write to decide CANCELLED
    from authoritative state (see the module docstring). DEADLINE_EXCEEDED means this exact claim
    WAS still active and NOT cancelled at that lock-held instant, but the run's own fixed
    deadline_at had -- by that same instant -- already arrived: this call atomically fails the
    run outright (error_code='deadline_exceeded') and persists nothing. None of the three is ever
    a success -- a caller must never read a result with any of them set as a synthesis having
    silently been persisted."""
    CLAIM_LOST = "claim_lost"
    CANCEL_REQUESTED = "cancel_requested"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True)
class SynthesisPersistResult:
    """The single typed outcome of `create_synthesis_if_claimed`. `failure_reason` is None iff
    `synthesis` is the newly-persisted Research Synthesis -- callers must never treat a non-None
    `failure_reason` as "still pending", only as "nothing was persisted this call"."""
    failure_reason: SynthesisPersistFailureReason | None
    synthesis: ResearchSynthesis | None = None

    @property
    def ok(self) -> bool:
        return self.failure_reason is None


def create_synthesis_if_claimed(
        conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
        evidence_state_revision_id: int, sufficiency_state: SufficiencyState,
        claims: tuple[SynthesisClaim, ...], model: str, language_code: str,
        now: datetime, *,
        now_fn: Callable[[], datetime] | None = None,
        reuse_existing_for_run: bool = False) -> SynthesisPersistResult:
    """Claim-, deadline-, AND cancellation-fenced sibling of create_synthesis, closing three
    races: evidence_items.py's upsert_evidence_item_if_claimed's stale-worker race (a claim
    stolen before this call runs), the lock-wait-crossing-deadline race db.research_finalization.
    finalize_snapshot_if_claimed already closes for finalization (Task C), and an Owner's
    cancellation committing while the two (potentially slow) AI calls that produce `claims` were
    still in flight. Any of the three can take long enough that the run's fixed deadline_at
    arrives -- or cancel_requested is set -- while THIS call is merely blocked behind another
    writer for the shared BEGIN IMMEDIATE lock, not before it. Before allocating a version or
    writing anything, this verifies -- under that same BEGIN IMMEDIATE transaction as the version
    allocation and citation writes themselves --

    1. that run_id is still 'processing' with exactly this claim_token and belongs to
       session_id. If a worker's claim was stolen (its lease expired, was recovered, and the run
       was reclaimed by another worker, or the run already reached a terminal state some other
       way) by the time this call runs, the run row no longer matches and this returns
       `SynthesisPersistResult(failure_reason=CLAIM_LOST)` with zero side effects: no version
       consumed, no research_syntheses row, no research_synthesis_citations row.
    2. that `cancel_requested` is NOT set -- checked BEFORE the deadline check in point 3, so
       cancellation wins over it. If it IS set, this persists nothing (same zero side effects as
       point 1) but does NOT fail the run -- it is left exactly 'processing' -- and returns
       `SynthesisPersistResult(failure_reason=CANCEL_REQUESTED)`: the two AI calls' already-
       computed `claims` are discarded, and the run's own terminal status is decided afterward by
       the caller's own cancellation-aware terminal write (see the module docstring).
    3. that the run's fixed deadline_at has not yet arrived (`now < deadline_at`, exactly the
       instant heartbeat_research_run/finalize_snapshot_if_claimed already treat as "arrived").
       If it HAS arrived (and point 2 did not already apply), this atomically fails the run right
       here (error_code='deadline_exceeded', phase/claim/lease cleared) and persists nothing,
       returning `SynthesisPersistResult(failure_reason=DEADLINE_EXCEEDED)`.

    A caller (research.synthesis.generate_synthesis) must treat every failure exactly like every
    other claim-fenced write in this package -- stop immediately, never retry blindly against a
    claim that is already gone, already cancelled, or a deadline that has already arrived; it
    maps CLAIM_LOST to `SynthesisClaimLostError`, DEADLINE_EXCEEDED to
    `SynthesisDeadlineExceededError` (a subclass of it), and CANCEL_REQUESTED to
    `SynthesisCancelledError` (a distinct, sibling exception -- NOT a subclass of either, since
    unlike them the run's claim is still perfectly live and callers must react differently: stop
    generating, but still let the run reach a cancellation-aware terminal write rather than
    treating it as a lost claim).

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like finalize_snapshot_if_
    claimed's own `now_fn` seam. A caller blocked behind another writer until after this run's
    deadline sees that arrival, rather than the stale `now` it happened to sample before ever
    attempting to acquire the lock. `now` itself is used verbatim only when `now_fn` is omitted
    (deterministic single-connection tests); every production caller MUST pass `now_fn` -- its
    own live clock callback, never a pre-sampled datetime."""
    if not claims:
        raise ValueError("Research Synthesis must contain at least one claim")
    conn.execute("BEGIN IMMEDIATE")
    try:
        effective_now = now_fn() if now_fn is not None else now
        failure: SynthesisPersistFailureReason | None = None
        synthesis_id: int | None = None

        run_row = conn.execute(
            "SELECT session_id, deadline_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if run_row is None or run_row["session_id"] != session_id:
            failure = SynthesisPersistFailureReason.CLAIM_LOST

        if failure is None and bool(run_row["cancel_requested"]):
            # Cancellation wins over the deadline check below: discard `claims` with zero
            # database writes, but do NOT fail the run -- leave it 'processing' for the
            # caller's own subsequent terminal write to decide CANCELLED (see the module
            # docstring's cancellation-carve-out paragraph).
            failure = SynthesisPersistFailureReason.CANCEL_REQUESTED

        if failure is None:
            deadline_at = (
                datetime.fromisoformat(run_row["deadline_at"])
                if run_row["deadline_at"] else None)
            if deadline_at is not None and effective_now >= deadline_at:
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (effective_now.isoformat(), run_id, claim_token))
                failure = SynthesisPersistFailureReason.DEADLINE_EXCEEDED

        if failure is None and reuse_existing_for_run:
            existing_row = conn.execute(
                "SELECT syn.id FROM research_syntheses syn "
                "JOIN research_evidence_state_revisions rev "
                "ON rev.id = syn.evidence_state_revision_id "
                "JOIN research_snapshots snap ON snap.id = rev.snapshot_id "
                "WHERE snap.run_id = ? ORDER BY syn.version LIMIT 1",
                (run_id,)).fetchone()
            if existing_row is not None:
                synthesis_id = existing_row["id"]

        if failure is None and synthesis_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM research_syntheses WHERE session_id = ?",
                (session_id,)).fetchone()
            version = row["next_version"]
            claims_json = _claims_to_json(claims)
            cur = conn.execute(
                "INSERT INTO research_syntheses (session_id, version, "
                "evidence_state_revision_id, sufficiency_state, claims_json, model, "
                "language_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, version, evidence_state_revision_id, sufficiency_state.value,
                 claims_json, model, language_code, effective_now.isoformat()))
            synthesis_id = cur.lastrowid
            citation_rows = [
                (synthesis_id, claim_index, citation.evidence_item_id,
                 citation.citation_number)
                for claim_index, claim in enumerate(claims)
                for citation in claim.citations
            ]
            if citation_rows:
                conn.executemany(
                    "INSERT INTO research_synthesis_citations "
                    "(synthesis_id, claim_index, evidence_item_id, citation_number) "
                    "VALUES (?, ?, ?, ?)",
                    citation_rows)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

    if failure is not None:
        return SynthesisPersistResult(failure_reason=failure)
    return SynthesisPersistResult(failure_reason=None, synthesis=get_synthesis(conn, synthesis_id))


class SynthesisAdmissionStatus(str, Enum):
    """The single typed outcome of `admit_synthesis_if_claimed` -- whether it is safe to start
    the two new LLM calls a fresh Research Synthesis requires, decided atomically under the
    exact same claim/session/deadline/cancellation fence `create_synthesis_if_claimed` itself
    re-verifies at persistence time. ALLOWED is the only outcome that clears a caller to proceed
    with those AI calls; every other member means `research.synthesis.generate_synthesis` must
    never be invoked this call:
      * CANCEL_REQUESTED -- `cancel_requested` was found set. Mirrors create_synthesis_if_
        claimed's own CANCEL_REQUESTED exactly: writes nothing at all, and does NOT fail the
        run -- it is left exactly 'processing' so the caller's own subsequent terminal write
        (complete_research_run_if_claimed's `honor_cancel=True` default) decides CANCELLED from
        authoritative state.
      * DEADLINE_EXCEEDED -- `cancel_requested` was NOT set, and the run's own fixed deadline_at
        had, by this lock-held instant, already arrived: this call atomically fails the run right
        here (error_code='deadline_exceeded', phase/claim/lease cleared) -- a caller must not
        attempt any further terminal write of its own for this outcome (the claim is already
        gone), unlike CANCEL_REQUESTED.
      * CLAIM_LOST -- (run_id, claim_token, status='processing') no longer matched anything at
        all -- some other heartbeat/recovery sweep/terminal write already transitioned or
        reclaimed this run earlier. Like DEADLINE_EXCEEDED, a caller must not attempt any further
        terminal write of its own."""
    ALLOWED = "allowed"
    CANCEL_REQUESTED = "cancel_requested"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CLAIM_LOST = "claim_lost"


@dataclass(frozen=True)
class SynthesisAdmissionResult:
    """The single typed outcome of `admit_synthesis_if_claimed`."""
    status: SynthesisAdmissionStatus

    @property
    def allowed(self) -> bool:
        return self.status is SynthesisAdmissionStatus.ALLOWED


def admit_synthesis_if_claimed(
        conn: sqlite3.Connection, run_id: int, claim_token: str, session_id: int,
        now: datetime, *,
        now_fn: Callable[[], datetime] | None = None) -> SynthesisAdmissionResult:
    """Atomic admission gate `research.orchestrator._synthesize_and_terminate` calls immediately
    BEFORE ever starting the two new LLM calls a fresh Research Synthesis requires (never before
    reusing an already-persisted one for the same revision, which needs no new AI work at all).
    Closes the race a caller's own local, possibly-stale `cancelled` boolean or a plain unfenced
    clock read cannot: that boolean is captured once, earlier in the same orchestration call --
    often well before finalization (clustering/sealing/pinning) even begins -- and a real
    cancellation (or the arrival of the run's own fixed deadline) can commit on a wholly separate
    connection any time between that capture and the moment the AI calls would actually start.
    Starting an AI call on the strength of that stale local state would waste a full round-trip
    whose output `create_synthesis_if_claimed` was always going to discard anyway once persistence
    itself rereads authoritative state -- this call instead makes that same claim/session/
    deadline/cancellation check UP FRONT, so no AI call is ever attempted at all once any of them
    has already been decided. Inside a single `BEGIN IMMEDIATE` transaction:

    1. Verifies (run_id, claim_token, status='processing') and that the claimed run belongs to
       `session_id` -- the same fence every other claim-fenced write in this package uses. If the
       row no longer matches, nothing is read or written and this returns
       `SynthesisAdmissionResult(status=CLAIM_LOST)`.
    2. Checks `cancel_requested` BEFORE the deadline check in point 3, so cancellation wins over
       it exactly like `create_synthesis_if_claimed` itself does. If it IS set, this writes
       NOTHING at all -- the run is left exactly 'processing' -- and returns
       `SynthesisAdmissionResult(status=CANCEL_REQUESTED)`; the caller must skip the two AI calls
       entirely and let its own subsequent cancellation-aware terminal write decide CANCELLED.
    3. Otherwise verifies the run's fixed deadline_at has not yet arrived (`now >= deadline_at`,
       the exact instant every other `now_fn`-seamed call in this package already treats as
       "arrived"). If it HAS arrived, this atomically fails the run right here (error_code=
       'deadline_exceeded', phase/claim/lease cleared) and returns `SynthesisAdmissionResult(
       status=DEADLINE_EXCEEDED)` -- a caller must not report success or attempt any further
       terminal write of its own; this call already made the run terminal.
    4. Otherwise returns `SynthesisAdmissionResult(status=ALLOWED)`: the claim is active, not
       cancelled, and not past its deadline as of this exact lock-held instant -- safe to start
       the two new LLM calls.

    `now_fn`, when given, is the authoritative clock: it is sampled ONLY after `BEGIN IMMEDIATE`
    has actually acquired the write lock -- never before -- exactly like every other `now_fn`
    seam in this package. `now` itself is used verbatim only when `now_fn` is omitted
    (deterministic single-connection tests); every production caller MUST pass `now_fn` -- its
    own live clock callback, never a pre-sampled datetime. A caller that receives ALLOWED should
    re-sample (or reuse) a fresh, lock-held-adjacent instant of its own for any subsequent
    per-call AI timeout computation, rather than falling back to whatever `now` it sampled before
    this call even ran."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        effective_now = now_fn() if now_fn is not None else now
        run_row = conn.execute(
            "SELECT session_id, deadline_at, cancel_requested FROM research_runs "
            "WHERE id = ? AND claim_token = ? AND status = 'processing'",
            (run_id, claim_token)).fetchone()
        if run_row is None or run_row["session_id"] != session_id:
            status = SynthesisAdmissionStatus.CLAIM_LOST
        elif bool(run_row["cancel_requested"]):
            # Cancellation wins over the deadline check below -- write nothing, leave the run
            # 'processing' for the caller's own terminal write to decide CANCELLED.
            status = SynthesisAdmissionStatus.CANCEL_REQUESTED
        else:
            deadline_at = (
                datetime.fromisoformat(run_row["deadline_at"])
                if run_row["deadline_at"] else None)
            if deadline_at is not None and effective_now >= deadline_at:
                conn.execute(
                    "UPDATE research_runs SET status = 'failed', phase = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, completed_at = ?, "
                    "error_code = 'deadline_exceeded' "
                    "WHERE id = ? AND claim_token = ? AND status = 'processing'",
                    (effective_now.isoformat(), run_id, claim_token))
                status = SynthesisAdmissionStatus.DEADLINE_EXCEEDED
            else:
                status = SynthesisAdmissionStatus.ALLOWED
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return SynthesisAdmissionResult(status=status)


def get_synthesis(conn: sqlite3.Connection, synthesis_id: int) -> ResearchSynthesis | None:
    row = conn.execute(
        "SELECT * FROM research_syntheses WHERE id = ?", (synthesis_id,)).fetchone()
    return _row_to_synthesis(conn, row) if row else None


def get_latest_synthesis(conn: sqlite3.Connection, session_id: int) -> ResearchSynthesis | None:
    row = conn.execute(
        "SELECT * FROM research_syntheses WHERE session_id = ? ORDER BY version DESC LIMIT 1",
        (session_id,)).fetchone()
    return _row_to_synthesis(conn, row) if row else None


def get_synthesis_for_revision(conn: sqlite3.Connection,
                                evidence_state_revision_id: int) -> ResearchSynthesis | None:
    """Returns the Research Synthesis already pinned to this exact Evidence State Revision, if
    one exists -- used by research.orchestrator.py's crash-recovery resume path (ADR-0009/0010)
    to detect "a synthesis was already persisted for this run's finalization before the worker
    crashed or lost its claim", so it can be REUSED (never regenerated) when a run is reclaimed
    after finalize_snapshot_if_claimed already committed. `evidence_state_revision_id` uniquely
    identifies at most one such synthesis in ordinary operation (each Research Synthesis version
    is generated against the Research Session's then-current revision and revision ids are never
    reused), but `ORDER BY version LIMIT 1` keeps this deterministic even in the degenerate case
    of more than one row somehow sharing a revision id."""
    row = conn.execute(
        "SELECT * FROM research_syntheses WHERE evidence_state_revision_id = ? "
        "ORDER BY version LIMIT 1",
        (evidence_state_revision_id,)).fetchone()
    return _row_to_synthesis(conn, row) if row else None


def get_synthesis_for_run(conn: sqlite3.Connection, run_id: int) -> ResearchSynthesis | None:
    """Returns the first synthesis persisted from any revision of this run's sealed snapshot.

    Owner curation can append newer Evidence State Revisions for the same immutable snapshot after
    a synthesis commits but before the run's terminal write. Recovery must reuse that persisted
    artifact regardless of which same-snapshot revision is now current, rather than generating a
    second synthesis for the same run.
    """
    row = conn.execute(
        "SELECT syn.* FROM research_syntheses syn "
        "JOIN research_evidence_state_revisions rev "
        "ON rev.id = syn.evidence_state_revision_id "
        "JOIN research_snapshots snap ON snap.id = rev.snapshot_id "
        "WHERE snap.run_id = ? ORDER BY syn.version LIMIT 1",
        (run_id,)).fetchone()
    return _row_to_synthesis(conn, row) if row else None


def list_syntheses(conn: sqlite3.Connection, session_id: int) -> list[ResearchSynthesis]:
    rows = conn.execute(
        "SELECT * FROM research_syntheses WHERE session_id = ? ORDER BY version",
        (session_id,)).fetchall()
    return [_row_to_synthesis(conn, r) for r in rows]
