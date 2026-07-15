# tests/collector/test_summary_rewrite.py
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from beehive.ai.summary_rewrite import RewrittenSummary
from beehive.collector.summary_rewrite import rollback_summary_rewrite, run_summary_rewrite
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import get_item, insert_new, mark_read, update_ai_ranking
from beehive.db.sources import create_source
from beehive.db.summary_rewrites import list_for_run
from beehive.localization import localizer_for

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
_EN = localizer_for("en")


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def source_id(conn):
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    return create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})


def _scored_item_id(conn, source_id, external_id, score=50.0, summary="old summary"):
    insert_new(conn, source_id, RawItem(
        external_id=external_id, title=f"title-{external_id}", url="https://x",
        body="body text", created_at=None, raw_metadata={}))
    update_ai_ranking(conn, source_id, external_id, score=score, summary=summary, rationale="r")
    return conn.execute(
        "SELECT id FROM items WHERE external_id = ?", (external_id,)).fetchone()[0]


def _echo_rewrite(prefix="new"):
    """Fake rewrite_item_summary that echoes back a deterministic new summary per item_id."""
    async def rewrite(item_id, context, language, model="claude-haiku-4.5"):
        return RewrittenSummary(item_id=item_id, summary=f"{prefix}-{item_id}")
    return rewrite


# ============================================================================
# dry_run: no LLM call, no write, no log entry
# ============================================================================

@pytest.mark.asyncio
async def test_dry_run_previews_without_calling_the_llm_or_writing(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=AsyncMock()) as mock_rewrite:
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN,
            dry_run=True, now=T0)

    mock_rewrite.assert_not_called()
    assert result.dry_run is True
    assert result.considered == 1
    assert result.rewritten == 1
    assert get_item(conn, item_id)["ai_summary"] == "old summary"  # unchanged
    assert list_for_run(conn, "run-1") == []  # no log entry for a preview


# ============================================================================
# confirmed execution: rewrites, writes, logs -- preserving everything else
# ============================================================================

@pytest.mark.asyncio
async def test_confirmed_run_rewrites_summary_and_preserves_other_fields(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1", score=77.0, summary="old summary")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)

    assert result.rewritten == 1
    assert result.failed == 0
    row = get_item(conn, item_id)
    assert row["ai_summary"] == f"new-{item_id}"
    assert row["ai_score"] == 77.0
    assert row["ai_rationale"] == "r"
    assert row["is_read"] == 0
    assert row["opened_at"] is None

    entries = list_for_run(conn, "run-1")
    assert len(entries) == 1
    assert entries[0].item_id == item_id
    assert entries[0].previous_summary == "old summary"
    assert entries[0].replacement_summary == f"new-{item_id}"


@pytest.mark.asyncio
async def test_confirmed_run_excludes_items_above_the_high_water_mark(conn, source_id):
    first_id = _scored_item_id(conn, source_id, "t1")
    second_id = _scored_item_id(conn, source_id, "t2")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        result = await run_summary_rewrite(
            conn, high_water_item_id=first_id, run_id="run-1", localizer=_EN, now=T0)

    assert result.considered == 1
    assert result.rewritten == 1
    assert get_item(conn, second_id)["ai_summary"] == "old summary"


@pytest.mark.asyncio
async def test_confirmed_run_skips_read_items(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")
    mark_read(conn, item_id)

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=AsyncMock()) as mock_rewrite:
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)

    mock_rewrite.assert_not_called()
    assert result.considered == 0
    assert result.rewritten == 0


# ============================================================================
# canary_limit: caps actual rewrites, deterministic resumable cursor
# ============================================================================

@pytest.mark.asyncio
async def test_canary_limit_caps_the_number_of_rewrites(conn, source_id):
    ids = [_scored_item_id(conn, source_id, f"t{i}") for i in range(5)]

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        result = await run_summary_rewrite(
            conn, high_water_item_id=ids[-1], run_id="run-1", localizer=_EN,
            canary_limit=2, now=T0)

    assert result.rewritten == 2
    assert result.last_item_id == ids[1]
    rewritten_rows = [get_item(conn, i)["ai_summary"] for i in ids]
    assert rewritten_rows[:2] == [f"new-{ids[0]}", f"new-{ids[1]}"]
    assert rewritten_rows[2:] == ["old summary"] * 3


@pytest.mark.asyncio
async def test_canary_limit_zero_does_nothing(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=AsyncMock()) as mock_rewrite:
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN,
            canary_limit=0, now=T0)

    mock_rewrite.assert_not_called()
    assert result.considered == 0
    assert result.rewritten == 0


@pytest.mark.asyncio
async def test_resuming_from_last_item_id_continues_past_a_canary(conn, source_id):
    ids = [_scored_item_id(conn, source_id, f"t{i}") for i in range(4)]

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        first = await run_summary_rewrite(
            conn, high_water_item_id=ids[-1], run_id="run-1", localizer=_EN,
            canary_limit=2, now=T0)
        second = await run_summary_rewrite(
            conn, high_water_item_id=ids[-1], run_id="run-1", localizer=_EN,
            after_id=first.last_item_id, now=T0)

    assert second.rewritten == 2
    assert [get_item(conn, i)["ai_summary"] for i in ids] == [f"new-{i}" for i in ids]


# ============================================================================
# Idempotent reruns: an item already logged under this run_id is never re-summarized
# ============================================================================

@pytest.mark.asyncio
async def test_rerun_with_same_run_id_skips_already_migrated_items(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=AsyncMock()) as mock_rewrite_second:
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)

    mock_rewrite_second.assert_not_called()
    assert result.already_migrated == 1
    assert result.rewritten == 0
    assert len(list_for_run(conn, "run-1")) == 1  # no duplicate log row


@pytest.mark.asyncio
async def test_different_run_id_is_not_blocked_by_a_prior_runs_log(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-2", localizer=_EN, now=T0)

    assert result.already_migrated == 0
    assert result.rewritten == 1


# ============================================================================
# LLM failure isolation: one item's failure doesn't stop the run
# ============================================================================

@pytest.mark.asyncio
async def test_llm_failure_is_isolated_to_the_failing_item(conn, source_id):
    good_id = _scored_item_id(conn, source_id, "t1")
    bad_id = _scored_item_id(conn, source_id, "t2")

    async def flaky(item_id, context, language, model="claude-haiku-4.5"):
        if item_id == bad_id:
            raise RuntimeError("LLM exploded")
        return RewrittenSummary(item_id=item_id, summary=f"new-{item_id}")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary", new=flaky):
        result = await run_summary_rewrite(
            conn, high_water_item_id=bad_id, run_id="run-1", localizer=_EN, now=T0)

    assert result.rewritten == 1
    assert result.failed == 1
    assert get_item(conn, good_id)["ai_summary"] == f"new-{good_id}"
    assert get_item(conn, bad_id)["ai_summary"] == "old summary"
    assert len(list_for_run(conn, "run-1")) == 1


# ============================================================================
# Race condition: item becomes ineligible between selection and write
# ============================================================================

@pytest.mark.asyncio
async def test_item_marked_read_mid_run_is_not_overwritten_or_logged(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1")

    async def rewrite_then_mark_read(item_id, context, language, model="claude-haiku-4.5"):
        mark_read(conn, item_id)  # simulates a race: the item is read before the write lands
        return RewrittenSummary(item_id=item_id, summary="new summary")

    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=rewrite_then_mark_read):
        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)

    assert result.rewritten == 0
    assert result.no_longer_eligible == 1
    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


# ============================================================================
# rollback_summary_rewrite
# ============================================================================

@pytest.mark.asyncio
async def test_rollback_restores_previous_summaries_and_clears_the_log(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1", summary="old summary")
    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)
    assert get_item(conn, item_id)["ai_summary"] == f"new-{item_id}"

    result = rollback_summary_rewrite(conn, "run-1")

    assert result.entries_found == 1
    assert result.reverted == 1
    assert result.changed_since == 0
    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


@pytest.mark.asyncio
async def test_rollback_leaves_items_changed_since_untouched_and_retains_the_log(
        conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1", summary="old summary")
    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)
    # Something else (a later run, a manual edit) changes the summary after run-1 wrote it.
    conn.execute("UPDATE items SET ai_summary = 'someone else edit' WHERE id = ?", (item_id,))
    conn.commit()

    result = rollback_summary_rewrite(conn, "run-1")

    assert result.reverted == 0
    assert result.changed_since == 1
    assert get_item(conn, item_id)["ai_summary"] == "someone else edit"
    assert len(list_for_run(conn, "run-1")) == 1


@pytest.mark.asyncio
async def test_out_of_order_rollbacks_retain_then_restore_the_earlier_run(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1", summary="old summary")
    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite("run-1")):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)
    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite("run-2")):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-2", localizer=_EN, now=T0)

    earlier = rollback_summary_rewrite(conn, "run-1")
    assert earlier.changed_since == 1
    assert len(list_for_run(conn, "run-1")) == 1

    later = rollback_summary_rewrite(conn, "run-2")
    assert later.reverted == 1
    assert get_item(conn, item_id)["ai_summary"] == f"run-1-{item_id}"

    retried = rollback_summary_rewrite(conn, "run-1")
    assert retried.reverted == 1
    assert retried.changed_since == 0
    assert get_item(conn, item_id)["ai_summary"] == "old summary"
    assert list_for_run(conn, "run-1") == []


@pytest.mark.asyncio
async def test_rollback_then_rerun_reprocesses_the_same_items(conn, source_id):
    item_id = _scored_item_id(conn, source_id, "t1", summary="old summary")
    with patch("beehive.collector.summary_rewrite.rewrite_item_summary",
               new=_echo_rewrite()):
        await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)
        rollback_summary_rewrite(conn, "run-1")

        result = await run_summary_rewrite(
            conn, high_water_item_id=item_id, run_id="run-1", localizer=_EN, now=T0)

    assert result.already_migrated == 0
    assert result.rewritten == 1
    assert get_item(conn, item_id)["ai_summary"] == f"new-{item_id}"


def test_rollback_of_unknown_run_id_is_a_noop(conn):
    result = rollback_summary_rewrite(conn, "does-not-exist")
    assert result.entries_found == 0
    assert result.reverted == 0
    assert result.changed_since == 0
