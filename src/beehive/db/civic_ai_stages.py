"""Durable receipts for the Civic AI stages: reserve, call, record, replay, or report drift.

Every generation or independent-verification call leaves one row per attempt, and that row is the
only record the rest of the system trusts. A worker's memory of what it just did survives nothing;
this table survives a crash, a restart and a second worker.

**Reserve, then call, then record.** `reserve_ai_stage` opens BEGIN IMMEDIATE, fences on the
Run's claim token and deadline, checks the per-Run AI budget and the per-stage attempt bound,
writes a `reserved` row and commits. The remote call happens afterwards, with no transaction
open, which is what keeps a slow model from holding SQLite's single write lock for two minutes.
`record_ai_stage_success` / `record_ai_stage_failure` then take the lock again, re-fence, and
settle the reservation. A worker whose claim moved on in between records nothing and is told so
by a `None` return -- the same convention the rest of `db/civic_*.py` uses.

**Replay is by hash, drift is loud.** `find_recorded_stage` returns a stored success only when
the input hash, provider, model, prompt version, prompt hash and schema version all still match.
Any difference raises `CivicAiStageDriftError` naming the field that changed, because silently
regenerating a statement whose inputs moved is how a published claim ends up unsupported by the
evidence that justified it.

**Refresh is revisioned, not destructive.** Active lookups follow the Candidate's ai_revision.
Prior successful rows remain audit records; superseded open reservations cannot later succeed.
Attempt numbers remain globally monotonic per Candidate/stage, while the attempt budget is
bounded per revision/Run. Every reservation still counts against its original Run's AI budget,
including retired revisions. Later-slot retries obey the Candidate's AI deferral limit.

**Nothing secret is stored.** Prompts are stored as hashes, never text; the columns hold a
provider name, a model id, versions, hashes, the normalized parsed output and a bounded error
code and detail. There is no credential, header or endpoint anywhere in this table.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from beehive.civic.ai_gateway import CivicAiStage
from beehive.civic.limits import MAX_AI_CALLS_PER_RUN, MAX_AI_STAGE_ATTEMPTS

# Bounded so a provider that returns a novel-length error message cannot fill the database.
MAX_ERROR_DETAIL_CHARS = 500


class CivicAiStageDriftError(ValueError):
    """A stored stage receipt was produced from something other than what is being offered now.

    Carries the field that moved and both values, so a caller can explain the drift rather than
    only report that there was some."""

    def __init__(self, candidate_id: int, stage: CivicAiStage, field: str,
                 stored: str, offered: str) -> None:
        super().__init__(
            f"Civic Candidate {candidate_id} stage {stage.value}: {field} changed since the "
            f"stored receipt (stored {stored[:16]!r}, offered {offered[:16]!r})")
        self.candidate_id = candidate_id
        self.stage = stage
        self.field = field
        self.stored = stored
        self.offered = offered


class CivicAiStageExhaustedError(RuntimeError):
    """The stage has used its bounded attempts in this revision and Run. A Candidate
    that cannot be generated
    within its budget is rejected for this batch, never retried indefinitely."""

    def __init__(self, candidate_id: int, stage: CivicAiStage, attempts: int) -> None:
        super().__init__(
            f"Civic Candidate {candidate_id} stage {stage.value} has used all {attempts} "
            "attempts")
        self.candidate_id = candidate_id
        self.stage = stage
        self.attempts = attempts


class CivicAiBudgetExhaustedError(RuntimeError):
    """The Run has spent its AI call budget (plan 7.4)."""

    def __init__(self, run_id: int, budget: int) -> None:
        super().__init__(f"Civic Run {run_id} has spent its budget of {budget} AI calls")
        self.run_id = run_id
        self.budget = budget


@dataclass(frozen=True)
class CivicAiStageReceipt:
    """One recorded attempt at one stage."""

    id: int | None
    candidate_id: int
    run_id: int
    stage: CivicAiStage
    attempt: int
    status: str
    provider: str
    model: str
    prompt_version: str
    prompt_hash: str
    schema_version: str
    input_hash: str
    claim_token: str
    reserved_at: datetime
    output_hash: str | None = None
    output_json: str | None = None
    error_code: str | None = None
    error_detail: str = ""
    completed_at: datetime | None = None
    ai_revision: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def output(self) -> Any:
        """The normalized parsed output, as it was stored. Never re-parsed from raw model text:
        what was validated is what was stored, and what was stored is what replays."""
        if self.output_json is None:
            return None
        return json.loads(self.output_json)


def output_hash_for(payload: Any) -> str:
    """SHA-256 over the normalized parsed output, key-sorted so a re-serialization cannot move
    it."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode()).hexdigest()


def _row(row: sqlite3.Row) -> CivicAiStageReceipt:
    return CivicAiStageReceipt(
        id=row["id"],
        candidate_id=row["candidate_id"],
        run_id=row["run_id"],
        stage=CivicAiStage(row["stage"]),
        attempt=row["attempt"],
        status=row["status"],
        provider=row["provider"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        prompt_hash=row["prompt_hash"],
        schema_version=row["schema_version"],
        input_hash=row["input_hash"],
        claim_token=row["claim_token"],
        reserved_at=datetime.fromisoformat(row["reserved_at"]),
        output_hash=row["output_hash"],
        output_json=row["output_json"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
        ai_revision=row["ai_revision"])


def get_stage_receipt(
    conn: sqlite3.Connection, candidate_id: int, stage: CivicAiStage,
) -> CivicAiStageReceipt | None:
    """The active revision's recorded success. Older successes remain in the audit listing."""
    row = conn.execute(
        "SELECT s.* FROM civic_ai_stages s JOIN civic_candidates c ON c.id = s.candidate_id "
        "WHERE s.candidate_id = ? AND s.stage = ? "
        "AND s.status = 'succeeded' AND s.ai_revision = c.ai_revision",
        (candidate_id, stage.value)).fetchone()
    return _row(row) if row else None


def find_recorded_stage(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    stage: CivicAiStage,
    input_hash: str,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    schema_version: str,
) -> CivicAiStageReceipt | None:
    """The stored output to replay, or a drift error naming what moved.

    Returns None when the stage has never succeeded, which is the caller's signal to reserve one
    and make the call."""
    stored = get_stage_receipt(conn, candidate_id, stage)
    if stored is None:
        return None
    for field, stored_value, offered in (
        ("input_hash", stored.input_hash, input_hash),
        ("provider", stored.provider, provider),
        ("model", stored.model, model),
        ("prompt_version", stored.prompt_version, prompt_version),
        ("prompt_hash", stored.prompt_hash, prompt_hash),
        ("schema_version", stored.schema_version, schema_version),
    ):
        if stored_value != offered:
            raise CivicAiStageDriftError(
                candidate_id, stage, field, stored_value, offered)
    return stored


def pending_stage_reservation(
    conn: sqlite3.Connection, candidate_id: int, stage: CivicAiStage,
) -> CivicAiStageReceipt | None:
    """A reservation whose outcome was never recorded: a worker died between the reservation and
    the response, or is making the call right now."""
    row = conn.execute(
        "SELECT s.* FROM civic_ai_stages s JOIN civic_candidates c ON c.id = s.candidate_id "
        "WHERE s.candidate_id = ? AND s.stage = ? "
        "AND s.status = 'reserved' AND s.ai_revision = c.ai_revision",
        (candidate_id, stage.value)).fetchone()
    return _row(row) if row else None


def count_run_ai_calls(conn: sqlite3.Connection, run_id: int) -> int:
    """Every AI call this Run has reserved, spent or failed. Reservations count: the budget is
    spent when the call is authorized, not when it succeeds."""
    row = conn.execute(
        "SELECT COUNT(*) AS calls FROM civic_ai_stages WHERE run_id = ?", (run_id,)).fetchone()
    return int(row["calls"])


def _fence(
    conn: sqlite3.Connection, run_id: int, claim_token: str, now: datetime,
) -> bool:
    row = conn.execute(
        "SELECT deadline_at FROM civic_runs WHERE id = ? AND claim_token = ? "
        "AND status = 'processing'", (run_id, claim_token)).fetchone()
    if row is None:
        return False
    return not (row["deadline_at"] and now >= datetime.fromisoformat(row["deadline_at"]))


def _candidate_revision(
    conn: sqlite3.Connection, candidate_id: int, run_id: int,
) -> int | None:
    row = conn.execute(
        "SELECT c.run_id, c.ai_revision, c.sealed_into_batch_at, b.status AS batch_status "
        "FROM civic_candidates c JOIN civic_batches b ON b.id = c.batch_id WHERE c.id = ?",
        (candidate_id,)).fetchone()
    if row is None or row["run_id"] != run_id:
        raise ValueError(f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
    if row["sealed_into_batch_at"] is not None or row["batch_status"] != "building":
        return None
    return row["ai_revision"]


def reserve_ai_stage(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    run_id: int,
    claim_token: str,
    stage: CivicAiStage,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    schema_version: str,
    input_hash: str,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    max_attempts: int = MAX_AI_STAGE_ATTEMPTS,
    call_budget: int = MAX_AI_CALLS_PER_RUN,
) -> CivicAiStageReceipt | None:
    """Authorize one model call, durably, before it is made.

    Commits alone, on purpose: the spend has to survive the crash it is protecting against, and
    the remote call that follows must not be inside a transaction. Returns None -- writing
    nothing -- when the Run's claim has moved on or its deadline has arrived, when this stage has
    already succeeded, and when the worker holding this same claim is already mid-call for it.

    A reservation left behind by a claim that has since moved on is settled as abandoned and a
    fresh attempt is taken, bounded by `max_attempts`: recovery must be able to finish a
    Candidate a dead worker started, while the crash still costs an attempt.

    Raises `CivicAiStageExhaustedError` when the stage's bounded attempts are gone and
    `CivicAiBudgetExhaustedError` when the Run has spent its AI budget: both are outcomes the
    caller must act on, not races to retry. Neither is a rejection -- `beehive.civic.ai_pipeline`
    defers the Candidate to a later Run for both.

    `max_attempts` and `call_budget` may only ever *narrow* the fixed ceilings. A caller asking
    for more than `MAX_AI_STAGE_ATTEMPTS` attempts or more than `MAX_AI_CALLS_PER_RUN` calls
    raises: the plan's bounds are the point, and a caller that could raise them would make every
    budget guarantee in this package advisory."""
    if max_attempts > MAX_AI_STAGE_ATTEMPTS:
        raise ValueError(
            f"max_attempts {max_attempts} exceeds the fixed ceiling of "
            f"{MAX_AI_STAGE_ATTEMPTS}")
    if call_budget > MAX_AI_CALLS_PER_RUN:
        raise ValueError(
            f"call_budget {call_budget} exceeds the plan's fixed ceiling of "
            f"{MAX_AI_CALLS_PER_RUN} AI calls per Civic Run")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        if not _fence(conn, run_id, claim_token, now):
            reserved = None
        elif (ai_revision := _candidate_revision(conn, candidate_id, run_id)) is None:
            reserved = None
        elif get_stage_receipt(conn, candidate_id, stage) is not None:
            reserved = None
        else:
            live = pending_stage_reservation(conn, candidate_id, stage)
            if live is not None and live.claim_token == claim_token:
                # The worker holding this Run is mid-call for this stage. Nobody else may start a
                # second call for it, and this worker must not start a second one either.
                reserved = None
            else:
                if live is not None:
                    # The reservation belongs to a claim that has since moved on: its worker died
                    # or was recovered mid-call, and its answer can never be recorded (recording
                    # is fenced on the same token). Settle it as abandoned so the stage is not
                    # blocked forever -- and so the attempt it cost stays visible, because a
                    # crash must cost an attempt rather than being silently refunded.
                    conn.execute(
                        "UPDATE civic_ai_stages SET status = 'failed', error_code = ?, "
                        "error_detail = ?, completed_at = ? WHERE id = ? AND status = 'reserved'",
                        ("reservation_abandoned",
                         f"claim {live.claim_token[:8]} no longer holds Civic Run {run_id}",
                         now.isoformat(), live.id))
                reserved = _insert_reservation(
                    conn, candidate_id=candidate_id, run_id=run_id, claim_token=claim_token,
                    stage=stage, provider=provider, model=model, prompt_version=prompt_version,
                    prompt_hash=prompt_hash, schema_version=schema_version,
                    input_hash=input_hash, now=now, max_attempts=max_attempts,
                    call_budget=call_budget, ai_revision=ai_revision)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return reserved


def _insert_reservation(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    run_id: int,
    claim_token: str,
    stage: CivicAiStage,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    schema_version: str,
    input_hash: str,
    now: datetime,
    max_attempts: int,
    call_budget: int,
    ai_revision: int,
) -> CivicAiStageReceipt:
    """The insert half of a reservation, inside the caller's transaction."""
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) AS latest, "
        "COUNT(CASE WHEN ai_revision = ? AND run_id = ? THEN 1 END) AS used FROM civic_ai_stages "
        "WHERE candidate_id = ? AND stage = ?",
        (ai_revision, run_id, candidate_id, stage.value)).fetchone()
    attempt = int(row["latest"]) + 1
    if row["used"] >= max_attempts:
        raise CivicAiStageExhaustedError(candidate_id, stage, max_attempts)
    if count_run_ai_calls(conn, run_id) >= call_budget:
        raise CivicAiBudgetExhaustedError(run_id, call_budget)
    if conn.execute(
            "SELECT 1 FROM civic_candidates WHERE id = ? AND run_id = ?",
            (candidate_id, run_id)).fetchone() is None:
        raise ValueError(
            f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
    cur = conn.execute(
        "INSERT INTO civic_ai_stages (candidate_id, run_id, stage, attempt, status, "
        "provider, model, prompt_version, prompt_hash, schema_version, input_hash, "
        "claim_token, reserved_at, ai_revision) "
        "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, run_id, stage.value, attempt, provider, model, prompt_version,
         prompt_hash, schema_version, input_hash, claim_token, now.isoformat(), ai_revision))
    return _row(conn.execute(
        "SELECT * FROM civic_ai_stages WHERE id = ?", (cur.lastrowid,)).fetchone())


def record_ai_stage_success(
    conn: sqlite3.Connection,
    *,
    reservation: CivicAiStageReceipt,
    output: Any,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicAiStageReceipt | None:
    """Settle a reservation with the validated, normalized output.

    Fenced on the same claim token the reservation was taken under: a worker whose lease was
    recovered while it was waiting for the model writes nothing and gets None back, so the
    Candidate is decided by whoever actually holds the Run."""
    payload = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _settle(
        conn, reservation=reservation, status="succeeded",
        output_hash=output_hash_for(output), output_json=payload, error_code=None,
        error_detail="", now=now, now_fn=now_fn)


def record_ai_stage_failure(
    conn: sqlite3.Connection,
    *,
    reservation: CivicAiStageReceipt,
    error_code: str,
    error_detail: str = "",
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> CivicAiStageReceipt | None:
    """Settle a reservation that produced nothing usable, with a terminal error code."""
    if not error_code.strip():
        raise ValueError("a failed Civic AI stage needs an error_code")
    return _settle(
        conn, reservation=reservation, status="failed", output_hash=None, output_json=None,
        error_code=error_code.strip(), error_detail=error_detail.strip()[:MAX_ERROR_DETAIL_CHARS],
        now=now, now_fn=now_fn)


def _settle(
    conn: sqlite3.Connection,
    *,
    reservation: CivicAiStageReceipt,
    status: str,
    output_hash: str | None,
    output_json: str | None,
    error_code: str | None,
    error_detail: str,
    now: datetime,
    now_fn: Callable[[], datetime] | None,
) -> CivicAiStageReceipt | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        if not _fence(conn, reservation.run_id, reservation.claim_token, now):
            settled = None
        else:
            cur = conn.execute(
                "UPDATE civic_ai_stages SET status = ?, output_hash = ?, output_json = ?, "
                "error_code = ?, error_detail = ?, completed_at = ? "
                "WHERE id = ? AND status = 'reserved' AND claim_token = ? "
                "AND ai_revision = ? AND EXISTS ("
                "SELECT 1 FROM civic_candidates c JOIN civic_batches b ON b.id = c.batch_id "
                "WHERE c.id = civic_ai_stages.candidate_id "
                "AND c.ai_revision = civic_ai_stages.ai_revision AND c.run_id = ? "
                "AND c.sealed_into_batch_at IS NULL AND b.status = 'building')",
                (status, output_hash, output_json, error_code, error_detail, now.isoformat(),
                 reservation.id, reservation.claim_token, reservation.ai_revision,
                 reservation.run_id))
            if not cur.rowcount:
                settled = None
            else:
                settled = _row(conn.execute(
                    "SELECT * FROM civic_ai_stages WHERE id = ?",
                    (reservation.id,)).fetchone())
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return settled


def list_candidate_stages(
    conn: sqlite3.Connection, candidate_id: int,
) -> list[CivicAiStageReceipt]:
    rows = conn.execute(
        "SELECT * FROM civic_ai_stages WHERE candidate_id = ? ORDER BY id",
        (candidate_id,)).fetchall()
    return [_row(row) for row in rows]


def list_run_stages(conn: sqlite3.Connection, run_id: int) -> list[CivicAiStageReceipt]:
    rows = conn.execute(
        "SELECT * FROM civic_ai_stages WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [_row(row) for row in rows]


__all__ = [
    "CivicAiBudgetExhaustedError",
    "CivicAiStageDriftError",
    "CivicAiStageExhaustedError",
    "CivicAiStageReceipt",
    "count_run_ai_calls",
    "find_recorded_stage",
    "get_stage_receipt",
    "list_candidate_stages",
    "list_run_stages",
    "output_hash_for",
    "pending_stage_reservation",
    "record_ai_stage_failure",
    "record_ai_stage_success",
    "reserve_ai_stage",
]
