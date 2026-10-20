"""Civic AI stage receipts: reservation fencing, concurrency, replay, drift, and what is stored.

`tests/civic/test_civic_ai_pipeline.py` exercises these rows through the pipeline. This file
tests the table itself, including the two things only a second connection can prove: that two
workers racing for the same stage produce one reservation rather than two model calls, and that a
reservation taken under a claim token that has since moved on can never be recorded.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.ai_gateway import CivicAiStage
from beehive.db.civic_ai_stages import (CivicAiBudgetExhaustedError, CivicAiStageDriftError,
                                        CivicAiStageExhaustedError, count_run_ai_calls,
                                        find_recorded_stage, get_stage_receipt,
                                        list_candidate_stages, list_run_stages,
                                        output_hash_for, pending_stage_reservation,
                                        record_ai_stage_failure, record_ai_stage_success,
                                        reserve_ai_stage)
from beehive.db.civic_batches import (
    get_or_create_batch_if_claimed,
    refresh_candidate_for_ai_if_claimed,
)
from beehive.db.civic_candidates import create_candidate, get_candidate, set_candidate_status
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicCandidateStatus, CivicRunStatus

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)

RESERVATION = dict(
    provider="github-copilot", model="claude-haiku-4.5",
    prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
    schema_version="civic-ai-schema/1", input_hash="i" * 64)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    watch_id = create_civic_watch(conn, "NZ civic", "What changed?", T0).id
    enqueued = enqueue_civic_run(conn, watch_id, T0, T0)
    lease = claim_civic_run(conn, enqueued.id, T0, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, enqueued.id, lease.run.claim_token, T0)
    candidate = create_candidate(
        conn, batch.id, "fp-1", "A title", "A statement", T0)
    return type("Db", (), {
        "path": path, "conn": conn, "watch_id": watch_id, "run_id": enqueued.id,
        "token": lease.run.claim_token, "batch": batch, "candidate": candidate})()


def reserve(db, stage=CivicAiStage.GENERATE, *, conn=None, token=None, **overrides):
    kwargs = dict(RESERVATION)
    kwargs.update(overrides)
    return reserve_ai_stage(
        conn or db.conn, candidate_id=db.candidate.id, run_id=db.run_id,
        claim_token=token or db.token, stage=stage, now=T0, **kwargs)


# ============================================================================
# Reservation, fencing and concurrency
# ============================================================================


def test_a_reservation_is_visible_before_the_call_is_made(db):
    reservation = reserve(db)
    assert reservation is not None
    assert reservation.status == "reserved"
    assert reservation.output_json is None
    assert reservation.completed_at is None
    assert pending_stage_reservation(db.conn, db.candidate.id, CivicAiStage.GENERATE) is not None
    assert count_run_ai_calls(db.conn, db.run_id) == 1


def test_two_workers_cannot_reserve_the_same_stage(db):
    """The loser reserves nothing and is told so, so exactly one model call is ever made."""
    other = connect(db.path)
    first = reserve(db)
    second = reserve(db, conn=other)
    assert first is not None
    assert second is None
    assert len(list_candidate_stages(db.conn, db.candidate.id)) == 1


def test_a_recovered_run_takes_over_an_abandoned_reservation(db):
    """A worker died mid-call. Its reservation can never be recorded (recording is fenced on the
    same token), so recovery must be able to take the stage over -- and the crash must still cost
    an attempt."""
    stranded = reserve(db)
    assert stranded is not None
    db.conn.execute(
        "UPDATE civic_runs SET claim_token = 'recovered-token' WHERE id = ?", (db.run_id,))
    db.conn.commit()

    retaken = reserve(db, token="recovered-token")

    assert retaken is not None
    assert retaken.attempt == stranded.attempt + 1
    receipts = list_candidate_stages(db.conn, db.candidate.id)
    assert [entry.status for entry in receipts] == ["failed", "reserved"]
    assert receipts[0].error_code == "reservation_abandoned"


def test_an_abandoned_reservation_still_respects_the_attempt_bound(db):
    reserve(db)
    db.conn.execute(
        "UPDATE civic_runs SET claim_token = 'second-token' WHERE id = ?", (db.run_id,))
    db.conn.commit()
    reserve(db, token="second-token")
    db.conn.execute(
        "UPDATE civic_runs SET claim_token = 'third-token' WHERE id = ?", (db.run_id,))
    db.conn.commit()

    with pytest.raises(CivicAiStageExhaustedError):
        reserve(db, token="third-token")


def test_two_workers_can_reserve_different_stages(db):
    other = connect(db.path)
    assert reserve(db, CivicAiStage.GENERATE) is not None
    assert reserve(db, CivicAiStage.VERIFICATION, conn=other) is not None
    assert count_run_ai_calls(db.conn, db.run_id) == 2


def test_a_reservation_needs_the_run_claim(db):
    assert reserve(db, token="someone-else") is None
    assert list_candidate_stages(db.conn, db.candidate.id) == []


def test_a_reservation_is_refused_past_the_run_deadline(db):
    late = T0 + timedelta(hours=2)
    assert reserve_ai_stage(
        db.conn, candidate_id=db.candidate.id, run_id=db.run_id, claim_token=db.token,
        stage=CivicAiStage.GENERATE, now=late, **RESERVATION) is None


def test_a_reservation_is_refused_once_the_run_is_finished(db):
    complete_civic_run(db.conn, db.run_id, db.token, CivicRunStatus.COMPLETED, T0)
    assert reserve(db) is None


def test_a_second_success_for_one_stage_is_impossible(db):
    reservation = reserve(db)
    record_ai_stage_success(db.conn, reservation=reservation, output={"ok": True}, now=T0)
    assert reserve(db) is None
    assert get_stage_receipt(db.conn, db.candidate.id, CivicAiStage.GENERATE).succeeded

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO civic_ai_stages (candidate_id, run_id, stage, attempt, status, "
            "provider, model, prompt_version, prompt_hash, schema_version, input_hash, "
            "claim_token, output_hash, output_json, reserved_at, completed_at) "
            "VALUES (?, ?, 'generate', 9, 'succeeded', 'p', 'm', 'v', 'h', 's', 'i', 't', "
            "'oh', '{}', ?, ?)",
            (db.candidate.id, db.run_id, T0.isoformat(), T0.isoformat()))


def test_refresh_excludes_old_cache_and_reservations_without_refunding_spend(db):
    set_candidate_status(db.conn, db.candidate.id, CivicCandidateStatus.VERIFYING, T0)
    original = record_ai_stage_success(
        db.conn, reservation=reserve(db), output={"original": True}, now=T0)
    pending = reserve(db, CivicAiStage.VERIFICATION)
    assert count_run_ai_calls(db.conn, db.run_id) == 2

    refreshed = refresh_candidate_for_ai_if_claimed(
        db.conn, candidate_id=db.candidate.id, run_id=db.run_id, claim_token=db.token,
        expected_revision=0, reason="publication_generation_changed", now=T0)
    assert refreshed.ai_revision == 1
    assert get_stage_receipt(db.conn, db.candidate.id, CivicAiStage.GENERATE) is None
    assert pending_stage_reservation(db.conn, db.candidate.id, CivicAiStage.VERIFICATION) is None
    assert record_ai_stage_success(
        db.conn, reservation=pending, output={"stale": True}, now=T0) is None
    current = reserve(db)
    assert current.ai_revision == 1
    assert current.attempt > original.attempt
    assert count_run_ai_calls(db.conn, db.run_id) == 3
    assert original in list_candidate_stages(db.conn, db.candidate.id)
    assert next(
        entry for entry in list_candidate_stages(db.conn, db.candidate.id)
        if entry.id == pending.id).error_code == "revision_superseded"


def test_failed_refresh_rolls_back_revision_and_keeps_original_success_active(db):
    set_candidate_status(db.conn, db.candidate.id, CivicCandidateStatus.VERIFYING, T0)
    original = record_ai_stage_success(
        db.conn, reservation=reserve(db), output={"original": True}, now=T0)
    db.conn.execute(
        "CREATE TEMP TRIGGER fail_ai_refresh AFTER UPDATE OF ai_revision ON civic_candidates "
        "BEGIN SELECT RAISE(ABORT, 'fixture refresh failed'); END")

    with pytest.raises(sqlite3.IntegrityError, match="fixture refresh failed"):
        refresh_candidate_for_ai_if_claimed(
            db.conn, candidate_id=db.candidate.id, run_id=db.run_id, claim_token=db.token,
            expected_revision=0, reason="publication_generation_changed", now=T0)

    assert get_candidate(db.conn, db.candidate.id).ai_revision == 0
    assert get_stage_receipt(db.conn, db.candidate.id, CivicAiStage.GENERATE) == original
    assert count_run_ai_calls(db.conn, db.run_id) == 1


# ============================================================================
# Recording an outcome
# ============================================================================


def test_success_stores_the_normalized_output_and_its_hash(db):
    reservation = reserve(db)
    payload = {"decision": "qualify", "editorial_exclusion": None, "reason": "ok"}
    recorded = record_ai_stage_success(
        conn=db.conn, reservation=reservation, output=payload, now=T0)
    assert recorded.succeeded
    assert recorded.output == payload
    assert recorded.output_hash == output_hash_for(payload)
    assert recorded.completed_at == T0


def test_failure_stores_a_terminal_code_and_no_output(db):
    reservation = reserve(db)
    recorded = record_ai_stage_failure(
        db.conn, reservation=reservation, error_code="model_timeout",
        error_detail="no response", now=T0)
    assert recorded.status == "failed"
    assert recorded.error_code == "model_timeout"
    assert recorded.output_json is None


def test_a_failure_needs_a_code(db):
    reservation = reserve(db)
    with pytest.raises(ValueError, match="error_code"):
        record_ai_stage_failure(db.conn, reservation=reservation, error_code="  ", now=T0)


def test_a_stale_claim_token_records_nothing(db):
    reservation = reserve(db)
    db.conn.execute("UPDATE civic_runs SET claim_token = 'moved-on' WHERE id = ?", (db.run_id,))
    db.conn.commit()
    assert record_ai_stage_success(
        db.conn, reservation=reservation, output={"ok": True}, now=T0) is None
    assert get_stage_receipt(db.conn, db.candidate.id, CivicAiStage.GENERATE) is None
    assert pending_stage_reservation(
        db.conn, db.candidate.id, CivicAiStage.GENERATE) is not None


def test_a_reservation_cannot_be_settled_twice(db):
    reservation = reserve(db)
    assert record_ai_stage_success(
        db.conn, reservation=reservation, output={"ok": True}, now=T0) is not None
    assert record_ai_stage_success(
        db.conn, reservation=reservation, output={"ok": True}, now=T0) is None


def test_error_detail_is_bounded(db):
    reservation = reserve(db)
    recorded = record_ai_stage_failure(
        db.conn, reservation=reservation, error_code="model_provider_error",
        error_detail="x" * 5000, now=T0)
    assert len(recorded.error_detail) <= 500


# ============================================================================
# Replay and drift
# ============================================================================


def test_replay_returns_the_stored_receipt(db):
    reservation = reserve(db)
    record_ai_stage_success(db.conn, reservation=reservation, output={"ok": True}, now=T0)
    replayed = find_recorded_stage(
        db.conn, candidate_id=db.candidate.id, stage=CivicAiStage.GENERATE, **RESERVATION)
    assert replayed is not None
    assert replayed.output == {"ok": True}


def test_nothing_recorded_yet_is_not_drift(db):
    assert find_recorded_stage(
        db.conn, candidate_id=db.candidate.id, stage=CivicAiStage.GENERATE,
        **RESERVATION) is None


@pytest.mark.parametrize("field,value", [
    ("input_hash", "j" * 64),
    ("provider", "another-provider"),
    ("model", "gpt-5.4"),
    ("prompt_version", "civic-ai-prompts/2"),
    ("prompt_hash", "q" * 64),
    ("schema_version", "civic-ai-schema/2"),
])
def test_a_changed_input_model_prompt_or_schema_is_drift(db, field: str, value: str):
    reservation = reserve(db)
    record_ai_stage_success(db.conn, reservation=reservation, output={"ok": True}, now=T0)
    offered = dict(RESERVATION)
    offered[field] = value
    with pytest.raises(CivicAiStageDriftError) as exc:
        find_recorded_stage(
            db.conn, candidate_id=db.candidate.id, stage=CivicAiStage.GENERATE, **offered)
    assert exc.value.field == field


# ============================================================================
# Bounds
# ============================================================================


def test_attempts_are_bounded_and_the_bound_is_terminal(db):
    for _ in range(2):
        reservation = reserve(db)
        record_ai_stage_failure(
            db.conn, reservation=reservation, error_code="model_timeout", now=T0)
    with pytest.raises(CivicAiStageExhaustedError):
        reserve(db)
    assert len(list_candidate_stages(db.conn, db.candidate.id)) == 2


def test_the_run_call_budget_is_enforced(db):
    reservation = reserve(db, call_budget=1)
    record_ai_stage_failure(
        db.conn, reservation=reservation, error_code="model_timeout", now=T0)
    with pytest.raises(CivicAiBudgetExhaustedError):
        reserve(db, CivicAiStage.VERIFICATION, call_budget=1)


def test_a_candidate_outside_the_run_cannot_reserve(db):
    """A second Watch's Run holds the same claim token in this test, so the only thing standing
    between it and a stage receipt is the candidate/run check."""
    other_watch = create_civic_watch(db.conn, "Other", "Q", T0).id
    other = enqueue_civic_run(db.conn, other_watch, T0, T0)
    lease = claim_civic_run(db.conn, other.id, T0, 600, 3600)
    with pytest.raises(ValueError, match="no Civic Candidate"):
        reserve_ai_stage(
            db.conn, candidate_id=db.candidate.id, run_id=other.id,
            claim_token=lease.run.claim_token, stage=CivicAiStage.GENERATE, now=T0,
            **RESERVATION)


def test_run_stages_are_listed_in_order(db):
    reserve(db, CivicAiStage.GENERATE)
    reserve(db, CivicAiStage.VERIFICATION)
    assert [entry.stage for entry in list_run_stages(db.conn, db.run_id)] == [
        CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]


# ============================================================================
# Schema shape
# ============================================================================


def test_the_table_refuses_a_half_recorded_row(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO civic_ai_stages (candidate_id, run_id, stage, attempt, status, "
            "provider, model, prompt_version, prompt_hash, schema_version, input_hash, "
            "claim_token, reserved_at) "
            "VALUES (?, ?, 'generate', 1, 'succeeded', 'p', 'm', 'v', 'h', 's', 'i', 't', ?)",
            (db.candidate.id, db.run_id, T0.isoformat()))


def test_the_table_refuses_an_unknown_stage(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO civic_ai_stages (candidate_id, run_id, stage, attempt, status, "
            "provider, model, prompt_version, prompt_hash, schema_version, input_hash, "
            "claim_token, reserved_at) "
            "VALUES (?, ?, 'translate_de', 1, 'reserved', 'p', 'm', 'v', 'h', 's', 'i', 't', ?)",
            (db.candidate.id, db.run_id, T0.isoformat()))


def test_stage_rows_vanish_with_their_candidate(db):
    reserve(db)
    db.conn.execute("DELETE FROM civic_candidates WHERE id = ?", (db.candidate.id,))
    db.conn.commit()
    assert list_candidate_stages(db.conn, db.candidate.id) == []
