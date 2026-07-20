import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import (
    assign_channel,
    create_email_group,
    get_channel_group,
    get_email_group,
    list_email_groups,
    list_member_channels,
)
from beehive.db.sessions import create_session
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


# init_schema's one-time migration always seeds a single empty "Default" group (see
# db/connection.py's _migrate_default_email_group) before any test creates its own channels or
# groups -- every assertion below accounts for that pre-existing baseline row.


def test_groups_tab_requires_session(db_path):
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    resp = client.get("/admin/?tab=groups")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_groups_tab_lists_default_group(authed_client):
    resp = authed_client.get("/admin/?tab=groups")
    assert resp.status_code == 200
    assert "Default" in resp.text
    assert "Email groups" in resp.text


def test_groups_tab_shows_empty_state_when_only_default_has_zero_channels(authed_client):
    resp = authed_client.get("/admin/?tab=groups")
    assert "0 channel" in resp.text or "channels" in resp.text


def test_new_email_group_form_requires_session(db_path):
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    resp = client.get("/admin/email-groups/new")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_new_email_group_form_includes_csrf_token(authed_client):
    resp = authed_client.get("/admin/email-groups/new")
    assert 'name="csrf_token" value="csrf1"' in resp.text


def test_new_email_group_form_lists_existing_channels(authed_client, db_path):
    conn = connect(db_path)
    create_channel(conn, "NZ Finance", "economic news")
    create_channel(conn, "Arcteryx Outlet", "watch for price drops", kind="monitor")
    conn.close()

    resp = authed_client.get("/admin/email-groups/new")
    assert "NZ Finance" in resp.text
    assert "Arcteryx Outlet" in resp.text
    assert "kind-badge" in resp.text  # monitor badge shown for the monitor channel


def test_new_email_group_form_notes_channel_already_in_another_group(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    assign_channel(conn, group_id, channel_id)
    conn.close()

    resp = authed_client.get("/admin/email-groups/new")
    assert "currently in Weekly" in resp.text


def test_create_email_group_succeeds_and_redirects_to_edit_page(authed_client, db_path):
    resp = authed_client.post("/admin/email-groups/new", data={
        "name": "Weekly Roundup",
        "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "owner@example.com",
        "send_interval_hours": "168",
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    groups = list_email_groups(conn)
    assert len(groups) == 2  # baseline "Default" + the new one
    new_group = next(g for g in groups if g["name"] == "Weekly Roundup")
    assert new_group["subject_template"] == "Weekly \u00b7 {date}"
    assert new_group["recipient_email"] == "owner@example.com"
    assert new_group["send_interval_hours"] == 168
    assert resp.headers["location"] == f"/admin/email-groups/{new_group['id']}/edit"


def test_create_email_group_assigns_selected_channels(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.post("/admin/email-groups/new", data={
        "name": "Weekly Roundup",
        "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "",
        "send_interval_hours": "24",
        "channel_ids": [str(channel_id)],
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    new_group_id = int(resp.headers["location"].split("/")[3])
    members = list_member_channels(conn, new_group_id)
    assert [c["id"] for c in members] == [channel_id]


def test_create_email_group_moves_channel_out_of_its_previous_group(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    old_group_id = create_email_group(conn, "Old Group", "Old \u00b7 {date}")
    assign_channel(conn, old_group_id, channel_id)
    conn.close()

    resp = authed_client.post("/admin/email-groups/new", data={
        "name": "New Group",
        "subject_template": "New \u00b7 {date}",
        "recipient_email": "",
        "send_interval_hours": "24",
        "channel_ids": [str(channel_id)],
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    assert list_member_channels(conn, old_group_id) == []
    current_group = get_channel_group(conn, channel_id)
    assert current_group["name"] == "New Group"


def test_create_email_group_rejects_wrong_csrf(authed_client, db_path):
    resp = authed_client.post("/admin/email-groups/new", data={
        "name": "Weekly Roundup",
        "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "",
        "send_interval_hours": "24",
        "csrf_token": "wrong",
    })
    assert resp.status_code == 403

    conn = connect(db_path)
    assert len(list_email_groups(conn)) == 1  # only the baseline "Default" group


def test_create_email_group_rejects_invalid_recipient_email(authed_client, db_path):
    resp = authed_client.post("/admin/email-groups/new", data={
        "name": "Weekly Roundup",
        "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "one@example.com,two@example.com",
        "send_interval_hours": "24",
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 400
    assert "Only one email address is supported" in resp.text
    assert 'value="Weekly Roundup"' in resp.text

    conn = connect(db_path)
    assert len(list_email_groups(conn)) == 1  # rejected -- no group was created


def test_edit_email_group_form_requires_session(db_path):
    conn = connect(db_path)
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    resp = client.get(f"/admin/email-groups/{group_id}/edit")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_edit_email_group_form_404_for_missing_group(authed_client):
    resp = authed_client.get("/admin/email-groups/999/edit")
    assert resp.status_code == 404


def test_edit_email_group_form_shows_current_values(authed_client, db_path):
    conn = connect(db_path)
    group_id = create_email_group(
        conn, "Weekly Roundup", "Weekly \u00b7 {date}", "owner@example.com", 168)
    conn.close()

    resp = authed_client.get(f"/admin/email-groups/{group_id}/edit")
    assert 'value="Weekly Roundup"' in resp.text
    assert 'value="Weekly \u00b7 {date}"' in resp.text
    assert 'value="owner@example.com"' in resp.text
    assert 'value="168"' in resp.text


def test_edit_email_group_form_shows_member_channel_checked(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    assign_channel(conn, group_id, channel_id)
    conn.close()

    resp = authed_client.get(f"/admin/email-groups/{group_id}/edit")
    assert f'value="{channel_id}" checked' in resp.text


def test_update_email_group_saves_changes_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/edit", data={
        "name": "Weekly Roundup",
        "subject_template": "New subject \u00b7 {date}",
        "recipient_email": "owner@example.com",
        "send_interval_hours": "48",
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/email-groups/{group_id}/edit"

    conn = connect(db_path)
    group = get_email_group(conn, group_id)
    assert group["name"] == "Weekly Roundup"
    assert group["subject_template"] == "New subject \u00b7 {date}"
    assert group["recipient_email"] == "owner@example.com"
    assert group["send_interval_hours"] == 48


def test_update_email_group_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/edit", data={
        "name": "Changed", "subject_template": "Changed \u00b7 {date}",
        "recipient_email": "", "send_interval_hours": "24", "csrf_token": "wrong",
    })
    assert resp.status_code == 403

    conn = connect(db_path)
    assert get_email_group(conn, group_id)["name"] == "Weekly"


def test_update_email_group_404_for_missing_group(authed_client):
    resp = authed_client.post("/admin/email-groups/999/edit", data={
        "name": "X", "subject_template": "X", "recipient_email": "",
        "send_interval_hours": "24", "csrf_token": "csrf1",
    })
    assert resp.status_code == 404


def test_update_email_group_rejects_invalid_recipient_email(authed_client, db_path):
    conn = connect(db_path)
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/edit", data={
        "name": "Weekly", "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "not-an-email", "send_interval_hours": "24",
        "csrf_token": "csrf1",
    })
    assert resp.status_code == 400
    assert "Email address must contain one" in resp.text

    conn = connect(db_path)
    assert get_email_group(conn, group_id)["recipient_email"] is None


def test_update_email_group_checking_new_channel_assigns_it(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/edit", data={
        "name": "Weekly", "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "", "send_interval_hours": "24",
        "channel_ids": [str(channel_id)], "csrf_token": "csrf1",
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    assert [c["id"] for c in list_member_channels(conn, group_id)] == [channel_id]


def test_update_email_group_unchecking_channel_removes_it(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    assign_channel(conn, group_id, channel_id)
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/edit", data={
        "name": "Weekly", "subject_template": "Weekly \u00b7 {date}",
        "recipient_email": "", "send_interval_hours": "24",
        "csrf_token": "csrf1",  # channel_ids omitted entirely -- none checked
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    assert list_member_channels(conn, group_id) == []


def test_update_email_group_moves_channel_from_another_group(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    other_group_id = create_email_group(conn, "Other Group", "Other \u00b7 {date}")
    assign_channel(conn, other_group_id, channel_id)
    target_group_id = create_email_group(conn, "Target Group", "Target \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{target_group_id}/edit", data={
        "name": "Target Group", "subject_template": "Target \u00b7 {date}",
        "recipient_email": "", "send_interval_hours": "24",
        "channel_ids": [str(channel_id)], "csrf_token": "csrf1",
    })
    assert resp.status_code == 303

    conn = connect(db_path)
    assert list_member_channels(conn, other_group_id) == []
    assert [c["id"] for c in list_member_channels(conn, target_group_id)] == [channel_id]


def test_delete_email_group_removes_it_and_redirects(authed_client, db_path):
    conn = connect(db_path)
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/delete",
                               data={"csrf_token": "csrf1"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/?tab=groups"

    conn = connect(db_path)
    assert get_email_group(conn, group_id) is None


def test_delete_email_group_does_not_delete_member_channels(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    assign_channel(conn, group_id, channel_id)
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/delete",
                               data={"csrf_token": "csrf1"})
    assert resp.status_code == 303

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM channels WHERE id = ?",
                         (channel_id,)).fetchone()[0] == 1
    assert get_channel_group(conn, channel_id) is None


def test_delete_email_group_rejects_wrong_csrf(authed_client, db_path):
    conn = connect(db_path)
    group_id = create_email_group(conn, "Weekly", "Weekly \u00b7 {date}")
    conn.close()

    resp = authed_client.post(f"/admin/email-groups/{group_id}/delete",
                               data={"csrf_token": "wrong"})
    assert resp.status_code == 403

    conn = connect(db_path)
    assert get_email_group(conn, group_id) is not None


def test_delete_email_group_404_for_missing_group(authed_client):
    resp = authed_client.post("/admin/email-groups/999/delete", data={"csrf_token": "csrf1"})
    assert resp.status_code == 404


def test_edit_channel_page_shows_no_group_message_when_unassigned(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/edit")
    assert "Not in any email group yet" in resp.text


def test_edit_channel_page_shows_current_group_and_links_to_it(authed_client, db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "NZ Finance", "economic news")
    group_id = create_email_group(conn, "Weekly Roundup", "Weekly \u00b7 {date}")
    assign_channel(conn, group_id, channel_id)
    conn.close()

    resp = authed_client.get(f"/admin/channels/{channel_id}/edit")
    assert "Weekly Roundup" in resp.text
    assert f"/admin/email-groups/{group_id}/edit" in resp.text


def test_group_frequency_label_distinguishes_24_from_other_multiples(authed_client, db_path):
    """A group's send_interval_hours is a free-form number, unlike the channel fetch-interval
    dropdown -- 48 must read as "Every 48 hours", not be folded into "Once a day" the way
    _fetch_interval_label folds any hours >= 24."""
    conn = connect(db_path)
    create_email_group(conn, "Every 48h", "Every48 \u00b7 {date}", send_interval_hours=48)
    create_email_group(conn, "Daily Group", "Daily \u00b7 {date}", send_interval_hours=24)
    conn.close()

    resp = authed_client.get("/admin/?tab=groups")
    assert "Every 48 hours" in resp.text
    assert "Once a day" in resp.text
