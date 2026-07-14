import sqlite3

import pytest

from beehive.db.connection import connect, init_schema
from beehive.email_routing import (
    EmailConfigurationError,
    ResolvedRecipient,
    get_stored_default_email,
    resolve_channel_email,
    resolve_default_email,
    set_stored_default_email,
    validate_email,
)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = connect(str(tmp_path / "routing.db"))
    init_schema(connection)
    return connection


def test_validate_email_trims_a_single_normal_address():
    assert validate_email("  owner@example.com  ") == "owner@example.com"


@pytest.mark.parametrize("value", [
    "",
    "owner",
    "@example.com",
    "owner@",
    "owner@example",
    "owner @example.com",
    "one@example.com,two@example.com",
    "one@example.com;two@example.com",
    "owner@example..com",
    ".owner@example.com",
    "owner.@example.com",
])
def test_validate_email_rejects_invalid_or_multiple_addresses(value):
    with pytest.raises(EmailConfigurationError):
        validate_email(value)


def test_database_default_wins_over_environment_fallback(conn):
    set_stored_default_email(conn, "database@example.com")
    resolved = resolve_default_email(conn, "environment@example.com")
    assert resolved == ResolvedRecipient("database@example.com", "database")


def test_environment_fallback_is_used_without_database_default(conn):
    resolved = resolve_default_email(conn, "environment@example.com")
    assert resolved == ResolvedRecipient("environment@example.com", "environment")


def test_missing_default_is_explicit(conn):
    assert resolve_default_email(conn, None) == ResolvedRecipient(None, "missing")


def test_channel_override_wins_over_default():
    default = ResolvedRecipient("default@example.com", "database")
    resolved = resolve_channel_email({"digest_email": "channel@example.com"}, default)
    assert resolved == ResolvedRecipient("channel@example.com", "channel")


def test_blank_channel_override_inherits_default():
    default = ResolvedRecipient("default@example.com", "database")
    assert resolve_channel_email({"digest_email": None}, default) == default


def test_clearing_stored_default_deletes_the_app_state_value(conn):
    set_stored_default_email(conn, "owner@example.com")
    set_stored_default_email(conn, None)
    assert get_stored_default_email(conn) is None
