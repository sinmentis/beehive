"""AI budget deferral and carryover: running out of calls must never destroy a Candidate.

A Civic Run has a fixed budget of 30 AI calls (plan 7.4) and a fixed cap of six or ten published
Candidates (plan 5.2). Two calls per Candidate fits both, but a Run can still run out: a slot
crowded with transient failures, a Candidate adopted late, an operator narrowing the budget. What
must never happen is that the Candidate is destroyed for it -- rejecting it would consume its
source items forever (`civic_consumed_items` is never released), throw away a qualification the
evidence engine already made, and discard the AI stage receipts it had already paid for.

These tests pin the alternative: DEFERRED_AI, an explicit resume state with its own carryover
reason, adopted by a later same-Watch Run back into VERIFYING and resumed at the first stage with
no receipt -- with a bounded lifetime so it cannot circle between Runs forever.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.ai_gateway import CivicAiStage, CivicModelError
from beehive.civic.ai_pipeline import (generate_civic_candidate, next_incomplete_stage,
                                       require_publishable_localization,
                                       stored_locale_contents)
from beehive.civic.documents import CivicDocument
from beehive.civic.generation import CIVIC_PROMPT_VERSION
from beehive.civic.evidence import (CivicCandidateClaim, CivicClaimCategory, CivicContentClass,
                                    CivicOwnershipRegistry, CivicPublisher, CivicPublisherKind,
                                    CivicRecordKind, CivicRecordOperation, CivicSourceKind)
from beehive.civic.limits import REQUIRED_LOCALES
from beehive.civic.pipeline import ingest_civic_documents, qualify_civic_candidate
from beehive.db.civic_ai_stages import (count_run_ai_calls, get_stage_receipt,
                                        list_candidate_stages)
from beehive.db.civic_batches import (MAX_AI_DEFERRALS_PER_CANDIDATE, count_ai_deferrals,
                                      count_pending_ai_deferrals, defer_candidate_for_ai,
                                      get_or_create_batch_if_claimed,
                                      list_candidate_capacity_deferrals,
                                      list_sealed_candidate_ids, seal_batch_if_claimed,
                                      refresh_candidate_for_ai_if_claimed)
from beehive.db.civic_candidates import (add_candidate_evidence, get_candidate,
                                         list_candidate_evidence)
from beehive.db.civic_consumed_items import list_candidate_items
from beehive.db.civic_qualifications import get_latest_qualification, list_qualifications
from beehive.db.civic_runs import (claim_civic_run, complete_civic_run, enqueue_civic_run,
                                   get_civic_run)
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier
from beehive.domain.civic import CivicRunStatus, CivicVerificationVerdict
from beehive.db.civic_verifications import list_verifications
from tests.civic.civic_ai_fakes import FakeCivicGateway, passing_gateway

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
EVENT_DATE = T0.date()

REGISTRY = CivicOwnershipRegistry.build([
    CivicPublisher(publisher_id="parliament", name="Parliament",
                   kind=CivicPublisherKind.OFFICIAL, source_kind=CivicSourceKind.PARLIAMENT),
])


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def make_run(conn, *, watch_id: int | None = None, slot: datetime | None = None,
             name: str = "NZ civic"):
    slot = slot or T0
    watch_id = watch_id or create_civic_watch(conn, name, "What changed?", T0).id
    enqueued = enqueue_civic_run(conn, watch_id, slot, slot)
    lease = claim_civic_run(conn, enqueued.id, slot, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, enqueued.id, lease.run.claim_token, slot)
    return type("Ctx", (), {
        "watch_id": watch_id, "id": enqueued.id, "token": lease.run.claim_token,
        "batch": batch, "slot": slot})()


def _document(key: str = "a") -> CivicDocument:
    return CivicDocument(
        source_key="nz_parliament", external_key=key,
        canonical_url=f"https://parliament.test/{key}",
        title="Housing bill passes second reading", tier=CivicEvidenceTier.OFFICIAL,
        publisher="Parliament", first_seen_at=T0, published_at=T0,
        excerpt="The record shows the bill passed its second reading on 2026-08-01.")


def qualified_candidate(conn, run):
    ingested = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0, documents=[_document()])
    candidate = ingested.created_candidates[0]
    add_candidate_evidence(
        conn, candidate.id, "official_feed", "src-1", "https://parliament.test/a",
        "Housing bill second reading", CivicEvidenceTier.OFFICIAL,
        CivicEvidenceStance.SUPPORTING, T0, publisher="Parliament",
        publisher_id="parliament", source_kind=CivicSourceKind.PARLIAMENT.value,
        subject_id="green-party", claim_category=CivicClaimCategory.LEGISLATION.value,
        event_date=EVENT_DATE, canonical_url="https://parliament.test/a",
        excerpt="The record shows the bill passed its second reading on 2026-08-01.",
        accessible_full_text=True)
    requalify_existing_candidate(conn, run, candidate)
    return candidate


def requalify_existing_candidate(conn, run, candidate):
    claim = CivicCandidateClaim(
        candidate_id=str(candidate.id), fingerprint=candidate.dedupe_key,
        record_kind=CivicRecordKind.TIMELINE, operation=CivicRecordOperation.ADD,
        subject_id="green-party", record_key=candidate.dedupe_key, event_date=EVENT_DATE,
        category=CivicClaimCategory.LEGISLATION, content_class=CivicContentClass.FACTUAL,
        durable=True, claim_text="The bill passed its second reading.")
    qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, claim=claim, registry=REGISTRY, now=run.slot)


def generate(conn, run, candidate_id, gateway, *, now=T0, **kwargs):
    return asyncio.run(generate_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate_id,
        gateway=gateway, now=now, **kwargs))


def next_run(conn, run, *, days: int = 1):
    """The next day's Run for the same Watch, claimed and with a BUILDING Batch."""
    complete_civic_run(
        conn, run.id, run.token, CivicRunStatus.COMPLETED, run.slot + timedelta(minutes=5))
    return make_run(conn, watch_id=run.watch_id, slot=run.slot + timedelta(days=days))


# ============================================================================
# Deferral instead of rejection
# ============================================================================


def test_budget_exhaustion_before_generation_defers_rather_than_rejects(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()

    result = generate(conn, run, candidate.id, gateway, call_budget=0)

    assert not result.verified
    assert result.rejection_code is None, "a spent budget is not a judgment about the statement"
    assert result.deferral_reason == "ai_budget_exhausted"
    assert result.candidate.status is CivicCandidateStatus.DEFERRED_AI
    assert gateway.calls == []
    assert next_incomplete_stage(conn, candidate.id) is CivicAiStage.GENERATE


def test_budget_exhaustion_after_generation_keeps_the_generated_entry(conn):
    """The expensive half is already paid for and recorded: deferring must not throw it away."""
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()

    result = generate(conn, run, candidate.id, gateway, call_budget=1)

    assert result.deferral_reason == "ai_budget_exhausted"
    assert gateway.stages == [CivicAiStage.GENERATE]
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE).succeeded
    assert set(stored_locale_contents(conn, candidate.id)) == set(REQUIRED_LOCALES)
    assert next_incomplete_stage(conn, candidate.id) is CivicAiStage.VERIFICATION
    assert list_verifications(conn, candidate.id) == []


def test_a_deferred_candidate_keeps_everything_it_earned(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)

    assert len(list_candidate_evidence(conn, candidate.id)) == 1
    assert get_latest_qualification(conn, candidate.id).is_publishable
    assert list_candidate_items(conn, candidate.id), "consumed items must stay claimed"
    assert [entry.stage for entry in list_candidate_stages(conn, candidate.id)] == [
        CivicAiStage.GENERATE]


def test_a_deferral_is_recorded_as_its_own_reason(conn):
    """Audit history: an AI deferral is neither a batch-cap decision nor an evidence finding, and
    the row says so."""
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=0)

    deferrals = list_candidate_capacity_deferrals(conn, candidate.id)
    assert [entry.reason for entry in deferrals] == ["ai_budget_exhausted"]
    assert deferrals[0].adopted_at is None
    assert deferrals[0].from_run_id == run.id
    assert count_ai_deferrals(conn, candidate.id) == 1
    assert count_pending_ai_deferrals(conn, run.watch_id) == 1


def test_a_transient_failure_that_outlives_its_retry_defers_with_its_own_reason(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.VERIFICATION: CivicModelError("model_timeout", "gone")})

    result = generate(conn, run, candidate.id, gateway)

    assert result.deferral_reason == "ai_transient_failed"
    assert [entry.reason for entry in list_candidate_capacity_deferrals(conn, candidate.id)] == [
        "ai_transient_failed"]


def test_next_slot_gets_bounded_real_verifier_retries_not_the_same_exhausted_cache(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    result = generate(
        conn, run, candidate.id,
        passing_gateway(**{CivicAiStage.VERIFICATION: CivicModelError("model_timeout", "offline")}))
    assert result.deferral_reason == "ai_transient_failed"
    assert count_run_ai_calls(conn, run.id) == 3
    generation = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)

    later = next_run(conn, run)
    ingest_civic_documents(
        conn, run_id=later.id, claim_token=later.token, now=later.slot, documents=[])
    gateway = passing_gateway()
    del gateway.responses[CivicAiStage.GENERATE]
    resumed = generate(conn, later, candidate.id, gateway, now=later.slot)

    assert resumed.verified
    assert gateway.stages == [CivicAiStage.VERIFICATION]
    assert count_run_ai_calls(conn, later.id) == 1
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) == generation


def test_a_deferred_candidate_does_not_block_the_seal(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=0)

    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert sealed is not None
    assert list_sealed_candidate_ids(conn, sealed.id) == []
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI


# ============================================================================
# Resume on a later Run
# ============================================================================


def test_the_next_empty_run_adopts_and_resumes_only_the_missing_stage(conn):
    """The whole point: an empty news day picks the Candidate back up, calls only the verifier,
    and publishes it."""
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))

    tomorrow = next_run(conn, run)
    adopted = ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])

    assert adopted.adopted_candidate_ids == (candidate.id,)
    resumed = get_candidate(conn, candidate.id)
    assert resumed.status is CivicCandidateStatus.VERIFYING
    assert resumed.run_id == tomorrow.id
    assert resumed.settled_at is None

    gateway = passing_gateway()
    del gateway.responses[CivicAiStage.GENERATE]
    result = generate(conn, tomorrow, candidate.id, gateway, now=tomorrow.slot)

    assert result.verified
    assert gateway.stages == [CivicAiStage.VERIFICATION]
    assert result.replayed_stages == (CivicAiStage.GENERATE,)
    assert count_run_ai_calls(conn, tomorrow.id) == 1


def test_a_resumed_candidate_records_no_second_stage_success(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)
    tomorrow = next_run(conn, run)
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])
    generate(conn, tomorrow, candidate.id, passing_gateway(), now=tomorrow.slot)

    receipts = list_candidate_stages(conn, candidate.id)
    successes = [entry for entry in receipts if entry.succeeded]
    assert [entry.stage for entry in successes] == [
        CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    assert len({entry.stage for entry in successes}) == len(successes)


def test_a_resumed_candidate_eventually_seals(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)
    seal_batch_if_claimed(conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))

    tomorrow = next_run(conn, run)
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])
    generate(conn, tomorrow, candidate.id, passing_gateway(), now=tomorrow.slot)

    sealed = seal_batch_if_claimed(
        conn, tomorrow.batch.id, tomorrow.id, tomorrow.token,
        tomorrow.slot + timedelta(minutes=1))
    assert list_sealed_candidate_ids(conn, sealed.id) == [candidate.id]
    assert require_publishable_localization(conn, candidate.id)
    assert [entry.verdict for entry in list_verifications(conn, candidate.id)] == [
        CivicVerificationVerdict.SUPPORTED]
    deferral = list_candidate_capacity_deferrals(conn, candidate.id)[0]
    assert deferral.adopted_at is not None
    assert deferral.to_run_id == tomorrow.id


def test_stale_cached_generation_is_replaced_then_budget_resumes_only_new_verification(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)
    conn.execute(
        "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/2' "
        "WHERE candidate_id = ?", (candidate.id,))
    conn.commit()
    original = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)
    original_qualification = get_latest_qualification(conn, candidate.id)
    original_items = list_candidate_items(conn, candidate.id)

    tomorrow = next_run(conn, run)
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])
    gateway = passing_gateway()
    stale = generate(conn, tomorrow, candidate.id, gateway, now=tomorrow.slot)
    assert stale.deferral_reason == "ai_provenance_stale"
    assert gateway.calls == []
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) is None
    assert get_latest_qualification(conn, candidate.id) is None

    refreshed_run = next_run(conn, tomorrow)
    ingest_civic_documents(
        conn, run_id=refreshed_run.id, claim_token=refreshed_run.token,
        now=refreshed_run.slot, documents=[])
    refreshed = get_candidate(conn, candidate.id)
    assert refreshed.status is CivicCandidateStatus.DRAFTED
    assert refreshed.ai_revision == 1
    requalify_existing_candidate(conn, refreshed_run, refreshed)
    generated = generate(
        conn, refreshed_run, candidate.id, passing_gateway(),
        now=refreshed_run.slot, call_budget=1)
    assert generated.deferral_reason == "ai_budget_exhausted"
    assert count_run_ai_calls(conn, refreshed_run.id) == 1
    current_generation = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)
    assert current_generation.id != original.id
    assert current_generation.ai_revision == 1
    assert current_generation.prompt_version == CIVIC_PROMPT_VERSION

    final_run = next_run(conn, refreshed_run)
    ingest_civic_documents(
        conn, run_id=final_run.id, claim_token=final_run.token,
        now=final_run.slot, documents=[])
    final_gateway = passing_gateway()
    del final_gateway.responses[CivicAiStage.GENERATE]
    final = generate(conn, final_run, candidate.id, final_gateway, now=final_run.slot)
    assert final.verified
    assert final_gateway.stages == [CivicAiStage.VERIFICATION]
    assert count_run_ai_calls(conn, final_run.id) == 1
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) == current_generation
    assert original in list_candidate_stages(conn, candidate.id)
    assert original_qualification in list_qualifications(conn, candidate.id)
    assert list_candidate_items(conn, candidate.id) == original_items


def test_a_second_watch_never_adopts_another_watchs_carryover(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=0)

    other = make_run(conn, name="Other watch")
    adopted = ingest_civic_documents(
        conn, run_id=other.id, claim_token=other.token, now=T0, documents=[])

    assert adopted.adopted_candidate_ids == ()
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert count_pending_ai_deferrals(conn, other.watch_id) == 0
    assert count_pending_ai_deferrals(conn, run.watch_id) == 1


# ============================================================================
# Repetition, bounds and terminal Runs
# ============================================================================


def test_a_candidate_can_be_deferred_more_than_once(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=0)

    tomorrow = next_run(conn, run)
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])
    result = generate(
        conn, tomorrow, candidate.id, passing_gateway(), now=tomorrow.slot, call_budget=0)

    assert result.deferral_reason == "ai_budget_exhausted"
    assert result.deferral_count == 2
    assert count_ai_deferrals(conn, candidate.id) == 2
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert len(list_candidate_capacity_deferrals(conn, candidate.id)) == 2


def test_the_carryover_has_a_bounded_lifetime(conn):
    """Documented and enforced: a Candidate that keeps being deferred is eventually settled
    rather than circling between Runs forever."""
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=0)

    current = run
    for _ in range(MAX_AI_DEFERRALS_PER_CANDIDATE - 1):
        current = next_run(conn, current)
        ingest_civic_documents(
            conn, run_id=current.id, claim_token=current.token, now=current.slot, documents=[])
        result = generate(
            conn, current, candidate.id, passing_gateway(), now=current.slot, call_budget=0)
        assert result.deferral_reason == "ai_budget_exhausted"

    assert count_ai_deferrals(conn, candidate.id) == MAX_AI_DEFERRALS_PER_CANDIDATE
    final = next_run(conn, current)
    ingest_civic_documents(
        conn, run_id=final.id, claim_token=final.token, now=final.slot, documents=[])
    exhausted = generate(
        conn, final, candidate.id, passing_gateway(), now=final.slot, call_budget=0)

    assert exhausted.deferral_reason is None
    assert exhausted.rejection_code == "ai_budget_exhausted_lifetime_exhausted"
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.REJECTED


def test_a_deferred_candidate_survives_a_terminal_run(conn):
    """A Run that fails after deferring must not turn the carryover into a rejection."""
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)

    complete_civic_run(
        conn, run.id, run.token, CivicRunStatus.FAILED, T0 + timedelta(minutes=2),
        error_code="worker_crashed")

    assert get_civic_run(conn, run.id).status is CivicRunStatus.FAILED
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE).succeeded

    tomorrow = make_run(conn, watch_id=run.watch_id, slot=run.slot + timedelta(days=1))
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])
    result = generate(conn, tomorrow, candidate.id, passing_gateway(), now=tomorrow.slot)
    assert result.verified


def test_interrupted_refresh_survives_a_terminal_run_without_discarding_the_candidate(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway())
    original = list_candidate_stages(conn, candidate.id)
    refreshed = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        expected_revision=0, reason="publication_stage_version_changed", now=T0)
    assert refreshed.status is CivicCandidateStatus.DRAFTED

    complete_civic_run(
        conn, run.id, run.token, CivicRunStatus.FAILED, T0 + timedelta(minutes=1),
        error_code="worker_interrupted")
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert get_candidate(conn, candidate.id).rejection_code is None

    later = make_run(conn, watch_id=run.watch_id, slot=T0 + timedelta(days=1))
    ingest_civic_documents(
        conn, run_id=later.id, claim_token=later.token, now=later.slot, documents=[])
    requalify_existing_candidate(conn, later, get_candidate(conn, candidate.id))
    result = generate(conn, later, candidate.id, passing_gateway(), now=later.slot)
    assert result.verified
    assert all(receipt in list_candidate_stages(conn, candidate.id) for receipt in original)


def test_refresh_revisions_are_bounded_and_duplicate_requests_are_idempotent(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway())
    original = list_candidate_stages(conn, candidate.id)
    for revision in range(MAX_AI_DEFERRALS_PER_CANDIDATE):
        refreshed = refresh_candidate_for_ai_if_claimed(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
            expected_revision=revision, reason="publication_generation_changed", now=T0)
        repeated = refresh_candidate_for_ai_if_claimed(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
            expected_revision=revision, reason="publication_generation_changed", now=T0)
        assert repeated == refreshed
        assert refreshed.ai_revision == revision + 1
        requalify_existing_candidate(conn, run, refreshed)
    exhausted = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        expected_revision=MAX_AI_DEFERRALS_PER_CANDIDATE,
        reason="publication_generation_changed", now=T0)
    assert exhausted.status is CivicCandidateStatus.REJECTED
    assert exhausted.rejection_code == "ai_refresh_lifetime_exhausted"
    assert list_candidate_stages(conn, candidate.id) == original
    assert count_run_ai_calls(conn, run.id) == 2


# ============================================================================
# Concurrency and idempotency
# ============================================================================


def test_deferring_twice_in_one_slot_writes_one_row(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)

    first = defer_candidate_for_ai(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        reason="ai_budget_exhausted", now=T0)
    second = defer_candidate_for_ai(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        reason="ai_budget_exhausted", now=T0)

    assert first.deferred is True
    assert second.deferred is False
    assert count_ai_deferrals(conn, candidate.id) == 1


def test_a_deferral_needs_the_run_claim(conn, tmp_path):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)

    assert defer_candidate_for_ai(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token="not-the-token",
        reason="ai_budget_exhausted", now=T0) is None
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFYING
    assert count_ai_deferrals(conn, candidate.id) == 0


def test_two_workers_racing_to_defer_produce_one_carryover(conn, tmp_path):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    other = connect(str(tmp_path / "test.db"))

    first = defer_candidate_for_ai(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        reason="ai_budget_exhausted", now=T0)
    second = defer_candidate_for_ai(
        other, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        reason="ai_transient_failed", now=T0)

    assert first.deferred is True
    assert second.deferred is False
    assert [entry.reason for entry in list_candidate_capacity_deferrals(conn, candidate.id)] == [
        "ai_budget_exhausted"]


def test_resuming_the_same_candidate_twice_is_idempotent(conn):
    run = make_run(conn)
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(), call_budget=1)
    tomorrow = next_run(conn, run)
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])

    first = generate(conn, tomorrow, candidate.id, passing_gateway(), now=tomorrow.slot)
    assert first.verified

    replayed: FakeCivicGateway = passing_gateway()
    replayed.responses.clear()
    ingest_civic_documents(
        conn, run_id=tomorrow.id, claim_token=tomorrow.token, now=tomorrow.slot, documents=[])
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFIED
    assert replayed.calls == []
