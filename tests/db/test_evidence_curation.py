from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_curation import (get_evidence_curation, list_evidence_curation,
                                           set_evidence_curation)
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.research_sessions import create_research_session
from beehive.db.research_sources import create_research_source
from beehive.domain.research import EvidenceQuality, ResearchSourceOrigin

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def evidence_item_id(conn):
    session_id = create_research_session(conn, "Q", T0).id
    source_id = create_research_source(
        conn, session_id, "web_search", {}, ResearchSourceOrigin.OWNER, T0).id
    return upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0).id


def test_get_evidence_curation_returns_none_before_any_decision(conn, evidence_item_id):
    assert get_evidence_curation(conn, evidence_item_id) is None


def test_set_evidence_curation_creates_row(conn, evidence_item_id):
    curation = set_evidence_curation(conn, evidence_item_id, True, "duplicate of #2", T0)
    assert curation.is_excluded is True
    assert curation.note == "duplicate of #2"
    assert curation.updated_at == T0


def test_set_evidence_curation_is_mutable_in_place(conn, evidence_item_id):
    set_evidence_curation(conn, evidence_item_id, True, "excluded", T0)
    updated = set_evidence_curation(
        conn, evidence_item_id, False, "re-included", T0 + timedelta(hours=1))
    assert updated.is_excluded is False
    assert updated.note == "re-included"
    assert updated.updated_at == T0 + timedelta(hours=1)
    # still exactly one row for this evidence item -- mutated in place, not versioned
    row_count = conn.execute(
        "SELECT COUNT(*) FROM research_evidence_curation WHERE evidence_item_id = ?",
        (evidence_item_id,)).fetchone()[0]
    assert row_count == 1


def test_list_evidence_curation_batches_lookup(conn, evidence_item_id):
    set_evidence_curation(conn, evidence_item_id, True, "x", T0)
    result = list_evidence_curation(conn, [evidence_item_id, 999])
    assert set(result.keys()) == {evidence_item_id}


def test_list_evidence_curation_empty_list_returns_empty_dict(conn):
    assert list_evidence_curation(conn, []) == {}
