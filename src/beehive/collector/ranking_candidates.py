"""The same prompt inputs for ordinary collection, rule previews and explicit reprocessing."""
from __future__ import annotations

import sqlite3

from beehive.ai.prompt_builder import ItemCandidate, ProductCandidate, VoteExample
from beehive.db.votes import get_vote_examples_for_channel

RANKING_CHUNK_SIZE = 10
RANKING_ATTEMPTS = 2


def build_ranking_candidates(
    items: list[dict], *, editorial: bool,
) -> list[ItemCandidate] | list[ProductCandidate]:
    if editorial:
        return [
            ItemCandidate(
                item_key=str(item["id"]),
                title=item["title"],
                body=item["body"],
                score=item["raw_metadata"].get("score", 0),
                num_comments=item["raw_metadata"].get("num_comments", 0),
            )
            for item in items
        ]
    return [
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
            discount_percent=item["raw_metadata"].get("discount_percent"),
        )
        for item in items
    ]


def load_ranking_votes(
    conn: sqlite3.Connection, channel_id: int, *, editorial: bool,
) -> list[VoteExample]:
    if not editorial:
        return []
    return [
        VoteExample(title=vote["title"], value=vote["value"], reason=vote["reason"])
        for vote in get_vote_examples_for_channel(conn, channel_id)
    ]
