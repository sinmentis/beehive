# tests/auth/test_rate_limit.py
from datetime import datetime, timedelta, timezone

import pytest

from beehive.auth.rate_limit import MAX_FAILED_ATTEMPTS, is_locked_out
from beehive.db.admin_login_attempts import record_attempt
from beehive.db.connection import connect, init_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


def test_not_locked_out_with_no_attempts(conn):
    now = datetime.now(timezone.utc)
    assert is_locked_out(conn, "1.2.3.4", now) is False


def test_locked_out_after_max_failed_attempts_within_window(conn):
    now = datetime.now(timezone.utc)
    for _ in range(MAX_FAILED_ATTEMPTS):
        record_attempt(conn, "1.2.3.4", "NZ", False, now.isoformat())
    assert is_locked_out(conn, "1.2.3.4", now) is True


def test_not_locked_out_below_max_failed_attempts(conn):
    now = datetime.now(timezone.utc)
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        record_attempt(conn, "1.2.3.4", "NZ", False, now.isoformat())
    assert is_locked_out(conn, "1.2.3.4", now) is False


def test_not_locked_out_when_failures_are_outside_window(conn):
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=30)
    for _ in range(MAX_FAILED_ATTEMPTS):
        record_attempt(conn, "1.2.3.4", "NZ", False, old.isoformat())
    assert is_locked_out(conn, "1.2.3.4", now) is False


def test_successful_attempts_do_not_count_toward_lockout(conn):
    now = datetime.now(timezone.utc)
    for _ in range(MAX_FAILED_ATTEMPTS):
        record_attempt(conn, "1.2.3.4", "NZ", True, now.isoformat())
    assert is_locked_out(conn, "1.2.3.4", now) is False
