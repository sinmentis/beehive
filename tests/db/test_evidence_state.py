from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import (create_evidence_state_revision,
                                        get_evidence_state_revision,
                                        get_evidence_state_revision_for_snapshot,
                                        get_latest_evidence_state_revision,
                                        get_latest_evidence_state_revision_for_snapshot,
                                        list_evidence_state_revisions)
from beehive.db.research_runs import (claim_research_run, complete_research_run,
                                       enqueue_research_run, get_active_research_run)
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.domain.research import EvidenceQuality, ResearchRunStatus, ResearchSourceOrigin

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
    seal_snapshot(conn, snapshot_id, T0)
    return session_id, snapshot_id, item1.id, item2.id


def test_create_evidence_state_revision_starts_at_version_one(conn, scenario):
    session_id, snapshot_id, item1_id, item2_id = scenario
    revision = create_evidence_state_revision(
        conn, session_id, snapshot_id, [item1_id, item2_id], T0)
    assert revision.version == 1
    assert revision.snapshot_id == snapshot_id
    assert set(revision.evidence_item_ids) == {item1_id, item2_id}


def test_create_evidence_state_revision_can_exclude_curated_items(conn, scenario):
    session_id, snapshot_id, item1_id, _ = scenario
    revision = create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    assert revision.evidence_item_ids == (item1_id,)


def test_evidence_state_revisions_are_immutable_and_append_only(conn, scenario):
    session_id, snapshot_id, item1_id, item2_id = scenario
    first = create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    second = create_evidence_state_revision(
        conn, session_id, snapshot_id, [item1_id, item2_id], T0 + timedelta(minutes=1))
    assert second.version == first.version + 1
    # the earlier revision's frozen item set is untouched by the later one
    reloaded_first = get_evidence_state_revision(conn, first.id)
    assert reloaded_first.evidence_item_ids == (item1_id,)


def test_get_latest_evidence_state_revision_returns_highest_version(conn, scenario):
    session_id, snapshot_id, item1_id, item2_id = scenario
    create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    second = create_evidence_state_revision(
        conn, session_id, snapshot_id, [item1_id, item2_id], T0)
    assert get_latest_evidence_state_revision(conn, session_id).id == second.id


def test_get_latest_evidence_state_revision_none_when_no_revisions(conn, scenario):
    session_id, *_ = scenario
    assert get_latest_evidence_state_revision(conn, session_id) is None


def test_get_evidence_state_revision_returns_none_for_missing_id(conn):
    assert get_evidence_state_revision(conn, 999) is None


def test_list_evidence_state_revisions_ordered_by_version(conn, scenario):
    session_id, snapshot_id, item1_id, _ = scenario
    create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    versions = [r.version for r in list_evidence_state_revisions(conn, session_id)]
    assert versions == [1, 2]


def test_create_evidence_state_revision_rejects_still_building_snapshot(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    building_snapshot_id = create_snapshot(conn, session_id, run_id, T0).id
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1", EvidenceQuality.REPORTING, T0)
    with pytest.raises(ValueError, match="not 'sealed'"):
        create_evidence_state_revision(conn, session_id, building_snapshot_id, [item.id], T0)


def test_create_evidence_state_revision_rejects_cross_session_snapshot(conn, scenario):
    _, sealed_snapshot_id, item1_id, _ = scenario
    other_session_id = create_research_session(conn, "Other question", T0).id
    with pytest.raises(ValueError, match="belongs to Research Session"):
        create_evidence_state_revision(
            conn, other_session_id, sealed_snapshot_id, [item1_id], T0)


# -- per-snapshot revision lookups (crash-recovery resume: R1 vs. a later curation revision) --

def test_get_evidence_state_revision_for_snapshot_returns_the_earliest_version(conn, scenario):
    session_id, snapshot_id, item1_id, item2_id = scenario
    r1 = create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id, item2_id], T0)
    create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    found = get_evidence_state_revision_for_snapshot(conn, snapshot_id)
    assert found.id == r1.id
    assert found.version == 1


def test_get_evidence_state_revision_for_snapshot_none_when_no_revisions(conn, scenario):
    _, snapshot_id, *_ = scenario
    assert get_evidence_state_revision_for_snapshot(conn, snapshot_id) is None


def test_get_latest_evidence_state_revision_for_snapshot_returns_the_highest_version(
        conn, scenario):
    session_id, snapshot_id, item1_id, item2_id = scenario
    create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id, item2_id], T0)
    r2 = create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id], T0)
    found = get_latest_evidence_state_revision_for_snapshot(conn, snapshot_id)
    assert found.id == r2.id
    assert found.version == 2


def test_get_latest_evidence_state_revision_for_snapshot_none_when_no_revisions(conn, scenario):
    _, snapshot_id, *_ = scenario
    assert get_latest_evidence_state_revision_for_snapshot(conn, snapshot_id) is None


def test_get_latest_evidence_state_revision_for_snapshot_equals_earliest_absent_curation(
        conn, scenario):
    """When no later curation revision has been built for this snapshot, the LATEST lookup must
    return the exact same (only) revision the EARLIEST lookup does -- the crash-recovery resume
    path relies on these two agreeing whenever no curation has happened yet."""
    session_id, snapshot_id, item1_id, item2_id = scenario
    only = create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id, item2_id], T0)
    assert get_evidence_state_revision_for_snapshot(conn, snapshot_id).id == only.id
    assert get_latest_evidence_state_revision_for_snapshot(conn, snapshot_id).id == only.id


def test_get_latest_evidence_state_revision_for_snapshot_scoped_to_exact_snapshot(conn, scenario):
    """A second, later-sealed snapshot for the SAME session must never leak into a lookup scoped
    to the first snapshot's own id -- each lookup is per-snapshot, never per-session."""
    session_id, snapshot_id, item1_id, item2_id = scenario
    r1 = create_evidence_state_revision(conn, session_id, snapshot_id, [item1_id, item2_id], T0)

    # Terminalize the scenario's own run so a second one can be enqueued for the same session
    # (ADR-0009: at most one active pending/processing Research Run per session).
    first_run = get_active_research_run(conn, session_id)
    claimed = claim_research_run(conn, first_run.id, T0, lease_seconds=60, deadline_seconds=3600)
    complete_research_run(
        conn, first_run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED, T0)

    other_run_id = enqueue_research_run(conn, session_id, T0 + timedelta(minutes=1)).id
    other_snapshot_id = create_snapshot(
        conn, session_id, other_run_id, T0 + timedelta(minutes=1),
        copy_forward_from=snapshot_id).id
    seal_snapshot(conn, other_snapshot_id, T0 + timedelta(minutes=1))
    create_evidence_state_revision(
        conn, session_id, other_snapshot_id, [item1_id, item2_id], T0 + timedelta(minutes=1))

    assert get_evidence_state_revision_for_snapshot(conn, snapshot_id).id == r1.id
    assert get_latest_evidence_state_revision_for_snapshot(conn, snapshot_id).id == r1.id
