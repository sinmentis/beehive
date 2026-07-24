# tests/collector/test_run_cycle.py
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from beehive.ai.response_parser import RankedItem
from beehive.collector.run_cycle import run_channel_cycle
from beehive.connectors.base import RawItem
from beehive.connectors.registry import register
from beehive.db.channels import create_channel, get_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import list_by_channel
from beehive.db.sources import create_source, record_fetch_success, set_source_paused
from beehive.db.sources import list_by_channel as list_sources
from beehive.domain.channels import ChannelKind
from beehive.localization import localizer_for
from beehive.notify import LogNotifier


_EN_LOCALIZER = localizer_for("en")

# The stubs stand in for a real connector on editorial, monitor, and tracker Channels alike, so
# they declare support for every kind. The compatibility policy (db.sources.create_source and the
# collector) fails closed on a connector with no declaration, so this is required, not optional.
_ALL_CHANNEL_KINDS = frozenset(ChannelKind)


class _StubConnector:
    type_key = "stub_test_source"
    supported_channel_kinds = _ALL_CHANNEL_KINDS

    def __init__(self, items=None, error=None):
        self._items = items or []
        self._error = error
        self.fetch_calls = []

    def validate_config(self, config):
        pass

    def fetch(self, config):
        self.fetch_calls.append(config)
        if self._error:
            raise self._error
        return self._items


class _StubCommentConnector(_StubConnector):
    type_key = "stub_comment_source"

    def __init__(
        self, items=None, error=None, comments_by_url=None, comment_error=None
    ):
        super().__init__(items=items, error=error)
        self._comments_by_url = comments_by_url or {}
        self._comment_error = comment_error
        self.received_targets = []

    def fetch_comments(self, target):
        self.received_targets.append(target)
        if self._comment_error:
            raise self._comment_error
        return self._comments_by_url.get(target.url, [])


class _StubRefreshConnector(_StubConnector):
    # A connector still carrying the obsolete refresh_existing_items flag. Persistence is now
    # decided by the Channel's definition (monitor/tracker -> MUTABLE_SNAPSHOT), not this flag, so
    # the flag is inert; the stub is kept to prove a lingering flag changes nothing.
    type_key = "stub_refresh_source"
    refresh_existing_items = True


class _EditorialOnlyStubConnector(_StubConnector):
    # Unlike the all-kinds stubs above, this one is compatible with editorial Channels only, so a
    # test can persist it onto a monitor Channel (via a direct INSERT that bypasses the
    # create_source gate) to exercise the collector's read-time compatibility skip.
    type_key = "stub_editorial_only_source"
    supported_channel_kinds = frozenset({ChannelKind.EDITORIAL})


def _insert_source_bypassing_policy(conn, channel_id, type_key):
    cur = conn.execute(
        "INSERT INTO sources (channel_id, type, config) VALUES (?, ?, '{}')",
        (channel_id, type_key))
    conn.commit()
    return cur.lastrowid


def _echo_ranker(scores=None):
    """A fake rank_channel that echoes each candidate's item_key back, so tests never assume
    item_key == RawItem.external_id. Scores default to a descending 100, 99, ... by candidate
    order; pass an explicit list to pin specific values."""

    async def rank(profile, votes, candidates, language, model):
        return [
            RankedItem(
                item_key=candidate.item_key,
                score=scores[index] if scores is not None else 100 - index,
                summary="s",
                rationale="r",
            )
            for index, candidate in enumerate(candidates)
        ]

    return rank


def _echo_monitor_ranker(scores=None):
    """Same as _echo_ranker but matching rank_monitor_channel's (profile, candidates, language,
    model) signature -- monitor Channels never pass a votes kwarg."""

    async def rank(profile, candidates, language, model):
        return [
            RankedItem(
                item_key=candidate.item_key,
                score=scores[index] if scores is not None else 100 - index,
                summary="s",
                rationale="r",
            )
            for index, candidate in enumerate(candidates)
        ]

    return rank


def _echo_summaries(text):
    """A fake summarize_comments keyed by each candidate's item_key (the opaque collector key),
    matching how the real summarizer echoes back the ids it was given."""

    async def summarize(candidates, language, model):
        return {candidate.item_key: text for candidate in candidates}

    return summarize


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def channel(conn):
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    return get_channel(conn, channel_id)


@pytest.mark.asyncio
async def test_scheduled_cycle_skips_a_recent_source(conn):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    channel_id = create_channel(
        conn,
        "Daily",
        "profile",
        fetch_interval_hours=24,
    )
    daily_channel = get_channel(conn, channel_id)
    connector = _StubConnector()
    register(connector)
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    record_fetch_success(
        conn,
        source_id,
        (now - timedelta(hours=23)).isoformat(),
    )

    await run_channel_cycle(
        conn, daily_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER
    )

    assert connector.fetch_calls == []


@pytest.mark.asyncio
async def test_scheduled_cycle_fetches_an_overdue_source(conn):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    channel_id = create_channel(
        conn,
        "Daily",
        "profile",
        fetch_interval_hours=24,
    )
    daily_channel = get_channel(conn, channel_id)
    connector = _StubConnector()
    register(connector)
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    record_fetch_success(
        conn,
        source_id,
        (now - timedelta(hours=25)).isoformat(),
    )

    await run_channel_cycle(
        conn, daily_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER
    )

    assert connector.fetch_calls == [{}]
    assert list_sources(conn, channel_id)[0]["last_fetch_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_forced_cycle_fetches_a_recent_source(conn):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    channel_id = create_channel(
        conn,
        "Daily",
        "profile",
        fetch_interval_hours=24,
    )
    daily_channel = get_channel(conn, channel_id)
    connector = _StubConnector()
    register(connector)
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    record_fetch_success(
        conn,
        source_id,
        (now - timedelta(hours=1)).isoformat(),
    )

    await run_channel_cycle(
        conn,
        daily_channel,
        LogNotifier(),
        force_fetch=True,
        now=now,
        localizer=_EN_LOCALIZER,
    )

    assert connector.fetch_calls == [{}]
    assert list_sources(conn, channel_id)[0]["last_fetch_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_failed_due_source_keeps_its_previous_success_timestamp(conn):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    channel_id = create_channel(
        conn,
        "Failing",
        "profile",
        fetch_interval_hours=24,
    )
    failing_channel = get_channel(conn, channel_id)
    connector = _StubConnector(error=RuntimeError("provider down"))
    register(connector)
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    previous_success = (now - timedelta(hours=25)).isoformat()
    record_fetch_success(conn, source_id, previous_success)

    await run_channel_cycle(
        conn, failing_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER
    )

    source = list_sources(conn, channel_id)[0]
    assert connector.fetch_calls == [{}]
    assert source["last_fetch_at"] == previous_success
    assert source["last_fetch_error"] == "provider down"


@pytest.mark.asyncio
async def test_scheduled_cycle_fetches_only_overdue_sources(conn):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    channel_id = create_channel(
        conn,
        "Mixed",
        "profile",
        fetch_interval_hours=24,
    )
    mixed_channel = get_channel(conn, channel_id)
    connector = _StubConnector()
    register(connector)
    recent_id = create_source(
        conn,
        channel_id,
        "stub_test_source",
        {"name": "recent"},
    )
    overdue_id = create_source(
        conn,
        channel_id,
        "stub_test_source",
        {"name": "overdue"},
    )
    recent_fetch = (now - timedelta(hours=23)).isoformat()
    record_fetch_success(conn, recent_id, recent_fetch)
    record_fetch_success(
        conn,
        overdue_id,
        (now - timedelta(hours=25)).isoformat(),
    )

    await run_channel_cycle(
        conn, mixed_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER
    )

    sources = {source["id"]: source for source in list_sources(conn, channel_id)}
    assert connector.fetch_calls == [{"name": "overdue"}]
    assert sources[recent_id]["last_fetch_at"] == recent_fetch
    assert sources[overdue_id]["last_fetch_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_scheduled_cycle_ranks_backlog_when_no_source_is_due(conn):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    channel_id = create_channel(
        conn,
        "Backlog",
        "profile",
        fetch_interval_hours=24,
    )
    backlog_channel = get_channel(conn, channel_id)
    connector = _StubConnector()
    register(connector)
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    record_fetch_success(
        conn,
        source_id,
        (now - timedelta(hours=1)).isoformat(),
    )
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) "
        "VALUES (?, 'backlog-1', 'Backlog item', 'https://example.com/backlog')",
        (source_id,),
    )
    conn.commit()

    fake_result = _echo_ranker([77])
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(
            conn, backlog_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER
        )

    assert connector.fetch_calls == []
    mock_rank.assert_awaited_once()
    assert list_by_channel(conn, channel_id)[0]["ai_score"] == 77


@pytest.mark.asyncio
async def test_happy_path_fetches_persists_and_ranks(conn, channel):
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="t1",
                    title="Rates fall",
                    url="https://x",
                    raw_metadata={"score": 100, "num_comments": 10},
                ),
            ]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})

    fake_result = _echo_ranker([91])
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91
    assert list_sources(conn, channel["id"])[0]["last_fetch_error"] is None


@pytest.mark.asyncio
async def test_monitor_channel_gets_ranked_via_rank_monitor_channel(conn):
    channel_id = create_channel(
        conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor"
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(external_id="t1", title="Beta jacket $199", url="https://x"),
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    fake_result = _echo_monitor_ranker([77])
    with (
        patch(
            "beehive.collector.run_cycle.rank_monitor_channel",
            new=AsyncMock(side_effect=fake_result),
        ) as mock_rank,
        patch(
            "beehive.collector.run_cycle.rank_channel", new=AsyncMock()
        ) as mock_editorial_rank,
    ):
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    mock_rank.assert_awaited_once()
    mock_editorial_rank.assert_not_awaited()
    assert "votes" not in mock_rank.call_args.kwargs
    items = list_by_channel(conn, channel_id)
    assert len(items) == 1
    assert items[0]["ai_score"] == 77


@pytest.mark.asyncio
async def test_refreshable_source_updates_and_re_ranks_one_stable_item(conn):
    channel_id = create_channel(
        conn, "Auction watch", "find underpriced lots", kind="monitor"
    )
    monitor_channel = get_channel(conn, channel_id)
    connector = _StubRefreshConnector(
        items=[
            RawItem(
                external_id="lot-42",
                title="Vintage amplifier",
                url="https://example.com/lot-42",
                body="RRP $1,200",
                raw_metadata={"price": 200.0, "current_bid": 200.0},
            )
        ]
    )
    register(connector)
    source_id = create_source(conn, channel_id, "stub_refresh_source", {})
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, body, raw_metadata, "
        "ai_score, ai_summary, ai_rationale, is_read) "
        "VALUES (?, 'lot-42', 'Vintage amplifier', 'https://example.com/lot-42', "
        "'Old description', '{\"price\":100.0}', 50, 'Old summary', 'Old rationale', 1)",
        (source_id,),
    )
    conn.commit()

    fake_result = _echo_monitor_ranker([88])
    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    candidate = mock_rank.call_args.kwargs["candidates"][0]
    assert candidate.price == 200.0
    assert candidate.description == "RRP $1,200"
    items = list_by_channel(conn, channel_id)
    assert len(items) == 1
    assert items[0]["ai_score"] == 88
    # A monitor Channel persists as a MUTABLE_SNAPSHOT because of its definition, so the stable row
    # is refreshed in place and re-ranked (its body changed) while its read state is preserved --
    # unlike the old editorial-style refresh, a snapshot item is never forced back to unread.
    assert items[0]["is_read"] == 1
    assert list_sources(conn, channel_id)[0]["last_fetch_new_count"] == 0


@pytest.mark.asyncio
async def test_monitor_channel_builds_product_candidates_from_raw_metadata(conn):
    channel_id = create_channel(
        conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor"
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="t1",
                    title="Beta jacket",
                    url="https://x",
                    raw_metadata={
                        "price": 199.0,
                        "compare_at_price": 299.0,
                        "on_sale": True,
                        "available": True,
                        "vendor": "Arc'teryx",
                        "product_type": "Jackets",
                        "tags": ["rain", "women"],
                    },
                ),
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    fake_result = _echo_monitor_ranker([77])
    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    candidate = mock_rank.call_args.kwargs["candidates"][0]
    assert candidate.title == "Beta jacket"
    assert candidate.price == 199.0
    assert candidate.compare_at_price == 299.0
    assert candidate.on_sale is True
    assert candidate.available is True
    assert candidate.vendor == "Arc'teryx"
    assert candidate.product_type == "Jackets"
    assert candidate.tags == ["rain", "women"]


@pytest.mark.asyncio
async def test_monitor_channel_passes_auction_context_to_the_ranker(conn):
    channel_id = create_channel(
        conn, "Auction watch", "Makita cordless tools", kind="monitor"
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="1-LOT",
                    title="MAKITA BL CORDLESS HEDGE TRIMMER",
                    url="https://x",
                    body="Unused, viewing advised",
                    raw_metadata={
                        "available": True,
                        "product_type": "Auction lot",
                        "tags": ["auction"],
                        "listing_kind": "auction_lot",
                        "auction_title": "Timed Online Only General Goods Auction",
                        "closing_at": "2026-07-23T10:00:00+12:00",
                        "currency_code": "NZD",
                        "current_bid": 500.0,
                        "buyer_premium_rate": 0.17,
                        "estimated_cost": 585.0,
                        "rrp": 1040.0,
                        "rrp_excludes_gst": True,
                        "starting_price": 100.0,
                        "estimate_low": 700.0,
                        "estimate_high": 900.0,
                        "sold_price": None,
                        "status": "active",
                    },
                ),
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    fake_result = _echo_monitor_ranker([91])
    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(
            conn,
            monitor_channel,
            LogNotifier(),
            localizer=_EN_LOCALIZER,
        )

    candidate = mock_rank.call_args.kwargs["candidates"][0]
    assert candidate.description == "Unused, viewing advised"
    assert candidate.listing_kind == "auction_lot"
    assert candidate.auction_title == "Timed Online Only General Goods Auction"
    assert candidate.closing_at == "2026-07-23T10:00:00+12:00"
    assert candidate.currency_code == "NZD"
    assert candidate.current_bid == 500.0
    assert candidate.buyer_premium_rate == 0.17
    assert candidate.estimated_cost == 585.0
    assert candidate.rrp == 1040.0
    assert candidate.rrp_excludes_gst is True
    assert candidate.starting_price == 100.0
    assert candidate.estimate_low == 700.0
    assert candidate.estimate_high == 900.0
    assert candidate.sold_price is None
    assert candidate.status == "active"


@pytest.mark.asyncio
async def test_monitor_channel_defaults_missing_product_fields_safely(conn):
    channel_id = create_channel(
        conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor"
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="t1", title="Mystery item", url="https://x"
                ),  # no raw_metadata at all
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    fake_result = _echo_monitor_ranker([50])
    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    candidate = mock_rank.call_args.kwargs["candidates"][0]
    assert candidate.price is None
    assert candidate.on_sale is False
    assert candidate.available is False
    assert candidate.vendor is None
    assert candidate.tags == []


@pytest.mark.asyncio
async def test_monitor_channel_skips_best_comment_enrichment_even_if_connector_supports_it(
    conn,
):
    channel_id = create_channel(
        conn, "Arc'teryx Outlet", "watch for price drops", kind="monitor"
    )
    monitor_channel = get_channel(conn, channel_id)
    connector = _StubCommentConnector(
        items=[
            RawItem(external_id="t1", title="Beta jacket", url="https://x"),
        ],
        comments_by_url={"https://x": ["great deal"]},
    )
    register(connector)
    create_source(conn, channel_id, "stub_comment_source", {})

    fake_result = _echo_monitor_ranker([90])
    with (
        patch(
            "beehive.collector.run_cycle.rank_monitor_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments", new=AsyncMock()
        ) as mock_summarize,
    ):
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    assert connector.received_targets == []
    mock_summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_failure_is_recorded_and_does_not_raise(conn, channel):
    register(_StubConnector(error=RuntimeError("reddit is down")))
    create_source(conn, channel["id"], "stub_test_source", {})

    await run_channel_cycle(
        conn, channel, LogNotifier(), localizer=_EN_LOCALIZER
    )  # must not raise

    sources = list_sources(conn, channel["id"])
    assert sources[0]["last_fetch_error"] == "reddit is down"


@pytest.mark.asyncio
async def test_incompatible_persisted_source_is_skipped_with_a_fetch_error(conn):
    """Collector defense in depth: a persisted Source whose type is incompatible with its
    Channel's kind is never fetched -- it records a clear fetch error and the cycle continues.
    The Source is inserted directly (bypassing create_source's write-time gate) to simulate one
    that slipped in some other way."""
    monitor_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    monitor_channel = get_channel(conn, monitor_id)
    connector = _EditorialOnlyStubConnector(
        items=[RawItem(external_id="t1", title="x", url="https://x")]
    )
    register(connector)
    source_id = _insert_source_bypassing_policy(
        conn, monitor_id, "stub_editorial_only_source"
    )

    await run_channel_cycle(
        conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
    )  # must not raise

    assert connector.fetch_calls == []  # never fetched
    source = list_sources(conn, monitor_id)[0]
    assert source["id"] == source_id
    assert source["last_fetch_error"] == (
        "Source type 'stub_editorial_only_source' is not compatible with a 'monitor' Channel"
    )
    assert list_by_channel(conn, monitor_id) == []  # nothing persisted


@pytest.mark.asyncio
async def test_llm_failure_sends_alert_and_leaves_items_unscored(conn, channel):
    register(
        _StubConnector(
            items=[RawItem(external_id="t1", title="Rates fall", url="https://x")]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=RuntimeError("timeout")),
        ),
        patch.object(notifier, "send") as mock_send,
    ):
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    mock_send.assert_called_once()
    subject, body = mock_send.call_args.args
    assert channel["name"] in subject
    assert "timeout" in body  # the raw provider error must survive untranslated
    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] is None


@pytest.mark.asyncio
async def test_llm_failure_alert_is_rendered_in_the_selected_non_english_language(
    conn, channel
):
    register(
        _StubConnector(
            items=[RawItem(external_id="t1", title="Rates fall", url="https://x")]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()
    german = localizer_for("de")

    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=RuntimeError("provider timeout")),
        ),
        patch.object(notifier, "send") as mock_send,
    ):
        await run_channel_cycle(conn, channel, notifier, localizer=german)

    subject, body = mock_send.call_args.args
    assert (
        "fehlgeschlagen" in subject
    )  # German wording, not the English/Chinese default
    assert "provider timeout" in body


@pytest.mark.asyncio
async def test_no_unscored_items_skips_ranking_call(conn, channel):
    register(_StubConnector(items=[]))
    create_source(conn, channel["id"], "stub_test_source", {})

    with patch(
        "beehive.collector.run_cycle.rank_channel", new=AsyncMock()
    ) as mock_rank:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)
    mock_rank.assert_not_called()


@pytest.mark.asyncio
async def test_ranking_call_receives_real_vote_examples(conn, channel):
    from beehive.ai.prompt_builder import VoteExample
    from beehive.db.votes import upsert_vote

    register(_StubConnector(items=[]))
    source_id = create_source(conn, channel["id"], "stub_test_source", {})
    # an already-scored item with a cast vote (feeds the few-shot block)
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, ai_score) "
        "VALUES (?, 'old1', 'Old rates news', 'https://x', 50)",
        (source_id,),
    )
    conn.commit()
    voted_item_id = conn.execute(
        "SELECT id FROM items WHERE external_id='old1'"
    ).fetchone()[0]
    upsert_vote(conn, voted_item_id, 1)
    # a fresh, unscored item so ranking actually runs this cycle
    register(
        _StubConnector(
            items=[
                RawItem(external_id="new1", title="New rates news", url="https://y"),
            ]
        )
    )

    fake_result = _echo_ranker([80])
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    _, kwargs = mock_rank.call_args
    assert len(kwargs["votes"]) == 1
    assert isinstance(kwargs["votes"][0], VoteExample)
    assert kwargs["votes"][0] == VoteExample(
        title="Old rates news", value=1, reason=None
    )


@pytest.mark.asyncio
async def test_ranking_call_receives_the_english_default_language(conn, channel):
    register(
        _StubConnector(
            items=[RawItem(external_id="t1", title="Rates fall", url="https://x")]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})

    fake_result = _echo_ranker([91])
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ) as mock_rank:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    _, kwargs = mock_rank.call_args
    assert kwargs["language"] is _EN_LOCALIZER.language
    assert kwargs["language"].llm_name == "English"


@pytest.mark.asyncio
async def test_ranking_and_comment_summary_calls_receive_the_selected_non_english_language(
    conn, channel
):
    japanese = localizer_for("ja")
    register(
        _StubCommentConnector(
            items=[RawItem(external_id="t1", title="Rates fall", url="https://x")],
            comments_by_url={"https://x": ["new context here"]},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ) as mock_rank,
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(return_value={}),
        ) as mock_summarize,
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=japanese)

    assert mock_rank.call_args.kwargs["language"] is japanese.language
    assert mock_summarize.call_args.kwargs["language"] is japanese.language
    assert japanese.language.llm_name == "Japanese"


@pytest.mark.asyncio
async def test_already_scored_items_are_never_re_ranked_or_translated(conn, channel):
    """An item that already has an ai_score/ai_summary from a previous cycle must never be
    re-sent to the LLM -- not even if the platform language changes afterwards. Existing
    stored summaries are a historical record, never retroactively translated or cleared."""
    register(_StubConnector(items=[]))
    source_id = create_source(conn, channel["id"], "stub_test_source", {})
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, ai_score, ai_summary, "
        "ai_rationale) VALUES (?, 'old1', 'Old rates news', 'https://x', 50, "
        "'existing summary text', 'existing rationale')",
        (source_id,),
    )
    conn.commit()

    with patch(
        "beehive.collector.run_cycle.rank_channel", new=AsyncMock()
    ) as mock_rank:
        await run_channel_cycle(
            conn, channel, LogNotifier(), localizer=localizer_for("fr")
        )

    mock_rank.assert_not_called()
    item = list_by_channel(conn, channel["id"])[0]
    assert item["ai_score"] == 50
    assert item["ai_summary"] == "existing summary text"
    assert item["ai_rationale"] == "existing rationale"


@pytest.mark.asyncio
async def test_run_channel_cycle_records_raw_and_new_fetch_counts(conn, channel):
    register(
        _StubConnector(
            items=[
                RawItem(external_id="t1", title="A", url="https://x", raw_metadata={}),
                RawItem(external_id="t2", title="B", url="https://y", raw_metadata={}),
            ]
        )
    )
    source_id = create_source(conn, channel["id"], "stub_test_source", {})
    # t1 already exists as a duplicate from a prior cycle -- only t2 should count as "new"
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'A', 'https://x')",
        (source_id,),
    )
    conn.commit()

    fake_result = _echo_ranker()
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    source = list_sources(conn, channel["id"])[0]
    assert source["last_fetch_raw_count"] == 2
    assert source["last_fetch_new_count"] == 1


@pytest.mark.asyncio
async def test_only_top_3_ranked_items_get_comment_fetch_attempted(conn, channel):
    register(
        _StubCommentConnector(
            items=[
                RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
                for i in range(5)
            ],
            comments_by_url={f"https://x/{i}": [f"comment {i}"] for i in range(5)},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(return_value={}),
        ) as mock_summarize,
        patch("beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    candidates = mock_summarize.await_args.args[0]
    assert {c.title for c in candidates} == {
        "Item 0",
        "Item 1",
        "Item 2",
    }  # top 3 by score


@pytest.mark.asyncio
async def test_connector_without_fetch_comments_is_skipped_without_error(conn, channel):
    register(
        _StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")])
    )
    create_source(conn, channel["id"], "stub_test_source", {})

    fake_result = _echo_ranker([91])
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ):
        await run_channel_cycle(
            conn, channel, LogNotifier(), localizer=_EN_LOCALIZER
        )  # must not raise

    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91
    assert items[0]["best_comment_summary"] is None


@pytest.mark.asyncio
async def test_fetch_comments_failure_is_caught_and_cycle_still_completes(
    conn, channel
):
    register(
        _StubCommentConnector(
            items=[RawItem(external_id="t1", title="A", url="https://x")],
            comment_error=RuntimeError("429"),
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_result),
    ):
        await run_channel_cycle(
            conn, channel, LogNotifier(), localizer=_EN_LOCALIZER
        )  # must not raise

    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91  # ranking result still persisted
    assert items[0]["best_comment_summary"] is None


@pytest.mark.asyncio
async def test_no_comments_found_skips_the_summarization_call(conn, channel):
    register(
        _StubCommentConnector(
            items=[RawItem(external_id="t1", title="A", url="https://x")],
            comments_by_url={},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments", new=AsyncMock()
        ) as mock_sum,
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)
    mock_sum.assert_not_called()


@pytest.mark.asyncio
async def test_comment_judged_not_valuable_does_not_get_persisted(conn, channel):
    register(
        _StubCommentConnector(
            items=[RawItem(external_id="t1", title="A", url="https://x")],
            comments_by_url={"https://x": ["lol same"]},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(side_effect=_echo_summaries("")),
        ),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel["id"])
    assert items[0]["best_comment_summary"] is None


@pytest.mark.asyncio
async def test_comment_judged_valuable_gets_persisted(conn, channel):
    register(
        _StubCommentConnector(
            items=[RawItem(external_id="t1", title="A", url="https://x")],
            comments_by_url={"https://x": ["actually the real number was different"]},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(side_effect=_echo_summaries("评论指出实际数字不同")),
        ),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel["id"])
    assert items[0]["best_comment_summary"] == "评论指出实际数字不同"


@pytest.mark.asyncio
async def test_sleeps_between_sequential_comment_fetches_not_before_the_first(
    conn, channel
):
    register(
        _StubCommentConnector(
            items=[
                RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
                for i in range(3)
            ],
            comments_by_url={f"https://x/{i}": [f"comment {i}"] for i in range(3)},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    assert mock_sleep.await_count == 2  # 2 delays between 3 sequential fetches


@pytest.mark.asyncio
async def test_a_failed_fetch_still_spaces_the_next_attempt(conn, channel):
    register(
        _StubCommentConnector(
            items=[
                RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
                for i in range(3)
            ],
            comments_by_url={
                "https://x/0": ["comment 0"],
                "https://x/2": ["comment 2"],
            },
            comment_error=None,
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()

    # Make the middle fetch (t1) raise, by using a connector whose fetch_comments raises only
    # for a specific URL -- build a tiny local subclass rather than reusing the shared stub,
    # since _StubCommentConnector's comment_error applies to every call uniformly.
    class _PartiallyFailingConnector(_StubCommentConnector):
        def fetch_comments(self, target):
            if target.url == "https://x/1":
                raise RuntimeError("429")
            return self._comments_by_url.get(target.url, [])

    register(
        _PartiallyFailingConnector(
            items=[
                RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
                for i in range(3)
            ],
            comments_by_url={
                "https://x/0": ["comment 0"],
                "https://x/2": ["comment 2"],
            },
        )
    )

    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    # 3 fetch attempts total (t0 succeeds, t1 raises, t2 succeeds) -- the delay must still fire
    # twice (before the 2nd and 3rd attempts), proving a failed fetch doesn't skip the spacing.
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_large_backlog_is_ranked_in_chunks_not_one_giant_call(conn, channel):
    # A single mega-batch risks exceeding the LLM call's timeout (confirmed empirically: a
    # 50-item batch took 280s+ to generate). 22 items must be split into fixed-size chunks
    # (chunk size 10, mirroring _RANKING_CHUNK_SIZE) instead of one 22-item call.
    register(
        _StubConnector(
            items=[
                RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
                for i in range(22)
            ]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})

    def fake_rank(profile, votes, candidates, language, model):
        return [
            RankedItem(item_key=c.item_key, score=50, summary="s", rationale="r")
            for c in candidates
        ]

    with patch(
        "beehive.collector.run_cycle.rank_channel", new=AsyncMock(side_effect=fake_rank)
    ) as mock_rank:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    assert mock_rank.await_count == 3  # 10 + 10 + 2
    call_sizes = [len(call.kwargs["candidates"]) for call in mock_rank.await_args_list]
    assert call_sizes == [10, 10, 2]

    items = list_by_channel(conn, channel["id"])
    assert len(items) == 22
    assert all(i["ai_score"] == 50 for i in items)


@pytest.mark.asyncio
async def test_chunk_failure_still_persists_earlier_successful_chunks(conn, channel):
    # If a later chunk's LLM call fails, items already ranked by earlier chunks must stay
    # persisted (forward progress) instead of being discarded by an all-or-nothing failure.
    register(
        _StubConnector(
            items=[
                RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
                for i in range(12)
            ]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    def fake_rank(profile, votes, candidates, language, model):
        if len(candidates) < 10:  # the 2nd (final, smaller) chunk fails
            raise RuntimeError("timeout")
        return [
            RankedItem(item_key=c.item_key, score=50, summary="s", rationale="r")
            for c in candidates
        ]

    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_rank),
        ),
        patch.object(notifier, "send") as mock_send,
    ):
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    mock_send.assert_called_once()
    items = {i["external_id"]: i for i in list_by_channel(conn, channel["id"])}
    scored = {eid for eid, i in items.items() if i["ai_score"] is not None}
    assert scored == {f"t{i}" for i in range(10)}  # first chunk persisted


@pytest.mark.asyncio
async def test_chunk_is_retried_once_before_failing(conn, channel):
    # A chunk can fail because "the model lost track of the set" (missing/extra ids -- an
    # intentional strict validation, see response_parser.py) as a one-off sampling flake, not
    # a persistent error. One retry gives the model a second independent attempt before the
    # chunk is treated as failed, without loosening the strict id-matching itself.
    register(
        _StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")])
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    call_count = {"n": 0}

    async def fake_rank(profile, votes, candidates, language, model):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("model lost track of the set")
        return [
            RankedItem(
                item_key=candidates[0].item_key, score=91, summary="s", rationale="r"
            )
        ]

    mock_rank = AsyncMock(side_effect=fake_rank)
    with (
        patch("beehive.collector.run_cycle.rank_channel", new=mock_rank),
        patch.object(notifier, "send") as mock_send,
    ):
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    assert mock_rank.await_count == 2
    mock_send.assert_not_called()
    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91


@pytest.mark.asyncio
async def test_persistently_failing_chunk_is_attempted_twice_then_alerts_once(
    conn, channel
):
    register(
        _StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")])
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    mock_rank = AsyncMock(side_effect=RuntimeError("still broken"))
    with (
        patch("beehive.collector.run_cycle.rank_channel", new=mock_rank),
        patch.object(notifier, "send") as mock_send,
    ):
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    assert mock_rank.await_count == 2  # 1 initial attempt + 1 retry, then give up
    mock_send.assert_called_once()
    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] is None


@pytest.mark.asyncio
async def test_a_connector_without_fetch_comments_between_capable_ones_adds_no_delay(
    conn, channel
):
    register(
        _StubConnector(
            items=[
                RawItem(external_id="t0", title="A", url="https://x/0"),
            ]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    register(
        _StubCommentConnector(
            items=[RawItem(external_id="t1", title="B", url="https://y/1")],
            comments_by_url={"https://y/1": ["comment 1"]},
        )
    )
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_result),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep,
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    # t0's connector has no fetch_comments (skipped, no real fetch attempt); only t1 fetches --
    # a single real attempt never needs a preceding delay.
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_persistently_failing_chunk_routes_alert_to_channel_recipient(
    conn, channel
):
    register(
        _StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")])
    )
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()
    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=RuntimeError("still broken")),
        ),
        patch.object(notifier, "send") as mock_send,
    ):
        await run_channel_cycle(
            conn,
            channel,
            notifier,
            recipient="channel@example.com",
            localizer=_EN_LOCALIZER,
        )

    assert mock_send.call_args.kwargs["to_addr"] == "channel@example.com"


@pytest.mark.asyncio
async def test_duplicate_external_ids_across_sources_update_the_correct_rows(
    conn, channel
):
    class _FirstConnector(_StubConnector):
        type_key = "duplicate_first_source"

    class _SecondConnector(_StubConnector):
        type_key = "duplicate_second_source"

    register(
        _FirstConnector(
            items=[
                RawItem(
                    external_id="shared",
                    title="First source title",
                    url="https://first",
                )
            ]
        )
    )
    register(
        _SecondConnector(
            items=[
                RawItem(
                    external_id="shared",
                    title="Second source title",
                    url="https://second",
                )
            ]
        )
    )
    first_source = create_source(conn, channel["id"], "duplicate_first_source", {})
    second_source = create_source(conn, channel["id"], "duplicate_second_source", {})

    async def fake_rank(profile, votes, candidates, language, model):
        return [
            RankedItem(
                item_key=candidate.item_key,
                score=91 if candidate.title == "First source title" else 72,
                summary=f"summary for {candidate.title}",
                rationale="source scoped",
            )
            for candidate in candidates
        ]

    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_rank),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    rows = conn.execute(
        "SELECT source_id, ai_score, ai_summary FROM items "
        "WHERE external_id = 'shared' ORDER BY source_id"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "source_id": first_source,
            "ai_score": 91.0,
            "ai_summary": "summary for First source title",
        },
        {
            "source_id": second_source,
            "ai_score": 72.0,
            "ai_summary": "summary for Second source title",
        },
    ]


@pytest.mark.asyncio
async def test_comment_fetch_receives_provider_identity_url_and_metadata(conn, channel):
    connector = _StubCommentConnector(
        items=[
            RawItem(
                external_id="48888193",
                title="HN story",
                url="https://example.com/article",
                raw_metadata={
                    "hn_id": 48888193,
                    "hn_url": "https://news.ycombinator.com/item?id=48888193",
                },
            )
        ],
        comments_by_url={"https://example.com/article": ["useful context"]},
    )
    register(connector)
    create_source(conn, channel["id"], "stub_comment_source", {})

    async def fake_rank(profile, votes, candidates, language, model):
        return [
            RankedItem(
                item_key=candidates[0].item_key,
                score=91,
                summary="s",
                rationale="r",
            )
        ]

    with (
        patch(
            "beehive.collector.run_cycle.rank_channel",
            new=AsyncMock(side_effect=fake_rank),
        ),
        patch(
            "beehive.collector.run_cycle.summarize_comments",
            new=AsyncMock(return_value={}),
        ),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    target = connector.received_targets[0]
    assert target.external_id == "48888193"
    assert target.url == "https://example.com/article"
    assert target.raw_metadata["hn_id"] == 48888193


@pytest.mark.asyncio
async def test_official_feed_raw_item_flows_through_the_cycle(conn):
    from beehive.connectors.official_feeds import FeedDefinition, OfficialFeedConnector

    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>OCR decision</title>"
        b"<link>https://www.rbnz.govt.nz/news/9</link>"
        b"<guid>https://www.rbnz.govt.nz/news/9</guid>"
        b"<pubDate>Wed, 09 Jul 2026 14:00:00 +1200</pubDate>"
        b"<description>&lt;p&gt;Held.&lt;/p&gt;</description></item>"
        b"</channel></rss>"
    )
    definition = FeedDefinition(
        "rbnz_pipeline_test", "https://www.rbnz.govt.nz/feeds/news", "RBNZ News"
    )
    register(OfficialFeedConnector(definition, fetch_bytes=lambda url: feed))

    channel_id = create_channel(conn, "Econ", "profile", fetch_interval_hours=24)
    create_source(conn, channel_id, "rbnz_pipeline_test", {})
    channel = get_channel(conn, channel_id)

    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=_echo_ranker()),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel_id)
    assert any(i["title"] == "OCR decision" for i in items)


# ===========================================================================
# Definition-driven persistence, actionable events, and event gating through a full cycle
# ===========================================================================


def _item_events(conn):
    return conn.execute(
        "SELECT item_id, event_type, ready_at, suppressed_at, delivered_at, payload "
        "FROM item_events ORDER BY id"
    ).fetchall()


@pytest.mark.asyncio
async def test_tracker_channel_gets_ranked_via_rank_monitor_channel(conn):
    # A tracker shares the LISTING ranking contract with monitor (rank_monitor_channel, no votes),
    # never the editorial rank_channel path.
    channel_id = create_channel(conn, "Lot tracker", "watch lots", kind="tracker")
    tracker_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="lot-1",
                    title="Vintage lot",
                    url="https://x/lot-1",
                    raw_metadata={"price": 100.0, "listing_kind": "auction_lot"},
                ),
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    fake_result = _echo_monitor_ranker([77])
    with (
        patch(
            "beehive.collector.run_cycle.rank_monitor_channel",
            new=AsyncMock(side_effect=fake_result),
        ) as mock_rank,
        patch(
            "beehive.collector.run_cycle.rank_channel", new=AsyncMock()
        ) as mock_editorial_rank,
    ):
        await run_channel_cycle(
            conn, tracker_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    mock_rank.assert_awaited_once()
    mock_editorial_rank.assert_not_awaited()
    assert "votes" not in mock_rank.call_args.kwargs
    items = list_by_channel(conn, channel_id)
    assert items[0]["ai_score"] == 77


@pytest.mark.asyncio
async def test_failed_fetch_does_not_reconcile_absent_monitor_items(conn):
    # Reconciliation retires listings absent from a *successful* snapshot. A failed fetch returned
    # nothing because it broke, not because the catalogue emptied, so nothing may go inactive.
    channel_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    monitor_channel = get_channel(conn, channel_id)
    register(_StubConnector(error=RuntimeError("store down")))
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    for external_id in ("a", "b"):
        conn.execute(
            "INSERT INTO items (source_id, external_id, title, url, ai_score, last_seen_at) "
            "VALUES (?, ?, 'T', 'https://x', 50, '2026-07-01T00:00:00')",
            (source_id, external_id),
        )
    conn.commit()

    await run_channel_cycle(
        conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
    )

    active = conn.execute(
        "SELECT COUNT(*) FROM items WHERE inactive_at IS NULL"
    ).fetchone()[0]
    assert active == 2  # nothing retired
    assert list_sources(conn, channel_id)[0]["last_fetch_error"] == "store down"


@pytest.mark.asyncio
async def test_successful_empty_monitor_fetch_reconciles_items_inactive(conn):
    # The contrast to the failed-fetch case: a *successful* empty snapshot does retire the lot.
    channel_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    monitor_channel = get_channel(conn, channel_id)
    register(_StubConnector(items=[]))
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    for external_id in ("a", "b"):
        conn.execute(
            "INSERT INTO items (source_id, external_id, title, url, ai_score, last_seen_at) "
            "VALUES (?, ?, 'T', 'https://x', 50, '2026-07-01T00:00:00')",
            (source_id, external_id),
        )
    conn.commit()

    await run_channel_cycle(
        conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
    )

    active = conn.execute(
        "SELECT COUNT(*) FROM items WHERE inactive_at IS NULL"
    ).fetchone()[0]
    assert active == 0  # every absent listing retired
    assert list_sources(conn, channel_id)[0]["last_fetch_error"] is None


@pytest.mark.asyncio
async def test_editorial_discovered_event_is_created_and_readied(conn, channel):
    # Editorial now stages a DISCOVERED event for the Email Group path (its definition permits it),
    # and ranking readies it exactly like the listing kinds.
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="t1",
                    title="Rates fall",
                    url="https://x",
                    raw_metadata={"score": 100, "num_comments": 10},
                ),
            ]
        )
    )
    create_source(conn, channel["id"], "stub_test_source", {})

    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=_echo_ranker([91])),
    ):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    events = _item_events(conn)
    assert len(events) == 1
    assert events[0]["event_type"] == "discovered"
    assert events[0]["ready_at"] is not None  # 91 >= minimum_score 0
    assert events[0]["suppressed_at"] is None


@pytest.mark.asyncio
async def test_monitor_discovered_event_is_readied_at_or_above_threshold(conn):
    channel_id = create_channel(
        conn, "Outlet", "deals", kind="monitor", minimum_score=50
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="1001",
                    title="Beta jacket",
                    url="https://x/1001",
                    raw_metadata={"price": 199.0, "available": True},
                ),
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=_echo_monitor_ranker([80])),
    ):
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    events = _item_events(conn)
    assert [e["event_type"] for e in events] == ["discovered"]
    assert events[0]["ready_at"] is not None  # 80 >= 50
    assert events[0]["suppressed_at"] is None


@pytest.mark.asyncio
async def test_monitor_discovered_event_is_suppressed_below_threshold(conn):
    channel_id = create_channel(
        conn, "Outlet", "deals", kind="monitor", minimum_score=50
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="1001",
                    title="Beta jacket",
                    url="https://x/1001",
                    raw_metadata={"price": 199.0, "available": True},
                ),
            ]
        )
    )
    create_source(conn, channel_id, "stub_test_source", {})

    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=_echo_monitor_ranker([40])),
    ):
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    events = _item_events(conn)
    assert [e["event_type"] for e in events] == ["discovered"]
    assert events[0]["ready_at"] is None  # 40 < 50
    assert events[0]["suppressed_at"] is not None


@pytest.mark.asyncio
async def test_monitor_price_drop_event_is_staged_and_readied_after_rerank(conn):
    channel_id = create_channel(
        conn, "Outlet", "deals", kind="monitor", minimum_score=50
    )
    monitor_channel = get_channel(conn, channel_id)
    register(
        _StubConnector(
            items=[
                RawItem(
                    external_id="1001",
                    title="Alpha",
                    url="https://x/1001",
                    raw_metadata={"price": 40.0, "available": True},
                ),
            ]
        )
    )
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    # A previously-scored listing at $50 -- only its price falls this cycle.
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url, body, raw_metadata, "
        "ai_score, ai_summary, ai_rationale, last_seen_at) "
        "VALUES (?, '1001', 'Alpha', 'https://x/1001', '', ?, 80, 's', 'r', '2026-07-01T00:00:00')",
        (source_id, json.dumps({"price": 50.0, "available": True})),
    )
    conn.commit()

    with patch(
        "beehive.collector.run_cycle.rank_monitor_channel",
        new=AsyncMock(side_effect=_echo_monitor_ranker([70])),
    ):
        await run_channel_cycle(
            conn, monitor_channel, LogNotifier(), localizer=_EN_LOCALIZER
        )

    events = _item_events(conn)
    assert [e["event_type"] for e in events] == ["price_drop"]
    assert json.loads(events[0]["payload"]) == {"old_price": 50.0, "new_price": 40.0}
    assert events[0]["ready_at"] is not None  # 70 >= 50, so the drop is deliverable


@pytest.mark.asyncio
async def test_paused_source_is_skipped_entirely(conn, channel):
    # A paused Source is dormant: even a forced cycle must not fetch it.
    connector = _StubConnector(items=[RawItem(external_id="t1", title="T", url="https://x")])
    register(connector)
    source_id = create_source(conn, channel["id"], "stub_test_source", {})
    set_source_paused(conn, source_id, True, now_iso="2026-07-01T00:00:00")

    await run_channel_cycle(
        conn, channel, LogNotifier(), force_fetch=True, localizer=_EN_LOCALIZER
    )

    assert connector.fetch_calls == []
    # Nothing was fetched, so no attempt/status was stamped either.
    row = list_sources(conn, channel["id"])[0]
    assert row["last_attempt_at"] is None
    assert row["last_fetch_status"] is None


@pytest.mark.asyncio
async def test_paused_monitor_source_is_not_reconciled_inactive(conn):
    # Skipping a paused Source must never run snapshot reconciliation for it, so its still-valid
    # listings stay active rather than being retired as "absent from the (never taken) snapshot".
    channel_id = create_channel(conn, "Outlet", "deals", kind="monitor")
    monitor_channel = get_channel(conn, channel_id)
    register(_StubConnector(items=[]))
    source_id = create_source(conn, channel_id, "stub_test_source", {})
    for external_id in ("a", "b"):
        conn.execute(
            "INSERT INTO items (source_id, external_id, title, url, ai_score, last_seen_at) "
            "VALUES (?, ?, 'T', 'https://x', 50, '2026-07-01T00:00:00')",
            (source_id, external_id),
        )
    conn.commit()
    set_source_paused(conn, source_id, True, now_iso="2026-07-01T00:00:00")

    await run_channel_cycle(
        conn, monitor_channel, LogNotifier(), force_fetch=True, localizer=_EN_LOCALIZER
    )

    active = conn.execute(
        "SELECT COUNT(*) FROM items WHERE inactive_at IS NULL"
    ).fetchone()[0]
    assert active == 2  # nothing retired for a paused Source
