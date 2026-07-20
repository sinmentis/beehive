"""Ties connector -> dedup -> AI ranking -> persistence together for one Channel, with the
per-Source/per-Channel failure isolation described in ADR-0002: a Source fetch failure is
recorded (not raised) and the cycle continues with the other Sources; an AI/LLM failure
alerts immediately and aborts only this Channel's ranking step. Ranking always re-queries
for EVERY currently-unscored item in the Channel (not just this cycle's fresh fetches), so
an item stranded unscored by a past LLM failure gets retried automatically on the next
cycle instead of staying stuck forever. After ranking, a best-comment step fetches and
judges a top comment for this cycle's top 3 ranked items (connectors that don't support
fetch_comments are skipped) -- unlike the ranking failure path, any failure here is caught
and printed, never alerted, since a missing best-comment enrichment is cosmetic, not a break
in the core pipeline."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from beehive.ai.comment_summarizer import CommentCandidate, summarize_comments
from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.ai.prompt_builder import ItemCandidate, VoteExample
from beehive.ai.ranker import rank_channel
from beehive.ai.response_parser import RankedItem
from beehive.connectors.base import CommentFetchTarget
from beehive.connectors.registry import get as get_connector
from beehive.db.items import (insert_new, list_by_channel, update_ai_ranking_by_id,
                              update_best_comment)
from beehive.db.sources import list_by_channel as list_sources
from beehive.db.sources import record_fetch_error, record_fetch_success
from beehive.db.votes import get_vote_examples_for_channel
from beehive.localization import Localizer
from beehive.notify import Notifier, format_llm_failure
from beehive.scheduling import source_is_due

_COMMENT_FETCH_COUNT = 3
_COMMENT_FETCH_DELAY_SECONDS = 2
# One LLM call must individually reason through every candidate's selected-language summary/
# rationale, so its generation time scales with batch size (measured: ~40s for 5 items, ~96s
# for 15 -- a 50-item batch reliably exceeded even a 280s timeout). Ranking the backlog in
# fixed-size chunks keeps each call comfortably inside run_prompt's 120s timeout no matter how
# large a channel's unscored backlog grows (e.g. from a freshly-added source dumping many
# items at once), and each chunk's results are persisted immediately so a later chunk's
# failure never discards work already done.
_RANKING_CHUNK_SIZE = 10
# A chunk can fail its strict id-matching validation (response_parser.py: "the model lost
# track of the set") as a one-off sampling flake rather than a persistent error. One retry
# gives the model a second independent attempt, which matters more now that fixed-size
# chunking means a retry (next cycle) would otherwise regroup the exact same items together
# again -- without loosening the strict validation itself.
_CHUNK_ATTEMPTS = 2


async def run_channel_cycle(
    conn: sqlite3.Connection,
    channel: dict,
    notifier: Notifier,
    model: str = DEFAULT_MODEL,
    recipient: str | None = None,
    *,
    localizer: Localizer,
    force_fetch: bool = False,
    now: datetime | None = None,
) -> None:
    cycle_now = now or datetime.now(timezone.utc)
    now_iso = cycle_now.isoformat()

    for source in list_sources(conn, channel["id"]):
        if (
            not force_fetch
            and not source_is_due(
                source,
                channel["fetch_interval_hours"],
                cycle_now,
            )
        ):
            continue
        connector = get_connector(source["type"])
        config = json.loads(source["config"])
        try:
            raw_items = connector.fetch(config)
        except Exception as exc:
            record_fetch_error(conn, source["id"], str(exc), now_iso)
            continue
        new_count = sum(1 for raw_item in raw_items if insert_new(conn, source["id"], raw_item))
        record_fetch_success(conn, source["id"], now_iso,
                             raw_count=len(raw_items), new_count=new_count)

    if channel["kind"] != "editorial":
        # 'monitor' Channels track deterministic state changes (e.g. a price drop), not
        # subjective "is this interesting" ranking -- fetching/deduping above already stored
        # whatever a monitor connector produced; there is nothing here for the AI ranker or
        # best-comment enrichment to do.
        return

    all_items = list_by_channel(conn, channel["id"])
    unscored = [i for i in all_items if i["ai_score"] is None]
    if not unscored:
        return

    items_by_key = {str(item["id"]): item for item in unscored}
    candidates = [
        ItemCandidate(item_key=str(item["id"]), title=item["title"], body=item["body"],
                      score=item["raw_metadata"].get("score", 0),
                      num_comments=item["raw_metadata"].get("num_comments", 0))
        for item in unscored
    ]

    vote_examples = [VoteExample(title=v["title"], value=v["value"], reason=v["reason"])
                      for v in get_vote_examples_for_channel(conn, channel["id"])]

    ranked: list[RankedItem] = []
    for start in range(0, len(candidates), _RANKING_CHUNK_SIZE):
        chunk = candidates[start:start + _RANKING_CHUNK_SIZE]
        chunk_ranked = None
        last_exc: Exception | None = None
        for _attempt in range(_CHUNK_ATTEMPTS):
            try:
                chunk_ranked = await rank_channel(profile=channel["profile"], votes=vote_examples,
                                                   candidates=chunk, language=localizer.language,
                                                   model=model)
                break
            except Exception as exc:
                last_exc = exc
        if chunk_ranked is None:
            subject, body = format_llm_failure(localizer, channel["name"], str(last_exc))
            notifier.send(subject, body, to_addr=recipient)
            break
        for ranked_item in chunk_ranked:
            item = items_by_key[ranked_item.item_key]
            update_ai_ranking_by_id(
                conn, item["id"],
                score=ranked_item.score, summary=ranked_item.summary,
                rationale=ranked_item.rationale)
        ranked.extend(chunk_ranked)

    if not ranked:
        return

    top_ranked = sorted(ranked, key=lambda r: r.score, reverse=True)[:_COMMENT_FETCH_COUNT]
    comment_candidates = []
    fetched_any = False
    for ranked_item in top_ranked:
        item = items_by_key[ranked_item.item_key]
        connector = get_connector(item["source_type"])
        if not hasattr(connector, "fetch_comments"):
            continue
        if fetched_any:
            await asyncio.sleep(_COMMENT_FETCH_DELAY_SECONDS)
        fetched_any = True
        target = CommentFetchTarget(
            external_id=item["external_id"],
            url=item["url"],
            raw_metadata=item["raw_metadata"],
        )
        try:
            comments = connector.fetch_comments(target)
        except Exception as exc:
            print(
                "[best-comment] fetch_comments failed for "
                f"source={item['source_id']} item={item['external_id']}: {exc}"
            )
            continue
        if comments:
            comment_candidates.append(CommentCandidate(
                item_key=ranked_item.item_key, title=item["title"],
                comment_text=comments[0]))

    if not comment_candidates:
        return

    try:
        summaries = await summarize_comments(comment_candidates, language=localizer.language,
                                              model=model)
    except Exception as exc:
        print(f"[best-comment] summarization failed: {exc}")
        return

    for item_key, summary in summaries.items():
        if summary:
            update_best_comment(conn, items_by_key[item_key]["id"], summary)
