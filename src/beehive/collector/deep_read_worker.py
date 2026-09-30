"""Deep-read jobs: fetch the article, extract its text and write the brief with a tool-free LLM
call. The worker claims one job at a time (claim_next_deep_read) and runs it on its own thread
(process_claimed_deep_read); `--mode deep-read` drains a few in one pass."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from beehive.ai.model_selection import load_model
from beehive.db.deep_reads import (
    DeepRead,
    claim_deep_read,
    complete_deep_read_success,
    fail_deep_read,
    heartbeat_deep_read,
    list_pending_deep_reads,
    recover_expired_deep_reads,
    requeue_deep_read,
)
from beehive.db.items import get_item
from beehive.deep_read.extract import (
    ExtractionQuality,
    ExtractionResult,
    PartialReason,
    extract_article_text,
)
from beehive.deep_read.fetch import ArticleFetcher, FetchFailure, FetchFailureReason
from beehive.deep_read.summarize import (
    DeepReadResult,
    ItemContext,
    PartialContent,
    generate_deep_read,
)
from beehive.localization import load_localizer

LEASE_SECONDS = 1500
_MAX_JOBS_PER_RUN = 1
_ERROR_DETAIL_CAP = 1000
_STORED_SOURCE_MAX_CHARS = 20_000

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


def _stored_reddit_body(item: dict) -> ExtractionResult | None:
    if item["source_type"] != "reddit_subreddit":
        return None
    text = str(item.get("body") or "").strip()
    if not text:
        return None

    reasons = [PartialReason.STORED_SOURCE]
    if len(text) > _STORED_SOURCE_MAX_CHARS:
        text = text[:_STORED_SOURCE_MAX_CHARS]
        reasons.append(PartialReason.EXTRACTION_TRUNCATED)
    if len(text) < 200:
        reasons.append(PartialReason.SHORT_CONTENT)
    return ExtractionResult(
        quality=ExtractionQuality.PARTIAL,
        text=text,
        reasons=tuple(reasons),
        char_count=len(text),
    )


def _warning_code(extraction: ExtractionResult) -> str | None:
    if PartialReason.STORED_SOURCE in extraction.reasons:
        return "stored_source_content"
    if extraction.quality is ExtractionQuality.PARTIAL:
        return "content_incomplete"
    return None


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


class DeepReadOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LOST = "lost"  # the claim expired or was taken over before this run could finish it


def claim_next_deep_read(
    conn: sqlite3.Connection, now: datetime, *, lease_seconds: int = LEASE_SECONDS
) -> DeepRead | None:
    """Claims the oldest pending deep read, or returns None when none is waiting."""
    for queued in list_pending_deep_reads(conn, limit=5):
        claimed = claim_deep_read(conn, queued.item_id, now, lease_seconds=lease_seconds)
        if claimed is not None and claimed.claim_token is not None:
            return claimed
    return None


async def process_claimed_deep_read(
    conn: sqlite3.Connection,
    claimed: DeepRead,
    *,
    fetcher: ArticleFetcher,
    extractor: Extractor = extract_article_text,
    generator: Generator = generate_deep_read,
    now_factory: NowFactory = _utc_now,
) -> DeepReadOutcome:
    """Fetches, extracts and writes the brief for one claimed deep read. A problem with the
    article is recorded on the deep read as a failure; an infrastructure error requeues the
    claim and re-raises."""
    item_id = claimed.item_id
    request_version = claimed.request_version
    claim_token = claimed.claim_token

    def fail(error_code: str, detail: str) -> DeepReadOutcome:
        _fail_claim(
            conn,
            item_id=item_id,
            request_version=request_version,
            claim_token=claim_token,
            error_code=error_code,
            detail=detail,
            now_factory=now_factory,
        )
        return DeepReadOutcome.FAILED

    try:
        item = get_item(conn, item_id)
        if item is None:
            return fail("unavailable", "Item no longer exists")
        if item["ai_score"] is None:
            return fail("unavailable", "Item has not been AI-ranked")

        localizer = load_localizer(conn)
        model = load_model(conn)
        fetched_url = item["url"]
        try:
            fetched = fetcher.fetch(item["url"])
        except Exception as exc:
            print(f"[deep-read] fetch failed for item {item_id}: {type(exc).__name__}: {exc}")
            return fail("fetch", f"{type(exc).__name__}: {exc}")
        if isinstance(fetched, FetchFailure):
            extraction = _stored_reddit_body(item)
            if extraction is None:
                return fail(
                    _fetch_error_code(fetched), f"{fetched.reason.value}: {fetched.detail}")
        else:
            fetched_url = fetched.url
            try:
                extraction = extractor(fetched.html, transport_truncated=fetched.truncated)
            except Exception as exc:
                print(
                    f"[deep-read] extraction failed for item {item_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                return fail("extraction", f"{type(exc).__name__}: {exc}")
            if extraction.quality is ExtractionQuality.UNUSABLE:
                stored_extraction = _stored_reddit_body(item)
                if stored_extraction is None:
                    return fail(
                        _unusable_extraction_error_code(fetched.url),
                        "Article extraction produced no usable text",
                    )
                extraction = stored_extraction
                fetched_url = item["url"]

        if not heartbeat_deep_read(
            conn,
            item_id,
            request_version,
            claim_token,
            now_factory(),
            lease_seconds=LEASE_SECONDS,
        ):
            return DeepReadOutcome.LOST

        partial_reason = ", ".join(reason.value for reason in extraction.reasons) or None
        try:
            result = await generator(
                item_id=item_id,
                item_context=ItemContext(
                    title=item["title"],
                    url=fetched_url,
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
                model=model,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[deep-read] LLM failed for item {item_id}: {type(exc).__name__}: {exc}")
            return fail("llm", f"{type(exc).__name__}: {exc}")
        completed = complete_deep_read_success(
            conn,
            item_id,
            request_version,
            claim_token,
            _result_json(result),
            localizer.code,
            now_factory(),
            warning_code=_warning_code(extraction),
        )
        return DeepReadOutcome.SUCCEEDED if completed else DeepReadOutcome.LOST
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


async def process_deep_read_queue(
    conn: sqlite3.Connection,
    *,
    fetcher_factory: FetcherFactory = ArticleFetcher,
    extractor: Extractor = extract_article_text,
    generator: Generator = generate_deep_read,
    now_factory: NowFactory = _utc_now,
    max_jobs: int = _MAX_JOBS_PER_RUN,
) -> DeepReadWorkerResult:
    """One pass for `--mode deep-read`: recover expired claims, then process up to max_jobs."""
    recovered = recover_expired_deep_reads(conn, now_factory())
    outcomes: list[DeepReadOutcome] = []
    fetcher = fetcher_factory()
    try:
        while len(outcomes) < max_jobs:
            claimed = claim_next_deep_read(conn, now_factory())
            if claimed is None:
                break
            outcomes.append(await process_claimed_deep_read(
                conn, claimed, fetcher=fetcher, extractor=extractor, generator=generator,
                now_factory=now_factory,
            ))
    finally:
        fetcher.close()
    return DeepReadWorkerResult(
        recovered=recovered,
        processed=len(outcomes),
        succeeded=outcomes.count(DeepReadOutcome.SUCCEEDED),
        failed=outcomes.count(DeepReadOutcome.FAILED),
        remaining=len(list_pending_deep_reads(conn, limit=1)),
    )
