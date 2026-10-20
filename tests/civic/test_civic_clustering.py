# tests/civic/test_clustering.py
"""Deterministic clustering of normalized source documents into Candidate-shaped groups.

The properties that matter are not "does it group similar headlines" -- `tests/
test_title_similarity.py` already covers the primitive -- but the three that make the result safe
to build a durable pipeline on: the same set of documents always clusters the same way whatever
order the connectors returned them in, the cluster key does not move when a headline is edited,
and the plan's 25-cluster ceiling defers rather than drops.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from beehive.civic.clustering import (MAX_CLUSTERS_PER_RUN, CivicDocumentCluster,
                                      cluster_civic_documents)
from beehive.civic.documents import CivicDocument
from beehive.domain.civic import CivicEvidenceTier

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


def _doc(key, title, *, source="nz_beehive_releases", tier=CivicEvidenceTier.OFFICIAL,
         minutes=0, url=None, excerpt=""):
    return CivicDocument(
        source_key=source,
        external_key=key,
        canonical_url=url or f"https://example.test/{source}/{key}",
        title=title,
        tier=tier,
        publisher="Beehive",
        first_seen_at=T0 + timedelta(minutes=minutes),
        published_at=T0 + timedelta(minutes=minutes),
        excerpt=excerpt)


# ============================================================================
# Grouping
# ============================================================================


def test_documents_about_one_development_become_one_cluster():
    result = cluster_civic_documents([
        _doc("a", "Government announces new housing policy"),
        _doc("b", "Government announces new housing policy today", source="nz_stuff",
             tier=CivicEvidenceTier.REPUTABLE_MEDIA),
    ])
    assert len(result.clusters) == 1
    assert result.clusters[0].item_keys == (
        "nz_beehive_releases:a", "nz_stuff:b")


def test_unrelated_documents_each_become_their_own_cluster():
    result = cluster_civic_documents([
        _doc("a", "Government announces new housing policy"),
        _doc("b", "Electoral Commission publishes enrolment statistics"),
    ])
    assert len(result.clusters) == 2


def test_a_lone_document_is_still_a_cluster():
    """min_group_size defaults to 2 in the primitive, which would drop every singleton. A single
    official release is a perfectly good Candidate, so clustering must not require corroboration
    to admit one."""
    result = cluster_civic_documents([_doc("a", "Minister resigns")])
    assert len(result.clusters) == 1
    assert result.clusters[0].cluster_key == "nz_beehive_releases:a"


# ============================================================================
# Determinism
# ============================================================================


def test_input_order_never_changes_the_clusters():
    documents = [
        _doc("a", "Government announces new housing policy", minutes=0),
        _doc("b", "Government announces new housing policy update", source="nz_stuff",
             tier=CivicEvidenceTier.REPUTABLE_MEDIA, minutes=5),
        _doc("c", "Electoral Commission publishes enrolment statistics",
             source="nz_electoral_commission", minutes=10),
        _doc("d", "Enrolment statistics published by Electoral Commission",
             source="nz_stuff", tier=CivicEvidenceTier.REPUTABLE_MEDIA, minutes=15),
        _doc("e", "Reserve Bank holds the official cash rate", source="nz_rbnz", minutes=20),
    ]
    expected = [
        (cluster.cluster_key, cluster.item_keys)
        for cluster in cluster_civic_documents(documents).clusters]
    rng = random.Random(20260801)
    for _ in range(25):
        shuffled = documents[:]
        rng.shuffle(shuffled)
        actual = [
            (cluster.cluster_key, cluster.item_keys)
            for cluster in cluster_civic_documents(shuffled).clusters]
        assert actual == expected


def test_the_cluster_key_is_the_lowest_stable_item_key():
    result = cluster_civic_documents([
        _doc("z", "Government announces new housing policy"),
        _doc("a", "Government announces new housing policy today", source="nz_stuff",
             tier=CivicEvidenceTier.REPUTABLE_MEDIA),
    ])
    assert result.clusters[0].cluster_key == "nz_beehive_releases:z"


def test_an_edited_headline_does_not_re_key_the_cluster():
    """external_key is the source's own identifier, so a corrected headline keeps the Candidate
    it already has instead of arriving tomorrow as a brand new development."""
    first = cluster_civic_documents([_doc("a", "Minister resigns")])
    second = cluster_civic_documents([_doc("a", "Minister resigns from Cabinet post")])
    assert first.clusters[0].cluster_key == second.clusters[0].cluster_key


def test_a_duplicate_document_in_one_run_is_a_fault_not_an_input():
    with pytest.raises(ValueError, match="duplicate Civic document"):
        cluster_civic_documents([_doc("a", "Minister resigns"), _doc("a", "Minister resigns")])


# ============================================================================
# Consumed items
# ============================================================================


def test_an_already_consumed_item_is_removed_before_grouping():
    result = cluster_civic_documents(
        [
            _doc("a", "Government announces new housing policy"),
            _doc("b", "Government announces new housing policy today", source="nz_stuff",
                 tier=CivicEvidenceTier.REPUTABLE_MEDIA),
        ],
        consumed_item_keys=["nz_beehive_releases:a"])
    assert result.skipped_item_keys == ("nz_beehive_releases:a",)
    assert len(result.clusters) == 1
    assert result.clusters[0].item_keys == ("nz_stuff:b",)


def test_consuming_every_item_leaves_nothing_to_cluster():
    result = cluster_civic_documents(
        [_doc("a", "Minister resigns")], consumed_item_keys=["nz_beehive_releases:a"])
    assert result.clusters == ()
    assert result.deferred_clusters == ()


# ============================================================================
# The plan 7.4 ceiling
# ============================================================================


_WORDS = [
    "housing", "enrolment", "tariff", "fisheries", "broadband", "railway", "curriculum",
    "customs", "biosecurity", "aquaculture", "seabed", "pharmac", "kiwisaver", "levy",
    "geothermal", "conservation", "immigration", "harbour", "forestry", "cadastre",
    "tourism", "spectrum", "estuary", "viticulture", "apprenticeship", "hydrogen",
    "cybersecurity", "philately", "orchard",
]


def _distinct(index: int) -> str:
    """Titles with no shared tokens at all: anything less and the Jaccard grouping legitimately
    merges them, which would test the primitive rather than the cap."""
    return f"{_WORDS[index]} {_WORDS[index]}scheme {_WORDS[index]}review"


def test_clusters_beyond_the_cap_are_deferred_not_dropped():
    documents = [
        _doc(f"k{index:02d}", _distinct(index), minutes=index)
        for index in range(MAX_CLUSTERS_PER_RUN + 4)]
    result = cluster_civic_documents(documents)
    assert len(result.clusters) == MAX_CLUSTERS_PER_RUN
    assert len(result.deferred_clusters) == 4
    assert result.is_truncated
    kept = {cluster.cluster_key for cluster in result.clusters}
    deferred = {cluster.cluster_key for cluster in result.deferred_clusters}
    assert kept.isdisjoint(deferred)
    assert len(kept | deferred) == MAX_CLUSTERS_PER_RUN + 4


def test_the_cap_keeps_the_most_recent_developments():
    documents = [
        _doc(f"k{index:02d}", _distinct(index), minutes=index) for index in range(6)]
    result = cluster_civic_documents(documents, max_clusters=2)
    assert [cluster.cluster_key for cluster in result.clusters] == [
        "nz_beehive_releases:k05", "nz_beehive_releases:k04"]


def test_a_zero_cap_is_rejected():
    with pytest.raises(ValueError, match="max_clusters"):
        cluster_civic_documents([_doc("a", "Minister resigns")], max_clusters=0)


# ============================================================================
# Cluster properties
# ============================================================================


def test_the_title_comes_from_the_strongest_source():
    """An official record's own wording beats a masthead's headline for the same development."""
    result = cluster_civic_documents([
        _doc("b", "Shock as minister quits", source="nz_stuff",
             tier=CivicEvidenceTier.REPUTABLE_MEDIA, minutes=0),
        _doc("a", "Shock as minister quits abruptly", minutes=5),
    ])
    assert result.clusters[0].title == "Shock as minister quits abruptly"


def test_effective_at_is_the_earliest_report():
    result = cluster_civic_documents([
        _doc("a", "Minister resigns", minutes=30),
        _doc("b", "Minister resigns today", source="nz_stuff",
             tier=CivicEvidenceTier.REPUTABLE_MEDIA, minutes=5),
    ])
    assert result.clusters[0].effective_at == T0 + timedelta(minutes=5)


def test_canonical_urls_are_deduplicated_in_order():
    cluster = CivicDocumentCluster(
        cluster_key="s:a",
        documents=(
            _doc("a", "One", url="https://example.test/x"),
            _doc("b", "Two", url="https://example.test/x"),
            _doc("c", "Three", url="https://example.test/y")))
    assert cluster.canonical_urls == ("https://example.test/x", "https://example.test/y")


def test_an_empty_cluster_is_impossible():
    with pytest.raises(ValueError, match="at least one document"):
        CivicDocumentCluster(cluster_key="s:a", documents=())


def test_no_documents_produces_no_clusters():
    result = cluster_civic_documents([])
    assert result.clusters == ()
    assert result.document_count == 0
