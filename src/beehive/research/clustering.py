# src/beehive/research/clustering.py
"""Snapshot-scoped Evidence Cluster assignment (CONTEXT.md: "A group of Evidence Items that
describe the same underlying event or substantially duplicate one another while preserving
their distinct publishers").

Deliberately NOT an AI call: grouping near-duplicate coverage of the same event is a title-
similarity problem, not one that needs a model's judgment, and keeping it free of
`run_data_only_prompt` means one fewer LLM round-trip per run and nothing here ever reads
Evidence text through a prompt. The similarity signal is a plain, deterministic Jaccard
overlap of lower-cased title tokens -- transparent, reproducible for tests, and cheap enough
to run over an entire snapshot's evidence every time orchestrator.py calls it.

`beehive.db.evidence_clusters.create_evidence_cluster` has no corresponding delete/replace --
clusters are append-only, exactly like every other Research table. This module is therefore
called exactly once per Research Run, against the run's final snapshot membership, immediately
before that snapshot is sealed (see orchestrator.py's module docstring for why re-clustering
every revision round would otherwise accumulate duplicate cluster rows for the same items)."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from beehive.db.evidence_clusters import create_evidence_cluster
from beehive.domain.research import EvidenceCluster, EvidenceItem

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A group must share at least this fraction of its (smaller) title's tokens with the group's
# founding member to be considered "the same underlying event" rather than merely the same
# general topic -- deliberately conservative so distinct stories about a shared topic (e.g.
# two different rate-decision articles) are not falsely merged.
_DEFAULT_SIMILARITY_THRESHOLD = 0.6


def _tokenize(title: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(title.lower()))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def group_evidence_items(
    evidence_items: list[EvidenceItem], *, similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> list[list[int]]:
    """Pure, deterministic grouping: returns the evidence_item ids for each group whose title
    tokens are similar enough to be the same underlying event, in first-seen order, for groups
    with 2 or more members only -- a "group" of one item is not a cluster (CONTEXT.md requires
    a cluster to be a group of items). Never mutates or reorders `evidence_items`."""
    groups: list[dict] = []  # each: {"tokens": frozenset[str], "ids": list[int]}
    for item in evidence_items:
        tokens = _tokenize(item.title)
        best_group = None
        best_score = 0.0
        for group in groups:
            score = _jaccard(tokens, group["tokens"])
            if score >= similarity_threshold and score > best_score:
                best_group = group
                best_score = score
        if best_group is not None:
            best_group["ids"].append(item.id)
            best_group["tokens"] = best_group["tokens"] | tokens
        else:
            groups.append({"tokens": tokens, "ids": [item.id]})
    return [group["ids"] for group in groups if len(group["ids"]) >= 2]


def cluster_snapshot(
    conn: sqlite3.Connection, snapshot_id: int, evidence_items: list[EvidenceItem], now: datetime,
    *, similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> list[EvidenceCluster]:
    """Groups `evidence_items` (the caller-supplied full membership of one still-'building'
    Evidence Snapshot) and persists one EvidenceCluster per qualifying group via
    beehive.db.evidence_clusters.create_evidence_cluster, which itself raises ValueError if
    `snapshot_id` is missing or already 'sealed' -- this function performs no snapshot-status
    check of its own, it relies entirely on that repository call as the final authority."""
    clusters: list[EvidenceCluster] = []
    for group_ids in group_evidence_items(evidence_items, similarity_threshold=similarity_threshold):
        clusters.append(create_evidence_cluster(conn, snapshot_id, group_ids, now))
    return clusters


__all__ = ["group_evidence_items", "cluster_snapshot"]
