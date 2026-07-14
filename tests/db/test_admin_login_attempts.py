import pytest

from beehive.db.admin_login_attempts import (count_recent_failures,
                                                 get_most_recent_attempt, record_attempt)
from beehive.db.connection import connect, init_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_record_attempt_and_count_recent_failures(conn):
    record_attempt(conn, "1.2.3.4", "NZ", False, "2026-07-09T10:00:00")
    record_attempt(conn, "1.2.3.4", "NZ", False, "2026-07-09T10:05:00")
    record_attempt(conn, "1.2.3.4", "NZ", True, "2026-07-09T10:06:00")  # success doesn't count
    assert count_recent_failures(conn, "1.2.3.4", "2026-07-09T09:00:00") == 2


def test_count_recent_failures_ignores_other_ips(conn):
    record_attempt(conn, "1.2.3.4", "NZ", False, "2026-07-09T10:00:00")
    record_attempt(conn, "5.6.7.8", "US", False, "2026-07-09T10:00:00")
    assert count_recent_failures(conn, "1.2.3.4", "2026-07-09T09:00:00") == 1


def test_count_recent_failures_ignores_attempts_before_window(conn):
    record_attempt(conn, "1.2.3.4", "NZ", False, "2026-07-09T08:00:00")
    assert count_recent_failures(conn, "1.2.3.4", "2026-07-09T09:00:00") == 0


def test_get_most_recent_attempt_returns_latest(conn):
    record_attempt(conn, "1.2.3.4", "NZ", False, "2026-07-09T10:00:00")
    record_attempt(conn, "5.6.7.8", "US", True, "2026-07-09T11:00:00")
    latest = get_most_recent_attempt(conn)
    assert latest["ip"] == "5.6.7.8"
    assert latest["success"] == 1


def test_get_most_recent_attempt_returns_none_when_empty(conn):
    assert get_most_recent_attempt(conn) is None
