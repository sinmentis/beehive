from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db import app_state
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    return path


@pytest.fixture
def authed_client(db_path):
    client = TestClient(
        create_app(db_path, session_secret="test-secret"),
        follow_redirects=False)
    client.cookies.set(
        SESSION_COOKIE_NAME,
        sign_session_id("sess1", "test-secret"))
    return client


def test_admin_root_requires_session(db_path):
    client = TestClient(
        create_app(db_path, session_secret="test-secret"),
        follow_redirects=False)
    response = client.get("/admin/")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_settings_page_shows_environment_fallback(
    authed_client, monkeypatch,
):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    response = authed_client.get("/admin/")
    assert response.status_code == 200
    assert "fallback@example.com" in response.text
    assert "环境变量" in response.text


def test_settings_page_shows_database_override(
    authed_client, db_path, monkeypatch,
):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    conn = connect(db_path)
    app_state.set(conn, "default_digest_email", "database@example.com")
    conn.close()
    response = authed_client.get("/admin/")
    assert 'value="database@example.com"' in response.text
    assert "数据库" in response.text


def test_admin_home_combines_settings_and_channel_management(
    authed_client, db_path, monkeypatch,
):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    conn = connect(db_path)
    create_channel(conn, "NZ Finance", "economic news", fetch_interval_hours=3)
    conn.close()

    response = authed_client.get("/admin/")

    assert response.status_code == 200
    assert "默认邮件收件地址" in response.text
    assert "fallback@example.com" in response.text
    assert "NZ Finance" in response.text
    assert "每 3 小时抓取一次" in response.text
    assert "+ 新建 Channel" in response.text


def test_settings_validation_error_preserves_channel_list(
    authed_client, db_path,
):
    conn = connect(db_path)
    create_channel(conn, "Still Visible", "profile")
    conn.close()

    response = authed_client.post("/admin/", data={
        "default_digest_email": "one@example.com,two@example.com",
        "csrf_token": "csrf1",
    })

    assert response.status_code == 400
    assert "仅支持一个邮箱地址" in response.text
    assert "Still Visible" in response.text


def test_save_valid_default_email(authed_client, db_path):
    response = authed_client.post("/admin/", data={
        "default_digest_email": " owner@example.com ",
        "csrf_token": "csrf1",
    })
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/?saved=1"
    conn = connect(db_path)
    assert app_state.get(conn, "default_digest_email") == "owner@example.com"


def test_blank_save_restores_environment_fallback(
    authed_client, db_path, monkeypatch,
):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    conn = connect(db_path)
    app_state.set(conn, "default_digest_email", "database@example.com")
    conn.close()
    response = authed_client.post("/admin/", data={
        "default_digest_email": "",
        "csrf_token": "csrf1",
    })
    assert response.status_code == 303
    conn = connect(db_path)
    assert app_state.get(conn, "default_digest_email") is None


def test_blank_save_is_rejected_without_valid_environment_fallback(
    authed_client, db_path, monkeypatch,
):
    monkeypatch.delenv("DIGEST_EMAIL_TO", raising=False)
    conn = connect(db_path)
    app_state.set(conn, "default_digest_email", "database@example.com")
    conn.close()
    response = authed_client.post("/admin/", data={
        "default_digest_email": "",
        "csrf_token": "csrf1",
    })
    assert response.status_code == 400
    assert "无法清除" in response.text
    conn = connect(db_path)
    assert app_state.get(conn, "default_digest_email") == "database@example.com"


def test_invalid_default_email_rerenders_without_writing(
    authed_client, db_path,
):
    response = authed_client.post("/admin/", data={
        "default_digest_email": "one@example.com,two@example.com",
        "csrf_token": "csrf1",
    })
    assert response.status_code == 400
    assert "仅支持一个邮箱地址" in response.text
    conn = connect(db_path)
    assert app_state.get(conn, "default_digest_email") is None


def test_settings_save_rejects_wrong_csrf(authed_client):
    response = authed_client.post("/admin/", data={
        "default_digest_email": "owner@example.com",
        "csrf_token": "wrong",
    })
    assert response.status_code == 403


def test_admin_home_drops_unused_add_row_css(authed_client):
    """The dashed .add-row rule has no markup after the new-Channel action
    switched to .btn ghost small, so it must not ship in the rendered page."""
    response = authed_client.get("/admin/")
    assert response.status_code == 200
    assert ".add-row" not in response.text


def test_channel_empty_state_uses_explicit_styled_class(authed_client):
    """With no channels the empty state must render through its own explicitly
    styled class, not the form-only .field .hint style."""
    response = authed_client.get("/admin/")
    assert response.status_code == 200
    assert 'class="empty-state"' in response.text
    assert "还没有 Channel。" in response.text
    stylesheet = (
        Path(__file__).parent.parent.parent
        / "src" / "beehive" / "web" / "static" / "beehive.css"
    ).read_text()
    assert ".empty-state" in stylesheet


def test_channel_rows_wrap_on_narrow_screens(authed_client, db_path):
    """Long metadata plus two actions must be allowed to wrap on very small
    viewports via a narrow-screen media query on the channel row."""
    conn = connect(db_path)
    create_channel(conn, "NZ Finance", "economic news", fetch_interval_hours=3)
    conn.close()
    response = authed_client.get("/admin/")
    assert response.status_code == 200
    stylesheet = (
        Path(__file__).parent.parent.parent
        / "src" / "beehive" / "web" / "static" / "beehive.css"
    ).read_text()
    assert "@media (max-width:720px)" in stylesheet
    assert "flex-wrap:wrap" in stylesheet


def test_clear_without_env_uses_english_exception_with_chinese_translation():
    """The missing-env clear branch raises an English exception message that is translated
    to the existing Chinese copy through _EMAIL_ERROR_MESSAGES, keeping code/logs English."""
    from beehive.email_routing import EmailConfigurationError
    from beehive.web.admin import _EMAIL_ERROR_MESSAGES, _email_error_message

    english = "Cannot clear default recipient because DIGEST_EMAIL_TO is not configured"
    assert english.isascii()
    assert english in _EMAIL_ERROR_MESSAGES
    assert _email_error_message(
        EmailConfigurationError(english)) == "无法清除：DIGEST_EMAIL_TO 环境变量未配置"
