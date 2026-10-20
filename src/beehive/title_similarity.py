# src/beehive/title_similarity.py
"""Deterministic title-similarity primitives, shared by every feature that has to decide whether
two headlines describe the same underlying event.

Deliberately NOT an AI call and deliberately domain-free: this module knows nothing about
Evidence Items, Civic Candidates, or any other record type -- callers hand it `(key, title)`
pairs and get back groups of their own keys. That keeps the one similarity rule in a single
place (transparent, reproducible in tests, and free of any LLM round-trip) while letting each
feature keep its own persistence rules where they belong.

The signal is a plain Jaccard overlap of lower-cased alphanumeric title tokens. Grouping is
greedy and first-seen ordered: each title joins the already-open group it overlaps *best*, and a
joined group absorbs the new title's tokens, so a later title can match the group through any of
its members rather than only through its founder.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TypeVar

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A group must share at least this fraction of its tokens with an already-open group to be
# considered "the same underlying event" rather than merely the same general topic -- deliberately
# conservative so distinct stories about a shared topic (e.g. two different rate-decision
# articles) are not falsely merged.
DEFAULT_SIMILARITY_THRESHOLD = 0.6

K = TypeVar("K")


def tokenize_title(title: str) -> frozenset[str]:
    """The lower-cased alphanumeric tokens of one title. ASCII-only by construction: a title with
    no matchable tokens yields an empty set, which `jaccard_similarity` always scores 0.0 rather
    than treating as a match against everything."""
    return frozenset(_TOKEN_RE.findall(title.lower()))


def jaccard_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Intersection over union, with an empty side scoring 0.0 instead of raising or matching."""
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def group_by_title_similarity(
    entries: Iterable[tuple[K, str]],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    min_group_size: int = 2,
) -> list[list[K]]:
    """Groups `(key, title)` pairs whose titles are similar enough to be the same underlying
    event, returning each group's keys in first-seen order.

    Pure and deterministic: never mutates, reorders, or retains `entries`, and identical input
    always produces identical output. Groups smaller than `min_group_size` are dropped, so the
    default (2) never reports a lone title as a "group" of one.
    """
    if min_group_size < 1:
        raise ValueError("min_group_size must be at least 1")
    groups: list[dict] = []  # each: {"tokens": frozenset[str], "keys": list[K]}
    for key, title in entries:
        tokens = tokenize_title(title)
        best_group = None
        best_score = 0.0
        for group in groups:
            score = jaccard_similarity(tokens, group["tokens"])
            if score >= similarity_threshold and score > best_score:
                best_group = group
                best_score = score
        if best_group is not None:
            best_group["keys"].append(key)
            best_group["tokens"] = best_group["tokens"] | tokens
        else:
            groups.append({"tokens": tokens, "keys": [key]})
    return [group["keys"] for group in groups if len(group["keys"]) >= min_group_size]


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "group_by_title_similarity",
    "jaccard_similarity",
    "tokenize_title",
]
