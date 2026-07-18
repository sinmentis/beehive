from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.research_plan_revisions import (create_plan_revision, get_latest_plan_revision,
                                                  get_plan_revision, list_plan_revisions)
from beehive.db.research_runs import enqueue_research_run
from beehive.db.research_sessions import create_research_session

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def run_id(conn):
    session_id = create_research_session(conn, "Q", T0).id
    return enqueue_research_run(conn, session_id, T0).id


def test_create_plan_revision_starts_at_version_one(conn, run_id):
    revision = create_plan_revision(conn, run_id, '{"queries":[]}', "initial", False, T0)
    assert revision.version == 1
    assert revision.is_validated is False
    assert revision.rationale == "initial"


def test_create_plan_revision_increments_version_per_run(conn, run_id):
    create_plan_revision(conn, run_id, '{"v":1}', "r1", False, T0)
    second = create_plan_revision(
        conn, run_id, '{"v":2}', "r2", True, T0 + timedelta(minutes=1))
    assert second.version == 2
    assert second.is_validated is True


def test_versions_are_independent_per_run(conn):
    session_id = create_research_session(conn, "Q", T0).id
    other_session_id = create_research_session(conn, "Q2", T0).id
    run_a = enqueue_research_run(conn, session_id, T0).id
    run_b = enqueue_research_run(conn, other_session_id, T0).id

    create_plan_revision(conn, run_a, '{"v":1}', "a1", False, T0)
    first_b = create_plan_revision(conn, run_b, '{"v":1}', "b1", False, T0)
    assert first_b.version == 1  # not affected by run_a's revisions


def test_list_plan_revisions_ordered_by_version(conn, run_id):
    create_plan_revision(conn, run_id, '{"v":1}', "r1", False, T0)
    create_plan_revision(conn, run_id, '{"v":2}', "r2", False, T0)
    revisions = list_plan_revisions(conn, run_id)
    assert [r.version for r in revisions] == [1, 2]


def test_get_latest_plan_revision_returns_highest_version(conn, run_id):
    create_plan_revision(conn, run_id, '{"v":1}', "r1", False, T0)
    latest = create_plan_revision(conn, run_id, '{"v":2}', "r2", False, T0)
    assert get_latest_plan_revision(conn, run_id).id == latest.id


def test_get_latest_plan_revision_none_when_no_revisions(conn, run_id):
    assert get_latest_plan_revision(conn, run_id) is None


def test_get_plan_revision_returns_none_for_missing_id(conn):
    assert get_plan_revision(conn, 999) is None
