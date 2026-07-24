"""Ties connector -> persistence -> AI ranking -> actionable events together for one Channel, with
the per-Source/per-Channel failure isolation described in ADR-0002: a Source fetch failure is
recorded (not raised) and the cycle continues with the other Sources; an AI/LLM failure
alerts immediately and aborts only this Channel's ranking step. Ranking always re-queries
for EVERY currently-unscored item in the Channel (not just this cycle's fresh fetches), so
an item stranded unscored by a past LLM failure gets retried automatically on the next
cycle instead of staying stuck forever.

Persistence and event staging are owned by a ChannelCollection resolved from the Channel's kind:
after a successful fetch it inserts (editorial APPEND) or reconciles a snapshot (monitor/tracker
MUTABLE_SNAPSHOT) and stages DISCOVERED / PRICE_DROP / BACK_IN_STOCK events the definition permits.
The collector never branches on the raw kind string -- it reads the definition's RankingMode to
build ItemCandidates (editorial: community-engagement score/num_comments plus past votes, via
rank_channel) or ProductCandidates (monitor/tracker: price/discount/vendor, no votes, via
rank_monitor_channel), both sharing this same chunking/retry loop. As each item is scored the
collection settles its staged events (ready for Email Group delivery at/above the Channel's
minimum_score, suppressed below it). After ranking, a best-comment step fetches and judges a top
comment for this cycle's top 3 ranked items, but only for editorial Channels (connectors that don't
support fetch_comments are skipped too; a product listing has no discussion thread) -- unlike the
ranking failure path, any failure here is caught and printed, never alerted, since a missing
best-comment enrichment is cosmetic, not a break in the core pipeline."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from beehive.ai.comment_summarizer import CommentCandidate, summarize_comments
from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.ai.prompt_builder import ItemCandidate, ProductCandidate, VoteExample
from beehive.ai.ranker import rank_channel, rank_monitor_channel
from beehive.ai.response_parser import RankedItem
from beehive.channels.collection import ChannelCollection
from beehive.channels.source_policy import assert_source_allowed
from beehive.connectors.base import CommentFetchTarget
from beehive.connectors.registry import get as get_connector
from beehive.db.items import (
    list_by_channel,
    update_ai_ranking_by_id,
    update_best_comment,
)
from beehive.db.sources import list_by_channel as list_sources
from beehive.db.sources import record_fetch_error, record_fetch_success
from beehive.db.votes import get_vote_examples_for_channel
from beehive.domain.channels import RankingMode
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

    # One typed handle over this Channel's Items, resolved to its kind and ChannelDefinition. It
    # decides persistence (append vs reconciled snapshot) and actionable-event staging by the
    # definition, so nothing below branches on the raw kind string. Building it also validates the
    # stored kind, so an unknown kind fails loudly here rather than defaulting.
    collection = ChannelCollection.for_channel(channel)

    for source in list_sources(conn, channel["id"]):
        # A paused Source is dormant: skip it entirely, before the due-check and before any fetch,
        # so a MUTABLE_SNAPSHOT Channel never reaches ingest_fetch for it and therefore never
        # reconciles its still-valid listings to inactive. The Source keeps its config, items, and
        # fetch history until an Owner resumes it.
        if source["paused_at"]:
            continue
        if not force_fetch and not source_is_due(
            source,
            channel["fetch_interval_hours"],
            cycle_now,
        ):
            continue
        # Defense in depth: a persisted Source that is incompatible with its Channel's kind (an
        # unknown Source type, or one the compatibility policy no longer allows for this kind)
        # must never be fetched. db.sources.create_source already gates this at write time; here
        # we re-check at read time so a Source that slipped in some other way records a clear
        # fetch error and is skipped, rather than fetching against the wrong ranking pipeline.
        try:
            assert_source_allowed(source["type"], collection.kind)
        except ValueError as exc:
            record_fetch_error(conn, source["id"], str(exc), now_iso)
            continue
        connector = get_connector(source["type"])
        config = json.loads(source["config"])
        try:
            raw_items = connector.fetch(config)
        except Exception as exc:
            record_fetch_error(conn, source["id"], str(exc), now_iso)
            continue
        # Persist and stage events only after a successful fetch: a MUTABLE_SNAPSHOT Channel
        # reconciles absent listings to inactive inside ingest_fetch, which would wrongly retire
        # everything a failed fetch (handled above by continue) could not return.
        new_count = collection.ingest_fetch(
            conn, source["id"], raw_items, now_iso=now_iso
        )
        record_fetch_success(
            conn, source["id"], now_iso, raw_count=len(raw_items), new_count=new_count
        )

    # An editorial Channel ranks community-signal ItemCandidates with past votes; a monitor/tracker
    # Channel ranks listing ProductCandidates with no votes. Both are the LISTING vs EDITORIAL
    # RankingMode on the definition, never the raw kind string.
    uses_editorial_ranking = (
        collection.definition.ranking_mode is RankingMode.EDITORIAL
    )

    all_items = list_by_channel(conn, channel["id"])
    unscored = [i for i in all_items if i["ai_score"] is None]
    if not unscored:
        return

    items_by_key = {str(item["id"]): item for item in unscored}
    if uses_editorial_ranking:
        candidates = [
            ItemCandidate(
                item_key=str(item["id"]),
                title=item["title"],
                body=item["body"],
                score=item["raw_metadata"].get("score", 0),
                num_comments=item["raw_metadata"].get("num_comments", 0),
            )
            for item in unscored
        ]
        vote_examples = [
            VoteExample(title=v["title"], value=v["value"], reason=v["reason"])
            for v in get_vote_examples_for_channel(conn, channel["id"])
        ]
    else:
        # 'monitor' Channels (e.g. a Shopify clearance watch) score their scraped products
        # against the owner's shopping profile instead of a news-interest profile, and never
        # accrue votes (the vote widget is editorial-only, see _item_card.html), so there is
        # no vote-example query to run here.
        candidates = [
            ProductCandidate(
                item_key=str(item["id"]),
                title=item["title"],
                price=item["raw_metadata"].get("price"),
                compare_at_price=item["raw_metadata"].get("compare_at_price"),
                on_sale=bool(item["raw_metadata"].get("on_sale")),
                available=bool(item["raw_metadata"].get("available")),
                vendor=item["raw_metadata"].get("vendor"),
                product_type=item["raw_metadata"].get("product_type"),
                tags=item["raw_metadata"].get("tags") or [],
                description=item["body"],
                listing_kind=item["raw_metadata"].get("listing_kind") or "product",
                auction_title=item["raw_metadata"].get("auction_title"),
                closing_at=item["raw_metadata"].get("closing_at"),
                currency_code=item["raw_metadata"].get("currency_code"),
                current_bid=item["raw_metadata"].get("current_bid"),
                buyer_premium_rate=item["raw_metadata"].get("buyer_premium_rate"),
                estimated_cost=item["raw_metadata"].get("estimated_cost"),
                rrp=item["raw_metadata"].get("rrp"),
                rrp_excludes_gst=bool(item["raw_metadata"].get("rrp_excludes_gst")),
                starting_price=item["raw_metadata"].get("starting_price"),
                estimate_low=item["raw_metadata"].get("estimate_low"),
                estimate_high=item["raw_metadata"].get("estimate_high"),
                sold_price=item["raw_metadata"].get("sold_price"),
                status=item["raw_metadata"].get("status"),
            )
            for item in unscored
        ]
        vote_examples = []

    ranked: list[RankedItem] = []
    for start in range(0, len(candidates), _RANKING_CHUNK_SIZE):
        chunk = candidates[start : start + _RANKING_CHUNK_SIZE]
        chunk_ranked = None
        last_exc: Exception | None = None
        for _attempt in range(_CHUNK_ATTEMPTS):
            try:
                if uses_editorial_ranking:
                    chunk_ranked = await rank_channel(
                        profile=channel["profile"],
                        votes=vote_examples,
                        candidates=chunk,
                        language=localizer.language,
                        model=model,
                    )
                else:
                    chunk_ranked = await rank_monitor_channel(
                        profile=channel["profile"],
                        candidates=chunk,
                        language=localizer.language,
                        model=model,
                    )
                break
            except Exception as exc:
                last_exc = exc
        if chunk_ranked is None:
            subject, body = format_llm_failure(
                localizer, channel["name"], str(last_exc)
            )
            notifier.send(subject, body, to_addr=recipient)
            break
        for ranked_item in chunk_ranked:
            item = items_by_key[ranked_item.item_key]
            update_ai_ranking_by_id(
                conn,
                item["id"],
                score=ranked_item.score,
                summary=ranked_item.summary,
                rationale=ranked_item.rationale,
            )
            # Now that this Item has a score, resolve its pending actionable events: keep them for
            # Email Group delivery at/above the Channel's minimum_score, drop them below it. A no-op
            # for an Item that staged nothing this cycle (an editorial backlog row, an unchanged
            # listing), so ranking such an Item just scores it, as required.
            collection.settle_item_events(
                conn, item["id"], ranked_item.score, now_iso=now_iso
            )
        ranked.extend(chunk_ranked)

    if not ranked or not uses_editorial_ranking:
        # Best-comment enrichment is an editorial-only nicety -- a product listing has no
        # discussion thread for a monitor connector's fetch_comments to fetch from, so a
        # 'monitor' Channel is done as soon as its items are scored.
        return

    top_ranked = sorted(ranked, key=lambda r: r.score, reverse=True)[
        :_COMMENT_FETCH_COUNT
    ]
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
            comment_candidates.append(
                CommentCandidate(
                    item_key=ranked_item.item_key,
                    title=item["title"],
                    comment_text=comments[0],
                )
            )

    if not comment_candidates:
        return

    try:
        summaries = await summarize_comments(
            comment_candidates, language=localizer.language, model=model
        )
    except Exception as exc:
        print(f"[best-comment] summarization failed: {exc}")
        return

    for item_key, summary in summaries.items():
        if summary:
            update_best_comment(conn, items_by_key[item_key]["id"], summary)
