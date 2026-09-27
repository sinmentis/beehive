"""Schema versioning: init_schema() is gated on PRAGMA user_version, so it must stamp fresh and
legacy files, skip current ones, accept a newer file that declares itself compatible, and refuse a
newer file that does not. The fingerprint test is what keeps the gate honest: a schema change
without a SCHEMA_VERSION bump would leave every already-versioned database (production included)
on the old shape, because init_schema skips the reconcile once user_version matches."""
from __future__ import annotations

import hashlib

import pytest

from beehive.db.connection import (
    COMPATIBLE_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SchemaTooNewError,
    connect,
    init_schema,
    schema_version,
)

# Fingerprint of a fresh database at each SCHEMA_VERSION. When the test below fails: bump
# SCHEMA_VERSION in db/connection.py and add the new fingerprint as a new entry. Never edit an
# existing entry -- a released version's shape is fixed.
_FINGERPRINTS = {
    1: "a96625c34c919e76d390bc58d9b0023eff8ef6ce0cab3bc58a95823c07ee9380",
    2: "a575cdcd7a913bcbf512b2b01595fb096cc45c24a814dacaf5affb4645e310f6",
    3: "29cf7c0842653dda73c9341eb37fc2e347f88702843645fc779debc5c9d2a750",
}


def _fingerprint(conn) -> str:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    digest = hashlib.sha256()
    for kind, name, sql in rows:
        digest.update(f"{kind}\0{name}\0{' '.join(sql.split())}\n".encode())
    return digest.hexdigest()


def _compatible_version(conn) -> int:
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = 'schema_compatible_version'").fetchone()
    return int(row["value"])


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def test_schema_changes_come_with_a_version_bump(conn):
    init_schema(conn)
    assert SCHEMA_VERSION in _FINGERPRINTS, (
        f"SCHEMA_VERSION {SCHEMA_VERSION} has no recorded fingerprint; add "
        f"{_fingerprint(conn)!r} to _FINGERPRINTS"
    )
    assert _fingerprint(conn) == _FINGERPRINTS[SCHEMA_VERSION], (
        "The schema changed without a SCHEMA_VERSION bump. Bump SCHEMA_VERSION in "
        f"db/connection.py and record {_fingerprint(conn)!r} as its fingerprint."
    )


def test_fresh_database_is_stamped_with_the_current_version(conn):
    init_schema(conn)
    assert schema_version(conn) == SCHEMA_VERSION
    assert _compatible_version(conn) == COMPATIBLE_SCHEMA_VERSION


def test_unversioned_legacy_database_is_upgraded_and_keeps_its_data(conn):
    init_schema(conn)
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Legacy', '')")
    conn.execute("DELETE FROM app_state WHERE key = 'schema_compatible_version'")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()

    init_schema(conn)

    assert schema_version(conn) == SCHEMA_VERSION
    assert conn.execute("SELECT name FROM channels").fetchone()["name"] == "Legacy"


def test_current_database_skips_the_reconcile(conn):
    init_schema(conn)
    # A reconcile would recreate this table from schema.sql; skipping it leaves it absent.
    conn.execute("DROP TABLE votes")
    conn.commit()

    init_schema(conn)

    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'votes'"
    ).fetchone() is None


def test_newer_but_compatible_database_is_used_without_downgrading(conn):
    init_schema(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    init_schema(conn)

    assert schema_version(conn) == SCHEMA_VERSION + 1


def test_newer_incompatible_database_is_refused_without_writing(conn):
    init_schema(conn)
    conn.execute(
        "UPDATE app_state SET value = ? WHERE key = 'schema_compatible_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    changes_before = conn.total_changes

    with pytest.raises(SchemaTooNewError, match="requires code at schema version"):
        init_schema(conn)

    assert conn.total_changes == changes_before
    assert schema_version(conn) == SCHEMA_VERSION + 1
