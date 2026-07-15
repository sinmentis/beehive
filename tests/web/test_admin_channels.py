import html
import os

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
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
def authed_client(db_path):
    conn = connect(db_path)
    create_session(conn, "sess1", "csrf1", "2099-01-01T00:00:00")
    conn.close()
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


def test_removed_channels_list_returns_404_for_authenticated_owner(authed_client):
    response = authed_client.get("/admin/channels")

    assert response.status_code == 404


def test_channels_list_shows_name_source_count_and_interval(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news", fetch_interval_hours=3)
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    conn.close()

    resp = authed_client.get("/admin/")
    assert resp.status_code == 200
    assert "NZ Finance" in resp.text
    assert "1 source" in resp.text
    assert "Every 3 hours" in resp.text
    assert "Admin" in resp.text


def test_channels_list_shows_daily_interval_label(authed_client, db_path):
    conn = connect(db_path)
    create_channel(conn, "Daily Channel", "profile", fetch_interval_hours=24)
    conn.close()

    resp = authed_client.get("/admin/")
    assert "Once a day" in resp.text


def test_new_channel_form_requires_session(db_path):
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    resp = client.get("/admin/channels/new")
    assert resp.status_code == 303


def test_new_channel_form_includes_csrf_token(authed_client):
    resp = authed_client.get("/admin/channels/new")
    assert 'name="csrf_token" value="csrf1"' in resp.text


def test_new_channel_form_links_back_to_admin_home(authed_client):
    response = authed_client.get("/admin/channels/new")

    assert '<p class="crumb"><a href="/admin/">← Channel list</a></p>' in response.text
    assert '<a class="btn ghost" href="/admin/">Cancel</a>' in response.text


def test_edit_channel_form_links_back_to_admin_home(
    authed_client, db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    response = authed_client.get(f"/admin/channels/{channel_id}/edit")

    assert '<p class="crumb"><a href="/admin/">← Channel list</a></p>' in response.text


def test_delete_channel_confirmation_uses_keyboard_accessible_details(
    authed_client, db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    response = authed_client.get(f"/admin/channels/{channel_id}/edit")

    assert '<details class="delete-confirm">' in response.text
    assert '<summary class="btn danger">' in response.text
    assert 'class="confirm-toggle"' not in response.text


def test_create_channel_succeeds_with_valid_csrf(authed_client, db_path):
    resp = authed_client.post("/admin/channels/new", data={
        "name": "NZ Finance", "profile": "economic news",
        "fetch_interval_hours": "3", "csrf_token": "csrf1",
    })
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/channels/")
    assert resp.headers["location"].endswith("/edit")

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM channels WHERE name = 'NZ Finance'").fetchone()
    assert row["profile"] == "economic news"
    assert row["fetch_interval_hours"] == 3


def test_create_channel_rejects_wrong_csrf_token(authed_client, db_path):
    resp = authed_client.post("/admin/channels/new", data={
        "name": "NZ Finance", "profile": "economic news",
        "fetch_interval_hours": "3", "csrf_token": "wrong-token",
    })
    assert resp.status_code == 403

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    assert count == 0


def test_create_channel_rejects_non_ascii_csrf_token(authed_client, db_path):
    """Regression guard: hmac.compare_digest raises TypeError on a non-ASCII str csrf_token, which
    would surface as an unhandled 500 instead of a clean 403. Comparing as bytes avoids this."""
    resp = authed_client.post("/admin/channels/new", data={
        "name": "NZ Finance", "profile": "economic news",
        "fetch_interval_hours": "3", "csrf_token": "caf\u00e9\u2122",
    })
    assert resp.status_code == 403

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    assert count == 0


def test_edit_channel_form_shows_current_values(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news", fetch_interval_hours=3)
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/edit")
    assert resp.status_code == 200
    assert 'value="NZ Finance"' in resp.text
    assert "economic news" in resp.text
    assert "r/PersonalFinanceNZ" in resp.text


def test_edit_channel_form_shows_google_news_source_label(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech News", "AI industry news")
    create_source(conn, channel_id, "google_news_query", {"query": "OpenAI"})
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/edit")
    assert resp.status_code == 200
    # Jinja auto-escapes the label's double quotes, so compare against the unescaped HTML.
    assert '"OpenAI"' in html.unescape(resp.text)


def test_edit_channel_form_shows_distinct_icons_per_source_type(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Mixed Sources", "everything")
    create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "PersonalFinanceNZ"})
    create_source(conn, channel_id, "google_news_query", {"query": "OpenAI"})
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/edit")
    assert resp.status_code == 200
    assert "📍 r/PersonalFinanceNZ" in resp.text
    assert '📰 "OpenAI"' in html.unescape(resp.text)


def test_edit_channel_404_for_missing_channel(authed_client):
    resp = authed_client.get("/admin/channels/999/edit")
    assert resp.status_code == 404


def test_update_channel_saves_changes(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Old", "old profile", fetch_interval_hours=3)
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/edit", data={
        "name": "New Name", "profile": "new profile",
        "fetch_interval_hours": "6", "digest_email": "", "csrf_token": "csrf1",
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    assert row["name"] == "New Name"
    assert row["fetch_interval_hours"] == 6


def test_update_channel_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Old", "old profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/edit", data={
        "name": "New Name", "profile": "new profile",
        "fetch_interval_hours": "6", "digest_email": "", "csrf_token": "wrong",
    })
    assert resp.status_code == 403


def test_delete_channel_removes_it_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "To Delete", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/delete",
                               data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM channels WHERE id = ?",
                         (channel_id,)).fetchone()[0] == 0


def test_delete_channel_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "To Delete", "profile")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/delete",
                               data={"csrf_token": "wrong"})
    assert resp.status_code == 403

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM channels WHERE id = ?",
                         (channel_id,)).fetchone()[0] == 1


def test_trigger_fetch_requires_session(db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    resp = client.post(f"/admin/channels/{channel_id}/trigger-fetch", data={"csrf_token": "x"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_trigger_fetch_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/trigger-fetch",
                               data={"csrf_token": "wrong"})
    assert resp.status_code == 403


def test_trigger_fetch_404_for_missing_channel(authed_client):
    resp = authed_client.post("/admin/channels/999/trigger-fetch", data={"csrf_token": "csrf1"})
    assert resp.status_code == 404


def test_trigger_fetch_writes_marker_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.post(f"/admin/channels/{channel_id}/trigger-fetch",
                               data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/?tab=channels&triggered={channel_id}"

    data_dir = os.path.dirname(db_path)
    marker_path = os.path.join(data_dir, "fetch_trigger_channel_id")
    with open(marker_path) as f:
        assert f.read() == str(channel_id)


def test_channels_list_shows_freshness_label(authed_client, db_path):
    conn = connect(db_path)
    create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.get("/admin/")
    assert "Not fetched yet" in resp.text


def test_channels_list_shows_bulk_fetch_controls(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.get("/admin/")
    assert 'action="/admin/channels/trigger-fetch"' in resp.text
    assert f'name="channel_ids" value="{channel_id}"' in resp.text
    assert "Fetch selected" in resp.text


def test_bulk_fetch_writes_selected_channels_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    first_id = create_channel(conn, "First", "profile")
    second_id = create_channel(conn, "Second", "profile")
    conn.close()

    response = authed_client.post(
        "/admin/channels/trigger-fetch",
        data={
            "channel_ids": [str(first_id), str(second_id)],
            "csrf_token": "csrf1",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/?tab=channels&triggered_count=2"
    marker_path = os.path.join(os.path.dirname(db_path), "fetch_trigger_channel_id")
    with open(marker_path) as marker:
        assert marker.read() == f"{first_id}\n{second_id}"


def test_bulk_fetch_requires_a_selection(authed_client):
    response = authed_client.post(
        "/admin/channels/trigger-fetch",
        data={"csrf_token": "csrf1"},
    )

    assert response.status_code == 400
    assert "Select at least one channel to fetch." in response.text


def test_bulk_fetch_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    response = authed_client.post(
        "/admin/channels/trigger-fetch",
        data={"channel_ids": str(channel_id), "csrf_token": "wrong"},
    )

    assert response.status_code == 403


def test_bulk_fetch_requires_session(db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()
    client = TestClient(
        create_app(db_path, session_secret="test-secret"),
        follow_redirects=False,
    )

    response = client.post(
        "/admin/channels/trigger-fetch",
        data={"channel_ids": str(channel_id), "csrf_token": "csrf1"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_bulk_fetch_rejects_missing_channel(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "profile")
    conn.close()

    response = authed_client.post(
        "/admin/channels/trigger-fetch",
        data={
            "channel_ids": [str(channel_id), "999"],
            "csrf_token": "csrf1",
        },
    )

    assert response.status_code == 404
    marker_path = os.path.join(os.path.dirname(db_path), "fetch_trigger_channel_id")
    assert not os.path.exists(marker_path)


def test_channels_list_shows_flash_message_when_triggered_param_matches(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.get(f"/admin/?triggered={channel_id}")
    assert "Fetch request submitted" in resp.text


def test_channels_list_no_flash_message_without_triggered_param(authed_client, db_path):
    conn = connect(db_path)
    create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.get("/admin/")
    assert "Fetch request submitted" not in resp.text


def test_admin_channels_logo_links_to_dashboard(authed_client, db_path):
    resp = authed_client.get("/admin/")
    assert 'class="brand"' in resp.text
    assert 'href="/"' in resp.text
    assert 'class="brand-mark"' in resp.text


def test_channels_list_freshness_has_exact_time_tooltip(authed_client, db_path):
    from beehive.db.sources import record_fetch_success
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00")
    conn.close()

    resp = authed_client.get("/admin/")
    assert 'title="2026-07-09 15:00"' in resp.text
    assert (
        f'href="/channels/{channel_id}"\n'
        '     \n'
        '     title="2026-07-09 15:00 ·'
    ) in resp.text


def test_admin_channels_list_shows_fetch_stats(authed_client, db_path):
    from beehive.db.sources import record_fetch_success
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    record_fetch_success(conn, source_id, "2026-07-09T03:00:00", raw_count=50, new_count=20)
    conn.close()

    resp = authed_client.get("/admin/")
    assert "50" in resp.text and "20" in resp.text and "40%" in resp.text


def test_channels_list_shows_effective_recipient(
    authed_client, db_path, monkeypatch,
):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    conn = connect(db_path)
    create_channel(conn, "Inherited", "profile")
    overridden = create_channel(conn, "Overridden", "profile")
    conn.execute(
        "UPDATE channels SET digest_email = ? WHERE id = ?",
        ("channel@example.com", overridden))
    conn.commit()
    conn.close()

    response = authed_client.get("/admin/")
    assert response.status_code == 200
    assert "fallback@example.com" in response.text
    assert "channel@example.com" in response.text


def test_edit_channel_form_shows_override_and_effective_recipient(
    authed_client, db_path, monkeypatch,
):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    conn = connect(db_path)
    channel_id = create_channel(conn, "Email Channel", "profile")
    conn.execute(
        "UPDATE channels SET digest_email = ? WHERE id = ?",
        ("channel@example.com", channel_id))
    conn.commit()
    conn.close()

    response = authed_client.get(
        f"/admin/channels/{channel_id}/edit")
    assert 'name="digest_email"' in response.text
    assert 'value="channel@example.com"' in response.text
    assert "Currently in effect: channel@example.com" in response.text


def test_update_channel_saves_recipient_override(
    authed_client, db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Email Channel", "profile")
    conn.close()

    response = authed_client.post(
        f"/admin/channels/{channel_id}/edit",
        data={
            "name": "Email Channel",
            "profile": "profile",
            "fetch_interval_hours": "3",
            "digest_email": " channel@example.com ",
            "csrf_token": "csrf1",
        })
    assert response.status_code == 303
    conn = connect(db_path)
    channel = conn.execute(
        "SELECT digest_email FROM channels WHERE id = ?",
        (channel_id,)).fetchone()
    assert channel["digest_email"] == "channel@example.com"


def test_update_channel_blank_recipient_restores_inheritance(
    authed_client, db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Email Channel", "profile")
    conn.execute(
        "UPDATE channels SET digest_email = 'channel@example.com' WHERE id = ?",
        (channel_id,))
    conn.commit()
    conn.close()

    response = authed_client.post(
        f"/admin/channels/{channel_id}/edit",
        data={
            "name": "Email Channel",
            "profile": "profile",
            "fetch_interval_hours": "3",
            "digest_email": "",
            "csrf_token": "csrf1",
        })
    assert response.status_code == 303
    conn = connect(db_path)
    channel = conn.execute(
        "SELECT digest_email FROM channels WHERE id = ?",
        (channel_id,)).fetchone()
    assert channel["digest_email"] is None


def test_invalid_channel_recipient_rerenders_without_writing(
    authed_client, db_path,
):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Email Channel", "profile")
    conn.close()

    response = authed_client.post(
        f"/admin/channels/{channel_id}/edit",
        data={
            "name": "Email Channel",
            "profile": "profile",
            "fetch_interval_hours": "3",
            "digest_email": "one@example.com,two@example.com",
            "csrf_token": "csrf1",
        })
    assert response.status_code == 400
    assert "Only one email address is supported" in response.text
    conn = connect(db_path)
    channel = conn.execute(
        "SELECT digest_email FROM channels WHERE id = ?",
        (channel_id,)).fetchone()
    assert channel["digest_email"] is None


def test_invalid_channel_override_rerender_shows_inherited_effective_default(
    authed_client, db_path, monkeypatch,
):
    """On a rejected override the field keeps the raw submitted value, but the effective
    hint is resolved from the unmodified row so it shows the inherited default, not 未配置."""
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    conn = connect(db_path)
    channel_id = create_channel(conn, "Email Channel", "profile")
    conn.close()

    response = authed_client.post(
        f"/admin/channels/{channel_id}/edit",
        data={
            "name": "Email Channel",
            "profile": "profile",
            "fetch_interval_hours": "3",
            "digest_email": "one@example.com,two@example.com",
            "csrf_token": "csrf1",
        })
    assert response.status_code == 400
    assert 'value="one@example.com,two@example.com"' in response.text
    assert "Currently in effect: fallback@example.com" in response.text
    assert "Currently in effect: Not configured" not in response.text


def test_edit_channel_shows_page_banner_for_invalid_global_default(
    authed_client, db_path, monkeypatch,
):
    """An invalid deployment-wide default surfaces in a page-level banner on the edit page,
    separate from the per-Channel field error slot."""
    monkeypatch.setenv("DIGEST_EMAIL_TO", "one@example.com,two@example.com")
    conn = connect(db_path)
    channel_id = create_channel(conn, "Email Channel", "profile")
    conn.close()

    response = authed_client.get(f"/admin/channels/{channel_id}/edit")
    assert response.status_code == 200
    assert 'class="page-banner"' in response.text
    assert "Only one email address is supported" in response.text


def test_channels_list_shows_page_banner_for_invalid_global_default(
    authed_client, db_path, monkeypatch,
):
    """The same invalid deployment-wide default must not silently vanish on the list page."""
    monkeypatch.setenv("DIGEST_EMAIL_TO", "one@example.com,two@example.com")
    conn = connect(db_path)
    create_channel(conn, "Email Channel", "profile")
    conn.close()

    response = authed_client.get("/admin/")
    assert response.status_code == 200
    assert 'class="page-banner"' in response.text
    assert "Only one email address is supported" in response.text


def test_edit_channel_shows_hackernews_icons_and_prefixed_labels(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Tech", "profile")
    create_source(conn, channel_id, "hackernews_stories", {"feed": "top"})
    create_source(
        conn,
        channel_id,
        "hackernews_query",
        {"query": "local-first", "sort": "recent"},
    )
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/edit")

    assert resp.status_code == 200
    assert resp.text.count("🟧") == 2
    assert "HN · Top" in resp.text
    assert "HN search · local-first" in resp.text
