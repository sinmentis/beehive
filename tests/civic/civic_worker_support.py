from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from email.utils import format_datetime

from beehive.civic.ai_gateway import CivicAiStage
from beehive.civic.documents import (
    CivicDocument,
    CivicFetchResult,
    CivicSourceHealth,
)
from beehive.connectors.civic_sources import get_source
from beehive.civic.source_extraction import PublicationWitness, SourceArticle, capture_metadata
from beehive.deep_read.extract import ExtractionQuality, ExtractionResult
from beehive.db.civic_vocabulary import (
    add_vocabulary_term,
    create_vocabulary_version,
    publish_vocabulary_version,
)
from beehive.db.civic_watches import create_civic_watch
from beehive.domain.civic import (
    CivicEvidenceTier,
    CivicTermCategory,
    CivicTermRole,
)
from tests.civic.civic_ai_fakes import (
    FakeCivicGateway,
    fenced,
    full_entry,
    generation_payload,
    verification_payload,
)

T0 = datetime(2026, 7, 31, 18, 30, tzinfo=timezone.utc)
SOURCE_ENTRY = get_source("nz_beehive_releases")


class MutableClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def create_partymap_watch(conn, *, term: str = "housing"):
    watch = create_civic_watch(
        conn, "partymap", "What changed?", T0, schedule_time="06:30")
    vocabulary = create_vocabulary_version(conn, watch.id, T0)
    add_vocabulary_term(
        conn,
        vocabulary.id,
        term,
        CivicTermCategory.TOPIC,
        CivicTermRole.REQUIRED,
        T0,
    )
    publish_vocabulary_version(conn, vocabulary.id, T0)
    return watch


def with_official_feed_metadata(document: CivicDocument) -> CivicDocument:
    """Bind an explicitly synthetic official RSS result to its identity and publication field."""
    if document.tier is not CivicEvidenceTier.OFFICIAL or document.published_at is None:
        raise ValueError("the official RSS fixture needs an explicit publication timestamp")
    witness = PublicationWitness(
        document.published_at, "rss-pubDate", "datetime", format_datetime(document.published_at))
    return replace(document, raw_metadata={
        **document.raw_metadata, "feed_guid": document.external_key,
        "feed_link": document.canonical_url, "publication": witness.as_metadata(),
    })


def with_article_metadata(document: CivicDocument) -> CivicDocument:
    """Represent a completed synthetic article extraction, never a metadata-only listing."""
    if not document.excerpt:
        raise ValueError("the article fixture needs an extracted body")
    publication = (
        PublicationWitness(
            document.published_at, "meta:article:published_time", "datetime",
            document.published_at.isoformat())
        if document.published_at is not None else None)
    article = SourceArticle(
        ExtractionResult(
            ExtractionQuality.COMPLETE, document.excerpt, (), len(document.excerpt)),
        publication, document.title, "article-html")
    return replace(document, raw_metadata={
        **document.raw_metadata,
        "source_capture": capture_metadata(
            document, article=article, body=document.excerpt,
            captured_at=document.first_seen_at, fetched_url=document.canonical_url),
    })


def housing_document() -> CivicDocument:
    return with_official_feed_metadata(CivicDocument(
        source_key=SOURCE_ENTRY.source_key,
        external_key="housing-bill-second-reading",
        canonical_url="https://www.beehive.govt.nz/release/housing-bill-second-reading",
        title="Green Party housing bill passes second reading",
        tier=CivicEvidenceTier.OFFICIAL,
        publisher=SOURCE_ENTRY.publisher,
        first_seen_at=T0,
        published_at=T0,
        excerpt="The Green Party housing bill passed its second reading on 2026-08-01.",
    ))


def housing_fetch_result() -> CivicFetchResult:
    return CivicFetchResult(
        source_key=SOURCE_ENTRY.source_key,
        documents=(housing_document(),),
        health=CivicSourceHealth.OK,
        fetched_at=T0,
    )


def passing_green_gateway() -> FakeCivicGateway:
    entry = full_entry(**{
        locale: {"entities": ["green"]}
        for locale in ("zh", "en", "hi", "tl")
    })
    return FakeCivicGateway(responses={
        CivicAiStage.GENERATE: fenced(generation_payload(entry=entry)),
        CivicAiStage.VERIFICATION: fenced(verification_payload()),
    })


async def seal_empty_daily_batches(conn, count: int, clock: MutableClock) -> list[int]:
    """Seal one empty Batch per daily slot, like the unsettled backlog a shadow ring keeps."""
    from beehive.civic.worker import CivicRunExecutionStatus, CivicWorker

    async def empty_fetch(entry, now):
        return CivicFetchResult(
            source_key=entry.source_key,
            documents=(),
            health=CivicSourceHealth.EMPTY,
            fetched_at=now,
        )

    start = clock.now
    batch_ids = []
    for day in range(count):
        clock.now = start + timedelta(days=day)
        worker = CivicWorker(
            conn,
            passing_green_gateway(),
            source_entries=(SOURCE_ENTRY,),
            source_executor=empty_fetch,
            now_fn=clock,
        )
        runs = (await worker.run_due_cycle()).runs
        assert [run.status for run in runs] == [CivicRunExecutionStatus.COMPLETED]
        batch_ids.append(runs[0].batch_id)
    return batch_ids
