import pytest
from fastapi.testclient import TestClient

from beehive.db.connection import connect, init_schema
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    set_admin_password(path, "correct-password")
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path, session_secret="test-secret"),
                       follow_redirects=False)


def test_login_form_renders(client):
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "密码" in resp.text
    assert "autofocus" not in resp.text


def test_login_form_redirects_to_admin_home_when_already_authenticated(client):
    # Clicking the public header's "⚙ 管理后台" link (added in Slice 2 Task 10) always points
    # at /admin/login -- an already-logged-in owner following that link must not be shown the
    # password form again just because they navigated back to it while their session is still
    # valid; that reads as "I got logged out" even though the 90-day session cookie is fine.
    login_resp = client.post("/admin/login", data={"password": "correct-password"})
    client.cookies.set(SESSION_COOKIE_NAME, login_resp.cookies[SESSION_COOKIE_NAME])

    resp = client.get("/admin/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"


def test_login_form_still_renders_with_an_expired_or_invalid_session_cookie(client):
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-value")
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "密码" in resp.text


def test_login_form_shows_last_login_after_an_attempt(client):
    client.post("/admin/login", data={"password": "wrong"})
    resp = client.get("/admin/login")
    assert "上次登录" in resp.text
    assert "失败" in resp.text


def test_correct_password_sets_cookie_and_redirects(client, db_path):
    resp = client.post("/admin/login", data={"password": "correct-password"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"
    assert SESSION_COOKIE_NAME in resp.cookies

    conn = connect(db_path)
    row = conn.execute("SELECT success FROM admin_login_attempts").fetchone()
    assert row["success"] == 1


def test_wrong_password_shows_error_and_sets_no_cookie(client, db_path):
    resp = client.post("/admin/login", data={"password": "wrong-password"})
    assert resp.status_code == 401
    assert "密码错误" in resp.text
    assert SESSION_COOKIE_NAME not in resp.cookies

    conn = connect(db_path)
    row = conn.execute("SELECT success FROM admin_login_attempts").fetchone()
    assert row["success"] == 0


def test_lockout_after_max_failed_attempts(client):
    from beehive.auth.rate_limit import MAX_FAILED_ATTEMPTS
    for _ in range(MAX_FAILED_ATTEMPTS):
        client.post("/admin/login", data={"password": "wrong-password"})
    resp = client.post("/admin/login", data={"password": "correct-password"})
    assert resp.status_code == 429
    assert "登录尝试过多" in resp.text


def test_logout_deletes_session_and_redirects(client):
    login_resp = client.post("/admin/login", data={"password": "correct-password"})
    old_cookie_value = login_resp.cookies[SESSION_COOKIE_NAME]
    client.cookies.set(SESSION_COOKIE_NAME, old_cookie_value)

    logout_resp = client.post("/admin/logout")
    assert logout_resp.status_code == 303
    assert logout_resp.headers["location"] == "/admin/login"

    # Re-apply the OLD (now server-side-revoked) cookie value explicitly — proves logout
    # actually deleted the session row server-side, rather than relying on the test client's
    # cookie jar having merely forgotten it after the Set-Cookie deletion response. The
    # signature still verifies fine (SESSION_SECRET is unchanged); only the DB lookup should
    # now fail.
    client.cookies.set(SESSION_COOKIE_NAME, old_cookie_value)
    resp = client.post("/admin/logout")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_login_form_logo_links_to_dashboard(client):
    resp = client.get("/admin/login")
    assert 'class="brand"' in resp.text
    assert 'href="/"' in resp.text
    assert 'class="brand-mark"' in resp.text


def test_last_login_time_uses_shared_host_local_formatting(client, db_path):
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO admin_login_attempts (attempted_at, ip, country, success) "
        "VALUES ('2026-07-09T00:00:00+00:00', '1.2.3.4', 'NZ', 1)")
    conn.commit()

    resp = client.get("/admin/login")
    assert "2026-07-09 12:00" in resp.text
