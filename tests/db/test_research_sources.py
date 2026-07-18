from datetime import datetime, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.research_sessions import create_research_session
from beehive.db.research_sources import (create_research_source, get_research_source,
                                          list_research_sources)
from beehive.domain.research import ResearchSourceOrigin

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def session_id(conn):
    return create_research_session(conn, "Q", T0).id


def test_create_research_source_persists_config_and_origin(conn, session_id):
    source = create_research_source(
        conn, session_id, "web_search", {"query": "x"}, ResearchSourceOrigin.PLAN, T0)
    assert source.session_id == session_id
    assert source.connector_type == "web_search"
    assert source.config == {"query": "x"}
    assert source.origin == ResearchSourceOrigin.PLAN


def test_get_research_source_returns_none_for_missing_id(conn):
    assert get_research_source(conn, 999) is None


def test_list_research_sources_scoped_to_session_ordered_by_id(conn, session_id):
    other_session = create_research_session(conn, "Other", T0).id
    first = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0)
    second = create_research_source(
        conn, session_id, "rss", {}, ResearchSourceOrigin.PLAN, T0)
    create_research_source(conn, other_session, "rss", {}, ResearchSourceOrigin.PLAN, T0)

    sources = list_research_sources(conn, session_id)
    assert [s.id for s in sources] == [first.id, second.id]


def test_source_deleted_when_session_hard_deleted(conn, session_id):
    from beehive.db.research_sessions import hard_delete_research_session

    source = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0)
    hard_delete_research_session(conn, session_id)
    assert get_research_source(conn, source.id) is None
