"""Process claimed rule jobs using the ordinary ranking prompts and models, never collection."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.ai.ranker import rank_channel, rank_monitor_channel
from beehive.ai.response_parser import RankedItem, ResponseParseError
from beehive.channels.definitions import get_definition, require_channel_kind
from beehive.collector.ranking_candidates import (
    RANKING_ATTEMPTS,
    RANKING_CHUNK_SIZE,
    build_ranking_candidates,
    load_ranking_votes,
)
from beehive.db.channels import get_channel
from beehive.db.ranking_requests import (
    RankingClaim,
    complete_ranking_batch,
    fail_ranking_request,
    get_ranking_request,
    prepare_ranking_batch,
    ranking_claim_is_live,
    requeue_ranking_request,
)
from beehive.domain.channels import RankingMode
from beehive.localization import Localizer

# Bound worker occupancy, not the request's snapshot: subsequent claims resume the remaining rows.
APPLY_BATCHES_PER_CLAIM = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _batch_results(ranked: list[RankedItem], items: list[dict]) -> list[dict]:
    expected = {str(item["id"]) for item in items}
    if len(ranked) != len(expected) or {item.item_key for item in ranked} != expected:
        raise ResponseParseError("Ranking must return exactly one result per candidate")
    if any(not 0 <= item.score <= 100 for item in ranked):
        raise ResponseParseError("Ranking scores must be between 0 and 100")
    return [
        {"item_id": int(item.item_key), "score": item.score,
         "summary": item.summary, "rationale": item.rationale}
        for item in ranked
    ]


async def process_claimed_ranking_request(
    conn: sqlite3.Connection,
    claim: RankingClaim,
    *,
    localizer: Localizer,
    model: str = DEFAULT_MODEL,
    clock: Callable[[], datetime] = _utc_now,
    max_batches: int | None = None,
) -> None:
    """Process a claim, yielding unfinished applies after max_batches when supplied by the worker."""
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be positive")
    request = get_ranking_request(conn, claim.request_id)
    channel = get_channel(conn, claim.channel_id)
    if request is None or channel is None:
        return
    editorial = (
        get_definition(require_channel_kind(channel["kind"])).ranking_mode is RankingMode.EDITORIAL
    )
    votes = load_ranking_votes(conn, claim.channel_id, editorial=editorial)
    batches = 0
    try:
        while True:
            if request["mode"] == "apply" and max_batches is not None and batches >= max_batches:
                requeue_ranking_request(conn, claim, clock(), failed=False)
                return
            batch = prepare_ranking_batch(conn, claim, clock(), limit=RANKING_CHUNK_SIZE)
            if batch is None:
                return
            batches += 1
            if not batch:
                continue
            candidates = build_ranking_candidates(batch, editorial=editorial)
            results = None
            for attempt in range(RANKING_ATTEMPTS):
                if not ranking_claim_is_live(conn, claim, clock()):
                    return
                try:
                    if editorial:
                        ranked = await rank_channel(
                            profile=request["requested_profile"], votes=votes, candidates=candidates,
                            language=localizer.language, model=model,
                        )
                    else:
                        ranked = await rank_monitor_channel(
                            profile=request["requested_profile"], candidates=candidates,
                            language=localizer.language, model=model,
                        )
                    results = _batch_results(ranked, batch)
                    break
                except Exception:
                    if attempt == RANKING_ATTEMPTS - 1:
                        raise
            if not complete_ranking_batch(conn, claim, results, clock()):
                return
    except Exception as exc:
        fail_ranking_request(
            conn, claim, clock(), error_code="ranking_failed",
            error=f"{type(exc).__name__}: {exc}"[:1000],
        )
        raise
