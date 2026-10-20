from beehive.db import app_state
from beehive.db.connection import connect, init_schema


def test_get_missing_key_returns_default(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    assert app_state.get(conn, "missing", default="fallback") == "fallback"


def test_set_then_get_roundtrip(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    app_state.set(conn, "last_digest_sent_at", "2026-07-09T08:00:00")
    assert app_state.get(conn, "last_digest_sent_at") == "2026-07-09T08:00:00"


def test_set_overwrites_existing_value(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    app_state.set(conn, "k", "v1")
    app_state.set(conn, "k", "v2")
    assert app_state.get(conn, "k") == "v2"


def test_delete_removes_existing_key(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    app_state.set(conn, "default_digest_email", "owner@example.com")
    app_state.delete(conn, "default_digest_email")
    assert app_state.get(conn, "default_digest_email") is None


def test_compare_and_set_only_replaces_the_expected_value(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)

    assert app_state.compare_and_set(conn, "cursor", None, "5")
    assert not app_state.compare_and_set(conn, "cursor", None, "7")
    assert not app_state.compare_and_set(conn, "cursor", "4", "7")
    assert app_state.get(conn, "cursor") == "5"
    assert app_state.compare_and_set(conn, "cursor", "5", "9")
    assert app_state.get(conn, "cursor") == "9"
