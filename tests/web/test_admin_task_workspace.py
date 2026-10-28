import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.channels.collection import ChannelCollection
from beehive.connectors.base import RawItem
from beehive.connectors.registry import get as get_connector
from beehive.db.channels import create_channel, get_channel
from beehive.db.connection import connect, init_schema
from beehive.db.email_groups import assign_channel, create_email_group, get_channel_group
from beehive.db.items import update_ai_ranking_by_id
from beehive.db.ranking_requests import get_ranking_request
from beehive.db.sessions import create_session
from beehive.db.sources import create_source, list_by_channel
from beehive.deep_read.fetch import FetchedArticle
from beehive.web.app import create_app
from beehive.web.admin import channels as channel_routes
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

SECRET = "test-secret-at-least-32-characters-long"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "owner@example.test")
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    path = str(tmp_path / "workspace.db")
    conn = connect(path)
    init_schema(conn)
    set_admin_password(path, "demo-password")
    create_session(conn, "owner", "csrf", "2099-01-01T00:00:00+00:00")
    app = create_app(path, session_secret=SECRET)
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("owner", SECRET))
        yield conn, client
    conn.close()


def _channel(conn):
    channel_id = create_channel(
        conn, "Deals", "Original rules", fetch_interval_hours=24,
        fetch_schedule_mode="calendar", fetch_schedule_time="14:00",
    )
    source_id = create_source(
        conn, channel_id, "rss_feed", {"feed_url": "https://example.test/deals/feed"},
    )
    now = datetime.now(timezone.utc)
    collection = ChannelCollection.for_channel(get_channel(conn, channel_id))
    collection.ingest_fetch(conn, source_id, [
        RawItem(external_id=str(index), title=f"Offer {index}",
                url=f"https://example.test/{index}", body="Reported price reduction",
                created_at=now)
        for index in range(3)
    ], now_iso=now.isoformat())
    for index, row in enumerate(conn.execute(
        "SELECT id FROM items WHERE source_id = ? ORDER BY id", (source_id,),
    ).fetchall()):
        score = 20 if index == 0 else 90
        update_ai_ranking_by_id(conn, row["id"], score, "Original summary", "Original reason")
        collection.settle_item_events(conn, row["id"], score, now_iso=now.isoformat())
    return channel_id, source_id


def _item_state(conn):
    return [
        dict(row) for row in conn.execute(
            "SELECT id, ai_score, ai_summary, is_read, fetched_at FROM items ORDER BY id"
        )
    ]


def test_workspace_has_contextual_navigation_and_source_action(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    response = client.get(f"/admin/channels/{channel_id}/edit?section=sources")

    assert response.status_code == 200
    assert 'data-active-section="sources"' in response.text
    assert 'data-section-link="sources" aria-current="page"' in response.text
    assert response.text.index("Add source") < response.text.index('id="channel-profile"')
    assert all(f'data-section-link="{section}"' in response.text
               for section in ("overview", "sources", "rules", "delivery", "settings"))


def test_settings_partial_save_preserves_rules_schedule_and_membership(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    group_id = create_email_group(conn, "Daily")
    assign_channel(conn, group_id, channel_id)
    before = get_channel(conn, channel_id)

    response = client.post(f"/admin/channels/{channel_id}/settings", data={
        "csrf_token": "csrf", "name": "Renamed",
        "profile": "Must not overwrite", "minimum_score": "99", "kind": "monitor",
    })

    assert response.status_code == 303
    after = get_channel(conn, channel_id)
    assert after["name"] == "Renamed"
    for field in ("profile", "minimum_score", "highlight_count", "kind",
                  "fetch_schedule_mode", "fetch_schedule_time", "fetch_schedule_timezone",
                  "fetch_interval_hours"):
        assert after[field] == before[field]
    assert get_channel_group(conn, channel_id)["id"] == group_id


def test_settings_can_clear_an_alert_override_without_resetting_other_fields(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    conn.execute("UPDATE channels SET digest_email = 'alerts@example.test' WHERE id = ?", (channel_id,))
    conn.commit()

    response = client.post(f"/admin/channels/{channel_id}/settings", data={
        "csrf_token": "csrf", "digest_email": "",
    })

    assert response.status_code == 303
    channel = get_channel(conn, channel_id)
    assert channel["digest_email"] is None
    assert channel["fetch_schedule_time"] == "14:00"
    assert channel["profile"] == "Original rules"


def test_saving_rules_is_future_only_but_current_mail_cutoff_is_visible(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    group_id = create_email_group(conn, "Daily")
    assign_channel(conn, group_id, channel_id)
    before = _item_state(conn)

    response = client.post(f"/admin/channels/{channel_id}/rules", data={
        "csrf_token": "csrf", "profile": "Only direct discounts",
        "highlight_count": "20", "minimum_score": "80",
    })

    assert response.status_code == 303
    channel = get_channel(conn, channel_id)
    assert channel["profile"] == "Only direct discounts"
    assert channel["fetch_schedule_time"] == "14:00"
    assert _item_state(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM ranking_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fetch_requests").fetchone()[0] == 0
    preview = client.get(f"/admin/email-groups/{group_id}/preview")
    assert preview.status_code == 200
    assert "Below the score cutoff" in preview.text
    assert "Original summary" in preview.text
    assert '<iframe class="mail-preview" sandbox' in preview.text
    assert "srcdoc=" in preview.text


def test_preview_queues_draft_rules_without_applying_them(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    before = _item_state(conn)
    response = client.post(f"/admin/channels/{channel_id}/rules/preview", data={
        "csrf_token": "csrf", "profile": "Unsaved preview rules",
    })

    assert response.status_code == 303
    request_id = parse_qs(urlparse(response.headers["location"]).query)["ranking_request"][0]
    operation = get_ranking_request(conn, request_id)
    assert operation["mode"] == "preview"
    assert operation["requested_profile"] == "Unsaved preview rules"
    assert operation["status"] == "queued"
    assert get_channel(conn, channel_id)["profile"] == "Original rules"
    assert _item_state(conn) == before
    status = client.get(f"/admin/channels/{channel_id}/rules/requests/{request_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert "Cancel processing" in status.json()["html"]

    cancelled = client.post(
        f"/admin/channels/{channel_id}/rules/requests/{request_id}/cancel",
        data={"csrf_token": "csrf"},
    )
    assert cancelled.status_code == 303
    assert get_ranking_request(conn, request_id)["status"] == "cancelled"
    assert _item_state(conn) == before


def test_reprocessing_requires_confirmation_and_saved_rules(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    denied = client.post(f"/admin/channels/{channel_id}/rules/apply", data={"csrf_token": "csrf"})
    assert denied.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM ranking_requests").fetchone()[0] == 0

    response = client.post(f"/admin/channels/{channel_id}/rules/apply", data={
        "csrf_token": "csrf", "confirmation": "true", "profile": "Must not apply unsaved rules",
    })
    request_id = parse_qs(urlparse(response.headers["location"]).query)["ranking_request"][0]
    assert get_ranking_request(conn, request_id)["requested_profile"] == "Original rules"


@pytest.mark.parametrize("poll_fragment", [False, True])
def test_preview_scores_use_display_rounding_without_changing_raw_cutoff_values(
    workspace, monkeypatch, poll_fragment,
):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    response = client.post(f"/admin/channels/{channel_id}/rules/preview", data={
        "csrf_token": "csrf", "profile": "Original rules",
    })
    request_id = parse_qs(urlparse(response.headers["location"]).query)["ranking_request"][0]
    operation = get_ranking_request(conn, request_id)
    operation.update(status="completed", result=[
        {"title": "Offer", "previous_score": 72.5, "score": 85.5,
         "previous_summary": "Previous", "summary": "Preview", "rationale": "Reason"},
        {"title": "Unscored", "previous_score": None, "score": 0,
         "previous_summary": None, "summary": "Excluded", "rationale": "Reason"},
    ])
    monkeypatch.setattr(channel_routes, "get_ranking_request", lambda *_: operation)

    if poll_fragment:
        response = client.get(f"/admin/channels/{channel_id}/rules/requests/{request_id}")
        html = response.json()["html"]
    else:
        response = client.get(
            f"/admin/channels/{channel_id}/edit?section=rules&ranking_request={request_id}"
        )
        html = response.text

    assert response.status_code == 200
    assert '<span class="score">72</span>' in html
    assert '<span class="score">86</span>' in html
    assert '<span class="score">0</span>' in html
    assert '<span class="score">—</span>' in html
    assert 'data-preview-score="85.5"' in html
    assert "<h4>Offer</h4>" in html
    assert "Edit rules" in html and "Rules used for this run" in html


def test_rule_operations_require_csrf_and_do_not_cross_channel_context(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    response = client.post(f"/admin/channels/{channel_id}/rules/preview", data={
        "csrf_token": "wrong", "profile": "Draft",
    })
    assert response.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM ranking_requests").fetchone()[0] == 0
    valid = client.post(f"/admin/channels/{channel_id}/rules/preview", data={
        "csrf_token": "csrf", "profile": "Draft",
    })
    request_id = parse_qs(urlparse(valid.headers["location"]).query)["ranking_request"][0]
    other_id = create_channel(conn, "Other", "Other rules")
    assert client.get(
        f"/admin/channels/{other_id}/rules/requests/{request_id}"
    ).status_code == 404


def test_guided_setup_waits_for_explicit_activation(workspace, monkeypatch):
    conn, client = workspace
    raw = b"<rss version='2.0'><channel><title>Demo feed</title><item><title>Offer</title><link>https://example.test/offer</link><guid>one</guid></item></channel></rss>"
    monkeypatch.setattr(get_connector("rss_feed"), "_fetch", lambda url: FetchedArticle(
        url=url, status_code=200, content_type="application/rss+xml",
        html=raw.decode(), raw=raw, truncated=False,
    ))
    response = client.post("/admin/channels/new", data={
        "csrf_token": "csrf", "setup": "true", "name": "New setup",
        "kind": "editorial", "profile": "", "fetch_interval_hours": "3",
    })
    assert response.status_code == 303
    channel_id = conn.execute("SELECT id FROM channels WHERE name = 'New setup'").fetchone()[0]
    assert get_channel(conn, channel_id)["setup_stage"] == "sources"
    assert client.post(f"/admin/channels/{channel_id}/trigger-fetch",
                       data={"csrf_token": "csrf"}).status_code == 409
    assert client.post(f"/admin/channels/{channel_id}/duplicate",
                       data={"csrf_token": "csrf"}).status_code == 409
    assert client.post(f"/admin/channels/{channel_id}/activate",
                       data={"csrf_token": "csrf"}).status_code == 400

    saved = client.post(f"/admin/channels/{channel_id}/sources/new", data={
        "csrf_token": "csrf", "type": "rss_feed", "rss_feed_url": "https://example.test/feed",
    })
    assert saved.status_code == 303
    assert get_channel(conn, channel_id)["setup_stage"] == "rules"
    assert list_by_channel(conn, channel_id)[0]["paused_at"] is not None
    client.post(f"/admin/channels/{channel_id}/rules", data={
        "csrf_token": "csrf", "profile": "Useful discounts", "minimum_score": "1",
        "highlight_count": "20",
    })
    assert get_channel(conn, channel_id)["setup_stage"] == "delivery"
    client.post(f"/admin/channels/{channel_id}/delivery", data={
        "csrf_token": "csrf", "delivery_mode": "none",
    })
    assert get_channel(conn, channel_id)["setup_stage"] == "review"
    assert conn.execute("SELECT COUNT(*) FROM fetch_requests").fetchone()[0] == 0
    review = client.get(f"/admin/channels/{channel_id}/edit?section=overview").text
    header = review.split('<div class="adm-actions">', 1)[1].split("</div>", 1)[0]
    facts = review.split('<dl class="workspace-facts">', 1)[1].split("</dl>", 1)[0]
    assert f'action="/admin/channels/{channel_id}/activate"' in header
    assert "Sources: 1" in facts
    assert "Collection starts after activation" in facts
    assert "Useful discounts" in facts
    assert "Minimum AI score: 1" in facts
    assert "Highlights per channel: 20" in facts
    activated = client.post(f"/admin/channels/{channel_id}/activate", data={"csrf_token": "csrf"})
    assert activated.status_code == 303
    assert get_channel(conn, channel_id)["setup_stage"] is None
    assert list_by_channel(conn, channel_id)[0]["paused_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM fetch_requests WHERE channel_id = ?",
                        (channel_id,)).fetchone()[0] == 1


def test_channel_delivery_creates_only_one_daily_group_and_preserves_others(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    other_id = create_channel(conn, "Other", "Other")
    shared = create_email_group(conn, "Shared")
    assign_channel(conn, shared, other_id)
    assign_channel(conn, shared, channel_id)
    data = {
        "csrf_token": "csrf", "delivery_mode": "new", "group_name": "Deals daily",
        "subject_template": "Deals | {date}", "schedule_time": "15:00",
        "schedule_timezone": "Pacific/Auckland",
    }
    assert client.post(f"/admin/channels/{channel_id}/delivery", data=data).status_code == 303
    created = get_channel_group(conn, channel_id)
    assert created["schedule_mode"] == "calendar"
    assert created["schedule_time"] == "15:00"
    assert created["recipient_email"] is None
    assert get_channel_group(conn, other_id)["id"] == shared
    assert client.post(f"/admin/channels/{channel_id}/delivery", data=data).status_code == 303
    assert get_channel_group(conn, channel_id)["id"] == created["id"]
    assert conn.execute("SELECT COUNT(*) FROM email_groups WHERE name = 'Deals daily'").fetchone()[0] == 1


def test_new_delivery_name_collision_preserves_the_submitted_settings(workspace):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    data = {
        "csrf_token": "csrf", "delivery_mode": "new", "group_name": "Deals daily",
        "subject_template": "Deals | {date}", "schedule_time": "15:00",
        "schedule_timezone": "Pacific/Auckland",
    }
    assert client.post(f"/admin/channels/{channel_id}/delivery", data=data).status_code == 303
    before = get_channel_group(conn, channel_id)
    response = client.post(
        f"/admin/channels/{channel_id}/delivery", data={**data, "schedule_time": "07:00"},
    )
    assert response.status_code == 400
    assert 'name="schedule_time" value="07:00"' in response.text
    assert 'id="new-group-name"' in response.text and 'aria-describedby="workspace-error"' in response.text
    assert "already uses this name" in response.text
    assert get_channel_group(conn, channel_id) == before
    assert conn.execute("SELECT COUNT(*) FROM email_groups WHERE name = 'Deals daily'").fetchone()[0] == 1


def test_new_channel_name_conflict_keeps_the_chosen_workflow(workspace):
    conn, client = workspace
    create_channel(conn, "Existing", "Kept rules")
    response = client.post("/admin/channels/new", data={
        "csrf_token": "csrf", "setup": "true", "name": "Existing",
        "profile": "", "kind": "monitor", "fetch_interval_hours": "3",
    })
    assert response.status_code == 400
    assert 'id="kind-monitor" checked' in response.text
    assert 'aria-describedby="channel-name-error"' in response.text
    assert conn.execute("SELECT COUNT(*) FROM channels WHERE name = 'Existing'").fetchone()[0] == 1


def test_schedule_preview_is_read_only_and_explains_first_interval_send(workspace):
    conn, client = workspace
    before = [dict(row) for row in conn.execute("SELECT * FROM email_groups")]
    response = client.get("/admin/email-schedule-preview", params={
        "schedule_mode": "interval", "send_interval_hours": "24",
        "schedule_timezone": "Pacific/Auckland", "schedule_time": "15:00",
    })
    assert response.status_code == 200
    assert "First email:" in response.json()["label"]
    assert "next worker check" in response.json()["label"]
    assert [dict(row) for row in conn.execute("SELECT * FROM email_groups")] == before
    invalid = client.get("/admin/email-schedule-preview", params={
        "schedule_mode": "calendar", "schedule_weekdays": "",
    })
    assert invalid.status_code == 400
    assert "error" in invalid.json()


def test_source_navigation_does_not_change_configurations(workspace):
    conn, client = workspace
    channel_id, source_id = _channel(conn)
    before = json.loads(conn.execute("SELECT config FROM sources WHERE id = ?", (source_id,)).fetchone()[0])
    for section in ("overview", "sources", "rules", "delivery", "settings"):
        assert client.get(f"/admin/channels/{channel_id}/edit?section={section}").status_code == 200
    after = json.loads(conn.execute("SELECT config FROM sources WHERE id = ?", (source_id,)).fetchone()[0])
    assert after == before


def test_delivery_test_works_without_content_but_does_not_claim_a_scheduled_send(
    workspace, monkeypatch,
):
    conn, client = workspace
    group_id = create_email_group(conn, "Empty daily", recipient_email="owner@example.test")
    sent = []

    class Mailer:
        def send(self, subject, plain_text, html=None, *, to_addr=None):
            sent.append((subject, plain_text, to_addr))

    monkeypatch.setattr("beehive.web.admin.email_groups.build_notifier", lambda *_a, **_kw: Mailer())
    response = client.post(
        f"/admin/email-groups/{group_id}/test-send", data={"csrf_token": "csrf"},
    )
    assert response.status_code == 303
    assert len(sent) == 1
    assert sent[0][0] == "[Test] Empty daily"
    assert "delivery test" in sent[0][1]
    assert conn.execute(
        "SELECT last_sent_at, last_checked_at FROM email_groups WHERE id = ?", (group_id,),
    ).fetchone()[:] == (None, None)


def test_delivery_test_does_not_report_log_only_transport_as_sent(workspace):
    conn, client = workspace
    group_id = create_email_group(conn, "No sender", recipient_email="owner@example.test")
    response = client.post(
        f"/admin/email-groups/{group_id}/test-send", data={"csrf_token": "csrf"},
    )
    assert response.status_code == 409
    assert conn.execute(
        "SELECT COUNT(*) FROM admin_actions WHERE action_type = 'email_group_test_sent'"
    ).fetchone()[0] == 0


def test_existing_delivery_group_needs_a_resolvable_recipient(workspace, monkeypatch):
    conn, client = workspace
    channel_id, _ = _channel(conn)
    group_id = create_email_group(conn, "Missing recipient")
    monkeypatch.delenv("DIGEST_EMAIL_TO")
    response = client.post(f"/admin/channels/{channel_id}/delivery", data={
        "csrf_token": "csrf", "delivery_mode": "existing", "group_id": str(group_id),
    })
    assert response.status_code == 400
    assert get_channel_group(conn, channel_id) is None
    assert 'id="delivery-group"' in response.text
    assert 'aria-describedby="workspace-error"' in response.text
