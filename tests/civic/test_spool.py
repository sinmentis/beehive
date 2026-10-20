from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import beehive.civic.spool as spool_module
from beehive.db import app_state
from beehive.civic.ai_gateway import CivicAiStage
from beehive.civic.ai_pipeline import generate_civic_candidate
from beehive.civic.destination import (
    DESTINATION_V1_ENGINE_VERSION,
    constrain_qualification_to_destination_v1,
)
from beehive.civic.pipeline import ingest_civic_documents, qualify_civic_candidate
from beehive.civic.production import (
    build_ownership_registry,
    build_source_profiles,
    construct_candidate_claim,
    persist_cluster_evidence,
)
from beehive.civic.scheduling import schedule_due_civic_runs
from beehive.civic.spool import (
    CivicOutcomeDisposition,
    CivicSpool,
    CivicSpoolIntegrityError,
    CivicSpoolLegacyProjectionError,
    canonical_json_bytes,
    payload_hash_for,
    validate_publication_envelope,
)
from beehive.civic.documents import CivicFetchResult, CivicSourceHealth
from beehive.civic.events import CivicEventLogger
from beehive.civic.worker import CivicRunExecutionStatus, CivicWorker
from beehive.db.civic_batches import (
    get_civic_batch,
    get_frozen_publication,
    get_or_create_batch_if_claimed,
    list_publication_rejections,
    list_candidate_capacity_deferrals,
    refresh_candidate_for_ai_if_claimed,
    list_sealed_candidate_ids,
    seal_batch_if_claimed,
)
from beehive.db.civic_ai_stages import get_stage_receipt, output_hash_for
from beehive.db.civic_candidates import (
    add_candidate_evidence,
    create_candidate,
    get_candidate,
    mark_candidate_verified,
    set_candidate_status,
)
from beehive.db.civic_receipts import get_receipt_for_batch
from beehive.db.civic_runs import claim_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (
    CivicBatchStatus,
    CivicCandidateStatus,
    CivicEvidenceStance,
    CivicEvidenceTier,
    CivicVerificationVerdict,
)
from tests.civic.civic_worker_support import (
    MutableClock,
    SOURCE_ENTRY,
    T0,
    create_partymap_watch,
    housing_document,
    housing_fetch_result,
    passing_green_gateway,
    seal_empty_daily_batches,
)

GOLDEN_ENVELOPE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "civic"
    / "civic-publication-v1.sample.json"
)
GOLDEN_PAYLOAD_HASH = "799cceffdb318cddcfedd54e0a897d2653c14acfd7e6187baebb0c1687b17878"
CORRELATION_ID = "civic-spool-test-0001"


async def _completed_batch(
    tmp_path,
    *,
    empty: bool = False,
    event_logger: CivicEventLogger | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(str(tmp_path / "spool.db"))
    init_schema(conn)
    create_partymap_watch(conn)

    async def fetch(_entry, _now):
        if empty:
            return CivicFetchResult(
                source_key=SOURCE_ENTRY.source_key,
                documents=(),
                health=CivicSourceHealth.EMPTY,
                fetched_at=T0,
            )
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
        event_logger=event_logger,
    )
    cycle = await worker.run_due_cycle()
    assert len(cycle.runs) == 1
    assert cycle.runs[0].status is CivicRunExecutionStatus.COMPLETED
    return conn, cycle.runs[0].batch_id


async def _prepared_publication_batch(tmp_path):
    conn = connect(str(tmp_path / "prepared.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = schedule_due_civic_runs(conn, T0)[0].run
    lease = claim_civic_run(conn, run.id, T0, 300, 1200)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    ingested = ingest_civic_documents(
        conn, run_id=run.id, claim_token=lease.run.claim_token,
        documents=(housing_document(),), now=T0)
    candidate = ingested.created_candidates[0]
    persist_cluster_evidence(
        conn, run_id=run.id, claim_token=lease.run.claim_token,
        candidate=candidate, cluster=ingested.clustering.clusters[0],
        watch=watch, subject_id="green", profiles=build_source_profiles(), now=T0)
    qualify_civic_candidate(
        conn, run_id=run.id, claim_token=lease.run.claim_token,
        claim=construct_candidate_claim(conn, candidate), registry=build_ownership_registry(),
        now=T0, engine_version=DESTINATION_V1_ENGINE_VERSION,
        qualification_scope=constrain_qualification_to_destination_v1)
    generated = await generate_civic_candidate(
        conn, run_id=run.id, claim_token=lease.run.claim_token, candidate_id=candidate.id,
        gateway=passing_green_gateway(), now=T0)
    assert generated.verified
    return conn, lease.run, batch, generated.candidate


def _remove_post_legacy_schema(conn):
    """Make a fixture database match the schema that existed before frozen publications."""
    conn.execute("DROP TABLE civic_batch_publications")
    conn.execute("DROP TABLE civic_publication_rejections")
    conn.execute("ALTER TABLE civic_batches DROP COLUMN publication_schema_version")
    # Mark the file as the last pre-Civic schema version so init_schema replays the reconcile.
    conn.execute("PRAGMA user_version = 4")
    conn.commit()


def _outcome(batch, outcome: str, **overrides) -> dict:
    payload = {
        "schema_version": 1,
        "idempotency_key": batch.idempotency_key,
        "payload_hash": batch.payload_hash,
        "outcome": outcome,
        "recorded_at": T0.isoformat(),
    }
    if outcome == "accepted":
        payload["proposal"] = {
            "branch": "newsroom/civic-test",
            "reference": "proposal:test",
            "pull_request_number": 7,
        }
    else:
        payload.update({
            "reason": "destination_validation_failed",
            "detail": "Destination refused the whole Batch.",
        })
    payload.update(overrides)
    return payload


def test_canonical_hash_omits_only_payload_hash_and_preserves_unicode():
    envelope = {
        "schema_version": 1,
        "batch": {
            "idempotency_key": "abcdefghijklmnop",
            "batch_content_hash": "a" * 64,
            "payload_hash": "f" * 64,
            "slot_at": "2026-08-01T06:30:00+12:00",
            "sealed_at": "2026-08-01T06:31:00+12:00",
        },
        "items": [{"title": "Te Pāti Māori"}],
    }
    projected = json.loads(json.dumps(envelope))
    del projected["batch"]["payload_hash"]
    expected = hashlib.sha256(canonical_json_bytes(projected)).hexdigest()
    assert payload_hash_for(envelope) == expected
    assert b"Te P\xc4\x81ti M\xc4\x81ori" in canonical_json_bytes(envelope)
    assert not canonical_json_bytes(envelope).endswith(b"\n")


def test_shared_golden_envelope_validates_and_reproduces_exact_payload_hash():
    envelope = json.loads(GOLDEN_ENVELOPE_PATH.read_text(encoding="utf-8"))
    validate_publication_envelope(envelope)
    assert envelope["batch"]["payload_hash"] == GOLDEN_PAYLOAD_HASH
    assert payload_hash_for(envelope) == GOLDEN_PAYLOAD_HASH
    assert not canonical_json_bytes(envelope).endswith(b"\n")


async def test_atomic_write_and_identical_replay_do_not_receipt(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    first = spool.write_batch(conn, batch_id, T0)
    second = spool.write_batch(conn, batch_id, T0)

    payload = json.loads(first.path.read_text(encoding="utf-8"))
    assert first.path.read_bytes() == canonical_json_bytes(payload)
    assert payload_hash_for(payload) == first.payload_hash
    assert not first.replayed
    assert second.replayed
    assert get_civic_batch(conn, batch_id).payload_hash == first.payload_hash
    assert get_civic_batch(conn, batch_id).attempt_count == 0
    assert get_receipt_for_batch(conn, batch_id) is None


@pytest.mark.parametrize("write_before_restart", [False, True])
async def test_sealed_projection_survives_policy_change_and_restart(
    tmp_path, monkeypatch, write_before_restart,
):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    original = get_frozen_publication(conn, batch_id)
    original_hash = get_civic_batch(conn, batch_id).payload_hash
    if write_before_restart:
        spool.write_batch(conn, batch_id, T0)
    conn.close()

    monkeypatch.setattr("beehive.civic.generation.is_citable", lambda *args, **kwargs: False)
    conn = connect(str(tmp_path / "spool.db"))
    init_schema(conn)
    replay = CivicSpool(tmp_path / "civic-spool").write_batch(conn, batch_id, T0)

    assert replay.replayed is write_before_restart
    assert replay.path.read_bytes() == original
    assert replay.payload_hash == original_hash
    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.SEALED
    assert get_receipt_for_batch(conn, batch_id) is None


async def test_failed_freeze_rolls_back_membership_hash_and_refresh(tmp_path):
    conn, run, batch, valid = await _prepared_publication_batch(tmp_path)
    invalid = create_candidate(conn, batch.id, "operator-only", "Title", "Statement", T0)
    add_candidate_evidence(
        conn, invalid.id, "official", "operator-evidence", "https://example.test/source",
        "Official", CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0)
    set_candidate_status(conn, invalid.id, CivicCandidateStatus.VERIFYING, T0)
    record_verification(conn, invalid.id, CivicVerificationVerdict.SUPPORTED, "gpt-5", T0)
    mark_candidate_verified(conn, invalid.id, T0)
    conn.execute(
        "CREATE TEMP TRIGGER fail_publication_freeze AFTER INSERT ON civic_batch_publications "
        "BEGIN SELECT RAISE(ABORT, 'fixture freeze failed'); END")

    with pytest.raises(sqlite3.IntegrityError, match="fixture freeze failed"):
        seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    unchanged = get_civic_batch(conn, batch.id)
    assert unchanged.status is CivicBatchStatus.BUILDING
    assert unchanged.content_hash is unchanged.payload_hash is unchanged.sealed_at is None
    assert get_frozen_publication(conn, batch.id) is None
    assert list_sealed_candidate_ids(conn, batch.id) == []
    assert list_publication_rejections(conn, batch.id) == []
    assert get_candidate(conn, valid.id).status is CivicCandidateStatus.VERIFIED
    assert get_candidate(conn, invalid.id).status is CivicCandidateStatus.VERIFIED
    assert get_candidate(conn, invalid.id).ai_revision == 0
    conn.execute("DROP TRIGGER fail_publication_freeze")

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    assert list_sealed_candidate_ids(conn, sealed.id) == [valid.id]
    assert get_candidate(conn, invalid.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(conn, invalid.id).ai_revision == 0
    assert get_candidate(conn, invalid.id).ai_refresh_reason.startswith("source_refresh:")
    written = CivicSpool(tmp_path / "spool").write_batch(conn, sealed.id, T0)
    assert written.path.read_bytes() == get_frozen_publication(conn, sealed.id)


@pytest.mark.parametrize(
    "problem,reason",
    [
        ("missing_receipt", "publication_verification_missing"),
        ("changed_entry", "publication_verification_changed"),
    ],
)
async def test_publication_requires_verification_of_the_exact_generated_entry(
    tmp_path, problem, reason,
):
    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    if problem == "missing_receipt":
        conn.execute(
            "DELETE FROM civic_ai_stages WHERE candidate_id = ? AND stage = 'verification'",
            (candidate.id,))
    else:
        receipt = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)
        output = receipt.output
        for content in output["entry"].values():
            content["summary"] += " "
        conn.execute(
            "UPDATE civic_ai_stages SET output_json = ?, output_hash = ? WHERE id = ?",
            (json.dumps(output), output_hash_for(output), receipt.id))
    conn.commit()

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert sealed.candidate_count == 0
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(conn, candidate.id).ai_refresh_reason == reason
    assert list_candidate_capacity_deferrals(conn, candidate.id)[-1].reason == "ai_provenance_stale"


@pytest.mark.parametrize(
    "stage,field,version",
    [
        (CivicAiStage.GENERATE, "prompt_version", "civic-ai-prompts/2"),
        (CivicAiStage.VERIFICATION, "prompt_version", "civic-ai-prompts/2"),
        (CivicAiStage.GENERATE, "schema_version", "civic-ai-schema/0"),
        (CivicAiStage.VERIFICATION, "schema_version", "civic-ai-schema/0"),
    ],
)
async def test_cached_stage_versions_must_match_even_when_hashes_match(
    tmp_path, stage, field, version,
):
    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    conn.execute(
        f"UPDATE civic_ai_stages SET {field} = ? WHERE candidate_id = ? AND stage = ?",
        (version, candidate.id, stage.value))
    conn.commit()

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert sealed.candidate_count == 0
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(conn, candidate.id).ai_refresh_reason == "publication_stage_version_changed"
    assert get_stage_receipt(conn, candidate.id, stage) is None
    assert list_publication_rejections(conn, batch.id) == []


@pytest.mark.parametrize(
    "stage,field,value,reason",
    [
        (CivicAiStage.GENERATE, "input_hash", "0" * 64, "publication_snapshot_changed"),
        (CivicAiStage.VERIFICATION, "input_hash", "0" * 64, "publication_verification_changed"),
        (CivicAiStage.GENERATE, "prompt_hash", "0" * 64, "publication_generation_changed"),
        (CivicAiStage.VERIFICATION, "prompt_hash", "0" * 64, "publication_verification_changed"),
        (CivicAiStage.GENERATE, "provider", "old-provider", "publication_stage_provider_changed"),
        (CivicAiStage.VERIFICATION, "provider", "old-provider", "publication_stage_provider_changed"),
        (CivicAiStage.GENERATE, "model", "retired-model", "publication_stage_model_unsupported"),
        (CivicAiStage.VERIFICATION, "model", "retired-model", "publication_stage_model_unsupported"),
    ],
)
async def test_current_version_receipts_still_require_matching_request_provenance(
    tmp_path, stage, field, value, reason,
):
    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    generation = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)
    verification = get_stage_receipt(conn, candidate.id, CivicAiStage.VERIFICATION)
    assert generation.input_hash == verification.input_hash
    assert generation.prompt_version == verification.prompt_version
    assert generation.schema_version == verification.schema_version
    conn.execute(
        f"UPDATE civic_ai_stages SET {field} = ? WHERE candidate_id = ? AND stage = ?",
        (value, candidate.id, stage.value))
    conn.commit()

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert sealed.candidate_count == 0
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(conn, candidate.id).ai_refresh_reason == reason
    assert get_stage_receipt(conn, candidate.id, stage) is None


@pytest.mark.parametrize("missing", [False, True])
async def test_verification_receipt_must_match_a_recorded_supporting_judgment(tmp_path, missing):
    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    if missing:
        conn.execute("DELETE FROM civic_verifications WHERE candidate_id = ?", (candidate.id,))
    else:
        conn.execute(
            "UPDATE civic_verifications SET model = 'claude-sonnet-5' WHERE candidate_id = ?",
            (candidate.id,))
    conn.commit()

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert sealed.candidate_count == 0
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(
        conn, candidate.id).ai_refresh_reason == "publication_verification_changed"


async def test_later_verification_transport_error_does_not_retract_a_matching_judgment(tmp_path):
    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    verification = get_stage_receipt(conn, candidate.id, CivicAiStage.VERIFICATION)
    record_verification(
        conn, candidate.id, CivicVerificationVerdict.ERROR, verification.model, T0,
        error_code="transport_unavailable")

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert sealed.candidate_count == 1
    assert list_sealed_candidate_ids(conn, batch.id) == [candidate.id]


async def test_deadline_expiring_during_preflight_rolls_back_the_seal(tmp_path, monkeypatch):
    from beehive.db import civic_batches

    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    clock = MutableClock()
    preflight = civic_batches.preflight_candidate

    def elapsed_preflight(conn, candidate_id):
        item = preflight(conn, candidate_id)
        clock.now += timedelta(hours=1)
        return item

    monkeypatch.setattr(civic_batches, "preflight_candidate", elapsed_preflight)
    sealed = seal_batch_if_claimed(
        conn, batch.id, run.id, run.claim_token, T0, now_fn=clock)

    assert sealed is None
    assert get_civic_batch(conn, batch.id).status is CivicBatchStatus.BUILDING
    assert get_frozen_publication(conn, batch.id) is None
    assert get_candidate(conn, candidate.id).sealed_into_batch_at is None


async def test_identical_seal_replay_never_reruns_preflight(tmp_path, monkeypatch):
    conn, run, batch, _candidate = await _prepared_publication_batch(tmp_path)
    first = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    frozen = get_frozen_publication(conn, batch.id)

    def must_not_revalidate(*_args):
        raise AssertionError("a sealed publication must not re-enter candidate preflight")

    monkeypatch.setattr("beehive.db.civic_batches.preflight_candidate", must_not_revalidate)
    changes = conn.total_changes
    replay = seal_batch_if_claimed(
        conn, batch.id, run.id, run.claim_token, T0 + timedelta(seconds=1))

    assert replay == first
    assert get_frozen_publication(conn, batch.id) == frozen
    assert conn.total_changes == changes


async def test_sealed_candidate_cannot_start_a_new_ai_revision(tmp_path):
    conn, run, batch, candidate = await _prepared_publication_batch(tmp_path)
    seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    original = get_frozen_publication(conn, batch.id)

    assert refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=0, reason="publication_generation_changed", now=T0) is None
    with pytest.raises(sqlite3.IntegrityError, match="AI refresh requires"):
        conn.execute(
            "UPDATE civic_candidates SET ai_revision = 1, status = 'drafted', settled_at = NULL "
            "WHERE id = ?", (candidate.id,))
    conn.rollback()
    assert get_candidate(conn, candidate.id).ai_revision == 0
    assert get_frozen_publication(conn, batch.id) == original


async def test_legacy_identical_artifact_replays_without_current_policy(tmp_path, monkeypatch):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "spool")
    first = spool.write_batch(conn, batch_id, T0)
    original = first.path.read_bytes()
    original_batch = get_civic_batch(conn, batch_id)
    _remove_post_legacy_schema(conn)
    init_schema(conn)
    monkeypatch.setattr("beehive.civic.generation.is_citable", lambda *args, **kwargs: False)
    changes = conn.total_changes

    replay = spool.write_batch(conn, batch_id, T0)

    assert replay.replayed
    assert replay.path.read_bytes() == original
    assert conn.total_changes == changes
    assert get_civic_batch(conn, batch_id) == original_batch
    assert get_frozen_publication(conn, batch_id) is None


@pytest.mark.parametrize("legacy", [False, True])
async def test_ai_version_changes_never_revalidate_sealed_artifacts(
    tmp_path, monkeypatch, legacy,
):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "spool")
    original = get_frozen_publication(conn, batch_id)
    if legacy:
        spool.write_batch(conn, batch_id, T0)
        _remove_post_legacy_schema(conn)
        init_schema(conn)
    monkeypatch.setattr("beehive.civic.publication.CIVIC_PROMPT_VERSION", "future-prompts")
    monkeypatch.setattr("beehive.civic.publication.CIVIC_SCHEMA_VERSION", "future-schema")
    changes = conn.total_changes

    written = spool.write_batch(conn, batch_id, T0)

    assert written.path.read_bytes() == original
    assert written.replayed is legacy
    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.SEALED
    assert conn.total_changes == changes


@pytest.mark.parametrize("bound", [False, True])
async def test_legacy_without_artifact_is_not_rebuilt_or_quarantined(tmp_path, bound):
    conn, batch_id = await _completed_batch(tmp_path)
    _remove_post_legacy_schema(conn)
    if not bound:
        conn.execute("UPDATE civic_batches SET payload_hash = NULL WHERE id = ?", (batch_id,))
        conn.commit()
    original = get_civic_batch(conn, batch_id)
    init_schema(conn)
    changes = conn.total_changes

    with pytest.raises(CivicSpoolLegacyProjectionError):
        CivicSpool(tmp_path / "spool").write_batch(conn, batch_id, T0)

    assert get_civic_batch(conn, batch_id) == original
    assert get_frozen_publication(conn, batch_id) is None
    assert conn.total_changes == changes


@pytest.mark.parametrize("outcome", ["accepted", "rejected"])
async def test_legacy_settled_outcome_replay_keeps_original_bytes_and_receipt(
    tmp_path, monkeypatch, outcome,
):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "spool")
    first = spool.write_batch(conn, batch_id, T0)
    original = first.path.read_bytes()
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    (spool.outcomes / "outcome.json").write_text(
        json.dumps(_outcome(batch, outcome)), encoding="utf-8")
    assert spool.process_outcomes(conn, T0)[0].disposition.value == outcome
    delivered = get_civic_batch(conn, batch_id)
    receipt = get_receipt_for_batch(conn, batch_id)
    _remove_post_legacy_schema(conn)
    init_schema(conn)
    monkeypatch.setattr("beehive.civic.generation.is_citable", lambda *args, **kwargs: False)

    result = spool.process_outcomes(conn, T0)[0]

    assert result.disposition is CivicOutcomeDisposition.REPLAYED
    assert get_civic_batch(conn, batch_id) == delivered
    assert get_receipt_for_batch(conn, batch_id) == receipt
    assert (spool.processed / outcome / first.path.name).read_bytes() == original
    assert get_frozen_publication(conn, batch_id) is None


async def test_frozen_publication_cannot_be_updated_or_retrofitted(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be updated"):
        conn.execute(
            "UPDATE civic_batch_publications SET envelope_bytes = ? WHERE batch_id = ?",
            (b"{}", batch_id))
    conn.rollback()
    frozen = get_frozen_publication(conn, batch_id)
    _remove_post_legacy_schema(conn)
    init_schema(conn)

    with pytest.raises(sqlite3.IntegrityError, match="before sealing"):
        conn.execute(
            "INSERT INTO civic_batch_publications (batch_id, envelope_bytes, frozen_at) "
            "VALUES (?, ?, ?)", (batch_id, frozen, T0.isoformat()))
    conn.rollback()
    assert get_frozen_publication(conn, batch_id) is None


@pytest.mark.parametrize("corruption", ["missing", "bytes", "membership"])
async def test_frozen_artifact_corruption_never_falls_back_to_legacy_rebuild(
    tmp_path, corruption,
):
    conn, batch_id = await _completed_batch(tmp_path)
    if corruption == "missing":
        conn.execute("DELETE FROM civic_batch_publications WHERE batch_id = ?", (batch_id,))
    elif corruption == "bytes":
        conn.execute("DROP TRIGGER civic_batch_publications_no_update")
        conn.execute(
            "UPDATE civic_batch_publications SET envelope_bytes = ? WHERE batch_id = ?",
            (b"{", batch_id))
    else:
        conn.execute(
            "UPDATE civic_candidates SET statement = 'corrupted' WHERE batch_id = ?",
            (batch_id,))
    conn.commit()
    spool = CivicSpool(tmp_path / "spool")

    with pytest.raises(CivicSpoolIntegrityError):
        spool.write_batch(conn, batch_id, T0)

    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.QUARANTINED
    assert not spool.inbox.exists()


async def test_spool_events_correlate_write_replay_and_outcome(tmp_path):
    stream = io.StringIO()
    event_logger = CivicEventLogger(
        stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID
    )
    conn, batch_id = await _completed_batch(
        tmp_path, event_logger=event_logger
    )
    spool = CivicSpool(tmp_path / "civic-spool", event_logger=event_logger)
    first = spool.write_batch(conn, batch_id, T0)
    spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    (spool.outcomes / "accepted.json").write_text(
        json.dumps(_outcome(batch, "accepted")), encoding="utf-8"
    )
    result = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]

    assert result.disposition is CivicOutcomeDisposition.ACCEPTED
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert {event["correlation_id"] for event in events} == {CORRELATION_ID}
    names = [event["event"] for event in events]
    assert names.index("civic.run.finished") < names.index(
        "civic.spool.write.started"
    )
    write_events = [
        event for event in events if event["event"] == "civic.spool.write.finished"
    ]
    assert [event["status"] for event in write_events] == ["written", "replayed"]
    assert all(event["batch_id"] == batch_id for event in write_events)
    assert all(event["payload_hash"] == first.payload_hash for event in write_events)
    outcome = next(
        event
        for event in events
        if event["event"] == "civic.outcome.finished"
        and event["status"] == "accepted"
    )
    assert outcome["batch_id"] == batch_id
    assert outcome["run_id"] == batch.run_id
    assert outcome["idempotency_key"] == batch.idempotency_key
    archive_names = [
        event["event"] for event in events if event["event"].startswith("civic.spool.archive.")
    ]
    assert archive_names == [
        "civic.spool.archive.started",
        "civic.spool.archive.finished",
    ]


async def test_sealed_locales_are_not_rebuilt_from_changed_generation_receipts(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    frozen = get_frozen_publication(conn, batch_id)
    row = conn.execute(
        "SELECT id, output_json FROM civic_ai_stages "
        "WHERE stage = 'generate' AND status = 'succeeded'").fetchone()
    output = json.loads(row["output_json"])
    for locale in ("zh", "en", "hi", "tl"):
        output["entry"][locale]["uncertainty"] = True
    conn.execute(
        "UPDATE civic_ai_stages SET output_json = ? WHERE id = ?",
        (json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
         row["id"]))
    conn.commit()
    spool = CivicSpool(tmp_path / "civic-spool")

    written = spool.write_batch(conn, batch_id, T0)

    assert written.path.read_bytes() == frozen
    assert json.loads(frozen)["items"][0]["uncertainty"] is False
    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.SEALED
    assert get_receipt_for_batch(conn, batch_id) is None


async def test_sealed_citations_do_not_follow_later_evidence_changes(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    frozen = get_frozen_publication(conn, batch_id)
    conn.execute(
        "UPDATE civic_candidate_evidence SET canonical_url = replace(canonical_url, "
        "'https://', 'http://'), url = replace(url, 'https://', 'http://')")
    conn.commit()
    spool = CivicSpool(tmp_path / "civic-spool")

    written = spool.write_batch(conn, batch_id, T0)

    assert written.path.read_bytes() == frozen
    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.SEALED


@pytest.mark.parametrize("replacement", [b"{", b'{"changed":true}'])
async def test_torn_or_changed_existing_file_quarantines(replacement, tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    written = spool.write_batch(conn, batch_id, T0)
    written.path.write_bytes(replacement)

    with pytest.raises(CivicSpoolIntegrityError, match="differs"):
        spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    assert batch.status is CivicBatchStatus.QUARANTINED
    assert batch.error_code == "spool_integrity_mismatch"
    assert get_receipt_for_batch(conn, batch_id) is None


async def test_no_outcome_leaves_sealed_batch_and_attempt_budget_untouched(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    stream = io.StringIO()
    spool = CivicSpool(
        tmp_path / "civic-spool",
        event_logger=CivicEventLogger(
            stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID
        ),
    )
    written = spool.write_batch(conn, batch_id, T0)

    assert spool.process_outcomes(conn, T0, now_fn=lambda: T0) == ()
    batch = get_civic_batch(conn, batch_id)
    assert batch.status is CivicBatchStatus.SEALED
    assert batch.attempt_count == 0
    assert get_receipt_for_batch(conn, batch_id) is None
    assert written.path.exists()
    assert not spool.processed.exists()
    pending = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event":"civic.outcome.pending"' in line
    ]
    assert pending == [
        {
            "batch_id": batch_id,
            "correlation_id": CORRELATION_ID,
            "event": "civic.outcome.pending",
            "idempotency_key": batch.idempotency_key,
            "payload_hash": batch.payload_hash,
            "phase": "outcome",
            "run_id": batch.run_id,
            "schema_version": "partymap.civic.event.v1",
            "status": "pending",
            "timestamp": "2026-07-31T18:30:00.000Z",
            "watch_id": batch.watch_id,
        }
    ]


async def _unsettled_backlog(tmp_path, count: int, *, stream=None):
    conn = connect(str(tmp_path / "backlog.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    clock = MutableClock()
    batch_ids = await seal_empty_daily_batches(conn, count, clock)
    logger = (
        CivicEventLogger(stream, now_fn=clock, correlation_id=CORRELATION_ID)
        if stream is not None else None
    )
    spool = CivicSpool(tmp_path / "civic-spool", shadow=True, event_logger=logger)
    return conn, clock, spool, batch_ids


def _inbox_names(conn, spool, batch_ids) -> tuple[set[str], set[str]]:
    expected = {
        f"{get_civic_batch(conn, batch_id).idempotency_key}.json" for batch_id in batch_ids
    }
    return {path.name for path in spool.inbox.glob("*.json")}, expected


async def test_unsettled_prefix_never_starves_newer_sealed_batches(tmp_path):
    conn, clock, spool, older = await _unsettled_backlog(tmp_path, 20)
    spool.emit_sealed_batches(conn, clock())
    clock.now += timedelta(days=1)
    newer = await seal_empty_daily_batches(conn, 3, clock)

    assert spool.plan_projection(conn, clock()).unprojected_batch_ids == tuple(newer)
    for _ in range(3):
        clock.now += timedelta(minutes=5)
        spool.emit_sealed_batches(conn, clock())

    actual, expected = _inbox_names(conn, spool, older + newer)
    assert actual == expected
    assert spool.plan_projection(conn, clock()).unprojected_batch_ids == ()


async def test_projection_writes_all_missing_first_then_bounds_verification(tmp_path):
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 23)

    first = spool.emit_sealed_batches(conn, clock())
    clock.now += timedelta(minutes=5)
    second = spool.emit_sealed_batches(conn, clock())

    assert [result.batch_id for result in first] == batch_ids
    assert not any(result.replayed for result in first)
    assert len(second) == 20
    assert all(result.replayed for result in second)
    assert {result.batch_id for result in second} < set(batch_ids)
    for batch_id in batch_ids:
        batch = get_civic_batch(conn, batch_id)
        assert batch.status is CivicBatchStatus.SEALED
        assert batch.attempt_count == 0
        assert get_receipt_for_batch(conn, batch_id) is None


async def test_projection_verification_cursor_survives_restart(tmp_path):
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 23)
    spool.emit_sealed_batches(conn, clock())
    verified = spool.emit_sealed_batches(conn, clock())
    before = spool.plan_projection(conn, clock())
    conn.close()

    conn = connect(str(tmp_path / "backlog.db"))
    init_schema(conn)
    after = CivicSpool(tmp_path / "civic-spool", shadow=True).plan_projection(conn, clock())

    assert [result.batch_id for result in verified] == batch_ids[:20]
    assert after == before
    assert after.unprojected_batch_ids == ()
    assert after.deliverable_count == len(batch_ids)
    assert after.verification_batch_ids == tuple(batch_ids[20:] + batch_ids[:17])


async def test_verification_covers_every_file_whatever_the_pass_cadence(tmp_path):
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 45)
    spool.emit_sealed_batches(conn, clock())

    verified = set()
    for _ in range(3):
        results = spool.emit_sealed_batches(conn, clock())
        assert len(results) <= 20
        assert all(result.replayed for result in results)
        verified |= {result.batch_id for result in results}

    assert verified == set(batch_ids)


async def test_one_failing_batch_never_blocks_the_rest_of_the_pass(tmp_path, monkeypatch):
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 5)
    stuck = batch_ids[0]
    frozen = spool_module._frozen_publication_bytes

    def legacy_for_stuck(conn, batch_id):
        if batch_id == stuck:
            raise CivicSpoolLegacyProjectionError("simulated legacy Batch without artifact")
        return frozen(conn, batch_id)

    monkeypatch.setattr(spool_module, "_frozen_publication_bytes", legacy_for_stuck)

    with pytest.raises(CivicSpoolLegacyProjectionError, match="no legacy artifact"):
        spool.emit_sealed_batches(conn, clock())

    actual, expected = _inbox_names(conn, spool, batch_ids[1:])
    assert actual == expected
    assert get_civic_batch(conn, stuck).status is CivicBatchStatus.SEALED
    assert spool.plan_projection(conn, clock()).unprojected_batch_ids == (stuck,)


async def test_missing_file_budget_rotates_past_permanent_failures(tmp_path, monkeypatch):
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 6)
    stuck = set(batch_ids[:3])
    frozen = spool_module._frozen_publication_bytes

    def legacy_for_stuck(conn, batch_id):
        if batch_id in stuck:
            raise CivicSpoolLegacyProjectionError("simulated legacy Batch without artifact")
        return frozen(conn, batch_id)

    monkeypatch.setattr(spool_module, "_frozen_publication_bytes", legacy_for_stuck)

    attempted = []
    for _ in range(3):
        projection = spool.project_deliverable_batches(conn, clock(), write_limit=2)
        attempted.append(
            sorted(
                [write.batch_id for write in projection.writes if not write.replayed]
                + [failure.batch_id for failure in projection.failures]
            )
        )

    assert attempted == [batch_ids[0:2], batch_ids[2:4], batch_ids[4:6]]
    actual, expected = _inbox_names(conn, spool, batch_ids[3:])
    assert actual == expected


async def test_stale_plan_never_moves_a_cursor_backwards(tmp_path):
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 45)
    spool.emit_sealed_batches(conn, clock())
    stale = spool.plan_projection(conn, clock())

    other = connect(str(tmp_path / "backlog.db"))
    concurrent = CivicSpool(tmp_path / "civic-spool", shadow=True)
    concurrent.project_deliverable_batches(other, clock())
    concurrent.project_deliverable_batches(other, clock())
    spool.execute_projection(conn, stale, clock())

    assert stale.verification_batch_ids == tuple(batch_ids[:20])
    assert app_state.get(conn, spool_module.PROJECTION_VERIFY_CURSOR_KEY) == str(batch_ids[39])
    assert spool.plan_projection(conn, clock()).verification_batch_ids == tuple(
        batch_ids[40:] + batch_ids[:15])


async def test_outcome_scan_counts_every_unsettled_batch_with_bounded_detail(tmp_path):
    stream = io.StringIO()
    conn, clock, spool, batch_ids = await _unsettled_backlog(tmp_path, 23, stream=stream)
    spool.emit_sealed_batches(conn, clock())

    assert spool.process_outcomes(conn, clock(), now_fn=clock) == ()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [
        event["batch_id"] for event in events if event["event"] == "civic.outcome.pending"
    ] == batch_ids[-20:]
    finished = [
        event for event in events if event["event"] == "civic.outcome.scan.finished"
    ]
    assert [event["deferred_count"] for event in finished] == [len(batch_ids)]


async def test_accepted_outcome_archives_and_replays_without_active_inbox(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    outcome_path = spool.outcomes / "accepted.json"
    outcome_path.write_text(
        json.dumps(_outcome(batch, "accepted")), encoding="utf-8")

    first = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    second = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    delivered = get_civic_batch(conn, batch_id)
    receipt = get_receipt_for_batch(conn, batch_id)
    archived = spool.processed_accepted / f"{batch.idempotency_key}.json"
    assert first.disposition is CivicOutcomeDisposition.ACCEPTED
    assert second.disposition is CivicOutcomeDisposition.REPLAYED
    assert delivered.status is CivicBatchStatus.DELIVERED
    assert delivered.attempt_count == 1
    assert receipt.content_hash == delivered.content_hash
    assert receipt.payload_hash == delivered.payload_hash
    assert receipt.provider_reference == "proposal:test"
    assert archived.exists()
    assert not (spool.inbox / archived.name).exists()
    assert outcome_path.exists()


async def test_nonempty_accepted_outcome_without_proposal_is_quarantined(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    (spool.outcomes / "local-apply.json").write_text(
        json.dumps(_outcome(batch, "accepted", proposal=None)),
        encoding="utf-8",
    )

    first = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    replay = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    quarantined = get_civic_batch(conn, batch_id)
    archived = spool.processed_rejected / f"{batch.idempotency_key}.json"
    assert first.disposition is CivicOutcomeDisposition.REJECTED
    assert replay.disposition is CivicOutcomeDisposition.REPLAYED
    assert quarantined.status is CivicBatchStatus.QUARANTINED
    assert quarantined.error_code == "destination_validation_failed"
    assert quarantined.attempt_count == 0
    assert get_receipt_for_batch(conn, batch_id) is None
    assert archived.exists()
    assert not (spool.inbox / archived.name).exists()


async def test_empty_accepted_outcome_without_proposal_is_delivered(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path, empty=True)
    spool = CivicSpool(tmp_path / "civic-spool")
    written = spool.write_batch(conn, batch_id, T0)
    assert json.loads(written.path.read_text(encoding="utf-8"))["items"] == []
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    (spool.outcomes / "empty.json").write_text(
        json.dumps(_outcome(batch, "accepted", proposal=None)),
        encoding="utf-8",
    )

    result = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    delivered = get_civic_batch(conn, batch_id)
    archived = spool.processed_accepted / f"{batch.idempotency_key}.json"
    assert result.disposition is CivicOutcomeDisposition.ACCEPTED
    assert delivered.status is CivicBatchStatus.DELIVERED
    assert delivered.attempt_count == 1
    assert get_receipt_for_batch(conn, batch_id).provider_reference == ""
    assert archived.exists()
    assert not (spool.inbox / archived.name).exists()


async def test_archive_failure_after_receipt_retries_on_next_scan(tmp_path, monkeypatch):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    written = spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    outcome_path = spool.outcomes / "accepted.json"
    outcome_path.write_text(
        json.dumps(_outcome(batch, "accepted")), encoding="utf-8")

    from beehive.civic import spool as spool_module

    replace = spool_module.os.replace

    def fail_archive(_source, _destination):
        raise OSError("simulated archive failure")

    monkeypatch.setattr(spool_module.os, "replace", fail_archive)
    failed = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    assert failed.disposition is CivicOutcomeDisposition.ERROR
    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.DELIVERED
    assert get_receipt_for_batch(conn, batch_id) is not None
    assert written.path.exists()

    monkeypatch.setattr(spool_module.os, "replace", replace)
    replay = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    archived = spool.processed_accepted / written.path.name
    assert replay.disposition is CivicOutcomeDisposition.REPLAYED
    assert archived.exists()
    assert not written.path.exists()
    assert outcome_path.exists()


async def test_rejected_outcome_archives_and_replays_without_active_inbox(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    outcome_path = spool.outcomes / "rejected.json"
    outcome_path.write_text(
        json.dumps(_outcome(batch, "rejected")), encoding="utf-8")

    result = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    replay = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    rejected = get_civic_batch(conn, batch_id)
    archived = spool.processed_rejected / f"{batch.idempotency_key}.json"
    assert result.disposition is CivicOutcomeDisposition.REJECTED
    assert replay.disposition is CivicOutcomeDisposition.REPLAYED
    assert rejected.status is CivicBatchStatus.QUARANTINED
    assert rejected.error_code == "destination_validation_failed"
    assert rejected.attempt_count == 0
    assert get_receipt_for_batch(conn, batch_id) is None
    assert archived.exists()
    assert not (spool.inbox / archived.name).exists()
    assert outcome_path.exists()


async def test_payload_mismatch_quarantines_and_shadow_acceptance_does_not_consume(tmp_path):
    conn, batch_id = await _completed_batch(tmp_path)
    spool = CivicSpool(tmp_path / "civic-spool")
    spool.write_batch(conn, batch_id, T0)
    batch = get_civic_batch(conn, batch_id)
    spool.outcomes.mkdir(parents=True)
    outcome_path = spool.outcomes / "outcome.json"
    outcome_path.write_text(
        json.dumps(_outcome(batch, "accepted", payload_hash="0" * 64)),
        encoding="utf-8",
    )
    result = spool.process_outcomes(conn, T0, now_fn=lambda: T0)[0]
    assert result.disposition is CivicOutcomeDisposition.ERROR
    assert get_civic_batch(conn, batch_id).status is CivicBatchStatus.QUARANTINED

    shadow_dir = tmp_path / "shadow"
    shadow_conn, shadow_batch_id = await _completed_batch(shadow_dir)
    shadow = CivicSpool(shadow_dir / "civic-spool", shadow=True)
    shadow.write_batch(shadow_conn, shadow_batch_id, T0)
    shadow_batch = get_civic_batch(shadow_conn, shadow_batch_id)
    shadow.outcomes.mkdir(parents=True)
    (shadow.outcomes / "accepted.json").write_text(
        json.dumps(_outcome(shadow_batch, "accepted", proposal=None)), encoding="utf-8")
    validated = shadow.process_outcomes(
        shadow_conn, T0, now_fn=lambda: T0)[0]
    assert validated.disposition is CivicOutcomeDisposition.VALIDATED_SHADOW
    assert get_civic_batch(shadow_conn, shadow_batch_id).status is CivicBatchStatus.SEALED
    assert get_civic_batch(shadow_conn, shadow_batch_id).attempt_count == 0
    assert get_receipt_for_batch(shadow_conn, shadow_batch_id) is None
    assert (shadow.inbox / f"{shadow_batch.idempotency_key}.json").exists()
    assert not shadow.processed.exists()
