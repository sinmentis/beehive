from dataclasses import replace
from datetime import timedelta

import pytest

from beehive.civic.documents import CivicDocument
from beehive.civic.production import CivicHydrationResult, CivicHydrationStatus
from beehive.civic.worker import CivicWorker
from beehive.db.civic_evidence_captures import count_run_hydrations, reserve_hydration_if_claimed
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicEvidenceTier
from tests.civic.civic_worker_support import passing_green_gateway
from tests.civic.test_civic_ai_pipeline import T0, _make_run


async def test_fresh_hydration_honours_the_already_reserved_shared_run_budget(tmp_path):
    conn = connect(str(tmp_path / "shared-hydration.db"))
    init_schema(conn)
    run = _make_run(conn)
    reservation = reserve_hydration_if_claimed(
        conn, run_id=run.id, claim_token=run.token, work_key="previously-authorized",
        units=24, now=T0)
    assert reservation is not None
    first = CivicDocument(
        source_key="green", external_key="first",
        canonical_url="https://www.greens.org.nz/first",
        title="Green Party housing policy announcement",
        tier=CivicEvidenceTier.PARTY_PRIMARY, publisher="Green Party",
        first_seen_at=T0, published_at=T0)
    second = replace(
        first, external_key="second", canonical_url="https://www.greens.org.nz/second")
    fetched = []

    async def hydrate(document, now):
        fetched.append(document.external_key)
        return CivicHydrationResult(
            document, CivicHydrationStatus.UNAVAILABLE, "fixture_not_fetched")

    worker = CivicWorker(
        conn, passing_green_gateway(), evidence_hydrator=hydrate, now_fn=lambda: T0)
    prepared, _, deferred, _ = await worker._prepare_candidate_documents(
        (first, second), watch_id=run.watch_id, run_id=run.id,
        batch_id=run.batch.id, claim_token=run.token)
    assert fetched == []
    assert prepared.clusters == ()
    assert deferred == 1
    assert count_run_hydrations(conn, run.id) == 24
    conn.close()


@pytest.mark.parametrize("field", ["subject_id", "url"])
async def test_capture_identity_corruption_is_not_treated_as_a_regeneratable_statement(
    tmp_path, field,
):
    from beehive.civic.documents import CivicFetchResult, CivicSourceHealth
    from beehive.civic.worker import CivicRunExecutionStatus
    from beehive.db.civic_batches import get_civic_batch, get_frozen_publication
    from beehive.db.civic_candidates import get_candidate
    from tests.civic.civic_worker_support import (
        SOURCE_ENTRY, create_partymap_watch, housing_fetch_result, with_official_feed_metadata,
    )
    from tests.civic.test_worker import _create_capacity_carryover_for_stale_test

    conn = connect(str(tmp_path / f"capture-corruption-{field}.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    candidate = await _create_capacity_carryover_for_stale_test(conn, watch)
    historical = get_frozen_publication(conn, candidate.batch_id)
    if field == "subject_id":
        conn.execute(
            "UPDATE civic_candidate_evidence SET subject_id='removed-party' WHERE candidate_id=?",
            (candidate.id,))
    else:
        conn.execute(
            "UPDATE civic_candidate_evidence SET url='http://invalid.example/changed' "
            "WHERE candidate_id=?", (candidate.id,))
    conn.commit()
    async def fresh_fetch(entry, now):
        result = housing_fetch_result()
        fresh = with_official_feed_metadata(replace(
            result.documents[0], external_key=f"fresh-{now.date()}",
            canonical_url=f"https://www.beehive.govt.nz/release/fresh-{now.date()}"))
        return CivicFetchResult(
            source_key=entry.source_key, documents=(fresh,),
            health=CivicSourceHealth.OK, fetched_at=now)

    for offset in (1, 2):
        day = T0 + timedelta(days=offset)
        worker = CivicWorker(
            conn, passing_green_gateway(), source_entries=(SOURCE_ENTRY,),
            source_executor=fresh_fetch, now_fn=lambda: day)
        execution = (await worker.run_due_cycle()).runs[0]
        assert execution.status is CivicRunExecutionStatus.COMPLETED
        assert get_civic_batch(conn, execution.batch_id).candidate_count == 1
        assert get_candidate(conn, candidate.id).status.value == "deferred_ai"
        assert get_frozen_publication(conn, candidate.batch_id) == historical
    conn.close()


async def test_current_capture_url_is_authoritative_over_mutable_legacy_projection(tmp_path):
    from beehive.civic.documents import CivicFetchResult, CivicSourceHealth
    from beehive.civic.worker import CivicRunExecutionStatus
    from beehive.db.civic_batches import list_sealed_candidate_ids
    from beehive.db.civic_candidates import list_candidate_evidence
    from tests.civic.civic_worker_support import SOURCE_ENTRY, create_partymap_watch
    from tests.civic.test_worker import _create_capacity_carryover_for_stale_test

    conn = connect(str(tmp_path / "current-capture.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn, term="Green Party")
    candidate = await _create_capacity_carryover_for_stale_test(conn, watch)
    original_url = list_candidate_evidence(conn, candidate.id)[0].canonical_url
    conn.execute(
        "UPDATE civic_candidate_evidence SET canonical_url='http://invalid.example/legacy' "
        "WHERE candidate_id=?", (candidate.id,))
    conn.commit()
    tomorrow = T0 + timedelta(days=1)

    async def empty_fetch(entry, now):
        return CivicFetchResult(
            source_key=entry.source_key, documents=(),
            health=CivicSourceHealth.EMPTY, fetched_at=now)

    worker = CivicWorker(
        conn, passing_green_gateway(), source_entries=(SOURCE_ENTRY,),
        source_executor=empty_fetch, now_fn=lambda: tomorrow)
    execution = (await worker.run_due_cycle()).runs[0]
    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert list_sealed_candidate_ids(conn, execution.batch_id) == [candidate.id]
    assert list_candidate_evidence(conn, candidate.id)[0].canonical_url == original_url
    conn.close()


async def test_completed_fresh_hydration_replays_across_worker_restart_without_refetch(tmp_path):
    from beehive.civic.source_extraction import PublicationWitness, SourceArticle, capture_metadata
    from beehive.deep_read.extract import ExtractionQuality, ExtractionResult

    conn = connect(str(tmp_path / "hydration-replay.db"))
    init_schema(conn)
    run = _make_run(conn)
    document = CivicDocument(
        source_key="green", external_key="policy",
        canonical_url="https://www.greens.org.nz/policy",
        title="Green Party housing policy announcement",
        tier=CivicEvidenceTier.PARTY_PRIMARY, publisher="Green Party",
        first_seen_at=T0, published_at=T0)
    calls = []

    async def hydrate(original, now):
        calls.append(original.external_key)
        body = "The Green Party announced its housing proposal."
        article = SourceArticle(
            ExtractionResult(ExtractionQuality.COMPLETE, body, (), len(body)),
            PublicationWitness(T0, "meta:article:published_time", "datetime", T0.isoformat()),
            original.title, "article-html")
        captured = replace(original, excerpt=body, raw_metadata={
            "source_capture": capture_metadata(
                original, article=article, body=body, captured_at=now,
                fetched_url=original.canonical_url),
        })
        return CivicHydrationResult(captured, CivicHydrationStatus.HYDRATED)

    for _ in range(2):
        worker = CivicWorker(
            conn, passing_green_gateway(), evidence_hydrator=hydrate, now_fn=lambda: T0)
        prepared, _, deferred, _ = await worker._prepare_candidate_documents(
            (document,), watch_id=run.watch_id, run_id=run.id,
            batch_id=run.batch.id, claim_token=run.token)
        assert len(prepared.clusters) == 1 and deferred == 0
    assert calls == ["policy"]
    assert count_run_hydrations(conn, run.id) == 1
    conn.close()
