"""Route tests for the deep-read owner-only request/regenerate endpoint plus the public/
optional-session dedicated brief page and its small HTMX status-polling endpoint. Also covers
Dashboard/Channel/Archive batch-loading and decorating ranked items with deep-read state/action
metadata (web/deep_read_view.decorate_deep_read_state) without touching their existing read/
open/vote behavior."""
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.deep_reads import (claim_deep_read, complete_deep_read_success, fail_deep_read,
                                   get_deep_read, request_deep_read)
from beehive.db.items import insert_new, update_ai_ranking
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.web.app import create_app
from beehive.web.deep_read_view import DeepReadCacheError, parse_deep_read_result
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

_NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)

_READY_RESULT = {
    "item_id": "1",
    "bottom_line": "Rates fell by 25 basis points.",
    "key_findings": ["Inflation cooled", "Wage growth held"],
    "important_figures": [{"value": "25bp", "label": "rate cut"}],
    "why_it_matters": "Borrowing costs will ease for households.",
    "limitations": "Based on a single central bank statement.",
}


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def client(conn):
    path, _ = conn
    return TestClient(create_app(path), follow_redirects=False)


@pytest.fixture
def db_path(conn):
    path, _ = conn
    return path


@pytest.fixture
def authed_client(db_path, conn):
    _, c = conn
    set_admin_password(db_path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(create_app(db_path, session_secret="test-secret"),
                         follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", "test-secret"))
    return client


def _create_ranked_item(c, *, channel_name="Tech", score=90):
    channel_id = create_channel(c, channel_name, "developer news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="A story", url="https://example.com/a"))
    update_ai_ranking(c, source_id, "t1", score=score, summary="s", rationale="r")
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    return channel_id, item_id


def _create_unranked_item(c, *, channel_name="Tech"):
    channel_id = create_channel(c, channel_name, "developer news")
    source_id = create_source(c, channel_id, "reddit_subreddit", {"subreddit": "x"})
    insert_new(c, source_id, RawItem(external_id="t1", title="A story", url="https://example.com/a"))
    item_id = c.execute("SELECT id FROM items WHERE external_id='t1'").fetchone()[0]
    return channel_id, item_id


def _complete_ready(c, item_id, *, warning_code=None, result=None):
    claimed = claim_deep_read(c, item_id, _NOW, lease_seconds=1500)
    complete_deep_read_success(
        c, item_id, claimed.request_version, claimed.claim_token,
        json.dumps(result or _READY_RESULT), "en", _NOW, warning_code=warning_code)


def _fail(c, item_id, *, error_code="fetch", error_detail="top secret stack trace"):
    claimed = claim_deep_read(c, item_id, _NOW, lease_seconds=1500)
    fail_deep_read(c, item_id, claimed.request_version, claimed.claim_token,
                   error_code, error_detail, _NOW)


# ============================================================================
# POST /items/{item_id}/deep-read
# ============================================================================

def test_deep_read_route_requires_session(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    resp = client.post(f"/items/{item_id}/deep-read",
                        data={"csrf_token": "x", "origin": "dashboard"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert get_deep_read(c, item_id) is None


def test_deep_read_route_rejects_wrong_csrf(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "wrong", "origin": "dashboard"})
    assert resp.status_code == 403
    assert get_deep_read(c, item_id) is None


def test_deep_read_route_rejects_missing_item(conn, authed_client):
    resp = authed_client.post("/items/999/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard"})
    assert resp.status_code == 404


def test_deep_read_route_rejects_unranked_item(conn, authed_client):
    _, c = conn
    _, item_id = _create_unranked_item(c)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard"})
    assert resp.status_code == 422
    assert get_deep_read(c, item_id) is None


@pytest.mark.parametrize("origin", ["dashboard", "archive", "channel"])
def test_deep_read_route_accepts_allowlisted_origins_and_builds_matching_redirect(
    conn, authed_client, origin,
):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c)
    data = {"csrf_token": "csrf1", "origin": origin}
    if origin == "channel":
        data["channel_id"] = str(channel_id)

    resp = authed_client.post(f"/items/{item_id}/deep-read", data=data)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(f"/items/{item_id}/brief")
    assert f"origin={origin}" in location
    if origin == "channel":
        assert f"channel_id={channel_id}" in location
    else:
        assert "channel_id" not in location


def test_deep_read_route_rejects_invalid_origin(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "evil"})
    assert resp.status_code == 422
    assert get_deep_read(c, item_id) is None


def test_deep_read_route_channel_origin_requires_valid_channel_id(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "channel",
                                     "channel_id": "999"})
    assert resp.status_code == 404
    assert get_deep_read(c, item_id) is None


def test_deep_read_route_channel_origin_rejects_a_real_but_unrelated_channel(conn, authed_client):
    """A crafted request naming a channel that genuinely exists -- but does not own this item --
    must be rejected exactly like a nonexistent one, not merely "any real channel_id"."""
    _, c = conn
    _, item_id = _create_ranked_item(c, channel_name="Tech")
    other_channel_id = create_channel(c, "Finance", "economic news")

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "channel",
                                     "channel_id": str(other_channel_id)})
    assert resp.status_code == 404
    assert get_deep_read(c, item_id) is None


def test_deep_read_route_creates_pending_row_and_redirects_to_brief(
    conn, authed_client, db_path,
):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/items/{item_id}/brief?origin=dashboard"

    deep_read = get_deep_read(c, item_id)
    assert deep_read is not None
    assert deep_read.status == "pending"
    assert deep_read.request_version == 1


def test_deep_read_route_writes_wakeup_marker_after_commit(conn, authed_client, db_path):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    authed_client.post(f"/items/{item_id}/deep-read",
                        data={"csrf_token": "csrf1", "origin": "dashboard"})

    marker_path = os.path.join(os.path.dirname(db_path), "deep_read_trigger")
    assert os.path.exists(marker_path)


def test_deep_read_route_cache_reuse_returns_ready_without_regenerate(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard"})
    assert resp.status_code == 303

    deep_read = get_deep_read(c, item_id)
    assert deep_read.status == "ready"
    assert deep_read.request_version == 1  # untouched: a cache hit, not a new attempt


def test_deep_read_route_pending_call_is_idempotent(conn, authed_client, db_path):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    authed_client.post(f"/items/{item_id}/deep-read",
                        data={"csrf_token": "csrf1", "origin": "dashboard"})
    first = get_deep_read(c, item_id)

    authed_client.post(f"/items/{item_id}/deep-read",
                        data={"csrf_token": "csrf1", "origin": "dashboard"})
    second = get_deep_read(c, item_id)

    assert first.status == second.status == "pending"
    assert first.request_version == second.request_version == 1
    rows = c.execute("SELECT COUNT(*) FROM deep_reads WHERE item_id = ?", (item_id,)).fetchone()[0]
    assert rows == 1


def test_deep_read_route_regenerate_ignored_while_pending(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    authed_client.post(f"/items/{item_id}/deep-read",
                        data={"csrf_token": "csrf1", "origin": "dashboard", "regenerate": "true"})

    deep_read = get_deep_read(c, item_id)
    assert deep_read.status == "pending"
    assert deep_read.request_version == 1


def test_deep_read_route_regenerate_from_ready_requeues(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard",
                                     "regenerate": "true"})
    assert resp.status_code == 303

    deep_read = get_deep_read(c, item_id)
    assert deep_read.status == "pending"
    assert deep_read.request_version == 2
    assert deep_read.result_json is None


def test_deep_read_route_regenerate_from_failed_requeues(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard",
                                     "regenerate": "true"})
    assert resp.status_code == 303

    deep_read = get_deep_read(c, item_id)
    assert deep_read.status == "pending"
    assert deep_read.request_version == 2
    assert deep_read.error_code is None


def test_deep_read_route_without_regenerate_keeps_stale_failure(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard"})
    assert resp.status_code == 303

    deep_read = get_deep_read(c, item_id)
    assert deep_read.status == "failed"
    assert deep_read.request_version == 1


def test_deep_read_route_marker_write_failure_is_logged_and_request_still_succeeds(
    conn, authed_client, monkeypatch, capsys,
):
    _, c = conn
    _, item_id = _create_ranked_item(c)

    def _boom(data_dir):
        raise OSError("disk full")

    monkeypatch.setattr("beehive.web.public.request_deep_read_worker", _boom)

    resp = authed_client.post(f"/items/{item_id}/deep-read",
                               data={"csrf_token": "csrf1", "origin": "dashboard"})
    assert resp.status_code == 303

    deep_read = get_deep_read(c, item_id)
    assert deep_read.status == "pending"  # DB commit already happened before the marker attempt

    captured = capsys.readouterr()
    assert "deep-read" in captured.out
    assert "disk full" in captured.out


# ============================================================================
# GET /items/{item_id}/brief
# ============================================================================

def test_brief_page_renders_ready_result_publicly(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "Rates fell by 25 basis points." in resp.text
    assert "Inflation cooled" in resp.text
    assert "25bp" in resp.text
    assert "rate cut" in resp.text
    assert "Borrowing costs will ease for households." in resp.text
    assert "Based on a single central bank statement." in resp.text


def test_brief_page_hides_owner_controls_when_anonymous(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "deep-read-owner-controls" not in resp.text
    assert "csrf_token" not in resp.text


def test_brief_page_shows_regenerate_control_to_owner(conn, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = authed_client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "deep-read-owner-controls" in resp.text
    assert 'name="csrf_token" value="csrf1"' in resp.text
    assert 'name="regenerate" value="true"' in resp.text


def test_brief_page_never_renders_raw_error_detail(conn, client, authed_client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id, error_detail="top secret stack trace")

    anon_resp = client.get(f"/items/{item_id}/brief")
    owner_resp = authed_client.get(f"/items/{item_id}/brief")
    assert "top secret stack trace" not in anon_resp.text
    assert "top secret stack trace" not in owner_resp.text
    # the safe, localized copy for a 'fetch' failure is shown instead
    assert "couldn&#39;t retrieve the original article" in anon_resp.text


@pytest.mark.parametrize("error_code,expected_snippet", [
    ("fetch", "couldn&#39;t retrieve the original article"),
    ("fetch_not_found", "returned HTTP 404"),
    ("fetch_http_error", "returned an HTTP error"),
    ("fetch_timeout", "20-second safety limit"),
    ("extraction", "did not expose readable article text"),
    ("extraction_no_text", "did not expose readable article text"),
    ("extraction_google_news", "Google News returned its own wrapper page"),
    ("llm", "article text was retrieved"),
    ("unavailable", "isn&#39;t available for this item"),
])
def test_brief_page_maps_error_code_to_localized_copy(conn, client, error_code, expected_snippet):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id, error_code=error_code)

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert expected_snippet in resp.text


def test_brief_page_explains_legacy_google_news_extraction_failure(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    c.execute(
        "UPDATE items SET url = ? WHERE id = ?",
        ("https://news.google.com/rss/articles/opaque-id?oc=5", item_id),
    )
    c.commit()
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id, error_code="extraction")

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert "Google News returned its own wrapper page" in resp.text
    assert "the LLM was not called" in resp.text
    assert "top secret stack trace" not in resp.text


def test_brief_page_preserves_new_post_redirect_extraction_classification(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    c.execute(
        "UPDATE items SET url = ? WHERE id = ?",
        ("https://news.google.com/rss/articles/opaque-id?oc=5", item_id),
    )
    c.commit()
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id, error_code="extraction_no_text")

    resp = client.get(f"/items/{item_id}/brief")

    assert resp.status_code == 200
    assert "did not expose readable article text" in resp.text
    assert "Google News returned its own wrapper page" not in resp.text


def test_brief_page_renders_localized_unavailable_failure_for_malformed_cache(conn, client, db_path):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)
    c.execute("UPDATE deep_reads SET result_json = ? WHERE item_id = ?",
              ("not valid json at all", item_id))
    c.commit()

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "not valid json at all" not in resp.text
    assert "isn&#39;t available for this item" in resp.text


def test_brief_page_renders_missing_field_malformed_cache_safely(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, result={"bottom_line": "only this field"})

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "only this field" not in resp.text
    assert "isn&#39;t available for this item" in resp.text


def test_brief_page_renders_whitespace_only_bottom_line_as_unavailable_failure(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, result=dict(_READY_RESULT, bottom_line="   "))

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "isn&#39;t available for this item" in resp.text


def test_brief_page_renders_empty_key_findings_as_unavailable_failure(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, result=dict(_READY_RESULT, key_findings=[]))

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "isn&#39;t available for this item" in resp.text


def test_parse_deep_read_result_rejects_missing_item_id():
    result = dict(_READY_RESULT)
    del result["item_id"]
    with pytest.raises(DeepReadCacheError):
        parse_deep_read_result(json.dumps(result), 1)


def test_parse_deep_read_result_rejects_mismatched_item_id():
    with pytest.raises(DeepReadCacheError):
        parse_deep_read_result(json.dumps(_READY_RESULT), 42)


def test_parse_deep_read_result_accepts_matching_item_id():
    view = parse_deep_read_result(json.dumps(_READY_RESULT), 1)
    assert view.bottom_line == _READY_RESULT["bottom_line"]


def test_parse_deep_read_result_accepts_empty_limitations():
    result = dict(_READY_RESULT, limitations="")
    view = parse_deep_read_result(json.dumps(result), 1)
    assert view.limitations == ""


@pytest.mark.parametrize("field", ["bottom_line", "why_it_matters"])
@pytest.mark.parametrize("blank_value", ["", "   ", "\n\t"])
def test_parse_deep_read_result_rejects_whitespace_only_required_text(field, blank_value):
    result = dict(_READY_RESULT, **{field: blank_value})
    with pytest.raises(DeepReadCacheError):
        parse_deep_read_result(json.dumps(result), 1)


def test_parse_deep_read_result_rejects_empty_key_findings_list():
    result = dict(_READY_RESULT, key_findings=[])
    with pytest.raises(DeepReadCacheError):
        parse_deep_read_result(json.dumps(result), 1)


@pytest.mark.parametrize("blank_finding", ["", "   ", "\n"])
def test_parse_deep_read_result_rejects_whitespace_only_key_finding(blank_finding):
    result = dict(_READY_RESULT, key_findings=["A real finding", blank_finding])
    with pytest.raises(DeepReadCacheError):
        parse_deep_read_result(json.dumps(result), 1)


def test_parse_deep_read_result_accepts_empty_important_figures_list():
    result = dict(_READY_RESULT, important_figures=[])
    view = parse_deep_read_result(json.dumps(result), 1)
    assert view.important_figures == []


@pytest.mark.parametrize("field", ["value", "label"])
@pytest.mark.parametrize("blank_value", ["", "   "])
def test_parse_deep_read_result_rejects_blank_important_figure_field(field, blank_value):
    figure = {"value": "10%", "label": "growth"}
    figure[field] = blank_value
    result = dict(_READY_RESULT, important_figures=[figure])
    with pytest.raises(DeepReadCacheError):
        parse_deep_read_result(json.dumps(result), 1)


def test_brief_page_renders_unavailable_failure_for_cross_item_cached_result(conn, client):
    """A stored result_json whose own item_id belongs to a DIFFERENT item (e.g. corrupted /
    copy-pasted cache row) must never be rendered as though it were this item's brief -- it is
    treated exactly like any other malformed cache: localized unavailable copy, no leak."""
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    cross_item_result = dict(_READY_RESULT, item_id=str(item_id + 999))
    _complete_ready(c, item_id, result=cross_item_result)

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert _READY_RESULT["bottom_line"] not in resp.text
    assert "isn&#39;t available for this item" in resp.text


def test_status_route_ready_with_cross_item_cache_still_terminates_polling(conn, client):
    """The raw DB status is still 'ready' even when the cached payload fails the item_id
    check, so the status route must still stop polling (HX-Redirect) -- the brief page itself
    is responsible for downgrading the *display* to the failure copy."""
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    cross_item_result = dict(_READY_RESULT, item_id=str(item_id + 999))
    _complete_ready(c, item_id, result=cross_item_result)

    resp = client.get(f"/items/{item_id}/brief/status")
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == f"/items/{item_id}/brief"
    assert "hx-get=" not in resp.text


def test_brief_page_404s_for_missing_item(conn, client):
    resp = client.get("/items/999/brief")
    assert resp.status_code == 404


def test_brief_page_shows_pending_state_and_polling_markup(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert f'hx-get="/items/{item_id}/brief/status' in resp.text


def test_brief_page_shows_incomplete_warning(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id, warning_code="content_incomplete")

    resp = client.get(f"/items/{item_id}/brief")
    assert resp.status_code == 200
    assert "incomplete source content" in resp.text


# ============================================================================
# GET /items/{item_id}/brief/status (HTMX polling)
# ============================================================================

def test_status_route_pending_keeps_polling(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    resp = client.get(f"/items/{item_id}/brief/status")
    assert resp.status_code == 200
    assert "hx-get=" in resp.text
    assert "HX-Redirect" not in resp.headers


def test_status_route_ready_sets_hx_redirect_header(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief/status?origin=dashboard")
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == f"/items/{item_id}/brief?origin=dashboard"
    assert "hx-get=" not in resp.text  # terminal: no further polling attribute


def test_status_route_failed_shows_safe_copy_and_stops_polling(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id, error_detail="raw internal trace")

    resp = client.get(f"/items/{item_id}/brief/status")
    assert resp.status_code == 200
    assert "raw internal trace" not in resp.text
    assert "hx-get=" not in resp.text
    assert "HX-Redirect" not in resp.headers


def test_status_route_404s_for_missing_item(conn, client):
    resp = client.get("/items/999/brief/status")
    assert resp.status_code == 404


# ============================================================================
# Channel-origin ownership validation (GET brief / status): a channel_id that names a real
# channel which does NOT own the item must be dropped back to "no back-nav context", exactly
# like an unrecognized origin -- never trusted just because the channel exists.
# ============================================================================

def test_brief_page_falls_back_to_default_back_link_for_unrelated_channel_id(conn, client):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c, channel_name="Tech")
    other_channel_id = create_channel(c, "Finance", "economic news")
    assert other_channel_id != channel_id

    resp = client.get(f"/items/{item_id}/brief?origin=channel&channel_id={other_channel_id}")
    assert resp.status_code == 200
    assert "Finance" not in resp.text
    assert '<a class="deep-read-back" href="/">← Back</a>' in resp.text


def test_brief_page_shows_correct_back_link_for_the_item_actual_channel(conn, client):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c, channel_name="Tech")

    resp = client.get(f"/items/{item_id}/brief?origin=channel&channel_id={channel_id}")
    assert resp.status_code == 200
    assert f'<a class="deep-read-back" href="/channels/{channel_id}">← Back to Tech</a>' in resp.text


def test_status_route_falls_back_to_default_origin_for_unrelated_channel_id(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c, channel_name="Tech")
    other_channel_id = create_channel(c, "Finance", "economic news")
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief/status?origin=channel&channel_id={other_channel_id}")
    assert resp.status_code == 200
    # no leaked origin/channel_id in the redirect target
    assert resp.headers["HX-Redirect"] == f"/items/{item_id}/brief"


def test_status_route_keeps_channel_origin_for_the_item_actual_channel(conn, client):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c, channel_name="Tech")
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/items/{item_id}/brief/status?origin=channel&channel_id={channel_id}")
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == (
        f"/items/{item_id}/brief?origin=channel&channel_id={channel_id}")


# ============================================================================
# Dashboard / Channel / Archive batch deep-read decoration
# ============================================================================

def test_dashboard_decorates_ranked_items_with_deep_read_state(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)

    resp = client.get("/")
    assert resp.status_code == 200
    highlights = resp.context["highlights"]
    assert highlights
    assert highlights[0]["deep_read"]["status"] == "pending"
    assert highlights[0]["deep_read"]["can_start"] is False  # anonymous, never an owner action


def test_dashboard_decorates_never_requested_item_for_owner(conn, authed_client):
    _, c = conn
    _create_ranked_item(c)

    resp = authed_client.get("/")
    assert resp.status_code == 200
    highlights = resp.context["highlights"]
    assert highlights[0]["deep_read"]["status"] == "not_requested"
    assert highlights[0]["deep_read"]["can_start"] is True
    assert highlights[0]["deep_read"]["csrf_token"] == "csrf1"


def test_channel_decorates_ranked_items_with_deep_read_state(conn, client):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _complete_ready(c, item_id)

    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    highlighted = resp.context["highlighted"]
    assert highlighted[0]["deep_read"]["status"] == "ready"
    assert highlighted[0]["deep_read"]["is_ready"] is True
    assert f"/items/{item_id}/brief?origin=channel&channel_id={channel_id}" == (
        highlighted[0]["deep_read"]["brief_url"])


def test_archive_decorates_ranked_items_with_deep_read_state(conn, client):
    _, c = conn
    _, item_id = _create_ranked_item(c)
    request_deep_read(c, item_id, _NOW)
    _fail(c, item_id)

    resp = client.get("/archive")
    assert resp.status_code == 200
    items = [it for _day, day_items in resp.context["groups"] for it in day_items]
    assert items
    assert items[0]["deep_read"]["status"] == "failed"
    assert items[0]["deep_read"]["is_failed"] is True
    assert items[0]["deep_read"]["origin"] == "archive"
    assert items[0]["deep_read"]["channel_id"] is None


def test_unranked_items_get_no_deep_read_bundle_in_list_views(conn, client):
    _, c = conn
    _, item_id = _create_unranked_item(c)

    resp = client.get("/archive")
    assert resp.status_code == 200
    items = [it for _day, day_items in resp.context["groups"] for it in day_items]
    assert items
    assert items[0]["deep_read"] is None


# ============================================================================
# POST /items/{item_id}/vote: the outerHTML-swapped _item_card.html fragment must keep the
# item's deep-read action/control across every vote -- regression for vote_on_item previously
# never decorating item["deep_read"] before rendering the fragment, which silently dropped the
# control from the card on every single vote.
# ============================================================================

def test_vote_route_keeps_deep_read_action_across_every_state_with_channel_origin(
    conn, authed_client,
):
    _, c = conn
    channel_id, item_id = _create_ranked_item(c, channel_name="Tech")

    # 1. never requested -> the "start" control.
    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200
    dr = resp.context["item"]["deep_read"]
    assert dr["status"] == "not_requested"
    assert dr["can_start"] is True
    assert dr["origin"] == "channel"
    assert dr["channel_id"] == channel_id
    assert dr["csrf_token"] == "csrf1"
    assert "deep-read-chip-start" in resp.text
    assert 'name="origin" value="channel"' in resp.text
    assert f'name="channel_id" value="{channel_id}"' in resp.text

    # 2. queued -> the "pending" control.
    request_deep_read(c, item_id, _NOW)
    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200
    dr = resp.context["item"]["deep_read"]
    assert dr["status"] == "pending"
    assert dr["is_pending"] is True
    assert dr["origin"] == "channel"
    assert dr["channel_id"] == channel_id
    assert "deep-read-chip-pending" in resp.text
    assert f'href="/items/{item_id}/brief?origin=channel' in resp.text
    assert f'channel_id={channel_id}"' in resp.text

    # 3. ready -> the "open" control.
    _complete_ready(c, item_id)
    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200
    dr = resp.context["item"]["deep_read"]
    assert dr["status"] == "ready"
    assert dr["is_ready"] is True
    assert dr["origin"] == "channel"
    assert dr["channel_id"] == channel_id
    assert "deep-read-chip-open" in resp.text
    assert f'href="/items/{item_id}/brief?origin=channel' in resp.text
    assert f'channel_id={channel_id}"' in resp.text

    # 4. failed (via regenerate) -> the "retry" control.
    request_deep_read(c, item_id, _NOW, regenerate=True)
    _fail(c, item_id)
    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200
    dr = resp.context["item"]["deep_read"]
    assert dr["status"] == "failed"
    assert dr["is_failed"] is True
    assert dr["can_regenerate"] is True
    assert dr["origin"] == "channel"
    assert dr["channel_id"] == channel_id
    assert "deep-read-chip-retry" in resp.text
    assert 'name="regenerate" value="true"' in resp.text
    assert 'name="origin" value="channel"' in resp.text
    assert f'name="channel_id" value="{channel_id}"' in resp.text


def test_vote_route_derives_the_item_actual_channel_not_any_channel(conn, authed_client):
    """Regression guard for the derivation itself: creating an unrelated second channel/source
    beforehand must not confuse which channel_id the vote fragment's deep-read action uses."""
    _, c = conn
    other_channel_id = create_channel(c, "Finance", "economic news")
    channel_id, item_id = _create_ranked_item(c, channel_name="Tech")
    assert other_channel_id != channel_id

    resp = authed_client.post(f"/items/{item_id}/vote",
                               data={"value": "1", "csrf_token": "csrf1"})
    assert resp.status_code == 200
    dr = resp.context["item"]["deep_read"]
    assert dr["channel_id"] == channel_id
    assert dr["channel_id"] != other_channel_id
