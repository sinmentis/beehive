import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session
from beehive.reading_access import reading_is_private, set_reading_private
from beehive.web.admin.common import _safe_return_path
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

_SECRET = "test-secret-at-least-32-characters-long"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "beehive.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    set_admin_password(path, "correct-password")
    return path


@pytest.fixture
def anonymous(db_path):
    return TestClient(create_app(db_path, session_secret=_SECRET), follow_redirects=False)


@pytest.fixture
def owner(db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    client = TestClient(create_app(db_path, session_secret=_SECRET), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", _SECRET))
    return client


def _make_private(db_path, private=True):
    conn = connect(db_path)
    set_reading_private(conn, private)
    conn.close()


def test_reading_pages_are_public_by_default(anonymous, db_path):
    conn = connect(db_path)
    assert reading_is_private(conn) is False
    conn.close()
    assert anonymous.get("/").status_code == 200
    assert anonymous.get("/archive").status_code == 200


def test_private_reading_sends_a_visitor_to_sign_in_and_back(anonymous, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "News", "profile")
    conn.close()
    _make_private(db_path)

    home = anonymous.get("/")
    channel = anonymous.get(f"/channels/{channel_id}?page=2")

    assert home.status_code == 303
    assert home.headers["location"] == "/admin/login?next=%2F"
    assert channel.status_code == 303
    assert channel.headers["location"] == f"/admin/login?next=%2Fchannels%2F{channel_id}%3Fpage%3D2"


def test_private_reading_moves_an_htmx_request_to_sign_in(anonymous, db_path):
    _make_private(db_path)
    response = anonymous.get("/items/1/brief/status", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/admin/login?next=%2Fitems%2F1%2Fbrief%2Fstatus"


def test_private_reading_still_serves_health_static_files_and_sign_in(anonymous, db_path):
    _make_private(db_path)
    assert anonymous.get("/readyz").status_code == 200
    assert anonymous.get("/static/admin.css").status_code == 200
    login = anonymous.get("/admin/login?next=%2F")
    assert login.status_code == 200
    assert "This site is private. Sign in to read it." in login.text
    assert 'href="/">' not in login.text


def test_the_owner_reads_a_private_site_as_usual(owner, db_path):
    _make_private(db_path)
    assert owner.get("/").status_code == 200


def test_signing_in_returns_to_the_page_that_asked(anonymous, db_path):
    _make_private(db_path)
    response = anonymous.post(
        "/admin/login", data={"password": "correct-password", "next": "/archive?q=rates"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/archive?q=rates"


@pytest.mark.parametrize(
    "value",
    ["//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "https://evil.example/"],
)
def test_return_paths_never_leave_the_site(value):
    assert _safe_return_path(value) == "/admin/"


def test_the_owner_switches_reading_access_in_settings(owner, db_path):
    page = owner.get("/admin/?tab=settings")
    assert 'action="/admin/reading-access"' in page.text
    assert '<option value="public" selected>' in page.text

    saved = owner.post("/admin/reading-access", data={"reading_access": "private", "csrf_token": "csrf1"})

    assert saved.status_code == 303
    assert saved.headers["location"] == "/admin/?tab=settings&reading_saved=1"
    conn = connect(db_path)
    assert reading_is_private(conn) is True
    action = conn.execute("SELECT action_type FROM admin_actions ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert action["action_type"] == "reading_made_private"
    settings = owner.get("/admin/?tab=settings&reading_saved=1").text
    assert '<option value="private" selected>' in settings
    assert "Reading access saved" in settings


def test_reading_access_needs_a_valid_choice_and_csrf(owner, db_path):
    assert owner.post(
        "/admin/reading-access", data={"reading_access": "friends", "csrf_token": "csrf1"}
    ).status_code == 422
    assert owner.post(
        "/admin/reading-access", data={"reading_access": "private", "csrf_token": "wrong"}
    ).status_code == 403
    conn = connect(db_path)
    assert reading_is_private(conn) is False
    conn.close()


@pytest.mark.parametrize("tab", ["channels", "groups", "settings", "system"])
def test_every_admin_chapter_has_unique_element_ids(owner, tab):
    """A label points at its control by id, so a repeated id silently breaks the link. The
    reading-access heading and its select once shared one."""
    import collections
    import re

    page = owner.get(f"/admin/?tab={tab}").text
    ids = collections.Counter(re.findall(r'\sid="([^"]+)"', page))
    assert [name for name, count in ids.items() if count > 1] == []
