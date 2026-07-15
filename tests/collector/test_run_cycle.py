# tests/collector/test_run_cycle.py
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
from beehive.db.sources import create_source, record_fetch_success
from beehive.db.sources import list_by_channel as list_sources
from beehive.localization import localizer_for
from beehive.notify import LogNotifier


_EN_LOCALIZER = localizer_for("en")


class _StubConnector:
    type_key = "stub_test_source"

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

    def __init__(self, items=None, error=None, comments_by_url=None, comment_error=None):
        super().__init__(items=items, error=error)
        self._comments_by_url = comments_by_url or {}
        self._comment_error = comment_error
        self.received_targets = []

    def fetch_comments(self, target):
        self.received_targets.append(target)
        if self._comment_error:
            raise self._comment_error
        return self._comments_by_url.get(target.url, [])


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

    await run_channel_cycle(conn, daily_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER)

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

    await run_channel_cycle(conn, daily_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER)

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

    await run_channel_cycle(conn, failing_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER)

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

    await run_channel_cycle(conn, mixed_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER)

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
        await run_channel_cycle(conn, backlog_channel, LogNotifier(), now=now, localizer=_EN_LOCALIZER)

    assert connector.fetch_calls == []
    mock_rank.assert_awaited_once()
    assert list_by_channel(conn, channel_id)[0]["ai_score"] == 77


@pytest.mark.asyncio
async def test_happy_path_fetches_persists_and_ranks(conn, channel):
    register(_StubConnector(items=[
        RawItem(external_id="t1", title="Rates fall", url="https://x",
                raw_metadata={"score": 100, "num_comments": 10}),
    ]))
    create_source(conn, channel["id"], "stub_test_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel", new=AsyncMock(side_effect=fake_result)):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91
    assert list_sources(conn, channel["id"])[0]["last_fetch_error"] is None


@pytest.mark.asyncio
async def test_source_failure_is_recorded_and_does_not_raise(conn, channel):
    register(_StubConnector(error=RuntimeError("reddit is down")))
    create_source(conn, channel["id"], "stub_test_source", {})

    await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)  # must not raise

    sources = list_sources(conn, channel["id"])
    assert sources[0]["last_fetch_error"] == "reddit is down"


@pytest.mark.asyncio
async def test_llm_failure_sends_alert_and_leaves_items_unscored(conn, channel):
    register(_StubConnector(items=[RawItem(external_id="t1", title="Rates fall", url="https://x")]))
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=RuntimeError("timeout"))), \
         patch.object(notifier, "send") as mock_send:
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    mock_send.assert_called_once()
    subject, body = mock_send.call_args.args
    assert channel["name"] in subject
    assert "timeout" in body  # the raw provider error must survive untranslated
    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] is None


@pytest.mark.asyncio
async def test_llm_failure_alert_is_rendered_in_the_selected_non_english_language(conn, channel):
    register(_StubConnector(items=[RawItem(external_id="t1", title="Rates fall", url="https://x")]))
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()
    german = localizer_for("de")

    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=RuntimeError("provider timeout"))), \
         patch.object(notifier, "send") as mock_send:
        await run_channel_cycle(conn, channel, notifier, localizer=german)

    subject, body = mock_send.call_args.args
    assert "fehlgeschlagen" in subject  # German wording, not the English/Chinese default
    assert "provider timeout" in body


@pytest.mark.asyncio
async def test_no_unscored_items_skips_ranking_call(conn, channel):
    register(_StubConnector(items=[]))
    create_source(conn, channel["id"], "stub_test_source", {})

    with patch("beehive.collector.run_cycle.rank_channel", new=AsyncMock()) as mock_rank:
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
        "VALUES (?, 'old1', 'Old rates news', 'https://x', 50)", (source_id,))
    conn.commit()
    voted_item_id = conn.execute(
        "SELECT id FROM items WHERE external_id='old1'").fetchone()[0]
    upsert_vote(conn, voted_item_id, 1)
    # a fresh, unscored item so ranking actually runs this cycle
    register(_StubConnector(items=[
        RawItem(external_id="new1", title="New rates news", url="https://y"),
    ]))

    fake_result = _echo_ranker([80])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)) as mock_rank:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    _, kwargs = mock_rank.call_args
    assert len(kwargs["votes"]) == 1
    assert isinstance(kwargs["votes"][0], VoteExample)
    assert kwargs["votes"][0] == VoteExample(title="Old rates news", value=1, reason=None)


@pytest.mark.asyncio
async def test_ranking_call_receives_the_english_default_language(conn, channel):
    register(_StubConnector(items=[RawItem(external_id="t1", title="Rates fall", url="https://x")]))
    create_source(conn, channel["id"], "stub_test_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)) as mock_rank:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    _, kwargs = mock_rank.call_args
    assert kwargs["language"] is _EN_LOCALIZER.language
    assert kwargs["language"].llm_name == "English"


@pytest.mark.asyncio
async def test_ranking_and_comment_summary_calls_receive_the_selected_non_english_language(
        conn, channel):
    japanese = localizer_for("ja")
    register(_StubCommentConnector(
        items=[RawItem(external_id="t1", title="Rates fall", url="https://x")],
        comments_by_url={"https://x": ["new context here"]}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)) as mock_rank, \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(return_value={})) as mock_summarize:
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
        "'existing summary text', 'existing rationale')", (source_id,))
    conn.commit()

    with patch("beehive.collector.run_cycle.rank_channel", new=AsyncMock()) as mock_rank:
        await run_channel_cycle(
            conn, channel, LogNotifier(), localizer=localizer_for("fr"))

    mock_rank.assert_not_called()
    item = list_by_channel(conn, channel["id"])[0]
    assert item["ai_score"] == 50
    assert item["ai_summary"] == "existing summary text"
    assert item["ai_rationale"] == "existing rationale"


@pytest.mark.asyncio
async def test_run_channel_cycle_records_raw_and_new_fetch_counts(conn, channel):
    register(_StubConnector(items=[
        RawItem(external_id="t1", title="A", url="https://x", raw_metadata={}),
        RawItem(external_id="t2", title="B", url="https://y", raw_metadata={}),
    ]))
    source_id = create_source(conn, channel["id"], "stub_test_source", {})
    # t1 already exists as a duplicate from a prior cycle -- only t2 should count as "new"
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 't1', 'A', 'https://x')",
        (source_id,))
    conn.commit()

    fake_result = _echo_ranker()
    with patch("beehive.collector.run_cycle.rank_channel", new=AsyncMock(side_effect=fake_result)):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    source = list_sources(conn, channel["id"])[0]
    assert source["last_fetch_raw_count"] == 2
    assert source["last_fetch_new_count"] == 1


@pytest.mark.asyncio
async def test_only_top_3_ranked_items_get_comment_fetch_attempted(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
               for i in range(5)],
        comments_by_url={f"https://x/{i}": [f"comment {i}"] for i in range(5)}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(return_value={})) as mock_summarize, \
         patch("beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    candidates = mock_summarize.await_args.args[0]
    assert {c.title for c in candidates} == {"Item 0", "Item 1", "Item 2"}  # top 3 by score


@pytest.mark.asyncio
async def test_connector_without_fetch_comments_is_skipped_without_error(conn, channel):
    register(_StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")]))
    create_source(conn, channel["id"], "stub_test_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)  # must not raise

    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91
    assert items[0]["best_comment_summary"] is None


@pytest.mark.asyncio
async def test_fetch_comments_failure_is_caught_and_cycle_still_completes(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id="t1", title="A", url="https://x")],
        comment_error=RuntimeError("429")))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)  # must not raise

    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91  # ranking result still persisted
    assert items[0]["best_comment_summary"] is None


@pytest.mark.asyncio
async def test_no_comments_found_skips_the_summarization_call(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id="t1", title="A", url="https://x")], comments_by_url={}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments", new=AsyncMock()) as mock_sum:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)
    mock_sum.assert_not_called()


@pytest.mark.asyncio
async def test_comment_judged_not_valuable_does_not_get_persisted(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id="t1", title="A", url="https://x")],
        comments_by_url={"https://x": ["lol same"]}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(side_effect=_echo_summaries(""))):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel["id"])
    assert items[0]["best_comment_summary"] is None


@pytest.mark.asyncio
async def test_comment_judged_valuable_gets_persisted(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id="t1", title="A", url="https://x")],
        comments_by_url={"https://x": ["actually the real number was different"]}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker([91])
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(side_effect=_echo_summaries("评论指出实际数字不同"))):
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    items = list_by_channel(conn, channel["id"])
    assert items[0]["best_comment_summary"] == "评论指出实际数字不同"


@pytest.mark.asyncio
async def test_sleeps_between_sequential_comment_fetches_not_before_the_first(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
               for i in range(3)],
        comments_by_url={f"https://x/{i}": [f"comment {i}"] for i in range(3)}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(return_value={})), \
         patch("beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    assert mock_sleep.await_count == 2  # 2 delays between 3 sequential fetches


@pytest.mark.asyncio
async def test_a_failed_fetch_still_spaces_the_next_attempt(conn, channel):
    register(_StubCommentConnector(
        items=[RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
               for i in range(3)],
        comments_by_url={"https://x/0": ["comment 0"], "https://x/2": ["comment 2"]},
        comment_error=None))
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

    register(_PartiallyFailingConnector(
        items=[RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
               for i in range(3)],
        comments_by_url={"https://x/0": ["comment 0"], "https://x/2": ["comment 2"]}))

    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(return_value={})), \
         patch("beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    # 3 fetch attempts total (t0 succeeds, t1 raises, t2 succeeds) -- the delay must still fire
    # twice (before the 2nd and 3rd attempts), proving a failed fetch doesn't skip the spacing.
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_large_backlog_is_ranked_in_chunks_not_one_giant_call(conn, channel):
    # A single mega-batch risks exceeding the LLM call's timeout (confirmed empirically: a
    # 50-item batch took 280s+ to generate). 22 items must be split into fixed-size chunks
    # (chunk size 10, mirroring _RANKING_CHUNK_SIZE) instead of one 22-item call.
    register(_StubConnector(items=[
        RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
        for i in range(22)
    ]))
    create_source(conn, channel["id"], "stub_test_source", {})

    def fake_rank(profile, votes, candidates, language, model):
        return [RankedItem(item_key=c.item_key, score=50, summary="s", rationale="r")
                for c in candidates]

    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_rank)) as mock_rank:
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
    register(_StubConnector(items=[
        RawItem(external_id=f"t{i}", title=f"Item {i}", url=f"https://x/{i}")
        for i in range(12)
    ]))
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    def fake_rank(profile, votes, candidates, language, model):
        if len(candidates) < 10:  # the 2nd (final, smaller) chunk fails
            raise RuntimeError("timeout")
        return [RankedItem(item_key=c.item_key, score=50, summary="s", rationale="r")
                for c in candidates]

    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_rank)), \
         patch.object(notifier, "send") as mock_send:
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
    register(_StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")]))
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    call_count = {"n": 0}

    async def fake_rank(profile, votes, candidates, language, model):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("model lost track of the set")
        return [RankedItem(item_key=candidates[0].item_key, score=91, summary="s", rationale="r")]

    mock_rank = AsyncMock(side_effect=fake_rank)
    with patch("beehive.collector.run_cycle.rank_channel", new=mock_rank), \
         patch.object(notifier, "send") as mock_send:
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    assert mock_rank.await_count == 2
    mock_send.assert_not_called()
    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] == 91


@pytest.mark.asyncio
async def test_persistently_failing_chunk_is_attempted_twice_then_alerts_once(conn, channel):
    register(_StubConnector(items=[RawItem(external_id="t1", title="A", url="https://x")]))
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()

    mock_rank = AsyncMock(side_effect=RuntimeError("still broken"))
    with patch("beehive.collector.run_cycle.rank_channel", new=mock_rank), \
         patch.object(notifier, "send") as mock_send:
        await run_channel_cycle(conn, channel, notifier, localizer=_EN_LOCALIZER)

    assert mock_rank.await_count == 2  # 1 initial attempt + 1 retry, then give up
    mock_send.assert_called_once()
    items = list_by_channel(conn, channel["id"])
    assert items[0]["ai_score"] is None


@pytest.mark.asyncio
async def test_a_connector_without_fetch_comments_between_capable_ones_adds_no_delay(conn, channel):
    register(_StubConnector(items=[
        RawItem(external_id="t0", title="A", url="https://x/0"),
    ]))
    create_source(conn, channel["id"], "stub_test_source", {})
    register(_StubCommentConnector(
        items=[RawItem(external_id="t1", title="B", url="https://y/1")],
        comments_by_url={"https://y/1": ["comment 1"]}))
    create_source(conn, channel["id"], "stub_comment_source", {})

    fake_result = _echo_ranker()
    with patch("beehive.collector.run_cycle.rank_channel",
               new=AsyncMock(side_effect=fake_result)), \
         patch("beehive.collector.run_cycle.summarize_comments",
               new=AsyncMock(return_value={})), \
         patch("beehive.collector.run_cycle.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await run_channel_cycle(conn, channel, LogNotifier(), localizer=_EN_LOCALIZER)

    # t0's connector has no fetch_comments (skipped, no real fetch attempt); only t1 fetches --
    # a single real attempt never needs a preceding delay.
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_persistently_failing_chunk_routes_alert_to_channel_recipient(conn, channel):
    register(_StubConnector(items=[
        RawItem(external_id="t1", title="A", url="https://x")
    ]))
    create_source(conn, channel["id"], "stub_test_source", {})
    notifier = LogNotifier()
    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=RuntimeError("still broken")),
    ), patch.object(notifier, "send") as mock_send:
        await run_channel_cycle(
            conn, channel, notifier,
            recipient="channel@example.com", localizer=_EN_LOCALIZER)

    assert mock_send.call_args.kwargs["to_addr"] == "channel@example.com"


@pytest.mark.asyncio
async def test_duplicate_external_ids_across_sources_update_the_correct_rows(conn, channel):
    class _FirstConnector(_StubConnector):
        type_key = "duplicate_first_source"

    class _SecondConnector(_StubConnector):
        type_key = "duplicate_second_source"

    register(_FirstConnector(items=[
        RawItem(external_id="shared", title="First source title", url="https://first")
    ]))
    register(_SecondConnector(items=[
        RawItem(external_id="shared", title="Second source title", url="https://second")
    ]))
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
                raw_metadata={"hn_id": 48888193, "hn_url": "https://news.ycombinator.com/item?id=48888193"},
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

    with patch(
        "beehive.collector.run_cycle.rank_channel",
        new=AsyncMock(side_effect=fake_rank),
    ), patch(
        "beehive.collector.run_cycle.summarize_comments",
        new=AsyncMock(return_value={}),
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
