from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.research_conversation_memory import (get_conversation_memory,
                                                       update_conversation_memory)
from beehive.db.research_messages import append_message
from beehive.db.research_sessions import create_research_session
from beehive.domain.research import ConversationRole

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def session_id(conn):
    return create_research_session(conn, "Q", T0).id


def test_get_conversation_memory_none_before_any_update(conn, session_id):
    assert get_conversation_memory(conn, session_id) is None


def test_update_conversation_memory_creates_row_at_version_one(conn, session_id):
    memory = update_conversation_memory(conn, session_id, "summary v1", None, T0)
    assert memory.version == 1
    assert memory.content == "summary v1"
    assert memory.covers_through_message_id is None


def test_update_conversation_memory_bumps_version_in_place(conn, session_id):
    message = append_message(conn, session_id, ConversationRole.OWNER, "Hi", T0)
    update_conversation_memory(conn, session_id, "summary v1", None, T0)
    updated = update_conversation_memory(
        conn, session_id, "summary v2", message.id, T0 + timedelta(hours=1))
    assert updated.version == 2
    assert updated.content == "summary v2"
    assert updated.covers_through_message_id == message.id

    # exactly one row for this session -- mutated in place, not versioned as separate rows
    row_count = conn.execute(
        "SELECT COUNT(*) FROM research_conversation_memory WHERE session_id = ?",
        (session_id,)).fetchone()[0]
    assert row_count == 1
