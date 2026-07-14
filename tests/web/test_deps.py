import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session
from beehive.web.deps import (
    SESSION_COOKIE_NAME,
    get_optional_session,
    require_admin_session,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture
def app(db_path):
    test_app = FastAPI()
    test_app.state.db_path = db_path
    test_app.state.session_secret = "test-secret"

    @test_app.get("/protected")
    def protected(session: dict = Depends(require_admin_session)):
        return {"session_id": session["session_id"]}

    return test_app


def test_valid_session_cookie_grants_access(app, db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    signed = sign_session_id("sess1", "test-secret")

    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, signed)
    resp = client.get("/protected")
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "sess1"}


def test_missing_cookie_redirects_to_login(app):
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/protected")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_tampered_cookie_redirects_to_login(app, db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    signed = sign_session_id("sess1", "test-secret")
    tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, tampered)
    resp = client.get("/protected")
    assert resp.status_code == 303


def test_expired_session_redirects_to_login(app, db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2000-01-01T00:00:00")  # already expired
    conn.close()
    signed = sign_session_id("sess1", "test-secret")

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, signed)
    resp = client.get("/protected")
    assert resp.status_code == 303


def test_deleted_or_nonexistent_session_redirects_to_login(app):
    signed = sign_session_id("never-existed", "test-secret")
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, signed)
    resp = client.get("/protected")
    assert resp.status_code == 303


@pytest.fixture
def optional_app(db_path):
    test_app = FastAPI()
    test_app.state.db_path = db_path
    test_app.state.session_secret = "test-secret"

    @test_app.get("/optional")
    def optional(session: dict | None = Depends(get_optional_session)):
        return {"is_admin": session is not None}

    return test_app


def test_optional_session_returns_none_without_cookie(optional_app):
    client = TestClient(optional_app)
    resp = client.get("/optional")
    assert resp.status_code == 200
    assert resp.json() == {"is_admin": False}


def test_optional_session_returns_session_with_valid_cookie(optional_app, db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    signed = sign_session_id("sess1", "test-secret")

    client = TestClient(optional_app)
    client.cookies.set(SESSION_COOKIE_NAME, signed)
    resp = client.get("/optional")
    assert resp.status_code == 200
    assert resp.json() == {"is_admin": True}


def test_optional_session_returns_none_for_tampered_cookie(optional_app, db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    signed = sign_session_id("sess1", "test-secret")
    tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")

    client = TestClient(optional_app)
    client.cookies.set(SESSION_COOKIE_NAME, tampered)
    resp = client.get("/optional")
    assert resp.status_code == 200
    assert resp.json() == {"is_admin": False}


def test_optional_session_returns_none_for_expired_session(optional_app, db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2000-01-01T00:00:00")  # already expired
    conn.close()
    signed = sign_session_id("sess1", "test-secret")

    client = TestClient(optional_app)
    client.cookies.set(SESSION_COOKIE_NAME, signed)
    resp = client.get("/optional")
    assert resp.status_code == 200
    assert resp.json() == {"is_admin": False}
