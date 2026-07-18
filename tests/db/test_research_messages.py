from datetime import datetime, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.research_messages import (append_message, get_message, list_message_citations,
                                           list_messages)
from beehive.db.research_sessions import archive_research_session, create_research_session
from beehive.db.research_sources import create_research_source
from beehive.domain.research import (ConversationMessageStatus, ConversationRole,
                                      EvidenceCitation, EvidenceQuality, ResearchSourceOrigin)

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
    item = upsert_evidence_item(
        conn, session_id, source_id, "e1", "T1", "https://x/1",
        EvidenceQuality.REPORTING, T0)
    return session_id, item.id, item.citation_number


def test_append_message_starts_sequence_at_one(conn, scenario):
    session_id, _, _ = scenario
    message = append_message(conn, session_id, ConversationRole.OWNER, "Hello", T0)
    assert message.sequence_number == 1
    assert message.role == ConversationRole.OWNER
    assert message.status == ConversationMessageStatus.READY


def test_append_message_rejects_archived_session(conn, scenario):
    session_id, _, _ = scenario
    archive_research_session(conn, session_id, T0)
    with pytest.raises(ValueError, match="non-active Research Session"):
        append_message(conn, session_id, ConversationRole.OWNER, "Hello", T0)
    assert list_messages(conn, session_id) == []


def test_append_message_rejects_nonexistent_session(conn):
    with pytest.raises(ValueError, match="non-active Research Session"):
        append_message(conn, 999, ConversationRole.OWNER, "Hello", T0)


def test_append_message_increments_sequence_per_session(conn, scenario):
    session_id, _, _ = scenario
    append_message(conn, session_id, ConversationRole.OWNER, "Hello", T0)
    second = append_message(conn, session_id, ConversationRole.ASSISTANT, "Hi", T0)
    assert second.sequence_number == 2


def test_append_message_with_citations_are_retrievable(conn, scenario):
    session_id, item_id, citation_number = scenario
    citation = EvidenceCitation(evidence_item_id=item_id, citation_number=citation_number)
    message = append_message(
        conn, session_id, ConversationRole.ASSISTANT, "Because of X [1]", T0,
        citations=(citation,))
    assert list_message_citations(conn, message.id) == (citation,)


def test_append_message_allows_pending_assistant_message(conn, scenario):
    session_id, _, _ = scenario
    message = append_message(
        conn, session_id, ConversationRole.ASSISTANT, "...", T0,
        status=ConversationMessageStatus.PENDING)
    assert message.status == ConversationMessageStatus.PENDING


def test_owner_message_must_be_ready(conn, scenario):
    session_id, _, _ = scenario
    with pytest.raises(ValueError, match="Owner messages must be ready"):
        append_message(
            conn, session_id, ConversationRole.OWNER, "Hi", T0,
            status=ConversationMessageStatus.PENDING)


def test_get_message_returns_none_for_missing_id(conn):
    assert get_message(conn, 999) is None


def test_list_messages_ordered_by_sequence_number(conn, scenario):
    session_id, _, _ = scenario
    first = append_message(conn, session_id, ConversationRole.OWNER, "Q1", T0)
    second = append_message(conn, session_id, ConversationRole.ASSISTANT, "A1", T0)
    assert [m.id for m in list_messages(conn, session_id)] == [first.id, second.id]


def test_list_message_citations_empty_when_none(conn, scenario):
    session_id, _, _ = scenario
    message = append_message(conn, session_id, ConversationRole.OWNER, "Q1", T0)
    assert list_message_citations(conn, message.id) == ()


def test_message_citations_are_not_a_polymorphic_table(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_message_citations)")}
    assert "parent_type" not in columns
    assert "message_id" in columns
    assert "evidence_item_id" in columns
