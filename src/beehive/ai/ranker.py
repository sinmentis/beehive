# src/beehive/ai/ranker.py
"""Orchestrates one Channel's ranking cycle: build the prompt -> call the LLM -> parse the
result. The collector (Task 14) calls this once per Channel per fetch cycle and catches any
exception itself (ADR-0002: an LLM failure alerts and is scoped to just that Channel)."""
from __future__ import annotations

from beehive.ai.llm_client import run_prompt
from beehive.ai.prompt_builder import ItemCandidate, VoteExample, build_ranking_prompt
from beehive.ai.response_parser import RankedItem, parse_ranking_response


async def rank_channel(profile: str, votes: list[VoteExample], candidates: list[ItemCandidate],
                        model: str = "claude-haiku-4.5") -> list[RankedItem]:
    if not candidates:
        return []
    prompt = build_ranking_prompt(profile, votes, candidates)
    raw_response = await run_prompt(prompt, model=model)
    return parse_ranking_response(raw_response, [c.item_key for c in candidates])
