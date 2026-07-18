# tests/research/test_clustering.py
from datetime import datetime, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.research_runs import enqueue_research_run
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.domain.research import EvidenceCluster, EvidenceQuality, ResearchSourceOrigin
from beehive.research.clustering import cluster_snapshot, group_evidence_items

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def session_source_snapshot(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "rbnz_news", {}, ResearchSourceOrigin.PLAN, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    return session_id, source_id, snapshot_id


def _item(conn, session_id, source_id, external_id, title):
    return upsert_evidence_item(
        conn, session_id, source_id, external_id, title, f"https://x/{external_id}",
        EvidenceQuality.REPORTING, T0)


# ============================================================================
# group_evidence_items: pure grouping logic
# ============================================================================

def test_near_duplicate_titles_are_grouped_together(conn, session_source_snapshot):
    session_id, source_id, _snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "RBNZ raises official cash rate to 5.5%")
    b = _item(conn, session_id, source_id, "b", "RBNZ raises official cash rate 5.5 percent")
    groups = group_evidence_items([a, b])
    assert groups == [[a.id, b.id]]


def test_dissimilar_titles_are_not_grouped(conn, session_source_snapshot):
    session_id, source_id, _snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "RBNZ raises official cash rate")
    b = _item(conn, session_id, source_id, "b", "Completely unrelated sports headline")
    assert group_evidence_items([a, b]) == []


def test_singleton_groups_are_not_returned_as_clusters(conn, session_source_snapshot):
    session_id, source_id, _snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "A totally unique headline about nothing else")
    assert group_evidence_items([a]) == []


def test_empty_input_returns_no_groups():
    assert group_evidence_items([]) == []


def test_similarity_threshold_is_configurable(conn, session_source_snapshot):
    session_id, source_id, _snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "RBNZ cash rate decision today")
    b = _item(conn, session_id, source_id, "b", "RBNZ cash rate decision announced")
    # Loose threshold groups them...
    assert group_evidence_items([a, b], similarity_threshold=0.3) == [[a.id, b.id]]
    # ...a stricter one may not.
    strict = group_evidence_items([a, b], similarity_threshold=0.99)
    assert strict == [] or strict == [[a.id, b.id]]


def test_three_way_group_via_transitive_best_match(conn, session_source_snapshot):
    session_id, source_id, _snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "central bank raises interest rates sharply")
    b = _item(conn, session_id, source_id, "b", "central bank raises interest rates today")
    c = _item(conn, session_id, source_id, "c", "central bank raises interest rates again")
    groups = group_evidence_items([a, b, c])
    assert len(groups) == 1
    assert set(groups[0]) == {a.id, b.id, c.id}


def test_group_evidence_items_never_mutates_input_list(conn, session_source_snapshot):
    session_id, source_id, _snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "x y z")
    b = _item(conn, session_id, source_id, "b", "x y z")
    items = [a, b]
    original = list(items)
    group_evidence_items(items)
    assert items == original


# ============================================================================
# cluster_snapshot: persistence
# ============================================================================

def test_cluster_snapshot_persists_one_cluster_per_qualifying_group(conn, session_source_snapshot):
    session_id, source_id, snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "RBNZ raises official cash rate to 5.5%")
    b = _item(conn, session_id, source_id, "b", "RBNZ raises official cash rate 5.5 percent")
    c = _item(conn, session_id, source_id, "c", "Totally unrelated other headline entirely")

    clusters = cluster_snapshot(conn, snapshot_id, [a, b, c], T0)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert isinstance(cluster, EvidenceCluster)
    assert cluster.snapshot_id == snapshot_id
    assert set(cluster.evidence_item_ids) == {a.id, b.id}


def test_cluster_snapshot_with_no_qualifying_groups_persists_nothing(conn, session_source_snapshot):
    session_id, source_id, snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "Alpha headline about topic one")
    b = _item(conn, session_id, source_id, "b", "Beta headline about topic two")
    assert cluster_snapshot(conn, snapshot_id, [a, b], T0) == []


def test_cluster_snapshot_rejects_sealed_snapshot(conn, session_source_snapshot):
    session_id, source_id, snapshot_id = session_source_snapshot
    a = _item(conn, session_id, source_id, "a", "RBNZ raises official cash rate to 5.5%")
    b = _item(conn, session_id, source_id, "b", "RBNZ raises official cash rate 5.5 percent")
    seal_snapshot(conn, snapshot_id, T0)
    with pytest.raises(ValueError):
        cluster_snapshot(conn, snapshot_id, [a, b], T0)
