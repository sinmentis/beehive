"""The Civic AI pipeline against a real database: budget, retry, crash, replay, drift, settlement.

`tests/civic/test_civic_ai_contracts.py` proves the contracts refuse bad content. What has to be
proved here is that the durable half holds, and that the two-call design really does fit the
plan's fixed budget: six normal Candidates and ten election Candidates must both complete inside
30 AI calls, including when every stage spends its one bounded transient retry.

Every model call is a scripted `FakeCivicGateway`; nothing here touches a network.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.civic.ai_gateway import CivicAiStage, CivicModelError
from beehive.civic.ai_pipeline import (AI_CALLS_PER_CANDIDATE, CivicAiPipelineError,
                                       CivicClaimLostError, generate_civic_candidate,
                                       next_incomplete_stage,
                                       require_publishable_localization,
                                       stored_locale_contents)
from beehive.civic.batch_policy import CivicRunMode, published_cap
from beehive.civic.candidate_policy import CivicContentError
from beehive.civic.documents import CivicDocument
from beehive.civic.evidence import (CivicCandidateClaim, CivicClaimCategory, CivicContentClass,
                                    CivicOwnershipRegistry, CivicPublisher, CivicPublisherKind,
                                    CivicRecordKind, CivicRecordOperation, CivicSourceKind)
from beehive.civic.generation import (build_candidate_snapshot, build_generation_prompt,
                                      prompt_hash)
from beehive.civic.limits import (MAX_AI_CALLS_PER_RUN, MAX_AI_STAGE_ATTEMPTS, REQUIRED_LOCALES,
                                  CivicLocale)
from beehive.civic.pipeline import ingest_civic_documents, qualify_civic_candidate
from beehive.db.civic_ai_stages import (CivicAiStageExhaustedError,
                                        count_run_ai_calls, get_stage_receipt,
                                        list_candidate_stages, pending_stage_reservation,
                                        record_ai_stage_success,
                                        reserve_ai_stage)
from beehive.db.civic_batches import (get_or_create_batch_if_claimed, list_sealed_candidate_ids,
                                      seal_batch_if_claimed)
from beehive.db.civic_candidates import add_candidate_evidence, get_candidate
from beehive.db.civic_qualifications import get_latest_qualification
from beehive.db.civic_runs import (claim_civic_run, complete_civic_run, enqueue_civic_run,
                                   get_civic_run)
from beehive.db.civic_verifications import list_verifications
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier,
                                  CivicRunStatus, CivicVerificationVerdict)
from tests.civic.civic_ai_fakes import (fenced, full_entry, generation_payload, generation_reply,
                                        passing_gateway, refusal_reply, verification_payload)

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


def _make_run(conn, *, mode: str = "normal", slot: datetime | None = None, watch_id=None):
    slot = slot or T0
    watch_id = watch_id or create_civic_watch(conn, "NZ civic", "What changed?", T0).id
    enqueued = enqueue_civic_run(conn, watch_id, slot, slot, run_mode=mode)
    lease = claim_civic_run(conn, enqueued.id, slot, 600, 3600)
    batch = get_or_create_batch_if_claimed(conn, enqueued.id, lease.run.claim_token, slot)
    return type("Ctx", (), {
        "watch_id": watch_id, "id": enqueued.id, "token": lease.run.claim_token,
        "batch": batch, "mode": mode})()


@pytest.fixture
def run(conn):
    return _make_run(conn)


# Deliberately unrelated titles: clustering groups by title-token similarity, so a slot of ten
# Candidates needs ten developments, not ten wordings of one.
_TITLES = (
    "Housing bill passes second reading",
    "Transport levy receives royal assent",
    "Health committee reports back",
    "Education order commences",
    "Energy statute amended",
    "Water regulator publishes finding",
    "Justice tribunal rules on appeal",
    "Defence appropriation approved",
    "Tourism charge repealed",
    "Fisheries quota notice gazetted",
    "Forestry permit scheme starts",
    "Climate target written into law",
)


def _document(key: str = "a", index: int = 0) -> CivicDocument:
    return CivicDocument(
        source_key="nz_parliament", external_key=key,
        canonical_url=f"https://parliament.test/{key}",
        title=_TITLES[index % len(_TITLES)], tier=CivicEvidenceTier.OFFICIAL,
        publisher="Parliament", first_seen_at=T0, published_at=T0,
        excerpt="The record shows the bill passed its second reading on 2026-08-01.")


def qualified_candidates(conn, run, count: int = 1, *, accessible: bool = True,
                         paywalled: bool = False, excerpt: str | None = None):
    """`count` Candidates the deterministic engine qualified, each sitting in VERIFYING."""
    ingested = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.token, now=T0,
        documents=[_document(f"doc-{index}", index) for index in range(count)])
    candidates = list(ingested.created_candidates)
    assert len(candidates) == count, f"clustering produced {len(candidates)} candidates"
    for index, candidate in enumerate(candidates):
        add_candidate_evidence(
            conn, candidate.id, "official_feed", f"src-{index}",
            f"https://parliament.test/{index}", "Housing bill second reading",
            CivicEvidenceTier.OFFICIAL, CivicEvidenceStance.SUPPORTING, T0,
            publisher="Parliament", publisher_id="parliament",
            source_kind=CivicSourceKind.PARLIAMENT.value, subject_id="green-party",
            claim_category=CivicClaimCategory.LEGISLATION.value, event_date=EVENT_DATE,
            canonical_url=f"https://parliament.test/{index}",
            excerpt=excerpt or "The record shows the bill passed its second reading on 2026-08-01.",
            accessible_full_text=accessible, paywalled=paywalled)
        claim = CivicCandidateClaim(
            candidate_id=str(candidate.id), fingerprint=candidate.dedupe_key,
            record_kind=CivicRecordKind.TIMELINE, operation=CivicRecordOperation.ADD,
            subject_id="green-party", record_key=candidate.dedupe_key, event_date=EVENT_DATE,
            category=CivicClaimCategory.LEGISLATION, content_class=CivicContentClass.FACTUAL,
            durable=True, claim_text="The bill passed its second reading.")
        output = qualify_civic_candidate(
            conn, run_id=run.id, claim_token=run.token, claim=claim, registry=REGISTRY, now=T0)
        assert output.qualification.is_publishable
    return candidates


def qualified_candidate(conn, run, **kwargs):
    return qualified_candidates(conn, run, 1, **kwargs)[0]


def generate(conn, run, candidate_id, gateway, **kwargs):
    return asyncio.run(generate_civic_candidate(
        conn, run_id=run.id, claim_token=run.token, candidate_id=candidate_id,
        gateway=gateway, now=T0, **kwargs))


# ============================================================================
# The happy path: one generation call, one verifier call
# ============================================================================


def test_a_verified_candidate_costs_exactly_two_calls(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()

    result = generate(conn, run, candidate.id, gateway)

    assert result.verified
    assert result.candidate.status is CivicCandidateStatus.VERIFIED
    assert [content.locale for content in result.contents] == list(REQUIRED_LOCALES)
    assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    assert result.ai_calls_made == AI_CALLS_PER_CANDIDATE == 2
    assert count_run_ai_calls(conn, run.id) == 2
    verdicts = list_verifications(conn, candidate.id)
    assert [entry.verdict for entry in verdicts] == [CivicVerificationVerdict.SUPPORTED]


def test_one_generation_call_leaves_exactly_one_receipt(conn, run):
    """Honest stage semantics: one call, one row. Nothing pretends the four locales were four
    separate model calls."""
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway())

    receipts = list_candidate_stages(conn, candidate.id)
    assert [entry.stage for entry in receipts] == [
        CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    stored = stored_locale_contents(conn, candidate.id)
    assert set(stored) == set(REQUIRED_LOCALES)
    assert stored[CivicLocale.ZH].title == "议会通过住房法案二读"


def test_every_request_is_tool_free_and_carries_no_endpoint(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()
    generate(conn, run, candidate.id, gateway)

    assert gateway.calls, "no model call was made"
    for call in gateway.calls:
        assert call.request.tools == ()
        assert "http" not in call.request.prompt
        assert not hasattr(call.request, "api_key")
        assert not hasattr(call.request, "endpoint")


def test_the_verifier_runs_on_a_different_model_with_no_generator_context(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()
    generate(conn, run, candidate.id, gateway)

    generation = gateway.requests_for(CivicAiStage.GENERATE)[0]
    verification = gateway.requests_for(CivicAiStage.VERIFICATION)[0]
    assert verification.model != generation.model
    assert "You did not write the entries below" in verification.prompt
    assert generation.prompt not in verification.prompt
    assert "You write civic reference entries" not in verification.prompt


def test_only_a_supporting_verdict_publishes(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{CivicAiStage.VERIFICATION: fenced(verification_payload(
        "reject", [{"code": "unsupported_claim", "locale": "en", "detail": "not in evidence"}]))})

    result = generate(conn, run, candidate.id, gateway)

    assert not result.verified
    assert result.rejection_code == "verification_rejected"
    assert result.candidate.status is CivicCandidateStatus.REJECTED
    assert [entry.verdict for entry in list_verifications(conn, candidate.id)] == [
        CivicVerificationVerdict.INSUFFICIENT]


def test_a_screening_refusal_never_reaches_the_verifier(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{CivicAiStage.GENERATE: refusal_reply()})

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "screening_discard_poll"
    assert gateway.stages == [CivicAiStage.GENERATE]
    assert result.candidate.status is CivicCandidateStatus.REJECTED


def test_a_candidate_that_is_not_qualified_is_never_generated(conn, run):
    candidate = qualified_candidate(conn, run)
    from beehive.db.civic_candidates import reject_candidate

    reject_candidate(conn, candidate.id, "screened_out", T0)
    with pytest.raises(CivicAiPipelineError, match="only a qualified"):
        generate(conn, run, candidate.id, passing_gateway())


# ============================================================================
# The budget really fits: both caps, and both caps with a retry each
# ============================================================================


def test_a_full_normal_slot_completes_inside_the_fixed_budget(conn, run):
    """Six Candidates, the normal cap, through the real pipeline: 12 calls of the 30 allowed."""
    cap = published_cap(CivicRunMode.NORMAL)
    candidates = qualified_candidates(conn, run, cap)
    gateway = passing_gateway()

    for candidate in candidates:
        assert generate(conn, run, candidate.id, gateway).verified

    assert len(gateway.calls) == cap * AI_CALLS_PER_CANDIDATE == 12
    assert count_run_ai_calls(conn, run.id) == 12
    assert count_run_ai_calls(conn, run.id) <= MAX_AI_CALLS_PER_RUN
    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert len(list_sealed_candidate_ids(conn, sealed.id)) == cap


def test_a_full_election_slot_completes_inside_the_fixed_budget(conn):
    """Ten Candidates, the election cap: 20 calls, leaving 10 for transient retries."""
    election = _make_run(conn, mode="election")
    cap = published_cap(CivicRunMode.ELECTION)
    candidates = qualified_candidates(conn, election, cap)
    gateway = passing_gateway()

    for candidate in candidates:
        assert generate(conn, election, candidate.id, gateway).verified

    assert len(gateway.calls) == cap * AI_CALLS_PER_CANDIDATE == 20
    assert count_run_ai_calls(conn, election.id) == 20
    assert MAX_AI_CALLS_PER_RUN - 20 == 10
    sealed = seal_batch_if_claimed(
        conn, election.batch.id, election.id, election.token, T0 + timedelta(minutes=1))
    assert len(list_sealed_candidate_ids(conn, sealed.id)) == cap


def test_an_election_slot_with_one_transient_failure_each_still_fits(conn):
    """Ten Candidates, each losing one call to a timeout: 30 calls exactly, the whole ceiling,
    and every Candidate still verified."""
    election = _make_run(conn, mode="election")
    cap = published_cap(CivicRunMode.ELECTION)
    candidates = qualified_candidates(conn, election, cap)
    gateway = passing_gateway()

    for index, candidate in enumerate(candidates):
        # Alternate which stage loses its first attempt, so both the generation and the verifier
        # retry paths are exercised across the slot.
        stage = CivicAiStage.GENERATE if index % 2 == 0 else CivicAiStage.VERIFICATION
        gateway.fail_once = {stage: CivicModelError("model_timeout", "no response")}
        gateway.failed_stages = set()
        assert generate(conn, election, candidate.id, gateway).verified

    assert len(gateway.calls) == cap * (AI_CALLS_PER_CANDIDATE + 1) == 30
    assert count_run_ai_calls(conn, election.id) == MAX_AI_CALLS_PER_RUN == 30
    sealed = seal_batch_if_claimed(
        conn, election.batch.id, election.id, election.token, T0 + timedelta(minutes=1))
    assert len(list_sealed_candidate_ids(conn, sealed.id)) == cap


def test_a_caller_cannot_widen_the_fixed_ceiling(conn, run):
    candidate = qualified_candidate(conn, run)
    with pytest.raises(ValueError, match="exceeds the plan's fixed ceiling"):
        generate(conn, run, candidate.id, passing_gateway(),
                 call_budget=MAX_AI_CALLS_PER_RUN + 1)
    with pytest.raises(ValueError, match="exceeds the fixed ceiling"):
        reserve_ai_stage(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
            stage=CivicAiStage.GENERATE, provider="github-copilot", model=DEFAULT_MODEL,
            prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
            schema_version="civic-ai-schema/1", input_hash="i" * 64, now=T0,
            max_attempts=MAX_AI_STAGE_ATTEMPTS + 1)


# ============================================================================
# The bounded transient retry
# ============================================================================


@pytest.mark.parametrize("stage", [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION])
def test_a_transient_failure_is_retried_exactly_once(conn, run, stage):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()
    gateway.fail_once = {stage: CivicModelError("model_timeout", "no response in 120s")}

    result = generate(conn, run, candidate.id, gateway)

    assert result.verified
    assert gateway.stages.count(stage) == 2
    receipts = [entry for entry in list_candidate_stages(conn, candidate.id)
                if entry.stage is stage]
    assert [entry.status for entry in receipts] == ["failed", "succeeded"]
    assert receipts[0].error_code == "model_timeout"
    assert [entry.attempt for entry in receipts] == [1, 2]


def test_a_retried_stage_replays_normally_afterwards(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway()
    gateway.fail_once = {CivicAiStage.GENERATE: CivicModelError("model_timeout", "gone")}
    generate(conn, run, candidate.id, gateway)

    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL WHERE id = ?",
        (candidate.id,))
    conn.commit()
    resumed = passing_gateway()
    del resumed.responses[CivicAiStage.GENERATE]

    result = generate(conn, run, candidate.id, resumed)

    assert result.verified
    assert CivicAiStage.GENERATE in result.replayed_stages
    assert CivicAiStage.GENERATE not in resumed.stages


def test_a_schema_failure_is_never_retried(conn, run):
    """Plan 7.8: a response that arrived and was wrong rejects the Candidate. Asking the model to
    correct itself is the self-repair loop this pipeline does not have."""
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{CivicAiStage.GENERATE: "no fenced json here"})

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "ai_schema_rejected"
    assert gateway.stages == [CivicAiStage.GENERATE]
    receipts = list_candidate_stages(conn, candidate.id)
    assert [entry.status for entry in receipts] == ["failed"]
    assert receipts[0].error_code == "schema_rejected"


def test_a_content_failure_is_never_retried(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: generation_reply(
            locale_overrides={"en": {"citations": ["e9"]}})})

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "content_alias_unknown"
    assert gateway.stages == [CivicAiStage.GENERATE]


def test_uncertainty_true_rejects_before_verification_or_seal(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: generation_reply(locale_overrides={
            locale: {"uncertainty": True}
            for locale in ("zh", "en", "hi", "tl")
        }),
    })

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "content_uncertainty_unsupported"
    assert result.candidate.status is CivicCandidateStatus.REJECTED
    assert gateway.stages == [CivicAiStage.GENERATE]
    assert list_verifications(conn, candidate.id) == []
    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert list_sealed_candidate_ids(conn, sealed.id) == []


def test_a_third_attempt_is_impossible(conn, run):
    """Two transient failures exhaust the stage. The Candidate is deferred, not rejected, and no
    third call is ever made."""
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: CivicModelError("model_timeout", "gone")})

    result = generate(conn, run, candidate.id, gateway)

    assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.GENERATE]
    assert result.deferral_reason == "ai_transient_failed"
    assert result.candidate.status is CivicCandidateStatus.DEFERRED_AI
    receipts = list_candidate_stages(conn, candidate.id)
    assert [entry.attempt for entry in receipts] == [1, 2]
    assert all(entry.status == "failed" for entry in receipts)
    with pytest.raises(CivicAiStageExhaustedError):
        reserve_ai_stage(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
            stage=CivicAiStage.GENERATE, provider="github-copilot", model=DEFAULT_MODEL,
            prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
            schema_version="civic-ai-schema/1", input_hash="i" * 64, now=T0)


def test_a_verifier_timeout_that_outlives_its_retry_defers(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.VERIFICATION: CivicModelError("model_timeout", "gone")})

    result = generate(conn, run, candidate.id, gateway)

    assert result.deferral_reason == "ai_transient_failed"
    assert result.candidate.status is CivicCandidateStatus.DEFERRED_AI
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE).succeeded
    assert list_verifications(conn, candidate.id) == []
    assert next_incomplete_stage(conn, candidate.id) is CivicAiStage.VERIFICATION


# ============================================================================
# Failure modes that really are the Candidate's fault
# ============================================================================


def test_an_invented_alias_rejects_the_candidate(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: generation_reply(
            locale_overrides={"zh": {"citations": ["e9"]}})})

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "content_alias_unknown"
    assert result.candidate.status is CivicCandidateStatus.REJECTED


@pytest.mark.parametrize("locale", ["en", "hi", "tl"])
@pytest.mark.parametrize("spoil,expected", [
    ({"event_date": "2026-08-02"}, "content_event_date_drift"),
    ({"citations": ["e9"]}, "content_alias_unknown"),
    ({"entities": ["invented-party"]}, "content_entity_not_allowed"),
    ({"policy_status": "enacted"}, "content_locale_status_drift"),
    ({"uncertainty": True}, "content_locale_uncertainty_drift"),
])
def test_drift_in_any_overlay_locale_rejects_the_candidate(conn, run, locale, spoil, expected):
    """Every overlay is held to the canonical Chinese entry, and every dimension of it: the date,
    the sources, the parties, the status and the caveat."""
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: generation_reply(locale_overrides={locale: spoil})})

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == expected
    assert result.candidate.status is CivicCandidateStatus.REJECTED
    assert list_verifications(conn, candidate.id) == []


def test_a_number_invented_by_an_overlay_rejects_the_candidate(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: generation_reply(locale_overrides={"en": {
            "summary": "The bill passed its second reading on 2026-08-01 by 61 votes.",
            "claim_text": "The bill passed on 2026-08-01."}})})

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "content_number_not_in_evidence"


def test_a_missing_locale_can_never_verify_or_seal(conn, run):
    candidate = qualified_candidate(conn, run)
    entry = full_entry()
    entry.pop("tl")
    gateway = passing_gateway(**{
        CivicAiStage.GENERATE: fenced(generation_payload(entry=entry))})

    result = generate(conn, run, candidate.id, gateway)

    assert not result.verified
    assert result.rejection_code == "ai_schema_rejected"
    assert CivicAiStage.VERIFICATION not in gateway.stages
    assert list_verifications(conn, candidate.id) == []
    assert stored_locale_contents(conn, candidate.id) == {}
    with pytest.raises(CivicContentError, match="locale_missing"):
        require_publishable_localization(conn, candidate.id)

    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert list_sealed_candidate_ids(conn, sealed.id) == []


def test_evidence_restricted_after_qualification_never_reaches_a_model(conn, run):
    candidate = qualified_candidate(conn, run)
    conn.execute(
        "UPDATE civic_candidate_evidence "
        "SET accessible_full_text = 0, paywalled = 1 WHERE candidate_id = ?",
        (candidate.id,),
    )
    conn.commit()
    gateway = passing_gateway()

    result = generate(conn, run, candidate.id, gateway)

    assert result.rejection_code == "snapshot_unusable"
    assert gateway.calls == []


# ============================================================================
# Receipts: replay, drift, idempotency
# ============================================================================


def test_a_resumed_worker_replays_stored_stages_instead_of_regenerating(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{CivicAiStage.VERIFICATION: "not json"})
    generate(conn, run, candidate.id, gateway)

    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL, "
        "rejection_code = NULL WHERE id = ?", (candidate.id,))
    conn.commit()
    resumed = passing_gateway()
    del resumed.responses[CivicAiStage.GENERATE]

    result = generate(conn, run, candidate.id, resumed)

    assert result.verified
    assert resumed.stages == [CivicAiStage.VERIFICATION]
    assert result.replayed_stages == (CivicAiStage.GENERATE,)


def test_changed_input_after_a_receipt_defers_a_fresh_revision(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = passing_gateway(**{CivicAiStage.VERIFICATION: "not json"})
    generate(conn, run, candidate.id, gateway)

    conn.execute(
        "UPDATE civic_candidate_evidence SET excerpt = ? WHERE candidate_id = ?",
        ("The record now says something else entirely.", candidate.id))
    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL, "
        "rejection_code = NULL WHERE id = ?", (candidate.id,))
    conn.commit()

    result = generate(conn, run, candidate.id, passing_gateway())
    assert result.deferral_reason == "ai_provenance_stale"
    assert result.candidate.ai_refresh_reason == "ai_stage_input_hash_changed"
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) is None


def test_a_changed_model_after_a_receipt_defers_a_fresh_revision(conn, run):
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id,
             passing_gateway(**{CivicAiStage.VERIFICATION: "not json"}))
    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL, "
        "rejection_code = NULL WHERE id = ?", (candidate.id,))
    conn.commit()

    result = generate(conn, run, candidate.id, passing_gateway(), model="gpt-5.4")
    assert result.deferral_reason == "ai_provenance_stale"
    assert result.candidate.ai_refresh_reason == "ai_stage_model_changed"


def test_a_changed_prompt_after_a_receipt_defers_a_fresh_revision(conn, run, monkeypatch):
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id,
             passing_gateway(**{CivicAiStage.VERIFICATION: "not json"}))
    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL, "
        "rejection_code = NULL WHERE id = ?", (candidate.id,))
    conn.commit()

    import beehive.civic.ai_pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "build_generation_prompt", lambda snapshot: "a reworded generation prompt")
    result = generate(conn, run, candidate.id, passing_gateway())
    assert result.deferral_reason == "ai_provenance_stale"
    assert result.candidate.ai_refresh_reason == "ai_stage_prompt_hash_changed"


def test_a_changed_schema_version_after_a_receipt_defers_a_fresh_revision(conn, run, monkeypatch):
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id,
             passing_gateway(**{CivicAiStage.VERIFICATION: "not json"}))
    conn.execute(
        "UPDATE civic_candidates SET status = 'verifying', settled_at = NULL, "
        "rejection_code = NULL WHERE id = ?", (candidate.id,))
    conn.commit()

    import beehive.civic.ai_pipeline as pipeline

    monkeypatch.setattr(pipeline, "CIVIC_SCHEMA_VERSION", "civic-ai-schema/2")
    result = generate(conn, run, candidate.id, passing_gateway())
    assert result.deferral_reason == "ai_provenance_stale"
    assert result.candidate.ai_refresh_reason == "ai_stage_schema_version_changed"


# ============================================================================
# Summary substance
# ============================================================================

_SUBSTANTIAL_RELEASE = (
    "The record shows the bill passed its second reading on 2026-08-01. The bill would fund "
    "social homes in Auckland and Christchurch, require councils to zone land near rapid "
    "transit for apartments, and give community housing providers first refusal on surplus "
    "Crown land. The select committee recommended the bill proceed with amendments that "
    "protect existing tenants during redevelopment and require annual reporting to "
    "Parliament on progress, including the number of homes started and completed.")


def _english(title, summary):
    return passing_gateway(**{CivicAiStage.GENERATE: generation_reply(
        locale_overrides={"en": {"title": title, "summary": summary}})})


def test_a_title_restating_summary_of_a_substantial_source_is_regenerated_later(conn, run):
    assert len(_SUBSTANTIAL_RELEASE) >= 400
    candidate = qualified_candidate(conn, run, excerpt=_SUBSTANTIAL_RELEASE)
    gateway = _english(
        "Housing bill passes second reading",
        "The housing bill passed its second reading on 2026-08-01.")

    result = generate(conn, run, candidate.id, gateway)

    assert not result.verified
    assert gateway.stages == [CivicAiStage.GENERATE]
    assert result.deferral_reason == "ai_provenance_stale"
    assert result.candidate.status is CivicCandidateStatus.DEFERRED_AI
    assert result.candidate.ai_refresh_reason == "summary_missing_substance"
    assert result.candidate.ai_revision == candidate.ai_revision + 1
    assert result.candidate.rejection_code is None


def test_a_substantive_summary_of_the_same_source_verifies(conn, run):
    candidate = qualified_candidate(conn, run, excerpt=_SUBSTANTIAL_RELEASE)
    gateway = _english(
        "Housing bill passes second reading",
        "The bill, which would fund social homes in Auckland and Christchurch and require "
        "councils to zone land near rapid transit, passed its second reading on 2026-08-01.")

    result = generate(conn, run, candidate.id, gateway)

    assert result.verified
    assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]


def test_a_brief_source_may_be_summarised_by_its_own_headline(conn, run):
    candidate = qualified_candidate(conn, run)
    gateway = _english(
        "Housing bill passes second reading",
        "The housing bill passed its second reading on 2026-08-01.")

    assert generate(conn, run, candidate.id, gateway).verified


# ============================================================================
# Crash points
# ============================================================================


def test_a_crash_after_the_reservation_costs_an_attempt(conn, run):
    candidate = qualified_candidate(conn, run)
    snapshot = build_candidate_snapshot(
        conn, candidate_id=candidate.id,
        qualification=get_latest_qualification(conn, candidate.id))

    reservation = reserve_ai_stage(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        stage=CivicAiStage.GENERATE, provider="github-copilot", model=DEFAULT_MODEL,
        prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
        schema_version="civic-ai-schema/1", input_hash=snapshot.snapshot_hash, now=T0)
    assert reservation is not None
    assert pending_stage_reservation(conn, candidate.id, CivicAiStage.GENERATE) is not None
    assert count_run_ai_calls(conn, run.id) == 1

    assert reserve_ai_stage(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        stage=CivicAiStage.GENERATE, provider="github-copilot", model=DEFAULT_MODEL,
        prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
        schema_version="civic-ai-schema/1", input_hash=snapshot.snapshot_hash, now=T0) is None


def test_a_worker_that_died_mid_call_is_taken_over_by_the_next_one(conn, run):
    """The dead worker's reservation is settled as abandoned, the recovered worker spends the
    stage's second attempt, and the Candidate still reaches VERIFIED."""
    candidate = qualified_candidate(conn, run)
    snapshot = build_candidate_snapshot(
        conn, candidate_id=candidate.id,
        qualification=get_latest_qualification(conn, candidate.id))
    stranded = reserve_ai_stage(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        stage=CivicAiStage.GENERATE, provider="github-copilot", model=DEFAULT_MODEL,
        prompt_version="civic-ai-prompts/1",
        prompt_hash=prompt_hash(build_generation_prompt(snapshot)),
        schema_version="civic-ai-schema/1", input_hash=snapshot.snapshot_hash, now=T0)
    assert stranded is not None

    conn.execute("UPDATE civic_runs SET claim_token = 'recovered' WHERE id = ?", (run.id,))
    conn.commit()
    run.token = "recovered"

    result = generate(conn, run, candidate.id, passing_gateway())

    assert result.verified
    generation = [entry for entry in list_candidate_stages(conn, candidate.id)
                  if entry.stage is CivicAiStage.GENERATE]
    assert [entry.status for entry in generation] == ["failed", "succeeded"]
    assert generation[0].error_code == "reservation_abandoned"
    assert generation[1].attempt == 2


def test_a_stale_claim_token_records_nothing(conn, run):
    candidate = qualified_candidate(conn, run)
    snapshot = build_candidate_snapshot(
        conn, candidate_id=candidate.id,
        qualification=get_latest_qualification(conn, candidate.id))
    reservation = reserve_ai_stage(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.token,
        stage=CivicAiStage.GENERATE, provider="github-copilot", model=DEFAULT_MODEL,
        prompt_version="civic-ai-prompts/1", prompt_hash="p" * 64,
        schema_version="civic-ai-schema/1", input_hash=snapshot.snapshot_hash, now=T0)

    conn.execute(
        "UPDATE civic_runs SET claim_token = 'someone-else' WHERE id = ?", (run.id,))
    conn.commit()

    assert record_ai_stage_success(
        conn, reservation=reservation, output={"decision": "qualify"}, now=T0) is None
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) is None


def test_a_lost_claim_stops_the_pipeline_without_writing(conn, run):
    candidate = qualified_candidate(conn, run)
    with pytest.raises(CivicClaimLostError):
        asyncio.run(generate_civic_candidate(
            conn, run_id=run.id, claim_token="not-the-token", candidate_id=candidate.id,
            gateway=passing_gateway(), now=T0))
    assert list_candidate_stages(conn, candidate.id) == []
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.VERIFYING


def test_no_write_lock_is_held_while_a_model_call_is_in_flight(conn, run, tmp_path):
    candidate = qualified_candidate(conn, run)
    observer = connect(str(tmp_path / "test.db"))
    acquired: list[bool] = []

    def probe(_request):
        try:
            observer.execute("BEGIN IMMEDIATE")
            observer.execute(
                "UPDATE civic_candidates SET title = title WHERE id = ?", (candidate.id,))
            observer.commit()
            acquired.append(True)
        except sqlite3.OperationalError:
            acquired.append(False)

    gateway = passing_gateway()
    gateway.on_call = probe
    result = generate(conn, run, candidate.id, gateway)

    assert result.verified
    assert acquired and all(acquired), "the write lock was held during a model call"


# ============================================================================
# Sealing and run settlement
# ============================================================================


def test_a_batch_seals_only_the_verified_candidate(conn, run):
    verified = qualified_candidate(conn, run)
    generate(conn, run, verified.id, passing_gateway())

    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert sealed is not None
    assert list_sealed_candidate_ids(conn, sealed.id) == [verified.id]
    assert require_publishable_localization(conn, verified.id)


def test_a_rejected_candidate_is_never_sealed(conn, run):
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(**{
        CivicAiStage.VERIFICATION: fenced(verification_payload(
            "reject", [{"code": "neutrality", "locale": None, "detail": "reads as commentary"}]))
    }))

    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert sealed is not None
    assert list_sealed_candidate_ids(conn, sealed.id) == []


def test_a_run_still_settles_after_the_ai_rejects_every_candidate(conn, run):
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway(**{
        CivicAiStage.GENERATE: refusal_reply("defer", "not_durable", "too minor")}))

    assert get_candidate(conn, candidate.id).is_settled
    sealed = seal_batch_if_claimed(
        conn, run.batch.id, run.id, run.token, T0 + timedelta(minutes=1))
    assert sealed is not None
    assert list_sealed_candidate_ids(conn, sealed.id) == []
    assert complete_civic_run(
        conn, run.id, run.token, CivicRunStatus.COMPLETED, T0 + timedelta(minutes=2))
    assert get_civic_run(conn, run.id).status is CivicRunStatus.COMPLETED


def test_stage_receipts_store_no_prompt_text_and_no_secret(conn, run):
    candidate = qualified_candidate(conn, run)
    generate(conn, run, candidate.id, passing_gateway())

    for receipt in list_candidate_stages(conn, candidate.id):
        stored = " ".join(str(value) for value in (
            receipt.prompt_hash, receipt.output_json, receipt.error_detail, receipt.model,
            receipt.provider))
        assert "You have no tools" not in stored
        assert "http" not in stored
        assert len(receipt.prompt_hash) == 64
