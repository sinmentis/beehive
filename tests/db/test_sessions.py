import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session, delete_session, get_session


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_create_and_get_session(conn):
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    session = get_session(conn, "sess1")
    assert session["session_id"] == "sess1"
    assert session["csrf_token"] == "csrf1"
    assert session["expires_at"] == "2099-01-01T00:00:00"


def test_get_session_returns_none_for_missing(conn):
    assert get_session(conn, "nonexistent") is None


def test_delete_session_removes_it(conn):
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    delete_session(conn, "sess1")
    assert get_session(conn, "sess1") is None


def test_delete_session_is_safe_on_missing_session(conn):
    delete_session(conn, "nonexistent")  # must not raise
