"""`/readyz` -- the one call the dashboard control plane makes to decide whether a resume worked.

Each test here stands for a way the endpoint could pass while the app is broken. A constant
handler passes the happy-path test and fails every other one, which is the point: the failures
below are what makes the check worth wiring a control button to.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from beehive.db.connection import connect, init_schema
from beehive.web.app import create_app
from beehive.web.readiness import REQUIRED_TABLES, check_readiness


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "readyz.db")
    conn = connect(path)
    try:
        init_schema(conn)
    finally:
        conn.close()
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


def test_readyz_reports_ready_over_a_usable_database(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_needs_no_session(client):
    """The prober runs on loopback with no cookie jar. If readiness were behind the admin
    session it would answer 303 forever and a resume would never be reported as complete."""
    response = client.get("/readyz")

    assert response.status_code == 200
    assert "set-cookie" not in {name.lower() for name in response.headers}


def test_readyz_is_unready_when_the_database_file_is_missing(client, tmp_path):
    """The `/data` volume failed to mount. The process is up and would 500 on every page."""
    missing = tmp_path / "gone" / "beehive.db"
    client.app.state.db_path = str(missing)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database_missing"}


def test_readyz_does_not_create_the_database_it_probes(client, tmp_path):
    """`sqlite3.connect` creates the file it is given. A probe that did so would convert an
    unmounted volume into a mounted-and-empty one, and the next check would then report a
    different failure than the one that actually happened."""
    missing = tmp_path / "never-created.db"
    client.app.state.db_path = str(missing)

    client.get("/readyz")

    assert not missing.exists()


def test_readyz_is_unready_when_the_schema_is_not_there(client, tmp_path):
    """An empty-but-real SQLite file: a fresh volume, or a restore that copied nothing."""
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.close()
    client.app.state.db_path = str(empty)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "schema_incomplete"}


def test_readyz_is_unready_when_a_core_table_is_gone(client, db_path):
    """Half a schema is the case a table-count-free check misses: `sqlite_master` opens fine
    and only the page that needs the missing table fails."""
    conn = connect(db_path)
    try:
        conn.execute("DROP TABLE sessions")
        conn.commit()
    finally:
        conn.close()

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["reason"] == "schema_incomplete"


def test_readyz_is_unready_when_the_file_is_not_a_database(client, tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database, just bytes" * 64)
    client.app.state.db_path = str(corrupt)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database_error"}


def test_readyz_says_nothing_private(client, tmp_path):
    """It answers on a loopback port that every other process on this host can reach, so the
    body stays a status word and a fixed reason code -- no path, no exception text, no counts."""
    ready = client.get("/readyz").json()
    client.app.state.db_path = str(tmp_path / "gone.db")
    unready = client.get("/readyz").json()

    assert set(ready) == {"status"}
    assert set(unready) == {"status", "reason"}
    assert str(tmp_path) not in str(ready) + str(unready)


def test_readyz_does_not_write_to_the_database(client, db_path):
    """Monitoring runs this every few seconds forever. A probe that recorded anything would be
    a write amplifier on a single-file SQLite database shared with every worker."""
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('readyz_probe_marker', '1')")
        conn.commit()
        before = _snapshot(conn)
    finally:
        conn.close()

    for _ in range(3):
        assert client.get("/readyz").status_code == 200

    conn = connect(db_path)
    try:
        assert _snapshot(conn) == before
    finally:
        conn.close()


def test_check_readiness_uses_the_real_database_rather_than_a_constant(db_path, tmp_path):
    """The unit under the route, exercised directly: same input, opposite answers."""
    assert check_readiness(db_path).ready is True
    assert check_readiness(str(tmp_path / "absent.db")).ready is False


def test_required_tables_all_exist_in_a_freshly_initialised_database(db_path):
    """Keeps the probe's table list honest against `init_schema`. A rename in `schema.sql`
    would otherwise leave `/readyz` reporting 503 on a perfectly good database."""
    conn = connect(db_path)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()

    assert set(REQUIRED_TABLES) <= present


def test_readyz_carries_the_apps_security_headers(client):
    """It goes through the same middleware as every other route; a health path quietly exempted
    from `no-store` is a response a shared cache is free to keep."""
    response = client.get("/readyz")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_readyz_does_not_disturb_existing_routing(client):
    """The literal path must not shadow the public dashboard or the admin session redirect."""
    assert client.get("/").status_code == 200

    redirect = client.get("/admin/", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/admin/login?")

    assert client.get("/readyzz").status_code == 404


def _snapshot(conn: sqlite3.Connection) -> list[tuple]:
    tables = sorted(
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    return [
        (table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables
    ] + [
        (key, value)
        for key, value in conn.execute("SELECT key, value FROM app_state ORDER BY key")
    ]
