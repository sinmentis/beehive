# src/beehive/ai/ranker.py
"""Orchestrates one Channel's ranking cycle: build the prompt -> call the LLM -> parse the
result. The collector (Task 14) calls this once per Channel per fetch cycle and catches any
exception itself (ADR-0002: an LLM failure alerts and is scoped to just that Channel)."""
from __future__ import annotations

from beehive.ai.llm_client import run_prompt
from beehive.ai.model_selection import DEFAULT_MODEL
from beehive.ai.prompt_builder import (ItemCandidate, ProductCandidate, VoteExample,
                                       build_monitor_ranking_prompt, build_ranking_prompt)
from beehive.ai.response_parser import RankedItem, parse_ranking_response
from beehive.localization import Language


async def rank_channel(profile: str, votes: list[VoteExample], candidates: list[ItemCandidate],
                        language: Language, model: str = DEFAULT_MODEL) -> list[RankedItem]:
    if not candidates:
        return []
    prompt = build_ranking_prompt(profile, votes, candidates, language)
    raw_response = await run_prompt(prompt, model=model)
    return parse_ranking_response(raw_response, [c.item_key for c in candidates])


async def rank_monitor_channel(profile: str, candidates: list[ProductCandidate],
                                language: Language, model: str = DEFAULT_MODEL) -> list[RankedItem]:
    """Same shape as rank_channel, for a 'monitor' Channel's scraped products -- no vote
    examples exist for these (the vote widget is editorial-only, see _item_card.html)."""
    if not candidates:
        return []
    prompt = build_monitor_ranking_prompt(profile, candidates, language)
    raw_response = await run_prompt(prompt, model=model)
    return parse_ranking_response(raw_response, [c.item_key for c in candidates])
