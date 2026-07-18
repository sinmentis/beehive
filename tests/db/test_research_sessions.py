from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.research_chat_requests import enqueue_chat_request
from beehive.db.research_messages import append_message
from beehive.db.research_runs import claim_research_run, enqueue_research_run
from beehive.db.research_sessions import (archive_research_session, create_research_session,
                                           get_research_session,
                                           hard_delete_research_session,
                                           list_research_sessions,
                                           touch_research_session_activity,
                                           unarchive_research_session)
from beehive.db.research_sources import create_research_source
from beehive.domain.research import ConversationRole, ResearchSessionStatus, ResearchSourceOrigin

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_create_research_session_starts_active_with_immutable_question(conn):
    session = create_research_session(conn, "What changed?", T0)
    assert session.status == ResearchSessionStatus.ACTIVE
    assert session.question == "What changed?"
    assert session.created_at == T0
    assert session.last_activity_at == T0
    assert session.archived_at is None


def test_get_research_session_returns_none_for_missing_id(conn):
    assert get_research_session(conn, 999) is None


def test_list_research_sessions_orders_by_last_activity_desc(conn):
    first = create_research_session(conn, "First", T0)
    second = create_research_session(conn, "Second", T0 + timedelta(minutes=1))
    sessions = list_research_sessions(conn)
    assert [s.id for s in sessions] == [second.id, first.id]


def test_list_research_sessions_filters_by_status(conn):
    active = create_research_session(conn, "Active", T0)
    archived = create_research_session(conn, "Archived", T0)
    archive_research_session(conn, archived.id, T0 + timedelta(hours=1))

    assert [s.id for s in list_research_sessions(conn, ResearchSessionStatus.ACTIVE)] == [
        active.id]
    assert [s.id for s in list_research_sessions(conn, ResearchSessionStatus.ARCHIVED)] == [
        archived.id]


def test_touch_research_session_activity_updates_only_last_activity(conn):
    session = create_research_session(conn, "Q", T0)
    touch_research_session_activity(conn, session.id, T0 + timedelta(hours=2))
    reloaded = get_research_session(conn, session.id)
    assert reloaded.last_activity_at == T0 + timedelta(hours=2)
    assert reloaded.status == ResearchSessionStatus.ACTIVE


def test_archive_then_unarchive_round_trips(conn):
    session = create_research_session(conn, "Q", T0)
    archived = archive_research_session(conn, session.id, T0 + timedelta(hours=1))
    assert archived.status == ResearchSessionStatus.ARCHIVED
    assert archived.archived_at == T0 + timedelta(hours=1)

    active = unarchive_research_session(conn, session.id, T0 + timedelta(hours=2))
    assert active.status == ResearchSessionStatus.ACTIVE
    assert active.archived_at is None


def test_archive_missing_session_raises(conn):
    with pytest.raises(ValueError, match="no Research Session"):
        archive_research_session(conn, 999, T0)


def test_archive_already_archived_session_raises(conn):
    session = create_research_session(conn, "Q", T0)
    archive_research_session(conn, session.id, T0)
    with pytest.raises(ValueError, match="invalid Research Session transition"):
        archive_research_session(conn, session.id, T0)


def test_archive_rejects_session_with_active_run(conn):
    session = create_research_session(conn, "Q", T0)
    enqueue_research_run(conn, session.id, T0)
    with pytest.raises(ValueError, match="active run or chat request"):
        archive_research_session(conn, session.id, T0)


def test_archive_rejects_session_with_processing_run(conn):
    session = create_research_session(conn, "Q", T0)
    run = enqueue_research_run(conn, session.id, T0)
    claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=600)
    with pytest.raises(ValueError, match="active run or chat request"):
        archive_research_session(conn, session.id, T0)


def test_archive_rejects_session_with_active_chat_request(conn):
    session = create_research_session(conn, "Q", T0)
    create_research_source(conn, session.id, "web_search", {}, ResearchSourceOrigin.OWNER, T0)
    owner_msg = append_message(conn, session.id, ConversationRole.OWNER, "Hi?", T0)
    # A chat request needs a pinned evidence-state revision; use a manufactured id since this
    # test only cares that an active chat request blocks archiving, not synthesis content.
    from beehive.db.evidence_state import create_evidence_state_revision
    from beehive.db.research_snapshots import create_snapshot, seal_snapshot
    run = enqueue_research_run(conn, session.id, T0)
    snapshot = create_snapshot(conn, session.id, run.id, T0)
    seal_snapshot(conn, snapshot.id, T0)
    revision = create_evidence_state_revision(conn, session.id, snapshot.id, [], T0)
    enqueue_chat_request(conn, session.id, owner_msg.id, revision.id, None, 0, T0)

    with pytest.raises(ValueError, match="active run or chat request"):
        archive_research_session(conn, session.id, T0)


def test_archive_succeeds_once_run_is_terminal(conn):
    from beehive.db.research_runs import complete_research_run
    from beehive.domain.research import ResearchRunStatus

    session = create_research_session(conn, "Q", T0)
    run = enqueue_research_run(conn, session.id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=600)
    complete_research_run(
        conn, run.id, claimed.run.claim_token, ResearchRunStatus.COMPLETED,
        T0 + timedelta(minutes=1))

    archived = archive_research_session(conn, session.id, T0 + timedelta(minutes=2))
    assert archived.status == ResearchSessionStatus.ARCHIVED


def test_unarchive_missing_session_raises(conn):
    with pytest.raises(ValueError, match="no Research Session"):
        unarchive_research_session(conn, 999, T0)


def test_hard_delete_removes_session_and_returns_true(conn):
    session = create_research_session(conn, "Q", T0)
    assert hard_delete_research_session(conn, session.id) is True
    assert get_research_session(conn, session.id) is None


def test_hard_delete_missing_session_returns_false(conn):
    assert hard_delete_research_session(conn, 999) is False


def test_hard_delete_cascades_and_revokes_claims(conn):
    session = create_research_session(conn, "Q", T0)
    source = create_research_source(
        conn, session.id, "web_search", {}, ResearchSourceOrigin.OWNER, T0)
    run = enqueue_research_run(conn, session.id, T0)
    claimed = claim_research_run(conn, run.id, T0, lease_seconds=60, deadline_seconds=600)

    hard_delete_research_session(conn, session.id)

    assert conn.execute(
        "SELECT COUNT(*) FROM research_sources WHERE id = ?", (source.id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM research_runs WHERE id = ?", (run.id,)
    ).fetchone()[0] == 0

    # The claim this worker held is revoked: the row it was validated against is gone, so any
    # further write against that claim_token now matches zero rows.
    from beehive.db.research_runs import heartbeat_research_run
    assert heartbeat_research_run(
        conn, run.id, claimed.run.claim_token, T0 + timedelta(seconds=1), lease_seconds=60
    ) is False
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
