from datetime import datetime, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_clusters import (create_evidence_cluster, get_evidence_cluster,
                                           list_evidence_clusters)
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.research_runs import enqueue_research_run
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.domain.research import EvidenceQuality, ResearchSourceOrigin

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def scenario(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item1 = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    item2 = upsert_evidence_item(
        conn, session_id, source_id, "e2", "T2", "https://x/2",
        EvidenceQuality.PRIMARY, T0)
    return snapshot_id, item1.id, item2.id


def test_create_evidence_cluster_persists_members(conn, scenario):
    snapshot_id, item1_id, item2_id = scenario
    cluster = create_evidence_cluster(conn, snapshot_id, [item1_id, item2_id], T0)
    assert cluster.snapshot_id == snapshot_id
    assert set(cluster.evidence_item_ids) == {item1_id, item2_id}


def test_get_evidence_cluster_returns_none_for_missing_id(conn):
    assert get_evidence_cluster(conn, 999) is None


def test_list_evidence_clusters_scoped_to_snapshot(conn, scenario):
    snapshot_id, item1_id, item2_id = scenario
    first = create_evidence_cluster(conn, snapshot_id, [item1_id], T0)
    second = create_evidence_cluster(conn, snapshot_id, [item2_id], T0)
    clusters = list_evidence_clusters(conn, snapshot_id)
    assert {c.id for c in clusters} == {first.id, second.id}


def test_evidence_items_table_has_no_column_referencing_a_cluster(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_evidence_items)")}
    assert "cluster_id" not in columns
    assert not any("cluster" in c for c in columns)


def test_create_evidence_cluster_after_sealing_raises(conn, scenario):
    snapshot_id, item1_id, _ = scenario
    seal_snapshot(conn, snapshot_id, T0)
    with pytest.raises(ValueError, match="not 'building'"):
        create_evidence_cluster(conn, snapshot_id, [item1_id], T0)
    assert list_evidence_clusters(conn, snapshot_id) == []


def test_create_evidence_cluster_missing_snapshot_raises(conn, scenario):
    _, item1_id, _ = scenario
    with pytest.raises(ValueError, match="no Evidence Snapshot"):
        create_evidence_cluster(conn, 999, [item1_id], T0)
