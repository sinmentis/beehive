"""Current AI approvals cannot substitute for an actual source capture."""
import json
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import pytest

from beehive.civic.ai_gateway import (
    CIVIC_MODEL_PROVIDER, CivicAiStage, CivicModelRequest, verifier_model_for,
)
from beehive.civic.ai_pipeline import generate_civic_candidate, stored_locale_contents
from beehive.civic.candidate_parser import parse_generation_response, parse_verification_response
from beehive.civic.candidate_policy import check_candidate_content
from beehive.civic.batch_policy import CivicRunMode, published_cap
from beehive.civic.clustering import CivicDocumentCluster
from beehive.civic.destination import (
    DESTINATION_V1_ENGINE_VERSION, constrain_qualification_to_destination_v1,
)
from beehive.civic.documents import CivicDocument
from beehive.civic.evidence import qualify_candidate
from beehive.civic.evidence_refresh import EvidenceRefreshStatus, prepare_evidence_refresh
from beehive.civic.generation import (
    CIVIC_PROMPT_VERSION, CIVIC_SCHEMA_VERSION, build_candidate_snapshot,
    build_generation_prompt, build_verification_prompt, prompt_hash,
)
from beehive.civic.pipeline import (
    evidence_item_for, ingest_civic_documents, qualification_input_hash,
    qualification_output_hash, qualify_civic_candidate,
)
from beehive.civic.production import (
    PartyMediaDocumentHydrator, build_ownership_registry, build_source_profiles,
    construct_candidate_claim, persist_cluster_evidence,
)
from beehive.civic.publication import preflight_candidate
from beehive.civic.spool import CivicSpool, validate_publication_envelope
from beehive.db.civic_ai_stages import (
    count_run_ai_calls, find_recorded_stage, get_stage_receipt, output_hash_for,
    record_ai_stage_success, reserve_ai_stage,
)
from beehive.db.civic_batches import (
    adopt_capacity_deferrals, count_ai_deferrals, get_frozen_publication,
    list_candidate_capacity_deferrals, list_publication_rejections, list_sealed_candidate_ids,
    refresh_candidate_for_ai_if_claimed, seal_batch_if_claimed,
)
from beehive.db.civic_candidates import (
    add_candidate_evidence, get_candidate, list_candidate_evidence,
    mark_candidate_verified, set_candidate_status,
)
from beehive.db.civic_evidence_captures import (
    CivicSourceRefreshBlocked, apply_evidence_refresh_if_claimed, candidate_source_plan,
    complete_hydration_if_claimed, count_run_hydrations,
    list_evidence_capture_history, list_evidence_capture_inputs,
    reserve_evidence_refresh_if_claimed, reserve_hydration_if_claimed,
)
from beehive.db.civic_qualifications import get_latest_qualification, record_qualification
from beehive.db.civic_runs import complete_civic_run
from beehive.db.civic_verifications import record_verification
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (
    CivicCandidateStatus, CivicEvidenceStance, CivicEvidenceTier,
    CivicRunStatus, CivicVerificationVerdict, require_candidate_transition,
)
from tests.civic.civic_ai_fakes import (
    FakeCivicGateway, fenced, full_entry, generation_payload, verification_payload,
)
from tests.civic.civic_stubs import transport_for
from tests.civic.civic_worker_support import create_partymap_watch
from tests.civic.test_publication_novelty import claimed_batch, gateway_for

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
LASTMOD = datetime(2026, 9, 4, 8, 41, tzinfo=timezone.utc)
DRAFT_MODEL = "gpt-6-astra"
TOPICS = ("school", "road", "hospital", "transport", "water")
SOURCE_DAYS = (date(2026, 9, 2), date(2026, 9, 3))
AUDIT_TABLES = (
    "civic_candidate_evidence", "civic_qualifications", "civic_ai_stages", "civic_verifications",
)


@pytest.fixture
def context(tmp_path):
    conn = connect(str(tmp_path / "source-contract.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="policy")
    run, batch = claimed_batch(conn, watch, NOW)
    yield conn, watch, run, batch, tmp_path
    conn.close()


def document_for(topic):
    return CivicDocument(
        source_key="national", external_key=f"source-contract-{topic}",
        canonical_url=f"https://www.national.org.nz/news/source-contract-{topic}",
        title=f"National announces a {topic} programme",
        excerpt=f"National announced a {topic} programme.",
        publisher=build_source_profiles()["national"].publisher,
        tier=CivicEvidenceTier.PARTY_PRIMARY, first_seen_at=NOW, published_at=LASTMOD,
    )


def hydrator_for(documents, source_days):
    pages = {}
    for document, day in zip(documents, source_days, strict=True):
        body = document.excerpt + (
            " The proposal would combine the work into a public delivery plan. "
            "National said it would consult local communities and service providers before "
            "setting priorities. The party would publish a timetable after that consultation. "
            "The announcement does not claim that implementation or delivery has already begun."
        )
        pages[document.canonical_url] = (
            '<html><head><meta property="article:published_time" '
            f'content="{day.isoformat()}T09:00:00+12:00"></head><body>'
            f"<article><h1>{escape(document.title)}</h1><p>{escape(body)}</p>"
            "</article></body></html>",
            "text/html",
        )
    return PartyMediaDocumentHydrator(transport_for(pages))


def audit_rows(conn, candidate_ids):
    placeholders = ",".join("?" for _ in candidate_ids)
    return {
        table: tuple(tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE candidate_id IN ({placeholders}) ORDER BY id",
            candidate_ids))
        for table in AUDIT_TABLES
    }


def assert_original_rows_unchanged(conn, originals):
    for table, expected in originals.items():
        placeholders = ",".join("?" for _ in expected)
        actual = tuple(tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE id IN ({placeholders}) ORDER BY id",
            tuple(row[0] for row in expected)))
        assert actual == expected, table


async def seed_current_legacy_approval(conn, run, document):
    """Create pre-contract stored approvals, without manufacturing a source certificate."""
    ingested = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.claim_token, documents=(document,), now=NOW)
    candidate = ingested.created_candidates[0]
    profile = build_source_profiles()["national"]
    add_candidate_evidence(
        conn, candidate.id, document.source_key, document.dedupe_key, document.canonical_url,
        document.title, document.tier, CivicEvidenceStance.SUPPORTING, NOW,
        publisher=document.publisher, publisher_id=profile.publisher_id,
        source_kind="party-primary", party_slug="national", subject_id="national",
        claim_category="party-timeline-event", event_date=LASTMOD.date(),
        canonical_url=document.canonical_url, excerpt=document.excerpt,
        published_at=LASTMOD, accessible_full_text=True,
    )
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
    candidate = refresh_candidate_for_ai_if_claimed(
        conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
        expected_revision=0, reason="previous_deployment_refresh", now=NOW)
    claim = construct_candidate_claim(conn, candidate)
    registry = build_ownership_registry()
    items = [evidence_item_for(row) for row in list_candidate_evidence(conn, candidate.id)]
    qualification = constrain_qualification_to_destination_v1(
        claim, qualify_candidate(claim, items, registry=registry))
    assert qualification.is_publishable
    conn.execute("BEGIN IMMEDIATE")
    record = record_qualification(
        conn, candidate_id=candidate.id, run_id=run.id, qualification=qualification,
        engine_version=DESTINATION_V1_ENGINE_VERSION,
        input_hash=qualification_input_hash(
            claim, items, registry=registry, dedup_fingerprints=(), dedup_urls=(),
            engine_version=DESTINATION_V1_ENGINE_VERSION),
        output_hash=qualification_output_hash(qualification), research_attempts_used=0,
        now=NOW, record_kind=claim.record_kind)
    conn.commit()
    set_candidate_status(conn, candidate.id, CivicCandidateStatus.VERIFYING, NOW)
    snapshot = build_candidate_snapshot(conn, candidate_id=candidate.id, qualification=record)
    generation = generation_payload(entry=full_entry(**{
        locale: {
            "title": document.title, "summary": document.excerpt, "claim_text": document.excerpt,
            "event_date": LASTMOD.date().isoformat(), "policy_status": "announced",
            "entities": ["national"],
        } for locale in ("zh", "en", "hi", "tl")
    }))
    verification = verification_payload()
    gateway = FakeCivicGateway(responses={
        CivicAiStage.GENERATE: fenced(generation),
        CivicAiStage.VERIFICATION: fenced(verification),
    })
    contents = None
    for stage, model, payload in (
        (CivicAiStage.GENERATE, DRAFT_MODEL, generation),
        (CivicAiStage.VERIFICATION, verifier_model_for(DRAFT_MODEL), verification),
    ):
        prompt = (build_generation_prompt(snapshot) if stage is CivicAiStage.GENERATE
                  else build_verification_prompt(snapshot, contents))
        reservation = reserve_ai_stage(
            conn, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
            stage=stage, provider=CIVIC_MODEL_PROVIDER, model=model,
            prompt_version=CIVIC_PROMPT_VERSION, prompt_hash=prompt_hash(prompt),
            schema_version=CIVIC_SCHEMA_VERSION, input_hash=snapshot.snapshot_hash, now=NOW)
        assert reservation is not None and not conn.in_transaction
        response = await gateway.complete(CivicModelRequest(
            stage=stage, prompt=prompt, model=model, timeout=1))
        if stage is CivicAiStage.GENERATE:
            contents = parse_generation_response(response.text).contents
            assert check_candidate_content(snapshot, contents)
        else:
            assert parse_verification_response(response.text).is_pass
        assert record_ai_stage_success(
            conn, reservation=reservation, output=payload, now=NOW) is not None
    record_verification(
        conn, candidate.id, CivicVerificationVerdict.SUPPORTED,
        verifier_model_for(DRAFT_MODEL), NOW)
    return mark_candidate_verified(conn, candidate.id, NOW)


def assert_current_approval_bindings(conn, candidate_id):
    candidate = get_candidate(conn, candidate_id)
    snapshot = build_candidate_snapshot(
        conn, candidate_id=candidate_id,
        qualification=get_latest_qualification(conn, candidate_id))
    contents = stored_locale_contents(conn, candidate_id)
    assert check_candidate_content(snapshot, contents)
    for stage, model, prompt in (
        (CivicAiStage.GENERATE, DRAFT_MODEL, build_generation_prompt(snapshot)),
        (CivicAiStage.VERIFICATION, verifier_model_for(DRAFT_MODEL),
         build_verification_prompt(snapshot, contents)),
    ):
        receipt = find_recorded_stage(
            conn, candidate_id=candidate_id, stage=stage, input_hash=snapshot.snapshot_hash,
            provider=CIVIC_MODEL_PROVIDER, model=model, prompt_version=CIVIC_PROMPT_VERSION,
            prompt_hash=prompt_hash(prompt), schema_version=CIVIC_SCHEMA_VERSION)
        assert receipt is not None and receipt.ai_revision == candidate.ai_revision
        assert receipt.output_hash == output_hash_for(receipt.output)


async def qualify_and_generate(conn, run, candidate_id, document, day, now):
    candidate = get_candidate(conn, candidate_id)
    qualified = qualify_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token,
        claim=construct_candidate_claim(conn, candidate), registry=build_ownership_registry(),
        now=now, engine_version=DESTINATION_V1_ENGINE_VERSION,
        qualification_scope=constrain_qualification_to_destination_v1)
    assert qualified.qualification.is_publishable
    gateway = gateway_for(document.title, document.excerpt, event_date=day)
    result = await generate_civic_candidate(
        conn, run_id=run.id, claim_token=run.claim_token, candidate_id=candidate_id,
        gateway=gateway, model=DRAFT_MODEL, now=now)
    assert result.verified, result.rejection_code
    assert result.ai_calls_made == 2 and result.replayed_stages == ()
    assert [call.model for call in gateway.calls] == [
        DRAFT_MODEL, verifier_model_for(DRAFT_MODEL)]
    return result.candidate


def adopt_legacy_capacity(conn, watch, old_run, old_batch, candidates, now):
    # These are historical capacity decisions. Current seal must not manufacture them by
    # admitting an untrusted source first, so seed that legacy state rather than bypass its gate.
    conn.execute("BEGIN IMMEDIATE")
    for candidate in candidates:
        require_candidate_transition(
            candidate.status, CivicCandidateStatus.DEFERRED_CAPACITY)
        conn.execute(
            "UPDATE civic_candidates SET status='deferred_capacity' WHERE id=?", (candidate.id,))
        conn.execute(
            "INSERT INTO civic_capacity_deferrals (watch_id, candidate_id, from_run_id, "
            "from_batch_id, published_cap, run_mode, reason, deferred_at) "
            "VALUES (?, ?, ?, ?, ?, 'normal', 'batch_cap_reached', ?)",
            (watch.id, candidate.id, old_run.id, old_batch.id,
             published_cap(CivicRunMode.NORMAL), NOW.isoformat()))
    conn.commit()
    sealed = seal_batch_if_claimed(
        conn, old_batch.id, old_run.id, old_run.claim_token, NOW)
    assert list_sealed_candidate_ids(conn, sealed.id) == []
    for candidate in candidates:
        held = get_candidate(conn, candidate.id)
        assert held.status is CivicCandidateStatus.DEFERRED_CAPACITY
        assert held.batch_id == sealed.id and held.sealed_into_batch_at is None
        assert held.ai_revision == 1
    complete_civic_run(
        conn, old_run.id, old_run.claim_token, CivicRunStatus.COMPLETED, NOW)
    run, batch = claimed_batch(conn, watch, now)
    adoption = adopt_capacity_deferrals(
        conn, batch.id, run.id, run.claim_token, now)
    assert adoption.adopted_candidate_ids == tuple(candidate.id for candidate in candidates)
    for candidate in candidates:
        adopted = get_candidate(conn, candidate.id)
        assert adopted.status is CivicCandidateStatus.VERIFIED
        assert adopted.batch_id == batch.id and adopted.sealed_into_batch_at is None
        assert adopted.ai_revision == 1
    return run, batch


async def test_current_ai_receipts_cannot_publish_legacy_sources_until_real_capture_refresh(context):
    conn, watch, run, batch, root = context
    documents = tuple(document_for(topic) for topic in TOPICS)
    source_days = tuple(SOURCE_DAYS[index % 2] for index in range(len(documents)))
    candidates = [
        await seed_current_legacy_approval(conn, run, document) for document in documents]
    candidate_ids = tuple(candidate.id for candidate in candidates)
    originals = audit_rows(conn, candidate_ids)
    legacy_run, legacy_batch = run, batch
    active_now = NOW + timedelta(days=1)
    run, batch = adopt_legacy_capacity(
        conn, watch, legacy_run, legacy_batch, candidates, active_now)
    legacy_frozen = get_frozen_publication(conn, legacy_batch.id)
    for candidate in candidates:
        assert_current_approval_bindings(conn, candidate.id)
        assert candidate.ai_revision == 1
        assert all(capture.source_metadata == {} for capture in
                   list_evidence_capture_inputs(conn, candidate.id))
        with pytest.raises(CivicSourceRefreshBlocked):
            preflight_candidate(conn, candidate.id)

    healthy_source = document_for("library")
    healthy_document = healthy_source
    fresh_reservation = reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token=run.claim_token, work_key="fresh-library",
        units=1, documents=(healthy_document,), now=active_now)
    healthy_document = hydrator_for((healthy_document,), (SOURCE_DAYS[1],))(
        healthy_document, active_now).document
    assert complete_hydration_if_claimed(
        conn, reservation=fresh_reservation, documents=(healthy_document,),
        now=active_now) is not None
    ingested = ingest_civic_documents(
        conn, run_id=run.id, claim_token=run.claim_token,
        documents=(healthy_document,), now=active_now)
    healthy = ingested.created_candidates[0]
    persist_cluster_evidence(
        conn, run_id=run.id, claim_token=run.claim_token, candidate=healthy,
        cluster=CivicDocumentCluster(
            cluster_key=healthy_document.dedupe_key, documents=(healthy_document,)),
        watch=watch, subject_id="national", profiles=build_source_profiles(), now=active_now)
    await qualify_and_generate(
        conn, run, healthy.id, healthy_source, SOURCE_DAYS[1], active_now)
    sealed = seal_batch_if_claimed(conn, batch.id, run.id, run.claim_token, active_now)
    assert list_sealed_candidate_ids(conn, sealed.id) == [healthy.id]
    assert list_publication_rejections(conn, sealed.id) == []
    assert count_run_ai_calls(conn, legacy_run.id) == 10
    assert count_run_ai_calls(conn, run.id) == 2
    assert count_run_hydrations(conn, run.id) == 1
    assert_original_rows_unchanged(conn, originals)
    for candidate in candidates:
        held = get_candidate(conn, candidate.id)
        assert held.status is CivicCandidateStatus.DEFERRED_AI
        assert held.ai_refresh_reason.startswith("source_refresh:")
        assert held.ai_revision == 1 and held.rejection_code is None
        assert count_ai_deferrals(conn, candidate.id) == 0
        assert list_candidate_capacity_deferrals(conn, candidate.id)[-1].reason == "source_refresh_required"
    spool = CivicSpool(root / "spool")
    frozen_before = spool.write_batch(conn, sealed.id, active_now).path.read_bytes()
    complete_civic_run(conn, run.id, run.claim_token, CivicRunStatus.COMPLETED, active_now)

    later = active_now + timedelta(days=1)
    next_run, next_batch = claimed_batch(conn, watch, later)
    adoption = adopt_capacity_deferrals(
        conn, next_batch.id, next_run.id, next_run.claim_token, later)
    assert adoption.adopted_candidate_ids == candidate_ids
    hydrator = hydrator_for(documents, source_days)

    async def hydrate(document, now):
        assert not conn.in_transaction
        return hydrator(document, now)

    for candidate, document, day in zip(candidates, documents, source_days, strict=True):
        assert_current_approval_bindings(conn, candidate.id)
        blocked_gateway = FakeCivicGateway()
        calls_before = count_run_ai_calls(conn, next_run.id)
        with pytest.raises(CivicSourceRefreshBlocked):
            await generate_civic_candidate(
                conn, run_id=next_run.id, claim_token=next_run.claim_token,
                candidate_id=candidate.id, gateway=blocked_gateway,
                model=DRAFT_MODEL, now=later)
        assert blocked_gateway.calls == []
        assert count_run_ai_calls(conn, next_run.id) == calls_before
        plan = candidate_source_plan(conn, candidate.id)
        assert plan.status is EvidenceRefreshStatus.PLANNED and plan.expected_ai_revision == 1
        reservation = reserve_evidence_refresh_if_claimed(
            conn, plan=plan, claim_token=next_run.claim_token, now=later)
        prepared = await prepare_evidence_refresh(
            plan, hydrate=hydrate, now_fn=lambda: later, deadline_at=next_run.deadline_at)
        applied = apply_evidence_refresh_if_claimed(
            conn, reservation=reservation, prepared=prepared, now=later)
        assert applied.status == "applied" and applied.ai_revision == 2
        assert get_latest_qualification(conn, candidate.id) is None
        assert get_stage_receipt(conn, candidate.id, CivicAiStage.GENERATE) is None
        evidence = list_candidate_evidence(conn, candidate.id)[0]
        assert evidence.event_date == day
        assert evidence.published_at.astimezone(ZoneInfo("Pacific/Auckland")).date() == day
        assert list_evidence_capture_history(conn, evidence.id)[-1].evidence == evidence
        metadata = list_evidence_capture_inputs(conn, candidate.id)[0].source_metadata
        assert metadata["source_capture"]["version"] == 1
        assert metadata["source_capture"]["extraction"]["quality"] == "complete"
        assert metadata["source_capture"]["extraction"]["full_text_chars"] >= 200
        await qualify_and_generate(conn, next_run, candidate.id, document, day, later)
        assert_current_approval_bindings(conn, candidate.id)
        assert get_candidate(conn, candidate.id).ai_revision == 2

    refreshed = seal_batch_if_claimed(
        conn, next_batch.id, next_run.id, next_run.claim_token, later)
    assert list_sealed_candidate_ids(conn, refreshed.id) == list(candidate_ids)
    assert count_run_ai_calls(conn, next_run.id) == 10
    assert count_run_hydrations(conn, next_run.id) == 5
    assert_original_rows_unchanged(conn, originals)
    written = spool.write_batch(conn, refreshed.id, later)
    refreshed_bytes = written.path.read_bytes()
    envelope = json.loads(refreshed_bytes)
    validate_publication_envelope(envelope)
    for item, day in zip(envelope["items"], source_days, strict=True):
        assert item["event_date"] == day.isoformat()
        assert datetime.fromisoformat(item["citations"][0]["published_at"]).astimezone(
            ZoneInfo("Pacific/Auckland")).date() == day
    conn.close()
    restarted = connect(str(root / "source-contract.db"))
    try:
        init_schema(restarted)
        assert spool.write_batch(restarted, refreshed.id, later).path.read_bytes() == refreshed_bytes
        assert spool.write_batch(restarted, sealed.id, later).path.read_bytes() == frozen_before
        assert get_frozen_publication(restarted, sealed.id) == frozen_before
        assert get_frozen_publication(restarted, legacy_batch.id) == legacy_frozen
    finally:
        restarted.close()


@pytest.mark.parametrize("race", ["seal", "fingerprint"])
async def test_current_approval_capture_install_refuses_concurrent_seal_or_changed_inputs(context, race):
    conn, watch, run, batch, root = context
    document = document_for("school")
    candidate = await seed_current_legacy_approval(conn, run, document)
    originals = audit_rows(conn, (candidate.id,))
    legacy_run = run
    now = NOW + timedelta(days=1)
    run, batch = adopt_legacy_capacity(conn, watch, run, batch, (candidate,), now)
    plan = candidate_source_plan(conn, candidate.id)
    reservation = reserve_evidence_refresh_if_claimed(
        conn, plan=plan, claim_token=run.claim_token, now=now)
    hydrator = hydrator_for((document,), (SOURCE_DAYS[0],))

    async def hydrate(document, now):
        return hydrator(document, now)

    prepared = await prepare_evidence_refresh(
        plan, hydrate=hydrate, now_fn=lambda: now, deadline_at=run.deadline_at)
    other = connect(str(root / "source-contract.db"))
    try:
        if race == "seal":
            sealed = seal_batch_if_claimed(other, batch.id, run.id, run.claim_token, now)
            assert list_sealed_candidate_ids(other, sealed.id) == []
            result = apply_evidence_refresh_if_claimed(
                conn, reservation=reservation, prepared=prepared, now=now)
            assert result is None
            assert get_candidate(conn, candidate.id).ai_revision == 1
        else:
            refresh_candidate_for_ai_if_claimed(
                other, candidate_id=candidate.id, run_id=run.id, claim_token=run.claim_token,
                expected_revision=1, reason="concurrent_policy_refresh", now=now)
            add_candidate_evidence(
                other, candidate.id, "national", "national:additional-source",
                document.canonical_url + "-additional", "Additional source",
                CivicEvidenceTier.PARTY_PRIMARY, CivicEvidenceStance.CORROBORATING, now,
                publisher=document.publisher)
            result = apply_evidence_refresh_if_claimed(
                conn, reservation=reservation, prepared=prepared, now=now)
            assert result.status == "stale" and result.reason == "source_refresh_conflict"
            assert get_candidate(conn, candidate.id).ai_revision == 2
        assert list_evidence_capture_history(
            conn, list_candidate_evidence(conn, candidate.id)[0].id) == []
        assert count_run_hydrations(conn, run.id) == 1
        assert count_run_ai_calls(conn, legacy_run.id) == 2
        assert count_run_ai_calls(conn, run.id) == 0
        assert_original_rows_unchanged(conn, originals)
    finally:
        other.close()


@pytest.mark.parametrize("expiry", ["before_preflight", "before_commit"])
async def test_expired_seal_cannot_commit_a_source_hold_or_frozen_artifact(context, expiry):
    from beehive.db.civic_batches import get_civic_batch
    from beehive.domain.civic import CivicBatchStatus

    conn, watch, run, batch, root = context
    candidate = await seed_current_legacy_approval(conn, run, document_for("school"))
    originals = audit_rows(conn, (candidate.id,))
    expired = NOW + timedelta(seconds=600)
    readings = iter((expired,) if expiry == "before_preflight" else (NOW, expired))
    result = seal_batch_if_claimed(
        conn, batch.id, run.id, run.claim_token, NOW, now_fn=lambda: next(readings, expired))
    assert result is None
    assert get_civic_batch(conn, batch.id).status is CivicBatchStatus.BUILDING
    assert get_candidate(conn, candidate.id) == candidate
    assert list_candidate_capacity_deferrals(conn, candidate.id) == []
    assert list_sealed_candidate_ids(conn, batch.id) == []
    assert get_frozen_publication(conn, batch.id) is None
    assert_original_rows_unchanged(conn, originals)
