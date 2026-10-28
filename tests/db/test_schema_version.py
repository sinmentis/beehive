"""Schema versioning: init_schema() is gated on PRAGMA user_version, so it must stamp fresh and
legacy files, skip current ones, accept a newer file that declares itself compatible, and refuse a
newer file that does not. The fingerprint test is what keeps the gate honest: a schema change
without a SCHEMA_VERSION bump would leave every already-versioned database (production included)
on the old shape, because init_schema skips the reconcile once user_version matches."""
from __future__ import annotations

import hashlib
import sqlite3

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
    4: "db77e1552a75aa61e515ae2a71faca130d39293271adac067d8300ce30f93d2d",
    5: "16c1cda6559fc9703e6607aa2bf83757204fc60629d6e539ae4ce478d76693bd",
    6: "05f544b1e62d427055a97ead3759cc35cb54037483392a39c30bb1d18519290c",
    7: "e3b8fbd4fed14bcb06e6bfb201fc0c9dcdf016a479d9c94d2ad4f94fd52c10de",
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


@pytest.mark.parametrize("old_version", [0, 5])
def test_upgrade_adds_setup_and_ranking_without_changing_existing_channels(conn, old_version):
    init_schema(conn)
    conn.execute("INSERT INTO channels (name, profile) VALUES ('Existing', 'Saved rules')")
    conn.execute("INSERT INTO sources (channel_id, type) VALUES (1, 'rss')")
    conn.execute("INSERT INTO items (source_id, external_id, title, url, ai_score, is_read) "
                 "VALUES (1, 'old', 'Kept item', 'https://example.com', 81, 1)")
    conn.execute("DROP TABLE ranking_request_items")
    conn.execute("DROP TABLE ranking_requests")
    conn.execute("ALTER TABLE channels DROP COLUMN setup_stage")
    conn.execute(f"PRAGMA user_version = {old_version}")
    conn.commit()

    init_schema(conn)

    channel = dict(conn.execute("SELECT * FROM channels").fetchone())
    assert channel["setup_stage"] is None
    assert channel["profile"] == "Saved rules"
    item = dict(conn.execute("SELECT * FROM items").fetchone())
    assert (item["id"], item["ai_score"], item["is_read"]) == (1, 81, 1)
    assert conn.execute("SELECT COUNT(*) FROM ranking_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ranking_request_items").fetchone()[0] == 0
    assert schema_version(conn) == SCHEMA_VERSION
    assert _compatible_version(conn) == COMPATIBLE_SCHEMA_VERSION
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE channels SET setup_stage = 'invalid'")
    conn.rollback()


def test_newer_but_compatible_database_is_used_without_downgrading(conn):
    init_schema(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    init_schema(conn)

    assert schema_version(conn) == SCHEMA_VERSION + 1


def test_upgrade_preserves_the_successful_brief_and_its_generation_time(conn):
    init_schema(conn)
    conn.execute("INSERT INTO channels (name) VALUES ('Existing')")
    conn.execute("INSERT INTO sources (channel_id,type) VALUES (1,'rss_feed')")
    conn.execute("INSERT INTO items (source_id,external_id,title,url) "
                 "VALUES (1,'old','Kept item','https://example.com')")
    conn.execute(
        "INSERT INTO deep_reads (item_id,status,requested_at,completed_at,result_json,language_code) "
        "VALUES (1,'ready','2026-07-01T00:00:00+00:00','2026-07-01T00:01:00+00:00',"
        "'{\"summary\":\"kept\"}','en')"
    )
    conn.execute("ALTER TABLE deep_reads DROP COLUMN result_completed_at")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()

    init_schema(conn)

    row = conn.execute("SELECT * FROM deep_reads WHERE item_id=1").fetchone()
    assert row["result_json"] == '{"summary":"kept"}'
    assert row["language_code"] == "en"
    assert row["result_completed_at"] == row["completed_at"] == "2026-07-01T00:01:00+00:00"
    assert schema_version(conn) == SCHEMA_VERSION
    assert _compatible_version(conn) == COMPATIBLE_SCHEMA_VERSION


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
