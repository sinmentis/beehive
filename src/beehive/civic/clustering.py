"""Turning normalized source documents into Candidate-shaped clusters, deterministically.

Several sources report the same development on the same morning -- a Beehive release, the
Parliament record behind it and two mastheads writing it up -- and each arrives as its own
`CivicDocument`. Publishing four statements about one event would be wrong, so the documents are
grouped first and each group becomes exactly one Candidate.

Three properties are what make this safe to run twice:

* **Order independence.** The input is canonically ordered before any grouping happens, so the
  same set of documents produces the same clusters however the connectors happened to return
  them. `beehive.title_similarity.group_by_title_similarity` is greedy and first-seen ordered by
  design (the Research Module depends on that), so leaving the caller's order in place would make
  the clustering depend on connector scheduling -- a resumed Run could then cluster differently
  and create a second Candidate for a development it had already recorded.
* **Stable cluster identity.** A cluster's key is its lowest `dedupe_key`, which is a source's own
  stable identifier namespaced by source -- never a hash of mutable text. An edited headline
  therefore does not re-key the cluster, and the key is the same whichever document the connectors
  delivered first.
* **A bounded, explicit overflow.** Plan 7.4 caps a Run at 25 clusters. Beyond that the extra
  clusters are *reported*, not dropped quietly: `CivicClusteringResult.deferred_clusters` carries
  them so the caller records a real deferral rather than a Run that silently published less than
  it found.

Pure: no clock, no database, no network. Documents are never mutated or retained.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from beehive.civic.documents import CivicDocument
from beehive.title_similarity import DEFAULT_SIMILARITY_THRESHOLD, group_by_title_similarity

# Plan 7.4: candidate clusters per run.
MAX_CLUSTERS_PER_RUN = 25


@dataclass(frozen=True)
class CivicDocumentCluster:
    """One development, as reported by one or more source documents.

    `documents` are in canonical order, so `title` and `effective_at` never depend on which
    connector answered first."""

    cluster_key: str
    documents: tuple[CivicDocument, ...]

    def __post_init__(self) -> None:
        if not self.documents:
            raise ValueError("a Civic document cluster needs at least one document")
        if not self.cluster_key.strip():
            raise ValueError("a Civic document cluster needs a cluster_key")

    @property
    def title(self) -> str:
        """The strongest-tier, earliest document's title: an official record's own wording is
        preferred over a masthead's headline for the same development."""
        return self._lead.title

    @property
    def publisher(self) -> str:
        return self._lead.publisher

    @property
    def effective_at(self) -> datetime:
        """The earliest moment any source placed this development."""
        return min(document.effective_at for document in self.documents)

    @property
    def item_keys(self) -> tuple[str, ...]:
        return tuple(document.dedupe_key for document in self.documents)

    @property
    def canonical_urls(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for document in self.documents:
            seen.setdefault(document.canonical_url, None)
        return tuple(seen)

    @property
    def _lead(self) -> CivicDocument:
        return min(
            self.documents,
            key=lambda document: (
                document.tier.rank, document.effective_at, document.dedupe_key))


@dataclass(frozen=True)
class CivicClusteringResult:
    """The clusters this Run may work on, and the ones its cap held back."""

    clusters: tuple[CivicDocumentCluster, ...]
    deferred_clusters: tuple[CivicDocumentCluster, ...] = ()
    skipped_item_keys: tuple[str, ...] = ()

    @property
    def is_truncated(self) -> bool:
        return bool(self.deferred_clusters)

    @property
    def document_count(self) -> int:
        return sum(len(cluster.documents) for cluster in self.clusters)


def _canonical_order(documents: Iterable[CivicDocument]) -> tuple[CivicDocument, ...]:
    """(effective_at, dedupe_key). dedupe_key is unique per Watch, so this is a total order and
    two runs over the same set always see the documents in the same sequence."""
    ordered = sorted(documents, key=lambda document: (document.effective_at,
                                                      document.dedupe_key))
    seen: set[str] = set()
    for document in ordered:
        if document.dedupe_key in seen:
            raise ValueError(f"duplicate Civic document: {document.dedupe_key}")
        seen.add(document.dedupe_key)
    return tuple(ordered)


def cluster_civic_documents(
    documents: Iterable[CivicDocument],
    *,
    consumed_item_keys: Iterable[str] = (),
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_clusters: int = MAX_CLUSTERS_PER_RUN,
) -> CivicClusteringResult:
    """Group documents into one cluster per development, newest development first.

    `consumed_item_keys` are items a previous Run already turned into a Candidate; they are
    removed before grouping rather than after, so a story whose first article was published
    yesterday clusters on today's coverage alone instead of silently re-opening yesterday's
    Candidate. Raises ValueError on a duplicate document rather than deduplicating it quietly:
    the same item arriving twice in one Run means a connector is repeating itself, which is a
    fault, not an input to absorb."""
    if max_clusters < 1:
        raise ValueError("max_clusters must be at least 1")
    consumed = frozenset(consumed_item_keys)
    ordered = _canonical_order(documents)
    skipped = tuple(
        document.dedupe_key for document in ordered if document.dedupe_key in consumed)
    remaining = tuple(
        document for document in ordered if document.dedupe_key not in consumed)

    by_key = {document.dedupe_key: document for document in remaining}
    groups = group_by_title_similarity(
        ((document.dedupe_key, document.title) for document in remaining),
        similarity_threshold=similarity_threshold,
        min_group_size=1)

    clusters = tuple(
        CivicDocumentCluster(
            cluster_key=min(keys),
            documents=tuple(
                sorted(
                    (by_key[key] for key in keys),
                    key=lambda document: (document.effective_at, document.dedupe_key))))
        for keys in groups)
    ranked = _rank(clusters)
    return CivicClusteringResult(
        clusters=ranked[:max_clusters],
        deferred_clusters=ranked[max_clusters:],
        skipped_item_keys=skipped)


def _rank(clusters: Sequence[CivicDocumentCluster]) -> tuple[CivicDocumentCluster, ...]:
    """Most recent development first, then strongest source, then cluster key.

    The cap has to cut somewhere, and cutting the oldest material is the only order under which
    the Candidates a Run drops are the ones a later Run is most likely to have another chance
    at."""
    return tuple(
        sorted(
            clusters,
            key=lambda cluster: (
                -cluster.effective_at.timestamp(),
                min(document.tier.rank for document in cluster.documents),
                cluster.cluster_key)))


__all__ = [
    "MAX_CLUSTERS_PER_RUN",
    "CivicClusteringResult",
    "CivicDocumentCluster",
    "cluster_civic_documents",
]
