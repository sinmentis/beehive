# src/beehive/research/clustering.py
"""Snapshot-scoped Evidence Cluster assignment (CONTEXT.md: "A group of Evidence Items that
describe the same underlying event or substantially duplicate one another while preserving
their distinct publishers").

Deliberately NOT an AI call: grouping near-duplicate coverage of the same event is a title-
similarity problem, not one that needs a model's judgment, and keeping it free of
`run_data_only_prompt` means one fewer LLM round-trip per run and nothing here ever reads
Evidence text through a prompt. The similarity signal itself lives in `beehive.title_similarity`
-- a plain, deterministic Jaccard overlap of lower-cased title tokens, shared with every other
feature that has to decide whether two headlines describe the same event -- so this module holds
only the Research-specific part: which records are grouped, and how a group becomes a persisted
Evidence Cluster.

`beehive.db.evidence_clusters.create_evidence_cluster` has no corresponding delete/replace --
clusters are append-only, exactly like every other Research table. This module is therefore
called exactly once per Research Run, against the run's final snapshot membership, immediately
before that snapshot is sealed (see orchestrator.py's module docstring for why re-clustering
every revision round would otherwise accumulate duplicate cluster rows for the same items)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from beehive.db.evidence_clusters import create_evidence_cluster
from beehive.domain.research import EvidenceCluster, EvidenceItem
from beehive.title_similarity import DEFAULT_SIMILARITY_THRESHOLD, group_by_title_similarity

_DEFAULT_SIMILARITY_THRESHOLD = DEFAULT_SIMILARITY_THRESHOLD


def group_evidence_items(
    evidence_items: list[EvidenceItem], *, similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> list[list[int]]:
    """Pure, deterministic grouping: returns the evidence_item ids for each group whose title
    tokens are similar enough to be the same underlying event, in first-seen order, for groups
    with 2 or more members only -- a "group" of one item is not a cluster (CONTEXT.md requires
    a cluster to be a group of items). Never mutates or reorders `evidence_items`."""
    return group_by_title_similarity(
        ((item.id, item.title) for item in evidence_items),
        similarity_threshold=similarity_threshold)



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
