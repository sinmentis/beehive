# src/beehive/ai/response_parser.py
"""Pure function, no I/O, with a strict-raise discipline: a missing/extra id is a hard failure
(the model lost track of the set, treated as an LLM failure per ADR-0002 by the caller), but an
overlong summary is soft-truncated rather than sinking the whole batch.

The "id" a response entry is keyed by is the small 1-based position number from that
item's prompt_builder.py <item id="N"> tag -- NEVER the item's own long item_key.
Confirmed live (2026-07-13): a Google News item_key (a ~280-char opaque token) can get
a single character transcribed wrong when a model reproduces it verbatim in its output
(e.g. one real response returned "...ek1SREY5..." for a source id containing
"...ek1SREJ5..."), which the old id-echoing design saw as a "missing" id and hard-failed
the whole batch. Position numbers are trivial for a model to reproduce exactly, which
removes this failure mode instead of just tolerating it."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
_SUMMARY_CAP = 300


class ResponseParseError(ValueError):
    pass


@dataclass(frozen=True)
class RankedItem:
    item_key: str
    score: float
    summary: str
    rationale: str


def parse_ranking_response(raw_text: str, candidate_item_keys: list[str]) -> list[RankedItem]:
    match = _FENCE_RE.search(raw_text)
    if match is None:
        raise ResponseParseError("no fenced ```json block found in LLM response")

    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"fenced block is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict) or "ranked" not in parsed:
        raise ResponseParseError("JSON block is missing the top-level 'ranked' key")

    # candidate_item_keys is positional: prompt_builder.py rendered the Nth candidate as
    # <item id="N">, 1-based, in this same order -- translate the model's position numbers
    # back to opaque item_keys here, at the one seam that needs to know both.
    index_to_item_key = {str(i): key for i, key in enumerate(candidate_item_keys, start=1)}
    expected_indices = set(index_to_item_key)

    entries = parsed["ranked"]
    seen_indices = {e.get("id") for e in entries if isinstance(e, dict)}

    missing = expected_indices - seen_indices
    if missing:
        raise ResponseParseError(f"response is missing ids: {sorted(missing)}")
    unexpected = seen_indices - expected_indices
    if unexpected:
        raise ResponseParseError(f"response has unexpected ids not in the batch: {sorted(unexpected)}")

    results = []
    for entry in entries:
        score = entry["score"]
        if not isinstance(score, (int, float)) or not (0 <= score <= 100):
            raise ResponseParseError(f"score out of 0-100 range for id {entry.get('id')!r}: {score!r}")
        summary = str(entry.get("summary", ""))[:_SUMMARY_CAP]
        results.append(RankedItem(
            item_key=index_to_item_key[entry["id"]], score=float(score),
            summary=summary, rationale=str(entry.get("rationale", ""))))
    return results
