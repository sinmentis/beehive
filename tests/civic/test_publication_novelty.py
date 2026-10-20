"""Final-output novelty, through genuine generation receipts and the publication seal."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.ai_gateway import CivicAiStage, verifier_model_for
from beehive.civic.ai_pipeline import generate_civic_candidate, stored_locale_contents
from beehive.civic.generation import (
    CIVIC_PROMPT_VERSION,
    build_candidate_snapshot,
    build_generation_prompt,
    build_verification_prompt,
)
from beehive.civic.language_fidelity import LANGUAGE_FIDELITY_RULES
from beehive.civic.novelty import NOVELTY_RULES
from beehive.civic.clustering import CivicDocumentCluster
from beehive.civic.destination import (
    DESTINATION_V1_ENGINE_VERSION,
    constrain_qualification_to_destination_v1,
)
from beehive.civic.documents import CivicDocument
from beehive.civic.pipeline import qualify_civic_candidate
from beehive.civic.production import (
    build_ownership_registry,
    build_source_profiles,
    construct_candidate_claim,
    persist_cluster_evidence,
)
from beehive.civic.spool import CivicSpool
from beehive.civic.source_extraction import PublicationWitness, SourceArticle, capture_metadata
from beehive.db.civic_batches import (
    adopt_capacity_deferrals,
    get_frozen_publication,
    get_or_create_batch_if_claimed,
    list_publication_rejections,
    list_sealed_candidate_ids,
    refresh_candidate_for_ai_if_claimed,
    seal_batch_if_claimed,
)
from beehive.db.civic_candidates import create_candidate, get_candidate
from beehive.db.civic_ai_stages import count_run_ai_calls, get_stage_receipt, list_candidate_stages
from beehive.db.civic_qualifications import get_latest_qualification
from beehive.db.civic_runs import claim_civic_run, complete_civic_run, enqueue_civic_run
from beehive.db.connection import connect, init_schema
from beehive.deep_read.extract import ExtractionQuality, ExtractionResult
from beehive.domain.civic import CivicCandidateStatus, CivicEvidenceTier, CivicRunStatus
from tests.civic.civic_ai_fakes import (
    FakeCivicGateway,
    fenced,
    full_entry,
    generation_payload,
    verification_payload,
)
from tests.civic.civic_worker_support import create_partymap_watch

T0 = datetime(2026, 9, 2, 6, tzinfo=timezone.utc)
BILL = "Maritime Transport (Lifejackets on Recreational Craft) Amendment Bill"


@pytest.fixture
def context(tmp_path):
    conn = connect(str(tmp_path / "novelty.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="policy")
    yield conn, watch, tmp_path
    conn.close()


def claimed_batch(conn, watch, now=T0):
    run = enqueue_civic_run(conn, watch.id, now, now)
    lease = claim_civic_run(conn, run.id, now, 600, 2000)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, now)
    return lease.run, batch


def gateway_for(title, summary, party="national", event_date=T0.date()):
    return FakeCivicGateway(responses={
        CivicAiStage.GENERATE: fenced(generation_payload(entry=full_entry(**{
            locale: {
                "title": title, "summary": summary, "claim_text": summary,
                "event_date": event_date.isoformat(), "policy_status": "announced",
                "entities": [party],
            }
            for locale in ("zh", "en", "hi", "tl")
        }))),
        CivicAiStage.VERIFICATION: fenced(verification_payload()),
    })


async def generated_candidate(
    conn, watch, run, batch, *, key, source_title, source_text, title, summary,
    party="national", event_date=T0.date(), now=T0,
):
    profile = build_source_profiles()[party]
    document = CivicDocument(
        source_key=party, external_key=key,
        canonical_url=f"https://www.{party}.org.nz/news/{key}",
        title=source_title, excerpt=source_text,
        tier=CivicEvidenceTier.PARTY_PRIMARY, publisher=profile.publisher,
        first_seen_at=now, published_at=datetime.combine(
            event_date, datetime.min.time(), tzinfo=timezone.utc),
        raw_metadata={"detail_hydrated": True, "detail_extraction_quality": "complete"},
    )
    article = SourceArticle(
        extraction=ExtractionResult(ExtractionQuality.COMPLETE, source_text, (), len(source_text)),
        publication=PublicationWitness(
            document.published_at, "meta:article:published_time", "datetime",
            document.published_at.isoformat()),
        title=source_title, method="article-html")
    document = replace(document, raw_metadata={"source_capture": capture_metadata(
        document, article=article, body=source_text, captured_at=now,
        fetched_url=document.canonical_url)})
    candidate = create_candidate(
        conn, batch.id, document.dedupe_key, document.title, document.excerpt, now)
    persist_cluster_evidence(
        conn, run_id=run.id, claim_token=run.claim_token, candidate=candidate,
        cluster=CivicDocumentCluster(cluster_key=document.dedupe_key, documents=(document,)),
        watch=watch, subject_id=party, profiles=build_source_profiles(), now=now)
    qualification = qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token,
        claim=construct_candidate_claim(conn, candidate), registry=build_ownership_registry(),
        now=now, engine_version=DESTINATION_V1_ENGINE_VERSION,
        qualification_scope=constrain_qualification_to_destination_v1)
    assert qualification.qualification.is_publishable
    gateway = gateway_for(title, summary, party, event_date)
    generated = await generate_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token, candidate_id=candidate.id,
        gateway=gateway, now=now)
    assert generated.verified, generated.rejection_code
    return generated.candidate


async def test_distinct_releases_of_same_final_bill_event_do_not_take_two_slots(context):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    first = await generated_candidate(
        conn, watch, run, batch, key="260902-shanereti",
        source_title="Lifejacket law to save lives",
        source_text=(
            f"The {BILL} passed Parliament unanimously on 2026-09-02. "
            "Lifejackets must be worn, not just carried, on recreational craft 6 metres or less."),
        title="National Party announces lifejackets will need to be worn on small recreational craft",
        summary=(
            "On 2026-09-02, the National Party announced that Shane Reti's bill had passed "
            "Parliament unanimously and lifejackets must be worn on recreational craft 6 metres or less."))
    duplicate = await generated_candidate(
        conn, watch, run, batch, key="260902-jamesmeager",
        source_title="Safer boating legislation clears Parliament",
        source_text=(
            f"The {BILL} passed its final reading on 2026-09-02. "
            "It will take effect 28 days after Royal Assent."),
        title="National Party says lifejacket bill passed",
        summary=(
            f"On 2026-09-02, the National Party said the {BILL} had passed "
            "and would take effect 28 days after Royal Assent."))
    healthy = await generated_candidate(
        conn, watch, run, batch, key="road-maintenance",
        source_title="National launches road maintenance programme",
        source_text="National announced a new road maintenance programme on 2026-09-02.",
        title="National announces road maintenance programme",
        summary="National announced a new road maintenance programme on 2026-09-02.")
    expected = [first.id, healthy.id]
    for topic in ("apprenticeships", "libraries", "broadband", "fisheries"):
        summary = f"National announced a new {topic} programme on 2026-09-02."
        candidate = await generated_candidate(
            conn, watch, run, batch, key=topic, source_title=f"National {topic} announcement",
            source_text=summary, title=f"National announces {topic} programme", summary=summary)
        expected.append(candidate.id)

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert list_sealed_candidate_ids(conn, sealed.id) == expected
    assert get_candidate(conn, duplicate.id).status is CivicCandidateStatus.REJECTED
    assert list_publication_rejections(conn, sealed.id)[0].reason == "publication_duplicate_event"
    written = CivicSpool(root / "spool").write_batch(conn, sealed.id, T0)
    assert len(json.loads(written.path.read_bytes())["items"]) == 6


@pytest.mark.parametrize("legacy", [False, True])
async def test_policy_restatement_does_not_become_another_announcement_across_days(context, legacy):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    first_date = T0.date() - timedelta(days=1)
    first = await generated_candidate(
        conn, watch, run, batch, party="act",
        key="even-the-public-service-knows-government-is-too-big",
        source_title="Even the public service knows Government is too big",
        source_text=(
            "ACT proposed a Government of 18 Ministers and 19 Departments on 2026-09-01. "
            "The current structure has 28 Ministers and 39 departments."),
        title="ACT proposes 18 Ministers and 19 Departments",
        summary=(
            "On 2026-09-01, ACT proposed restructuring the Government to have 18 Ministers "
            "and 19 Departments, and said the current structure has 28 Ministers and 39 departments."),
        event_date=first_date)
    seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    original_candidate = get_candidate(conn, first.id)
    original = get_frozen_publication(conn, batch.id)
    spool = CivicSpool(root / "spool")
    original_path = spool.write_batch(conn, batch.id, T0).path
    if legacy:
        conn.execute("DELETE FROM civic_batch_publications WHERE batch_id = ?", (batch.id,))
        conn.execute(
            "UPDATE civic_batches SET publication_schema_version = NULL WHERE id = ?", (batch.id,))
        conn.commit()
    complete_civic_run(conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, T0)
    later = T0 + timedelta(days=1)
    second_run, second_batch = claimed_batch(conn, watch, later)
    repeated = await generated_candidate(
        conn, watch, second_run, second_batch, party="act",
        key="labour-promises-to-protect-the-bureaucracy-whatever-the-cost",
        source_title="Labour promises to protect the bureaucracy whatever the cost",
        source_text=(
            "ACT criticised Labour's public service position on 2026-09-02. "
            "ACT would have 18 ministers and 19 departments under a smaller government."),
        title="ACT proposes a government with 18 ministers and 19 departments",
        summary=(
            "On 2026-09-02, ACT New Zealand announced a policy proposal for a smaller "
            "government with 18 ministers and 19 departments."),
        event_date=T0.date(), now=later)

    sealed = seal_batch_if_claimed(
        conn, second_batch.id, second_run.id, second_run.claim_token, later)

    assert list_sealed_candidate_ids(conn, sealed.id) == []
    assert get_candidate(conn, repeated.id).rejection_code == "publication_duplicate_event"
    assert get_candidate(conn, first.id) == original_candidate
    assert original_path.read_bytes() == original
    if not legacy:
        assert get_frozen_publication(conn, batch.id) == original


@pytest.mark.parametrize("related", [False, True])
async def test_new_publication_after_generation_is_seen_at_seal_without_hash_drift(context, related):
    conn, watch, root = context
    initial_run, initial_batch = claimed_batch(conn, watch)
    source = f"The {BILL} passed its final reading on 2026-09-02."
    summary = f"National said the {BILL} had passed its final reading on 2026-09-02."
    second = await generated_candidate(
        conn, watch, initial_run, initial_batch, key="second-source",
        source_title="Lifejacket requirement approved", source_text=source,
        title="National says lifejacket legislation passed", summary=summary)
    receipt = get_stage_receipt(conn, second.id, CivicAiStage.GENERATE)
    complete_civic_run(
        conn, initial_run.id, initial_run.claim_token, CivicRunStatus.FAILED, T0,
        error_code="interrupted_before_seal")
    first_run, first_batch = claimed_batch(conn, watch, T0 + timedelta(seconds=1))
    prior_text = source if related else "National announced a new road programme on 2026-09-02."
    first = await generated_candidate(
        conn, watch, first_run, first_batch, key="first-source",
        source_title="Parliament approves boating legislation" if related else "New road programme",
        source_text=prior_text,
        title="National reports final reading" if related else "National road programme",
        summary=summary if related else prior_text)
    seal_batch_if_claimed(
        conn, first_batch.id, first_run.id, first_run.claim_token, T0 + timedelta(seconds=2))
    frozen = get_frozen_publication(conn, first_batch.id)
    complete_civic_run(
        conn, first_run.id, first_run.claim_token, CivicRunStatus.COMPLETED, T0 + timedelta(seconds=2))
    second_run, second_batch = claimed_batch(conn, watch, T0 + timedelta(seconds=3))
    adopt_capacity_deferrals(
        conn, second_batch.id, second_run.id, second_run.claim_token, T0 + timedelta(seconds=3))

    sealed = seal_batch_if_claimed(
        conn, second_batch.id, second_run.id, second_run.claim_token, T0 + timedelta(seconds=4))

    assert list_sealed_candidate_ids(conn, sealed.id) == ([] if related else [second.id])
    assert get_candidate(conn, second.id).rejection_code == (
        "publication_duplicate_event" if related else None)
    assert get_candidate(conn, second.id).ai_revision == 0
    assert get_stage_receipt(conn, second.id, CivicAiStage.GENERATE) == receipt
    assert get_frozen_publication(conn, first_batch.id) == frozen
    assert get_candidate(conn, first.id).sealed_into_batch_at is not None


async def test_explicit_source_reiteration_cannot_be_serialized_as_a_first_announcement(context):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    candidate = await generated_candidate(
        conn, watch, run, batch, party="act", key="reiteration-only",
        source_title="ACT reiterates its government reform position",
        source_text="ACT reiterated its existing policy for a smaller government on 2026-09-02.",
        title="ACT announces a government reform policy",
        summary="ACT announced a policy for a smaller government on 2026-09-02.")

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert list_sealed_candidate_ids(conn, sealed.id) == []
    assert get_candidate(conn, candidate.id).rejection_code == "publication_unsupported_newness"
    assert list_publication_rejections(conn, sealed.id)[0].reason == "publication_unsupported_newness"


@pytest.mark.parametrize("bill_admitted", [False, True])
async def test_only_admitted_or_historical_events_can_suppress_an_overflow_peer(context, bill_admitted):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    summary = f"National said the {BILL} passed its final reading on 2026-09-02."

    async def bill_candidate(key):
        return await generated_candidate(
            conn, watch, run, batch, key=key, source_title=f"Boating legislation {key}",
            source_text=summary, title="National lifejacket legislation update", summary=summary)

    first = await bill_candidate("first") if bill_admitted else None
    for topic in ("road", "rail", "health", "education", "forestry", "tourism")[:5 if bill_admitted else 6]:
        text = f"National announced a new {topic} programme on 2026-09-02."
        await generated_candidate(
            conn, watch, run, batch, key=topic, source_title=f"New {topic} programme",
            source_text=text, title=f"National {topic} programme", summary=text)
    if first is None:
        first = await bill_candidate("first")
    second = await bill_candidate("second")

    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)

    assert len(list_sealed_candidate_ids(conn, sealed.id)) == 6
    if bill_admitted:
        assert get_candidate(conn, second.id).rejection_code == "publication_duplicate_event"
    else:
        assert get_candidate(conn, first.id).status is CivicCandidateStatus.DEFERRED_CAPACITY
        assert get_candidate(conn, second.id).status is CivicCandidateStatus.DEFERRED_CAPACITY
        assert list_publication_rejections(conn, sealed.id) == []


@pytest.mark.parametrize("drift", ["prompt_version", "verifier_quality", "automatic_draft"])
async def test_v4_prompts_bind_both_rule_sets_and_old_unsealed_receipts_get_real_refresh(context, drift):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    title = "National announces road programme"
    summary = "National announced a new road programme on 2026-09-02."
    candidate = await generated_candidate(
        conn, watch, run, batch, key="version-refresh", source_title=title,
        source_text=summary, title=title, summary=summary)
    snapshot = build_candidate_snapshot(
        conn, candidate_id=candidate.id, qualification=get_latest_qualification(conn, candidate.id))
    for prompt in (
        build_generation_prompt(snapshot),
        build_verification_prompt(snapshot, stored_locale_contents(conn, candidate.id)),
    ):
        assert LANGUAGE_FIDELITY_RULES in prompt
        assert NOVELTY_RULES in prompt
    assert CIVIC_PROMPT_VERSION == "civic-ai-prompts/5"
    if drift == "prompt_version":
        conn.execute(
            "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/4' WHERE candidate_id = ?",
            (candidate.id,))
    elif drift == "verifier_quality":
        conn.execute(
            "UPDATE civic_ai_stages SET model = 'gpt-5.6-sol' "
            "WHERE candidate_id = ? AND stage = 'verification'", (candidate.id,))
        conn.execute(
            "UPDATE civic_verifications SET model = 'gpt-5.6-sol' "
            "WHERE candidate_id = ?", (candidate.id,))
    else:
        conn.execute(
            "UPDATE civic_ai_stages SET model = 'auto' WHERE candidate_id = ? AND stage = 'generate'",
            (candidate.id,))
    conn.commit()
    original = list_candidate_stages(conn, candidate.id)
    seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    assert get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    if drift != "prompt_version":
        assert get_candidate(conn, candidate.id).ai_refresh_reason == "publication_verifier_policy_changed"
    complete_civic_run(conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, T0)
    later = T0 + timedelta(days=1)
    next_run, next_batch = claimed_batch(conn, watch, later)
    adopt_capacity_deferrals(
        conn, next_batch.id, next_run.id, next_run.claim_token, later)
    candidate = get_candidate(conn, candidate.id)
    qualify_civic_candidate(
        conn, run_id=next_run.id, claim_token=next_run.claim_token,
        claim=construct_candidate_claim(conn, candidate), registry=build_ownership_registry(),
        now=later, engine_version=DESTINATION_V1_ENGINE_VERSION,
        qualification_scope=constrain_qualification_to_destination_v1)
    gateway = gateway_for(title, summary)
    generated = await generate_civic_candidate(
        conn, run_id=next_run.id, claim_token=next_run.claim_token,
        candidate_id=candidate.id, gateway=gateway, now=later)
    assert generated.verified
    assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    assert count_run_ai_calls(conn, next_run.id) == 2
    assert all(receipt in list_candidate_stages(conn, candidate.id) for receipt in original)
    current_draft = get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE)
    current_verifier = get_stage_receipt(conn, candidate.id, CivicAiStage.VERIFICATION)
    assert current_verifier.model == verifier_model_for(current_draft.model)
    sealed = seal_batch_if_claimed(
        conn, next_batch.id, next_run.id, next_run.claim_token, later)
    assert list_sealed_candidate_ids(conn, sealed.id) == [candidate.id]
    assert get_candidate(conn, candidate.id).ai_revision == 1


@pytest.mark.parametrize("legacy", [False, True])
async def test_historical_model_choice_is_not_revalidated_for_frozen_or_legacy_replay(context, legacy):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    text = "National announced a new road programme on 2026-09-02."
    candidate = await generated_candidate(
        conn, watch, run, batch, key="historical-verifier",
        source_title="National road programme", source_text=text,
        title="National announces road programme", summary=text)
    seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, T0)
    spool = CivicSpool(root / "spool")
    written = spool.write_batch(conn, batch.id, T0)
    original = written.path.read_bytes()
    conn.execute(
        "UPDATE civic_ai_stages SET model = 'gpt-5.6-sol' "
        "WHERE candidate_id = ? AND stage = 'verification'", (candidate.id,))
    if legacy:
        conn.execute("DELETE FROM civic_batch_publications WHERE batch_id = ?", (batch.id,))
        conn.execute(
            "UPDATE civic_batches SET publication_schema_version = NULL WHERE id = ?", (batch.id,))
    conn.commit()

    replay = spool.write_batch(conn, batch.id, T0)

    assert replay.replayed
    assert replay.path.read_bytes() == original
    assert get_candidate(conn, candidate.id).ai_revision == 0


@pytest.mark.parametrize("prior_revision", [0, 1])
async def test_source_and_policy_refresh_reuse_one_pending_revision_before_ai(context, prior_revision):
    conn, watch, root = context
    run, batch = claimed_batch(conn, watch)
    text = "National announced a new road programme on 2026-09-02."
    candidate = await generated_candidate(
        conn, watch, run, batch, key="combined-refresh", source_title="National road programme",
        source_text=text, title="National announces road programme", summary=text)
    for revision in range(prior_revision):
        candidate = refresh_candidate_for_ai_if_claimed(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
            expected_revision=revision, reason="earlier_rollout", now=T0)
        qualify_civic_candidate(
            conn, run_id=run.id, claim_token=run.claim_token,
            claim=construct_candidate_claim(conn, candidate), registry=build_ownership_registry(),
            now=T0, engine_version=DESTINATION_V1_ENGINE_VERSION,
            qualification_scope=constrain_qualification_to_destination_v1)
        result = await generate_civic_candidate(
            conn, run_id=run.id, claim_token=run.claim_token, candidate_id=candidate.id,
            gateway=gateway_for("National announces road programme", text), now=T0)
        assert result.verified
    original = list_candidate_stages(conn, candidate.id)
    source_refresh = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=prior_revision, reason="source_capture_changed", now=T0)
    assert source_refresh.ai_revision == prior_revision + 1

    policy_refresh = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=source_refresh.ai_revision,
        reason="publication_stage_version_changed", now=T0)

    assert policy_refresh.ai_revision == prior_revision + 1
    assert policy_refresh.status is CivicCandidateStatus.DRAFTED
    assert get_latest_qualification(conn, candidate.id) is None
    assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) is None
    assert list_candidate_stages(conn, candidate.id) == original
    assert count_run_ai_calls(conn, run.id) == 2 * (prior_revision + 1)
    qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token,
        claim=construct_candidate_claim(conn, policy_refresh), registry=build_ownership_registry(),
        now=T0, engine_version=DESTINATION_V1_ENGINE_VERSION,
        qualification_scope=constrain_qualification_to_destination_v1)
    generated = await generate_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token, candidate_id=candidate.id,
        gateway=gateway_for("National announces road programme", text), now=T0)
    assert generated.verified
    assert generated.candidate.ai_revision == prior_revision + 1
    assert all(receipt in list_candidate_stages(conn, candidate.id) for receipt in original)
    changed_after_ai = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=prior_revision + 1, reason="new_input_after_ai_started", now=T0)
    assert changed_after_ai.ai_revision == prior_revision + 2
