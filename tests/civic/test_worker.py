import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from beehive.civic.ai_gateway import CivicAiStage
from beehive.civic.ai_pipeline import generate_civic_candidate
from beehive.civic.generation import CIVIC_PROMPT_VERSION
from beehive.civic.documents import CivicDocument, CivicFetchResult, CivicSourceHealth
from beehive.civic.destination import (
    DESTINATION_V1_ENGINE_VERSION,
    constrain_qualification_to_destination_v1,
)
from beehive.civic.pipeline import ingest_civic_documents, qualify_civic_candidate
from beehive.civic.production import (
    CivicHydrationResult,
    CivicHydrationStatus,
    PartyMediaDocumentHydrator,
    build_ownership_registry,
    build_source_profiles,
    construct_candidate_claim,
    persist_cluster_evidence,
    resolve_cluster_party_subject,
)
from beehive.civic.reasons import CivicPolicyReason
from beehive.civic.scheduling import schedule_due_civic_runs
from beehive.civic.spool import CivicSpool, validate_publication_envelope
from beehive.civic.worker import CivicRunExecutionStatus, CivicWorker
from beehive.connectors.civic_sources import get_source
from beehive.db.civic_batches import (
    get_civic_batch,
    get_or_create_batch_if_claimed,
    defer_candidate_for_ai,
    get_frozen_publication,
    list_publication_rejections,
    list_sealed_candidate_ids,
    seal_batch_if_claimed,
)
from beehive.db.civic_candidates import (
    add_candidate_evidence,
    get_candidate,
    list_candidate_evidence,
    list_run_candidates,
)
from beehive.db.civic_ai_stages import (
    count_run_ai_calls,
    get_stage_receipt,
    list_candidate_stages,
    output_hash_for,
)
from beehive.db.civic_consumed_items import list_consumed_item_keys
from beehive.db.civic_notifications import list_sendable_notifications
from beehive.db.civic_qualifications import get_latest_qualification, list_qualifications
from beehive.db.civic_verifications import list_research_attempts, list_verifications
from beehive.db.civic_runs import (
    claim_civic_run,
    complete_civic_run,
    enqueue_civic_run,
    get_civic_run,
    recover_expired_civic_runs,
)
from beehive.db.connection import connect, init_schema
from beehive.db.civic_vocabulary import get_published_vocabulary_version
from beehive.domain.civic import (
    CivicBatchStatus,
    CivicCandidateStatus,
    CivicEvidenceStance,
    CivicEvidenceTier,
    CivicRunStatus,
)
from tests.civic.civic_ai_fakes import fenced, full_entry, generation_payload, verification_payload
from tests.civic.civic_stubs import transport_for
from tests.civic.civic_worker_support import (
    MutableClock,
    SOURCE_ENTRY,
    T0,
    create_partymap_watch,
    housing_fetch_result,
    passing_green_gateway,
    with_article_metadata,
    with_official_feed_metadata,
)

ELECTORAL_ENTRY = get_source("nz_electoral_commission")
GREEN_ENTRY = get_source("green")
STUFF_ENTRY = get_source("stuff")


class AdvancingGateway:
    def __init__(
        self,
        delegate,
        clock,
        *,
        steps: int,
        seconds_per_step: int,
        real_delay: float = 0.006,
    ):
        self.delegate = delegate
        self.clock = clock
        self.steps = steps
        self.seconds_per_step = seconds_per_step
        self.real_delay = real_delay
        self.cancelled = False

    async def complete(self, request):
        try:
            for _ in range(self.steps):
                self.clock.now += timedelta(seconds=self.seconds_per_step)
                await asyncio.sleep(self.real_delay)
            return await self.delegate.complete(request)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_source_failure_is_isolated_recorded_and_other_source_completes(tmp_path):
    conn = connect(str(tmp_path / "isolated.db"))
    init_schema(conn)
    create_partymap_watch(conn)

    async def fetch(entry, _now):
        if entry.source_key == ELECTORAL_ENTRY.source_key:
            raise RuntimeError("fixture source unavailable")
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(ELECTORAL_ENTRY, SOURCE_ENTRY),
        source_executor=fetch,
        now_fn=lambda: T0,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert [failure.source_key for failure in execution.source_failures] == [
        ELECTORAL_ENTRY.source_key]
    notifications = list_sendable_notifications(conn, T0)
    assert len(notifications) == 1
    assert ELECTORAL_ENTRY.source_key in notifications[0].body


async def test_empty_day_seals_and_completes_cleanly(tmp_path):
    conn = connect(str(tmp_path / "empty.db"))
    init_schema(conn)
    create_partymap_watch(conn)

    async def fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(),
            health=CivicSourceHealth.EMPTY,
            fetched_at=T0,
        )

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    batch = get_civic_batch(conn, execution.batch_id)
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert batch.status is CivicBatchStatus.SEALED
    assert batch.candidate_count == 0
    assert list_run_candidates(conn, execution.run_id) == []


@pytest.mark.parametrize("listing_excerpt", ["", "A short RSS housing summary."])
async def test_metadata_only_party_document_is_hydrated_before_qualification(
    tmp_path, listing_excerpt,
):
    conn = connect(str(tmp_path / "party-hydration.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    document = CivicDocument(
        source_key=GREEN_ENTRY.source_key,
        external_key="green-housing-release",
        canonical_url="https://www.greens.org.nz/green-housing-release",
        title="Green Party housing policy release",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher=GREEN_ENTRY.publisher,
        first_seen_at=T0,
        published_at=T0,
        excerpt=listing_excerpt,
    )
    async def fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(document,),
            health=CivicSourceHealth.OK,
            fetched_at=T0,
        )

    def hydrate(item, _now):
        page = (
            "<html><body><article><h1>Green Party housing policy release</h1>"
            f'<time datetime="{T0.isoformat()}">1 August 2026</time>'
            "<p>The Green Party released a detailed housing policy on 2026-08-01. "
            "The publication explains the proposal and who it would affect. "
            "It describes the consultation process and the next steps without claiming "
            "that any proposed changes have already taken effect.</p></article></body></html>"
        )
        return PartyMediaDocumentHydrator(
            transport_for({item.canonical_url: (page, "text/html")}),
        )(item, _now)

    gateway = passing_green_gateway()
    text = "The Green Party announced a housing policy on 2026-08-01."
    gateway.responses[CivicAiStage.GENERATE] = fenced(generation_payload(
        entry=full_entry(**{
            locale: {
                "title": "Green Party housing policy announcement",
                "summary": text, "claim_text": text, "policy_status": "announced",
                "entities": ["green"],
            }
            for locale in ("zh", "en", "hi", "tl")
        })))
    worker = CivicWorker(
        conn,
        gateway,
        source_entries=(GREEN_ENTRY,),
        source_executor=fetch,
        evidence_hydrator=hydrate,
        now_fn=lambda: T0,
    )

    execution = (await worker.run_due_cycle()).runs[0]
    candidate = list_run_candidates(conn, execution.run_id)[0]
    evidence = list_candidate_evidence(conn, candidate.id)

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert get_civic_batch(conn, execution.batch_id).candidate_count == 1
    assert evidence[0].accessible_full_text is True
    assert "detailed housing policy" in evidence[0].excerpt
    assert "detailed housing policy" in candidate.statement
    assert len(gateway.calls) == 2


async def test_failed_party_hydration_stays_unconsumed_for_a_later_run(tmp_path):
    conn = connect(str(tmp_path / "party-hydration-retry.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    party_document = CivicDocument(
        source_key=GREEN_ENTRY.source_key,
        external_key="green-housing-release",
        canonical_url="https://www.greens.org.nz/green-housing-release",
        title="Green Party housing policy release",
        tier=CivicEvidenceTier.PARTY_PRIMARY,
        publisher=GREEN_ENTRY.publisher,
        first_seen_at=T0,
        published_at=T0,
    )
    media_document = CivicDocument(
        source_key=STUFF_ENTRY.source_key,
        external_key="green-housing-release-report",
        canonical_url="https://www.stuff.co.nz/politics/green-housing-release-report",
        title=party_document.title,
        tier=CivicEvidenceTier.REPUTABLE_MEDIA,
        publisher=STUFF_ENTRY.publisher,
        first_seen_at=T0,
        published_at=T0,
        excerpt="Stuff reported the Green Party housing policy release.",
    )

    async def fetch(entry, _now):
        document = (
            party_document
            if entry.source_key == GREEN_ENTRY.source_key
            else media_document
        )
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(document,),
            health=CivicSourceHealth.OK,
            fetched_at=T0,
        )

    def unavailable(item, _now):
        return CivicHydrationResult(
            document=item,
            status=CivicHydrationStatus.UNAVAILABLE,
            error_code="detail_fetch_failed",
        )

    gateway = passing_green_gateway()
    worker = CivicWorker(
        conn,
        gateway,
        source_entries=(GREEN_ENTRY, STUFF_ENTRY),
        source_executor=fetch,
        evidence_hydrator=unavailable,
        now_fn=lambda: T0,
    )

    execution = (await worker.run_due_cycle()).runs[0]

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert list_run_candidates(conn, execution.run_id) == []
    assert list_consumed_item_keys(conn, watch.id) == []
    assert gateway.calls == []
    notifications = list_sendable_notifications(conn, T0)
    assert [item.kind for item in notifications] == [
        "civic_detail_hydration_deferred"
    ]


async def test_a_refused_detail_host_is_not_retried_within_the_run(tmp_path):
    conn = connect(str(tmp_path / "party-hydration-refused.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    titles = (
        "Green Party housing policy release",
        "Rent caps for every tenant in new housing bill",
        "Warrant of fitness rules for rental housing stock",
    )
    documents = tuple(
        CivicDocument(
            source_key=GREEN_ENTRY.source_key,
            external_key=f"green-release-{index}",
            canonical_url=f"https://www.greens.org.nz/green-release-{index}",
            title=title,
            tier=CivicEvidenceTier.PARTY_PRIMARY,
            publisher=GREEN_ENTRY.publisher,
            first_seen_at=T0,
            published_at=T0,
        )
        for index, title in enumerate(titles)
    )

    async def fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=documents,
            health=CivicSourceHealth.OK, fetched_at=T0)

    calls = []

    def refused(item, _now):
        calls.append(item.external_key)
        return CivicHydrationResult(
            document=item, status=CivicHydrationStatus.UNAVAILABLE,
            error_code="detail_fetch_access_denied")

    worker = CivicWorker(
        conn, passing_green_gateway(), source_entries=(GREEN_ENTRY,),
        source_executor=fetch, evidence_hydrator=refused, now_fn=lambda: T0)

    execution = (await worker.run_due_cycle()).runs[0]

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert len(calls) == 1
    assert list_run_candidates(conn, execution.run_id) == []


@pytest.mark.parametrize("has_dated_source", [True, False])
async def test_hydration_date_requirement_is_cluster_local(tmp_path, has_dated_source):
    conn = connect(str(tmp_path / "cluster-source-dates.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    run = enqueue_civic_run(conn, watch.id, T0, T0)
    lease = claim_civic_run(conn, run.id, T0, 60, 600)
    batch = get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, T0)
    documents = []
    routes = {}
    for index in range(2):
        url = f"https://www.greens.org.nz/housing-consultation-{index}"
        documents.append(CivicDocument(
            source_key="green", external_key=url, canonical_url=url,
            title="Green Party housing consultation", tier=CivicEvidenceTier.PARTY_PRIMARY,
            publisher=GREEN_ENTRY.publisher, first_seen_at=T0,
        ))
        date_markup = (
            f'<time datetime="{T0.isoformat()}">1 August 2026</time>'
            if has_dated_source and index == 0 else ""
        )
        page = (
            "<html><body><article><h1>Green Party housing consultation</h1>"
            + date_markup
            + "<p>The Green Party has announced a consultation on its housing proposal. "
            "This synthetic release explains the scope of the proposal and the next steps. "
            "It describes who can make a submission and makes clear that the proposal "
            "has not yet been implemented.</p></article></body></html>"
        )
        routes[url] = (page, "text/html")
    transport = transport_for(routes)
    worker = CivicWorker(
        conn, passing_green_gateway(), source_entries=(GREEN_ENTRY,),
        evidence_hydrator=PartyMediaDocumentHydrator(transport), now_fn=lambda: T0,
    )
    clustering, _, deferred, _ = await worker._prepare_candidate_documents(
        documents, watch_id=watch.id, run_id=run.id, batch_id=batch.id,
        claim_token=lease.run.claim_token,
    )
    assert len(transport.recorded.calls) == 2
    assert len(clustering.clusters) == int(has_dated_source)
    assert deferred == int(not has_dated_source)
    assert list_consumed_item_keys(conn, watch.id) == []
    if has_dated_source:
        assert sum(
            document.published_at is not None
            for document in clustering.clusters[0].documents
        ) == 1


async def test_long_gather_renews_lease_until_source_returns(tmp_path):
    conn = connect(str(tmp_path / "slow-gather.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    clock = MutableClock()

    async def fetch(_entry, _now):
        for _ in range(4):
            clock.now += timedelta(seconds=100)
            await asyncio.sleep(0.006)
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=clock,
        heartbeat_interval_seconds=0.002,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert get_civic_run(conn, execution.run_id).status is CivicRunStatus.COMPLETED
    assert clock.now == T0 + timedelta(seconds=400)


async def test_all_source_failures_fail_instead_of_looking_like_empty_day(tmp_path):
    conn = connect(str(tmp_path / "failed.db"))
    init_schema(conn)
    create_partymap_watch(conn)

    async def fetch(_entry, _now):
        raise RuntimeError("source failed")

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.FAILED
    assert execution.error_code == "all_civic_sources_failed"
    assert get_civic_run(conn, execution.run_id).status is CivicRunStatus.FAILED


async def test_under_evidenced_candidate_spends_one_bounded_research_pass(tmp_path):
    conn = connect(str(tmp_path / "research.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    clock = MutableClock()
    document = CivicDocument(
        source_key="stuff",
        external_key="housing-report",
        canonical_url="https://www.stuff.co.nz/politics/housing-report",
        title="Green Party housing announcement reported",
        tier=CivicEvidenceTier.REPUTABLE_MEDIA,
        publisher="Stuff",
        first_seen_at=T0,
        published_at=T0,
        excerpt="A Green Party housing announcement was reported on 2026-08-01.",
    )
    document = with_article_metadata(document)

    async def fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(document,),
            health=CivicSourceHealth.OK,
            fetched_at=T0,
        )

    async def research(_claim, _now):
        for _ in range(4):
            clock.now += timedelta(seconds=100)
            await asyncio.sleep(0.006)
        return ()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(STUFF_ENTRY,),
        source_executor=fetch,
        researcher=research,
        now_fn=clock,
        heartbeat_interval_seconds=0.002,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    candidate = list_run_candidates(conn, execution.run_id)[0]
    attempts = list_research_attempts(conn, candidate.id)
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert len(attempts) == 1
    assert attempts[0].outcome.value == "unresolved"
    assert candidate.status.value == "rejected"
    assert clock.now == T0 + timedelta(seconds=400)


async def test_global_watch_label_never_becomes_candidate_party(tmp_path):
    conn = connect(str(tmp_path / "resolved-subject.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)

    async def fetch(_entry, _now):
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    candidate = list_run_candidates(conn, execution.run_id)[0]
    evidence = list_candidate_evidence(conn, candidate.id)
    assert watch.subject == "partymap"
    assert {item.subject_id for item in evidence} == {"green"}
    assert get_civic_batch(conn, execution.batch_id).candidate_count == 1


async def test_multi_candidate_model_work_renews_lease_beyond_300_seconds(tmp_path):
    conn = connect(str(tmp_path / "slow-models.db"))
    init_schema(conn)
    create_partymap_watch(conn, term="Green Party")
    clock = MutableClock()
    documents = (
        CivicDocument(
            source_key=SOURCE_ENTRY.source_key,
            external_key="housing",
            canonical_url="https://www.beehive.govt.nz/release/green-housing",
            title="Green Party housing affordability homes rentals",
            tier=CivicEvidenceTier.OFFICIAL,
            publisher=SOURCE_ENTRY.publisher,
            first_seen_at=T0,
            published_at=T0,
            excerpt="Green Party housing policy recorded on 2026-08-01.",
        ),
        CivicDocument(
            source_key=SOURCE_ENTRY.source_key,
            external_key="rail",
            canonical_url="https://www.beehive.govt.nz/release/green-rail",
            title="Green Party rail freight locomotives tracks",
            tier=CivicEvidenceTier.OFFICIAL,
            publisher=SOURCE_ENTRY.publisher,
            first_seen_at=T0,
            published_at=T0,
            excerpt="Green Party rail policy recorded on 2026-08-01.",
        ),
    )
    documents = tuple(with_official_feed_metadata(document) for document in documents)

    async def fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=documents,
            health=CivicSourceHealth.OK,
            fetched_at=T0,
        )

    delegate = passing_green_gateway()
    gateway = AdvancingGateway(
        delegate, clock, steps=4, seconds_per_step=100)
    worker = CivicWorker(
        conn,
        gateway,
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=clock,
        deadline_seconds=2000,
        heartbeat_interval_seconds=0.002,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert get_civic_batch(conn, execution.batch_id).candidate_count == 2
    assert len(delegate.calls) == 4
    assert clock.now == T0 + timedelta(seconds=1600)


async def test_hard_deadline_cancels_inflight_model_and_fences_completion(tmp_path):
    conn = connect(str(tmp_path / "model-deadline.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    clock = MutableClock()

    async def fetch(_entry, _now):
        return housing_fetch_result()

    delegate = passing_green_gateway()
    gateway = AdvancingGateway(
        delegate, clock, steps=4, seconds_per_step=150)
    worker = CivicWorker(
        conn,
        gateway,
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=clock,
        deadline_seconds=500,
        heartbeat_interval_seconds=0.002,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    run = get_civic_run(conn, execution.run_id)
    assert execution.status is CivicRunExecutionStatus.STALE_CLAIM
    assert run.status is CivicRunStatus.FAILED
    assert run.error_code == "deadline_exceeded"
    assert gateway.cancelled
    assert delegate.calls == []


@pytest.mark.parametrize(
    ("title", "excerpt", "reason"),
    [
        (
            "National Party and Labour Party discuss housing",
            "The National Party and Labour Party both addressed housing.",
            CivicPolicyReason.SUBJECT_AMBIGUOUS_V1,
        ),
        (
            "Government releases housing update",
            "The update describes housing administration.",
            CivicPolicyReason.SUBJECT_UNSUPPORTED_V1,
        ),
    ],
)
async def test_unresolved_party_settles_before_ai_and_is_not_sealed(
    title, excerpt, reason, tmp_path,
):
    conn = connect(str(tmp_path / f"{reason.value}.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    document = CivicDocument(
        source_key=SOURCE_ENTRY.source_key,
        external_key=reason.value,
        canonical_url=f"https://www.beehive.govt.nz/release/{reason.value}",
        title=title,
        tier=CivicEvidenceTier.OFFICIAL,
        publisher=SOURCE_ENTRY.publisher,
        first_seen_at=T0,
        published_at=T0,
        excerpt=excerpt,
    )

    async def fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(document,),
            health=CivicSourceHealth.OK,
            fetched_at=T0,
        )

    gateway = passing_green_gateway()
    worker = CivicWorker(
        conn,
        gateway,
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    candidate = list_run_candidates(conn, execution.run_id)[0]
    qualification = get_latest_qualification(conn, candidate.id)
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert candidate.rejection_code == reason.value
    assert qualification.outcome.value == "deferred_unsupported"
    assert qualification.reasons == (reason,)
    assert gateway.calls == []
    assert list_sealed_candidate_ids(conn, execution.batch_id) == []
    assert get_civic_batch(conn, execution.batch_id).candidate_count == 0


async def test_lost_claim_during_gather_stops_before_ingest(tmp_path):
    conn = connect(str(tmp_path / "lost.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    clock = MutableClock()

    async def fetch(_entry, _now):
        clock.now += timedelta(seconds=301)
        recovered = recover_expired_civic_runs(conn, clock())
        assert recovered.requeued_count == 1
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=clock,
        lease_seconds=300,
        deadline_seconds=1200,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.STALE_CLAIM
    assert get_civic_run(conn, execution.run_id).status is CivicRunStatus.PENDING
    assert list_run_candidates(conn, execution.run_id) == []


async def test_reclaimed_run_with_sealed_batch_resumes_only_completion(tmp_path):
    conn = connect(str(tmp_path / "resume.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    run = schedule_due_civic_runs(conn, T0)[0].run
    lease = claim_civic_run(conn, run.id, T0, 10, 600)
    batch = get_or_create_batch_if_claimed(
        conn, run.id, lease.run.claim_token, T0)
    sealed = seal_batch_if_claimed(
        conn, batch.id, run.id, lease.run.claim_token, T0)
    assert sealed.status is CivicBatchStatus.SEALED

    clock = MutableClock(T0 + timedelta(seconds=11))
    assert recover_expired_civic_runs(conn, clock()).requeued_count == 1

    async def must_not_fetch(_entry, _now):
        raise AssertionError("a sealed resume must not gather again")

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=must_not_fetch,
        now_fn=clock,
        lease_seconds=60,
        deadline_seconds=600,
    )
    execution = await worker.execute_run(run.id)
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert get_civic_run(conn, run.id).status is CivicRunStatus.COMPLETED
    assert get_civic_batch(conn, batch.id).status is CivicBatchStatus.SEALED


async def test_empty_day_adopts_capacity_and_ai_carryovers_without_duplicate_logic(tmp_path):
    conn = connect(str(tmp_path / "carryovers.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    vocabulary = get_published_vocabulary_version(conn, watch.id)
    prior = enqueue_civic_run(
        conn, watch.id, T0, T0, vocabulary_version_id=vocabulary.id)
    lease = claim_civic_run(conn, prior.id, T0, 300, 2000)
    batch = get_or_create_batch_if_claimed(
        conn, prior.id, lease.run.claim_token, T0)
    words = (
        "housing homes rentals affordability",
        "rail freight tracks locomotives",
        "fisheries oceans quotas vessels",
        "schools teachers classrooms learning",
        "health hospitals nurses clinics",
        "energy solar wind electricity",
        "justice courts policing sentences",
        "forestry trees exports milling",
    )
    documents = [
        CivicDocument(
            source_key=SOURCE_ENTRY.source_key,
            external_key=f"carry-{index}",
            canonical_url=f"https://www.beehive.govt.nz/release/carry-{index}",
            title=f"Green Party {word}",
            tier=CivicEvidenceTier.OFFICIAL,
            publisher=SOURCE_ENTRY.publisher,
            first_seen_at=T0,
            published_at=T0,
            excerpt=(
                "The Green Party housing bill passed its second reading on 2026-08-01. "
                f"The update covers {word}."),
        )
        for index, word in enumerate(words)
    ]
    documents = [with_official_feed_metadata(document) for document in documents]
    ingest = ingest_civic_documents(
        conn,
        run_id=prior.id,
        claim_token=lease.run.claim_token,
        documents=documents,
        now=T0,
    )
    assert len(ingest.created_candidates) == 8
    profiles = build_source_profiles()
    registry = build_ownership_registry()
    by_key = {candidate.dedupe_key: candidate for candidate in ingest.created_candidates}
    for cluster in ingest.clustering.clusters:
        candidate = by_key[cluster.cluster_key]
        resolution = resolve_cluster_party_subject(cluster, profiles)
        persist_cluster_evidence(
            conn,
            run_id=prior.id,
            claim_token=lease.run.claim_token,
            candidate=candidate,
            cluster=cluster,
            watch=watch,
            subject_id=resolution.subject_id,
            profiles=profiles,
            now=T0,
        )
        output = qualify_civic_candidate(
            conn,
            run_id=prior.id,
            claim_token=lease.run.claim_token,
            claim=construct_candidate_claim(conn, candidate),
            registry=registry,
            now=T0,
            engine_version=DESTINATION_V1_ENGINE_VERSION,
            qualification_scope=constrain_qualification_to_destination_v1,
        )
        assert output.candidate.status is CivicCandidateStatus.VERIFYING

    candidates = sorted(ingest.created_candidates, key=lambda candidate: candidate.id)
    for candidate in candidates[:7]:
        generated = await generate_civic_candidate(
            conn, run_id=prior.id, claim_token=lease.run.claim_token,
            candidate_id=candidate.id, gateway=passing_green_gateway(), now=T0)
        assert generated.verified
    ai_candidate = candidates[7]
    deferred = defer_candidate_for_ai(
        conn,
        candidate_id=ai_candidate.id,
        run_id=prior.id,
        claim_token=lease.run.claim_token,
        reason="ai_budget_exhausted",
        now=T0,
    )
    assert deferred.deferred
    seal_batch_if_claimed(
        conn, batch.id, prior.id, lease.run.claim_token, T0)
    capacity_candidates = [
        candidate for candidate in candidates
        if get_candidate(conn, candidate.id).status is CivicCandidateStatus.DEFERRED_CAPACITY
    ]
    assert len(capacity_candidates) == 1
    capacity_candidate = capacity_candidates[0]
    assert get_candidate(
        conn, ai_candidate.id).status is CivicCandidateStatus.DEFERRED_AI
    assert complete_civic_run(
        conn, prior.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0)

    tomorrow = T0 + timedelta(days=1)

    async def empty_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(),
            health=CivicSourceHealth.EMPTY,
            fetched_at=tomorrow,
        )

    gateway = passing_green_gateway()
    worker = CivicWorker(
        conn,
        gateway,
        source_entries=(SOURCE_ENTRY,),
        source_executor=empty_fetch,
        now_fn=lambda: tomorrow,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    sealed_ids = set(list_sealed_candidate_ids(conn, execution.batch_id))
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert sealed_ids == {capacity_candidate.id, ai_candidate.id}
    adopted_capacity = get_candidate(conn, capacity_candidate.id)
    adopted_ai = get_candidate(conn, ai_candidate.id)
    assert adopted_capacity.run_id == execution.run_id
    assert adopted_ai.run_id == execution.run_id
    assert adopted_capacity.status is CivicCandidateStatus.VERIFIED
    assert adopted_ai.status is CivicCandidateStatus.VERIFIED
    assert adopted_capacity.sealed_into_batch_at is not None
    assert adopted_ai.sealed_into_batch_at is not None
    assert len(gateway.calls) == 2
    written = CivicSpool(tmp_path / "spool").write_batch(conn, execution.batch_id, tomorrow)
    assert written.path.read_bytes() == get_frozen_publication(conn, execution.batch_id)
    assert len(json.loads(written.path.read_bytes())["items"]) == 2


async def _create_capacity_carryover_for_stale_test(
    conn, watch, *, secondary_evidence=None, ai_carryover=False,
):
    vocabulary = get_published_vocabulary_version(conn, watch.id)
    prior = enqueue_civic_run(
        conn, watch.id, T0, T0, vocabulary_version_id=vocabulary.id)
    lease = claim_civic_run(conn, prior.id, T0, 300, 2000)
    batch = get_or_create_batch_if_claimed(
        conn, prior.id, lease.run.claim_token, T0)
    words = (
        "housing homes rentals affordability",
        "rail freight tracks locomotives",
        "fisheries oceans quotas vessels",
        "schools teachers classrooms learning",
        "health hospitals nurses clinics",
        "energy solar wind electricity",
        "justice courts policing sentences",
    )
    documents = [
        CivicDocument(
            source_key=SOURCE_ENTRY.source_key,
            external_key=f"stale-{index}",
            canonical_url=f"https://www.beehive.govt.nz/release/stale-{index}",
            title=f"Green Party {word}",
            tier=CivicEvidenceTier.OFFICIAL,
            publisher=SOURCE_ENTRY.publisher,
            first_seen_at=T0,
            published_at=T0,
            excerpt=(
                "The Green Party housing bill passed its second reading on 2026-08-01. "
                f"The update covers {word}."),
        )
        for index, word in enumerate(words)
    ]
    documents = [with_official_feed_metadata(document) for document in documents]
    ingest = ingest_civic_documents(
        conn,
        run_id=prior.id,
        claim_token=lease.run.claim_token,
        documents=documents,
        now=T0,
    )
    assert len(ingest.created_candidates) == 7
    profiles = build_source_profiles()
    registry = build_ownership_registry()
    by_key = {candidate.dedupe_key: candidate for candidate in ingest.created_candidates}
    for cluster in ingest.clustering.clusters:
        candidate = by_key[cluster.cluster_key]
        resolution = resolve_cluster_party_subject(cluster, profiles)
        persist_cluster_evidence(
            conn,
            run_id=prior.id,
            claim_token=lease.run.claim_token,
            candidate=candidate,
            cluster=cluster,
            watch=watch,
            subject_id=resolution.subject_id,
            profiles=profiles,
            now=T0,
        )
        if secondary_evidence is not None:
            evidence = list_candidate_evidence(conn, candidate.id)[0]
            extra_url = (
                f"http://www.beehive.govt.nz/release/another-record-{candidate.id}"
                if secondary_evidence == "uncited_http"
                else f"https://www.beehive.govt.nz/release/another-record-{candidate.id}")
            add_candidate_evidence(
                conn, candidate.id, evidence.source_ref, f"zz-extra-{candidate.id}",
                extra_url, evidence.title,
                (CivicEvidenceTier.AGGREGATOR
                 if secondary_evidence == "excluded_https" else evidence.tier),
                CivicEvidenceStance.SUPPORTING, T0,
                publisher=evidence.publisher, publisher_id=evidence.publisher_id,
                source_kind=(
                    None if secondary_evidence == "excluded_https" else evidence.source_kind),
                subject_id=evidence.subject_id, claim_category=evidence.claim_category,
                event_date=evidence.event_date, canonical_url=extra_url,
                excerpt=evidence.excerpt, published_at=evidence.published_at,
                accessible_full_text=True)
        qualify_civic_candidate(
            conn,
            run_id=prior.id,
            claim_token=lease.run.claim_token,
            claim=construct_candidate_claim(conn, candidate),
            registry=registry,
            now=T0,
            engine_version=DESTINATION_V1_ENGINE_VERSION,
            qualification_scope=constrain_qualification_to_destination_v1,
        )
        generated = await generate_civic_candidate(
            conn, run_id=prior.id, claim_token=lease.run.claim_token,
            candidate_id=candidate.id, gateway=passing_green_gateway(), now=T0,
            call_budget=13 if ai_carryover else 30)
        if ai_carryover and candidate.id == max(by_key[key].id for key in by_key):
            assert generated.deferral_reason == "ai_budget_exhausted"
        else:
            assert generated.verified
    seal_batch_if_claimed(
        conn, batch.id, prior.id, lease.run.claim_token, T0)
    carryovers = [
        candidate for candidate in ingest.created_candidates
        if get_candidate(conn, candidate.id).status is (
            CivicCandidateStatus.DEFERRED_AI if ai_carryover
            else CivicCandidateStatus.DEFERRED_CAPACITY)
    ]
    assert len(carryovers) == 1
    assert complete_civic_run(
        conn, prior.id, lease.run.claim_token, CivicRunStatus.COMPLETED, T0)
    return carryovers[0]


async def test_preverification_cache_drift_does_not_abort_run_or_starve_fresh_news(tmp_path):
    conn = connect(str(tmp_path / "preverification-drift.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    stale = await _create_capacity_carryover_for_stale_test(conn, watch, ai_carryover=True)
    conn.execute(
        "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/2' "
        "WHERE candidate_id = ?", (stale.id,))
    conn.commit()
    original = list_candidate_stages(conn, stale.id)
    tomorrow = T0 + timedelta(days=1)

    async def fresh_fetch(entry, _now):
        result = housing_fetch_result()
        document = replace(
            result.documents[0], external_key="fresh-after-deployment",
            canonical_url="https://www.beehive.govt.nz/release/fresh-after-deployment")
        document = with_official_feed_metadata(document)
        return replace(result, documents=(document,), fetched_at=tomorrow)

    gateway = passing_green_gateway()
    worker = CivicWorker(
        conn, gateway, source_entries=(SOURCE_ENTRY,), source_executor=fresh_fetch,
        now_fn=lambda: tomorrow)
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert get_candidate(conn, stale.id).status is CivicCandidateStatus.DEFERRED_AI
    assert stale.id not in list_sealed_candidate_ids(conn, execution.batch_id)
    assert get_civic_batch(conn, execution.batch_id).candidate_count == 1
    assert count_run_ai_calls(conn, execution.run_id) == 2
    assert original[0] in list_candidate_stages(conn, stale.id)
    assert get_stage_receipt(conn, stale.id, CivicAiStage.GENERATE) is None
    written = CivicSpool(tmp_path / "spool").write_batch(conn, execution.batch_id, tomorrow)
    assert len(json.loads(written.path.read_bytes())["items"]) == 1

    later = tomorrow + timedelta(days=1)

    async def empty_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=(), health=CivicSourceHealth.EMPTY,
            fetched_at=later)

    next_gateway = passing_green_gateway()
    next_worker = CivicWorker(
        conn, next_gateway, source_entries=(SOURCE_ENTRY,), source_executor=empty_fetch,
        now_fn=lambda: later)
    next_execution = (await next_worker.run_due_cycle()).runs[0]
    assert next_execution.status is CivicRunExecutionStatus.COMPLETED
    assert list_sealed_candidate_ids(conn, next_execution.batch_id) == [stale.id]
    assert next_gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]


async def test_corrupt_carryover_cannot_destroy_newly_ingested_source_items(tmp_path):
    conn = connect(str(tmp_path / "corrupt-carryover.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    corrupt = await _create_capacity_carryover_for_stale_test(conn, watch)
    conn.execute(
        "UPDATE civic_ai_stages SET output_hash = ? WHERE candidate_id = ? AND stage = 'generate'",
        ("0" * 64, corrupt.id))
    conn.commit()
    tomorrow = T0 + timedelta(days=1)

    async def fresh_fetch(entry, _now):
        result = housing_fetch_result()
        return replace(
            result, documents=(with_official_feed_metadata(replace(
                result.documents[0], external_key="fresh-beside-corruption",
                canonical_url="https://www.beehive.govt.nz/release/fresh-beside-corruption")),),
            fetched_at=tomorrow)

    gateway = passing_green_gateway()
    worker = CivicWorker(
        conn, gateway, source_entries=(SOURCE_ENTRY,), source_executor=fresh_fetch,
        now_fn=lambda: tomorrow)
    execution = (await worker.run_due_cycle()).runs[0]

    assert execution.status is CivicRunExecutionStatus.FAILED
    fresh = next(
        candidate for candidate in list_run_candidates(conn, execution.run_id)
        if candidate.id != corrupt.id)
    assert fresh.status is CivicCandidateStatus.DEFERRED_CAPACITY
    assert fresh.rejection_code is None
    assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    assert list_sealed_candidate_ids(conn, execution.batch_id) == []
    assert "nz_beehive_releases:fresh-beside-corruption" in list_consumed_item_keys(conn, watch.id)


async def test_non_publication_watch_does_not_refresh_its_verified_carryover(tmp_path):
    conn = connect(str(tmp_path / "generic-carryover.db"))
    init_schema(conn)
    watch = replace(create_partymap_watch(conn, term="Green Party"), subject="custom-civic")
    conn.execute("UPDATE civic_watches SET subject = ? WHERE id = ?", (watch.subject, watch.id))
    conn.commit()
    candidate = await _create_capacity_carryover_for_stale_test(conn, watch)
    conn.execute(
        "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/2' WHERE candidate_id = ?",
        (candidate.id,))
    conn.commit()
    tomorrow = T0 + timedelta(days=1)

    async def empty_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=(), health=CivicSourceHealth.EMPTY,
            fetched_at=tomorrow)

    gateway = passing_green_gateway()
    worker = CivicWorker(
        conn, gateway, source_entries=(SOURCE_ENTRY,), source_executor=empty_fetch,
        now_fn=lambda: tomorrow, global_watch_subject=watch.subject)
    execution = (await worker.run_due_cycle()).runs[0]

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert list_sealed_candidate_ids(conn, execution.batch_id) == [candidate.id]
    assert get_candidate(conn, candidate.id).ai_revision == 0
    assert get_frozen_publication(conn, execution.batch_id) is None
    assert gateway.calls == []


@pytest.mark.parametrize("supported", [True, False])
async def test_prompt2_capacity_carryover_gets_genuine_prompt3_generation_and_verification(
    tmp_path, supported,
):
    conn = connect(str(tmp_path / "deployment-refresh.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    candidate = await _create_capacity_carryover_for_stale_test(conn, watch)
    prior_publication = get_frozen_publication(conn, candidate.batch_id)
    consumed = list_consumed_item_keys(conn, watch.id)
    conn.execute(
        "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/2', "
        "input_hash = ?, prompt_hash = ? WHERE candidate_id = ?",
        ("0" * 64, "1" * 64, candidate.id))
    conn.commit()
    original_receipts = list_candidate_stages(conn, candidate.id)
    original_qualification = get_latest_qualification(conn, candidate.id)
    original_judgments = list_verifications(conn, candidate.id)
    tomorrow = T0 + timedelta(days=1)

    async def empty_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=(),
            health=CivicSourceHealth.EMPTY, fetched_at=tomorrow)

    gateway = passing_green_gateway()
    if not supported:
        gateway.responses[CivicAiStage.VERIFICATION] = fenced(verification_payload(
            "reject", [{"code": "unsupported_claim", "locale": "en",
                        "detail": "The fresh verification did not support the claim."}]))
    worker = CivicWorker(
        conn, gateway, source_entries=(SOURCE_ENTRY,),
        source_executor=empty_fetch, now_fn=lambda: tomorrow)
    execution = (await worker.run_due_cycle()).runs[0]

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert list_sealed_candidate_ids(conn, execution.batch_id) == (
        [candidate.id] if supported else [])
    settled = get_candidate(conn, candidate.id)
    assert settled.status is (
        CivicCandidateStatus.VERIFIED if supported else CivicCandidateStatus.REJECTED)
    if not supported:
        assert settled.rejection_code == "verification_rejected"
    assert gateway.stages == [CivicAiStage.GENERATE, CivicAiStage.VERIFICATION]
    assert count_run_ai_calls(conn, execution.run_id) == 2
    current = [
        get_stage_receipt(conn, candidate.id, stage)
        for stage in (CivicAiStage.GENERATE, CivicAiStage.VERIFICATION)]
    assert all(receipt.prompt_version == CIVIC_PROMPT_VERSION for receipt in current)
    assert {receipt.id for receipt in current}.isdisjoint(
        receipt.id for receipt in original_receipts)
    audit = list_candidate_stages(conn, candidate.id)
    assert all(receipt in audit for receipt in original_receipts)
    assert original_qualification in list_qualifications(conn, candidate.id)
    assert get_latest_qualification(conn, candidate.id).ai_revision == 1
    judgments = list_verifications(conn, candidate.id)
    assert all(judgment in judgments for judgment in original_judgments)
    assert len(judgments) == len(original_judgments) + 1
    assert list_consumed_item_keys(conn, watch.id) == consumed
    assert get_frozen_publication(conn, candidate.batch_id) == prior_publication
    written = CivicSpool(tmp_path / "spool").write_batch(conn, execution.batch_id, tomorrow)
    assert len(json.loads(written.path.read_bytes())["items"]) == int(supported)


async def test_27_stale_candidates_keep_30_sources_and_drain_under_real_budget(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "deployment-backlog.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    vocabulary = get_published_vocabulary_version(conn, watch.id)
    clock = MutableClock()
    spool = CivicSpool(tmp_path / "spool")

    async def historic_fetch(entry, now):
        if now == T0:
            return housing_fetch_result()
        return CivicFetchResult(
            source_key=entry.source_key, documents=(), health=CivicSourceHealth.EMPTY,
            fetched_at=now)

    historical = {}
    for day in range(6):
        clock.now = T0 + timedelta(days=day)
        worker = CivicWorker(
            conn, passing_green_gateway(), source_entries=(SOURCE_ENTRY,),
            source_executor=historic_fetch, now_fn=clock)
        execution = (await worker.run_due_cycle()).runs[0]
        written = spool.write_batch(conn, execution.batch_id, clock())
        historical[execution.batch_id] = written.path.read_bytes()
    # These are fixtures for the old, already-bound spool format, before frozen DB projections.
    conn.execute("DELETE FROM civic_batch_publications")
    conn.execute("UPDATE civic_batches SET publication_schema_version = NULL")
    conn.commit()

    topics = (
        "housing homes rentals affordability", "rail freight tracks locomotives",
        "fisheries oceans quotas vessels", "schools teachers classrooms learning",
        "health hospitals nurses clinics", "energy solar wind electricity",
        "justice courts policing sentences", "forestry trees exports milling",
        "water pipes reservoirs irrigation", "defence navy frigates procurement",
        "tourism visitors hotels attractions", "immigration visas migrants borders",
        "tax revenue levies deductions", "culture arts museums heritage",
        "climate carbon emissions adaptation", "technology broadband networks spectrum",
        "commerce markets competition prices", "disability accessibility carers support",
        "workplace employment wages bargaining", "local councils districts rates",
        "foreign diplomacy treaties embassies", "sport athletes facilities coaching",
        "emergency disasters firefighters rescue", "biosecurity pests quarantine inspections",
        "land survey titles ownership", "conservation biodiversity habitats reserves",
        "transport aviation airports terminals",
    )

    def response(request):
        topic = next(topic for topic in topics if topic in request.prompt)
        text = f"The Green Party announced a policy covering {topic} on 2026-08-01."
        return fenced(generation_payload(entry=full_entry(**{
            locale: {
                "title": f"Green Party {topic} policy announcement",
                "summary": text, "claim_text": text,
                "policy_status": "announced", "entities": ["green"],
            }
            for locale in ("zh", "en", "hi", "tl")
        })))

    def gateway():
        result = passing_green_gateway()
        result.responses[CivicAiStage.GENERATE] = response
        return result

    candidates = []
    profiles = build_source_profiles()
    registry = build_ownership_registry()
    for group in range(3):
        slot = T0 + timedelta(days=6 + group)
        run = enqueue_civic_run(
            conn, watch.id, slot, slot, vocabulary_version_id=vocabulary.id)
        lease = claim_civic_run(conn, run.id, slot, 300, 2000)
        get_or_create_batch_if_claimed(conn, run.id, lease.run.claim_token, slot)
        documents = []
        for index in range(group * 9, group * 9 + 9):
            document = CivicDocument(
                source_key=SOURCE_ENTRY.source_key, external_key=f"cohort-{index}",
                canonical_url=f"https://www.beehive.govt.nz/release/cohort-{index}",
                title=f"Green Party {topics[index]}",
                tier=CivicEvidenceTier.OFFICIAL, publisher=SOURCE_ENTRY.publisher,
                first_seen_at=slot, published_at=T0,
                excerpt=(
                    f"The Green Party announced a policy covering {topics[index]} "
                    "on 2026-08-01."),
            )
            document = with_official_feed_metadata(document)
            documents.append(document)
            if index % 9 == 0:
                documents.append(with_official_feed_metadata(replace(
                    document, external_key=f"cohort-{index}-confirmation",
                    canonical_url=f"{document.canonical_url}-confirmation")))
        ingested = ingest_civic_documents(
            conn, run_id=run.id, claim_token=lease.run.claim_token,
            documents=documents, now=slot)
        assert len(ingested.created_candidates) == 9
        by_key = {candidate.dedupe_key: candidate for candidate in ingested.created_candidates}
        for cluster in ingested.clustering.clusters:
            candidate = by_key[cluster.cluster_key]
            persist_cluster_evidence(
                conn, run_id=run.id, claim_token=lease.run.claim_token, candidate=candidate,
                cluster=cluster, watch=watch, subject_id="green", profiles=profiles, now=slot)
            qualify_civic_candidate(
                conn, run_id=run.id, claim_token=lease.run.claim_token,
                claim=construct_candidate_claim(conn, candidate), registry=registry, now=slot,
                engine_version=DESTINATION_V1_ENGINE_VERSION,
                qualification_scope=constrain_qualification_to_destination_v1)
            generated = await generate_civic_candidate(
                conn, run_id=run.id, claim_token=lease.run.claim_token,
                candidate_id=candidate.id, gateway=gateway(), now=slot)
            assert generated.verified
            candidates.append(candidate)
        assert count_run_ai_calls(conn, run.id) == 18
        complete_civic_run(
            conn, run.id, lease.run.claim_token, CivicRunStatus.FAILED, slot,
            error_code="old_worker_interrupted")
    assert len(candidates) == 27
    assert all(get_candidate(conn, item.id).status is CivicCandidateStatus.DEFERRED_CAPACITY
               for item in candidates)
    consumed = list_consumed_item_keys(conn, watch.id)
    assert len(consumed) == 31  # 30 held source items plus the historical published article.
    conn.execute(
        "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/2', input_hash = ? "
        "WHERE candidate_id IN (SELECT id FROM civic_candidates WHERE status = 'deferred_capacity')",
        ("0" * 64,))
    conn.commit()
    original = [receipt for item in candidates for receipt in list_candidate_stages(conn, item.id)]

    async def empty_fetch(entry, now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=(), health=CivicSourceHealth.EMPTY,
            fetched_at=now)

    new_calls = []
    published = []
    for day in range(5):
        clock.now = T0 + timedelta(days=9 + day)
        worker = CivicWorker(
            conn, gateway(), source_entries=(SOURCE_ENTRY,), source_executor=empty_fetch,
            now_fn=clock)
        execution = (await worker.run_due_cycle()).runs[0]
        assert execution.status is CivicRunExecutionStatus.COMPLETED
        new_calls.append(count_run_ai_calls(conn, execution.run_id))
        published.extend(list_sealed_candidate_ids(conn, execution.batch_id))
        spool.write_batch(conn, execution.batch_id, clock())
        assert all(get_candidate(conn, item.id).status is not CivicCandidateStatus.REJECTED
                   for item in candidates)
    assert new_calls == [30, 24, 0, 0, 0]
    assert sorted(published) == sorted(item.id for item in candidates)
    assert list_consumed_item_keys(conn, watch.id) == consumed
    audit = [receipt for item in candidates for receipt in list_candidate_stages(conn, item.id)]
    assert all(receipt in audit for receipt in original)
    assert all(get_candidate(conn, item.id).ai_revision == 1 for item in candidates)
    monkeypatch.setattr("beehive.civic.generation.is_citable", lambda *args, **kwargs: False)
    for batch_id, data in historical.items():
        replay = spool.write_batch(conn, batch_id, clock())
        assert replay.replayed
        assert replay.path.read_bytes() == data


@pytest.mark.parametrize(
    "stale_kind,reason,refreshed",
    [
        ("engine", "publication_destination_scope", True),
        ("generation_version", "publication_stage_version_changed", True),
        ("verification_version", "publication_stage_version_changed", True),
        ("generation_request", "publication_generation_changed", True),
        ("verification_request", "publication_verification_changed", True),
        ("invalid_locales", "publication_schema_invalid", False),
        ("missing_locale", "publication_schema_invalid", False),
        ("missing_qualification", "publication_qualification_missing", True),
    ],
)
async def test_drift_refreshes_but_invalid_carryovers_reject_without_poisoning_later_days(
    stale_kind, reason, refreshed, tmp_path,
):
    conn = connect(str(tmp_path / f"stale-{stale_kind}.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    stale = await _create_capacity_carryover_for_stale_test(conn, watch)
    prior_publication = get_frozen_publication(conn, stale.batch_id)
    if stale_kind == "engine":
        conn.execute(
            "UPDATE civic_qualifications SET engine_version = 'destination-v0' "
            "WHERE candidate_id = ?",
            (stale.id,),
        )
    elif stale_kind in {"generation_version", "verification_version"}:
        stage = "generate" if stale_kind == "generation_version" else "verification"
        conn.execute(
            "UPDATE civic_ai_stages SET prompt_version = 'civic-ai-prompts/2' "
            "WHERE candidate_id = ? AND stage = ?", (stale.id, stage))
    elif stale_kind in {"generation_request", "verification_request"}:
        stage = "generate" if stale_kind == "generation_request" else "verification"
        conn.execute(
            "UPDATE civic_ai_stages SET prompt_hash = ? WHERE candidate_id = ? AND stage = ?",
            ("0" * 64, stale.id, stage))
    elif stale_kind in {"invalid_locales", "missing_locale"}:
        receipt = get_stage_receipt(conn, stale.id, CivicAiStage.GENERATE)
        output = receipt.output
        if stale_kind == "missing_locale":
            del output["entry"]["tl"]
        else:
            for locale in ("zh", "en", "hi", "tl"):
                output["entry"][locale]["title"] = "A" * 121
        conn.execute(
            "UPDATE civic_ai_stages SET output_json = ?, output_hash = ? WHERE id = ?",
            (json.dumps(output), output_hash_for(output), receipt.id))
    elif stale_kind == "missing_qualification":
        conn.execute("DELETE FROM civic_qualifications WHERE candidate_id = ?", (stale.id,))
    conn.commit()

    tomorrow = T0 + timedelta(days=1)
    valid_document = CivicDocument(
        source_key=SOURCE_ENTRY.source_key,
        external_key=f"day-two-{stale_kind}",
        canonical_url=f"https://www.beehive.govt.nz/release/day-two-{stale_kind}",
        title="Green Party housing bill passes second reading",
        tier=CivicEvidenceTier.OFFICIAL,
        publisher=SOURCE_ENTRY.publisher,
        first_seen_at=tomorrow,
        published_at=T0,
        excerpt="The Green Party housing bill passed its second reading on 2026-08-01.",
    )
    valid_document = with_official_feed_metadata(valid_document)

    async def valid_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(valid_document,),
            health=CivicSourceHealth.OK,
            fetched_at=tomorrow,
        )

    day_two = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=valid_fetch,
        now_fn=lambda: tomorrow,
    )
    second = (await day_two.run_due_cycle()).runs[0]
    stale_after = get_candidate(conn, stale.id)
    assert second.status is CivicRunExecutionStatus.COMPLETED
    rejections = list_publication_rejections(conn, second.batch_id)
    if refreshed:
        assert stale_after.status is CivicCandidateStatus.VERIFIED
        assert stale_after.ai_revision == 1
        assert stale_after.ai_refresh_reason == reason
        assert stale.id in list_sealed_candidate_ids(conn, second.batch_id)
        assert rejections == []
        assert count_run_ai_calls(conn, second.run_id) == 4
    else:
        assert stale_after.status is CivicCandidateStatus.REJECTED
        assert stale_after.rejection_code == reason
        assert stale_after.sealed_into_batch_at is None
        assert stale.id not in list_sealed_candidate_ids(conn, second.batch_id)
        assert [(entry.candidate_id, entry.reason) for entry in rejections] == [(stale.id, reason)]
        assert rejections[0].detail
    assert get_civic_batch(conn, second.batch_id).candidate_count == 1 + int(refreshed)
    written = CivicSpool(tmp_path / "spool").write_batch(conn, second.batch_id, tomorrow)
    assert written.path.read_bytes() == get_frozen_publication(conn, second.batch_id)
    envelope = json.loads(written.path.read_bytes())
    validate_publication_envelope(envelope)
    assert len(envelope["items"]) == 1 + int(refreshed)
    assert any(item["record_key"] != stale.dedupe_key for item in envelope["items"])

    day_three_at = T0 + timedelta(days=2)

    async def empty_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(),
            health=CivicSourceHealth.EMPTY,
            fetched_at=day_three_at,
        )

    day_three = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=empty_fetch,
        now_fn=lambda: day_three_at,
    )
    third = (await day_three.run_due_cycle()).runs[0]
    assert third.status is CivicRunExecutionStatus.COMPLETED
    assert get_civic_batch(conn, third.batch_id).candidate_count == 0
    assert get_candidate(conn, stale.id).run_id == second.run_id
    assert list_run_candidates(conn, third.run_id) == []
    assert get_frozen_publication(conn, stale.batch_id) == prior_publication


async def test_uncited_http_evidence_does_not_drop_valid_https_carryover(tmp_path):
    conn = connect(str(tmp_path / "mixed-evidence.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    candidate = await _create_capacity_carryover_for_stale_test(
        conn, watch, secondary_evidence="uncited_http")
    tomorrow = T0 + timedelta(days=1)

    async def empty_fetch(entry, _now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=(),
            health=CivicSourceHealth.EMPTY, fetched_at=tomorrow)

    worker = CivicWorker(
        conn, passing_green_gateway(), source_entries=(SOURCE_ENTRY,),
        source_executor=empty_fetch, now_fn=lambda: tomorrow)
    execution = (await worker.run_due_cycle()).runs[0]

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert list_sealed_candidate_ids(conn, execution.batch_id) == [candidate.id]
    assert list_publication_rejections(conn, execution.batch_id) == []
    written = CivicSpool(tmp_path / "spool").write_batch(conn, execution.batch_id, tomorrow)
    envelope = json.loads(written.path.read_bytes())
    assert len(envelope["items"]) == 1
    assert len(envelope["items"][0]["citations"]) == 1
    assert envelope["items"][0]["citations"][0]["canonical_url"].startswith("https://")
    assert envelope["items"][0]["citations"][0]["document_key"] != f"zz-extra-{candidate.id}"
