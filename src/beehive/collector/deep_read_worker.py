"""Drain queued deep-read jobs through fetch, extraction, and tool-free LLM synthesis."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from beehive.collector.deep_read_trigger import (
    consume_deep_read_wakeup,
    request_deep_read_worker,
)
from beehive.db.deep_reads import (
    claim_deep_read,
    complete_deep_read_success,
    fail_deep_read,
    heartbeat_deep_read,
    list_pending_deep_reads,
    recover_expired_deep_reads,
    requeue_deep_read,
)
from beehive.db.items import get_item
from beehive.deep_read.extract import ExtractionQuality, ExtractionResult, extract_article_text
from beehive.deep_read.fetch import ArticleFetcher, FetchFailure, FetchFailureReason
from beehive.deep_read.summarize import (
    DeepReadResult,
    ItemContext,
    PartialContent,
    generate_deep_read,
)
from beehive.localization import load_localizer

_LEASE_SECONDS = 1500
_MAX_JOBS_PER_RUN = 1
_ERROR_DETAIL_CAP = 1000

FetcherFactory = Callable[[], ArticleFetcher]
Extractor = Callable[..., ExtractionResult]
Generator = Callable[..., Awaitable[DeepReadResult]]
NowFactory = Callable[[], datetime]


@dataclass(frozen=True)
class DeepReadWorkerResult:
    recovered: int
    processed: int
    succeeded: int
    failed: int
    remaining: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _result_json(result: DeepReadResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":"))


def _source_name(item: dict) -> str:
    raw_metadata = item.get("raw_metadata") or {}
    return str(raw_metadata.get("source_name") or item["source_type"])


def _fetch_error_code(failure: FetchFailure) -> str:
    if failure.reason is FetchFailureReason.HTTP_ERROR:
        if failure.status_code == 404:
            return "fetch_not_found"
        return "fetch_http_error"
    if failure.reason is FetchFailureReason.TIMEOUT:
        return "fetch_timeout"
    return "fetch"


def _unusable_extraction_error_code(final_url: str) -> str:
    if (urlsplit(final_url).hostname or "").lower() == "news.google.com":
        return "extraction_google_news"
    return "extraction_no_text"


def _fail_claim(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    request_version: int,
    claim_token: str,
    error_code: str,
    detail: str,
    now_factory: NowFactory,
) -> bool:
    return fail_deep_read(
        conn,
        item_id,
        request_version,
        claim_token,
        error_code,
        detail[:_ERROR_DETAIL_CAP],
        now_factory(),
    )


async def process_deep_read_queue(
    conn: sqlite3.Connection,
    data_dir: str,
    *,
    fetcher_factory: FetcherFactory = ArticleFetcher,
    extractor: Extractor = extract_article_text,
    generator: Generator = generate_deep_read,
    now_factory: NowFactory = _utc_now,
    max_jobs: int = _MAX_JOBS_PER_RUN,
) -> DeepReadWorkerResult:
    """Process a bounded queue slice and re-arm systemd when work remains."""
    consume_deep_read_wakeup(data_dir)
    recovered = recover_expired_deep_reads(conn, now_factory())
    processed = succeeded = failed = 0

    fetcher = fetcher_factory()
    try:
        pending = list_pending_deep_reads(conn, limit=max_jobs)
        for queued in pending:
            claimed = claim_deep_read(
                conn,
                queued.item_id,
                now_factory(),
                lease_seconds=_LEASE_SECONDS,
            )
            if claimed is None or claimed.claim_token is None:
                continue

            processed += 1
            item_id = claimed.item_id
            request_version = claimed.request_version
            claim_token = claimed.claim_token
            try:
                item = get_item(conn, item_id)
                if item is None:
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code="unavailable",
                        detail="Item no longer exists",
                        now_factory=now_factory,
                    )
                    failed += 1
                    continue
                if item["ai_score"] is None:
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code="unavailable",
                        detail="Item has not been AI-ranked",
                        now_factory=now_factory,
                    )
                    failed += 1
                    continue

                localizer = load_localizer(conn)
                try:
                    fetched = fetcher.fetch(item["url"])
                except Exception as exc:
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code="fetch",
                        detail=f"{type(exc).__name__}: {exc}",
                        now_factory=now_factory,
                    )
                    failed += 1
                    print(
                        f"[deep-read] fetch failed for item {item_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                if isinstance(fetched, FetchFailure):
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code=_fetch_error_code(fetched),
                        detail=f"{fetched.reason.value}: {fetched.detail}",
                        now_factory=now_factory,
                    )
                    failed += 1
                    continue

                try:
                    extraction = extractor(
                        fetched.html,
                        transport_truncated=fetched.truncated,
                    )
                except Exception as exc:
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code="extraction",
                        detail=f"{type(exc).__name__}: {exc}",
                        now_factory=now_factory,
                    )
                    failed += 1
                    print(
                        f"[deep-read] extraction failed for item {item_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                if extraction.quality is ExtractionQuality.UNUSABLE:
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code=_unusable_extraction_error_code(fetched.url),
                        detail="Article extraction produced no usable text",
                        now_factory=now_factory,
                    )
                    failed += 1
                    continue

                if not heartbeat_deep_read(
                    conn,
                    item_id,
                    request_version,
                    claim_token,
                    now_factory(),
                    lease_seconds=_LEASE_SECONDS,
                ):
                    continue

                partial_reason = ", ".join(reason.value for reason in extraction.reasons) or None
                try:
                    result = await generator(
                        item_id=item_id,
                        item_context=ItemContext(
                            title=item["title"],
                            url=fetched.url,
                            source_name=_source_name(item),
                            source_type=item["source_type"],
                            published_at=item["created_at"],
                        ),
                        article_text=extraction.text,
                        partial_content=PartialContent(
                            is_partial=extraction.quality is ExtractionQuality.PARTIAL,
                            reason=partial_reason,
                        ),
                        localizer=localizer,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _fail_claim(
                        conn,
                        item_id=item_id,
                        request_version=request_version,
                        claim_token=claim_token,
                        error_code="llm",
                        detail=f"{type(exc).__name__}: {exc}",
                        now_factory=now_factory,
                    )
                    failed += 1
                    print(
                        f"[deep-read] LLM failed for item {item_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                completed = complete_deep_read_success(
                    conn,
                    item_id,
                    request_version,
                    claim_token,
                    _result_json(result),
                    localizer.code,
                    now_factory(),
                    warning_code=(
                        "content_incomplete"
                        if extraction.quality is ExtractionQuality.PARTIAL
                        else None
                    ),
                )
                if completed:
                    succeeded += 1
            except asyncio.CancelledError:
                requeue_deep_read(conn, item_id, request_version, claim_token)
                raise
            except Exception as exc:
                requeue_deep_read(conn, item_id, request_version, claim_token)
                print(
                    f"[deep-read] infrastructure failure for item {item_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise
    finally:
        fetcher.close()

    remaining = len(list_pending_deep_reads(conn, limit=1))
    if remaining:
        request_deep_read_worker(data_dir)

    return DeepReadWorkerResult(
        recovered=recovered,
        processed=processed,
        succeeded=succeeded,
        failed=failed,
        remaining=remaining,
    )
