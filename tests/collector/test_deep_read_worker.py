import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from beehive.collector.deep_read_trigger import MARKER_NAME
from beehive.collector.deep_read_worker import process_deep_read_queue
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import get_deep_read, request_deep_read
from beehive.db.items import insert_new, update_ai_ranking_by_id
from beehive.db.sources import create_source
from beehive.deep_read.extract import ExtractionQuality, ExtractionResult, PartialReason
from beehive.deep_read.fetch import ArticleFetcher, FetchedArticle, FetchFailure, FetchFailureReason
from beehive.deep_read.summarize import DeepReadResult, ImportantFigure
from beehive.localization import save_language

_NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)


def _create_ranked_item(
    conn,
    *,
    title="Fed report",
    url="https://example.com/article",
    source_type="google_news_query",
    body="Feed excerpt",
):
    channel_id = create_channel(conn, "Markets", "macro news")
    source_id = create_source(conn, channel_id, source_type, {"query": "Fed"})
    insert_new(conn, source_id, RawItem(
        external_id="item-1",
        title=title,
        url=url,
        body=body,
        created_at=_NOW,
        raw_metadata={"source_name": "Federal Reserve"},
    ))
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    update_ai_ranking_by_id(conn, item_id, 94, "Existing summary", "Relevant")
    return item_id


class _FakeFetcher:
    def __init__(self, outcome):
        self.outcome = outcome
        self.urls = []
        self.closed = False

    def fetch(self, url):
        self.urls.append(url)
        return self.outcome

    def close(self):
        self.closed = True


def _complete_extraction(*_args, **_kwargs):
    return ExtractionResult(
        quality=ExtractionQuality.COMPLETE,
        text="The report says inflation slowed while wage growth remained elevated.",
        reasons=(),
        char_count=70,
    )


def _partial_extraction(*_args, **_kwargs):
    return ExtractionResult(
        quality=ExtractionQuality.PARTIAL,
        text="Subscribers can read the report excerpt showing inflation slowed.",
        reasons=(PartialReason.PAYWALL_LIKE,),
        char_count=65,
    )


def _result(item_id):
    return DeepReadResult(
        item_id=str(item_id),
        bottom_line="Inflation slowed, but wages still argue against an immediate cut.",
        key_findings=["Core inflation slowed for a third month."],
        important_figures=[ImportantFigure(value="2.6%", label="Annual core inflation")],
        why_it_matters="The report supports patience at the next meeting.",
        limitations="",
    )


@pytest.fixture
def queued_db(tmp_path):
    db_path = str(tmp_path / "worker.db")
    conn = connect(db_path)
    init_schema(conn)
    item_id = _create_ranked_item(conn)
    request_deep_read(conn, item_id, _NOW)
    return conn, item_id, tmp_path


@pytest.mark.asyncio
async def test_worker_completes_queued_deep_read_in_current_language(queued_db):
    conn, item_id, data_dir = queued_db
    save_language(conn, "ja")
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/final",
        status_code=200,
        content_type="text/html",
        html="<article>full article</article>",
        truncated=False,
    ))
    generator = AsyncMock(return_value=_result(item_id))

    result = await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        extractor=_complete_extraction,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert result.succeeded == 1
    assert stored.status == "ready"
    assert stored.language_code == "ja"
    assert stored.warning_code is None
    assert '"bottom_line"' in stored.result_json
    assert generator.await_args.kwargs["localizer"].code == "ja"
    assert generator.await_args.kwargs["item_context"].url == "https://example.com/final"
    assert fetcher.urls == ["https://example.com/article"]
    assert fetcher.closed is True


@pytest.mark.asyncio
async def test_worker_marks_partial_content_with_warning(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        html="<article>partial article</article>",
        truncated=False,
    ))
    generator = AsyncMock(return_value=_result(item_id))

    await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        extractor=_partial_extraction,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert stored.status == "ready"
    assert stored.warning_code == "content_incomplete"
    partial = generator.await_args.kwargs["partial_content"]
    assert partial.is_partial is True
    assert partial.reason == "paywall_like"


@pytest.mark.asyncio
async def test_worker_persists_fetch_failure_without_calling_llm(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = _FakeFetcher(FetchFailure(
        FetchFailureReason.PROHIBITED_ADDRESS,
        "destination resolved to loopback",
    ))
    generator = AsyncMock()

    result = await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert result.failed == 1
    assert stored.status == "failed"
    assert stored.error_code == "fetch"
    assert "prohibited_address" in stored.error_detail
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_classifies_publisher_404_without_calling_llm(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = ArticleFetcher(
        resolve_host=lambda _hostname: ["93.184.216.34"],
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                404,
                headers={"content-type": "text/html"},
                content=b"missing",
            )
        ),
    )
    generator = AsyncMock()

    await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert stored.error_code == "fetch_not_found"
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_persists_unusable_extraction_failure(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        html="<nav>no article</nav>",
        truncated=False,
    ))
    unusable = ExtractionResult(
        quality=ExtractionQuality.UNUSABLE,
        text="",
        reasons=(),
        char_count=0,
    )

    await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        extractor=lambda *_args, **_kwargs: unusable,
        generator=AsyncMock(),
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert stored.status == "failed"
    assert stored.error_code == "extraction_no_text"


@pytest.mark.asyncio
async def test_worker_classifies_google_news_wrapper_without_calling_llm(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://news.google.com/rss/articles/opaque-id?hl=en",
        status_code=200,
        content_type="text/html",
        html="<html><title>Google News</title></html>",
        truncated=False,
    ))
    unusable = ExtractionResult(
        quality=ExtractionQuality.UNUSABLE,
        text="",
        reasons=(),
        char_count=0,
    )
    generator = AsyncMock()

    await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        extractor=lambda *_args, **_kwargs: unusable,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert stored.error_code == "extraction_google_news"
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_uses_stored_reddit_body_when_fetch_is_blocked(tmp_path):
    conn = connect(str(tmp_path / "worker.db"))
    init_schema(conn)
    stored_body = (
        "The Reserve Bank increased the OCR to 2.50%. The post explains the rate outlook, "
        "recent swap-rate moves, and the likely effect on mortgage rates. " * 5
    )
    item_id = _create_ranked_item(
        conn,
        title="OCR increased to 2.50%",
        url="https://www.reddit.com/r/PersonalFinanceNZ/comments/example/post/",
        source_type="reddit_subreddit",
        body=stored_body,
    )
    request_deep_read(conn, item_id, _NOW)
    fetcher = _FakeFetcher(FetchFailure(
        FetchFailureReason.HTTP_ERROR,
        "unexpected status code 403",
        status_code=403,
    ))
    generator = AsyncMock(return_value=_result(item_id))

    result = await process_deep_read_queue(
        conn,
        str(tmp_path),
        fetcher_factory=lambda: fetcher,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert result.succeeded == 1
    assert stored.status == "ready"
    assert stored.warning_code == "stored_source_content"
    assert generator.await_args.kwargs["article_text"] == stored_body.strip()
    partial = generator.await_args.kwargs["partial_content"]
    assert partial.is_partial is True
    assert partial.reason == "stored_source"


@pytest.mark.asyncio
async def test_worker_uses_stored_reddit_body_when_live_html_is_unusable(tmp_path):
    conn = connect(str(tmp_path / "worker.db"))
    init_schema(conn)
    stored_body = "A detailed stored Reddit self-post. " * 20
    item_url = "https://www.reddit.com/r/example/comments/item/post/"
    item_id = _create_ranked_item(
        conn,
        url=item_url,
        source_type="reddit_subreddit",
        body=stored_body,
    )
    request_deep_read(conn, item_id, _NOW)
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://www.reddit.com/interstitial",
        status_code=200,
        content_type="text/html",
        html="<nav>bot check</nav>",
        truncated=False,
    ))
    unusable = ExtractionResult(
        quality=ExtractionQuality.UNUSABLE,
        text="",
        reasons=(),
        char_count=0,
    )
    generator = AsyncMock(return_value=_result(item_id))

    result = await process_deep_read_queue(
        conn,
        str(tmp_path),
        fetcher_factory=lambda: fetcher,
        extractor=lambda *_args, **_kwargs: unusable,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    assert result.succeeded == 1
    assert get_deep_read(conn, item_id).warning_code == "stored_source_content"
    assert generator.await_args.kwargs["article_text"] == stored_body.strip()
    assert generator.await_args.kwargs["item_context"].url == item_url


@pytest.mark.asyncio
async def test_worker_keeps_fetch_failure_when_reddit_body_is_empty(tmp_path):
    conn = connect(str(tmp_path / "worker.db"))
    init_schema(conn)
    item_id = _create_ranked_item(
        conn,
        url="https://www.reddit.com/r/example/comments/item/post/",
        source_type="reddit_subreddit",
        body="   ",
    )
    request_deep_read(conn, item_id, _NOW)
    fetcher = _FakeFetcher(FetchFailure(
        FetchFailureReason.HTTP_ERROR,
        "unexpected status code 403",
        status_code=403,
    ))
    generator = AsyncMock()

    result = await process_deep_read_queue(
        conn,
        str(tmp_path),
        fetcher_factory=lambda: fetcher,
        generator=generator,
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert result.failed == 1
    assert stored.status == "failed"
    assert stored.error_code == "fetch_http_error"
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_classifies_extractor_exception_as_extraction_failure(queued_db):
    conn, item_id, data_dir = queued_db
    def bad_extractor(*_args, **_kwargs):
        raise ValueError("bad HTML")

    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        html="<article>full article</article>",
        truncated=False,
    ))

    await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        extractor=bad_extractor,
        generator=AsyncMock(),
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert stored.status == "failed"
    assert stored.error_code == "extraction"
    assert "bad HTML" in stored.error_detail


@pytest.mark.asyncio
async def test_worker_isolates_llm_failure(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        html="<article>full article</article>",
        truncated=False,
    ))

    await process_deep_read_queue(
        conn,
        str(data_dir),
        fetcher_factory=lambda: fetcher,
        extractor=_complete_extraction,
        generator=AsyncMock(side_effect=RuntimeError("Copilot unavailable")),
        now_factory=lambda: _NOW,
    )

    stored = get_deep_read(conn, item_id)
    assert stored.status == "failed"
    assert stored.error_code == "llm"
    assert "Copilot unavailable" in stored.error_detail


@pytest.mark.asyncio
async def test_worker_processes_bounded_jobs_and_rearms_remaining_work(tmp_path):
    conn = connect(str(tmp_path / "worker.db"))
    init_schema(conn)
    first = _create_ranked_item(conn, title="First")
    source_id = conn.execute("SELECT source_id FROM items WHERE id = ?", (first,)).fetchone()[0]
    insert_new(conn, source_id, RawItem(
        external_id="item-2",
        title="Second",
        url="https://example.com/second",
        body="Excerpt",
        created_at=_NOW,
        raw_metadata={},
    ))
    second = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    update_ai_ranking_by_id(conn, second, 90, "Existing", "Relevant")
    request_deep_read(conn, first, _NOW)
    request_deep_read(conn, second, _NOW + timedelta(seconds=1))
    inflight = tmp_path / f"{MARKER_NAME}.inflight"
    inflight.write_text("pending\n")
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        html="<article>full article</article>",
        truncated=False,
    ))

    result = await process_deep_read_queue(
        conn,
        str(tmp_path),
        fetcher_factory=lambda: fetcher,
        extractor=_complete_extraction,
        generator=AsyncMock(side_effect=lambda **kwargs: _result(kwargs["item_id"])),
        now_factory=lambda: _NOW + timedelta(minutes=1),
        max_jobs=1,
    )

    assert result.processed == 1
    assert result.remaining == 1
    assert not inflight.exists()
    assert (tmp_path / MARKER_NAME).exists()


@pytest.mark.asyncio
async def test_worker_requeues_claim_when_cancelled(queued_db):
    conn, item_id, data_dir = queued_db
    fetcher = _FakeFetcher(FetchedArticle(
        url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        html="<article>full article</article>",
        truncated=False,
    ))

    with pytest.raises(asyncio.CancelledError):
        await process_deep_read_queue(
            conn,
            str(data_dir),
            fetcher_factory=lambda: fetcher,
            extractor=_complete_extraction,
            generator=AsyncMock(side_effect=asyncio.CancelledError),
            now_factory=lambda: _NOW,
        )

    assert get_deep_read(conn, item_id).status == "pending"
